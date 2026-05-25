<script lang="ts">
  /**
   * /feed — unified live event stream.
   *
   * Hybrid model: hydrate from /api/events/recent on first paint so a
   * fresh tab isn't blank, then merge with the live `liveEvents` rune
   * (which is what the bell uses). Filter chips per kind. Click any
   * row to deep-link.
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { recentEvents } from '$api';
  import { liveEvents } from '$lib/events.svelte';
  import { base } from '$app/paths';
  import Card from '$components/Card.svelte';
  import Pill from '$components/Pill.svelte';
  import TickerLink from '$components/TickerLink.svelte';
  import Spinner from '$components/Spinner.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import {
    Activity as ActivityIcon, Newspaper, FileText, Target as TargetIcon,
    Bell, Sparkles, Trash2
  } from 'lucide-svelte';
  import { timeAgo } from '$lib/format';

  type Kind = 'all' | 'news' | 'call' | 'filing' | 'watch' | 'trade';
  let kind: Kind = $state('all');
  let tickerFilter = $state('');

  // Hydrate with backend history once.
  const historyQ = createQuery({
    queryKey: ['events-recent'],
    queryFn: () => recentEvents()
  });

  // Merge: live items first (newest), then history older than live's
  // oldest, dedup by id.
  const merged = $derived.by(() => {
    const live = liveEvents.items;
    const hist = ($historyQ.data?.events ?? []).slice().reverse();
    const seen = new Set<number>();
    const out: typeof live = [];
    for (const ev of live) {
      if (!seen.has(ev.id)) {
        seen.add(ev.id);
        out.push(ev);
      }
    }
    for (const ev of hist) {
      if (!seen.has(ev.id)) {
        seen.add(ev.id);
        out.push(ev);
      }
    }
    return out;
  });

  const filtered = $derived(
    merged.filter((ev) => {
      if (kind !== 'all' && ev.kind !== kind) return false;
      const t = tickerFilter.trim().toUpperCase().replace(/^\$/, '');
      if (t && (ev.payload?.ticker ?? '') !== t) return false;
      return true;
    })
  );

  function iconFor(k: string) {
    switch (k) {
      case 'news': return Newspaper;
      case 'filing': return FileText;
      case 'call': return TargetIcon;
      case 'watch': return Bell;
      case 'trade': return Sparkles;
      default: return ActivityIcon;
    }
  }
  function colourFor(k: string): string {
    switch (k) {
      case 'news': return 'text-primary';
      case 'filing': return 'text-violet';
      case 'call': return 'text-good';
      case 'watch': return 'text-warn';
      case 'trade': return 'text-good';
      default: return 'text-muted';
    }
  }
  function pillFor(k: string): 'pos' | 'neg' | 'warn' | 'info' | 'violet' | 'neutral' {
    if (k === 'news') return 'info';
    if (k === 'filing') return 'violet';
    if (k === 'call') return 'pos';
    if (k === 'watch') return 'warn';
    if (k === 'trade') return 'pos';
    return 'neutral';
  }
  function labelFor(ev: any): string {
    const p = ev.payload || {};
    if (ev.kind === 'news') return p.title || 'news';
    if (ev.kind === 'filing') {
      return p.summary || `${p.form_type ?? 'FILING'}${p.materiality_score ? ` · mat ${p.materiality_score}/10` : ''}`;
    }
    if (ev.kind === 'call') {
      const d = (p.direction || '').toUpperCase();
      return `${d} call · conv ${p.conviction ?? '?'}/5 · ${p.thesis ?? ''}`;
    }
    if (ev.kind === 'watch') return `Watch #${p.id ?? '?'} — ${p.raw_text ?? ''}`;
    if (ev.kind === 'trade') return `Trade · ${p.summary ?? ''}`;
    return ev.kind;
  }
  function hrefFor(ev: any): string {
    const t = ev.payload?.ticker;
    if (t) return `${base}/symbol/${encodeURIComponent(t)}`;
    if (ev.kind === 'watch') return `${base}/watches`;
    if (ev.kind === 'filing') return `${base}/intel`;
    if (ev.kind === 'news') return `${base}/intel`;
    if (ev.kind === 'call') return `${base}/calls`;
    return `${base}/overview`;
  }

  const KINDS: Array<[Kind, string, any]> = [
    ['all', 'All', ActivityIcon],
    ['news', 'News', Newspaper],
    ['call', 'Calls', TargetIcon],
    ['filing', 'Filings', FileText],
    ['watch', 'Watches', Bell],
    ['trade', 'Trades', Sparkles]
  ];

  function counts(k: Kind): number {
    if (k === 'all') return merged.length;
    return merged.filter((e) => e.kind === k).length;
  }
</script>

<svelte:head><title>Live feed · Sentinel</title></svelte:head>

<div class="mb-4 flex flex-wrap items-end justify-between gap-3 border-b border-border pb-3">
  <div>
    <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
      <ActivityIcon class="h-5 w-5 text-primary" /><span>Live feed</span>
    </h1>
    <div class="mt-0.5 text-[11.5px] text-faint">
      Unified stream of bot events — news, calls, filings, watch trips,
      trades. Hydrates from history then streams over SSE.
    </div>
  </div>

  <div class="flex items-center gap-3 text-[11px]">
    <span class="flex items-center gap-1.5 tabular">
      <span
        class={[
          'inline-block h-1.5 w-1.5 rounded-full',
          liveEvents.connected ? 'animate-pulse bg-good' : 'bg-faint'
        ].join(' ')}
      ></span>
      <span class={liveEvents.connected ? 'text-good' : 'text-faint'}>
        {liveEvents.connected ? 'streaming' : 'reconnecting…'}
      </span>
    </span>
    <span class="tabular text-faint">last id #{liveEvents.lastSeenId || '—'}</span>
  </div>
</div>

<!-- ── filter ribbon ───────────────────────────────────── -->
<Card class="px-4 py-3">
  <div class="flex flex-wrap items-center gap-2">
    {#each KINDS as [k, label, Icon] (k)}
      <button
        type="button"
        onclick={() => (kind = k)}
        class={[
          'flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[11.5px] transition-colors',
          kind === k
            ? 'border-primary/50 bg-primary-soft text-primary'
            : 'border-border bg-surface-2 text-muted hover:text-text'
        ].join(' ')}
      >
        <Icon class="h-3 w-3" />
        {label}
        <span class={[
          'rounded px-1 py-px text-[9px] tabular',
          kind === k ? 'bg-primary/20 text-primary' : 'bg-bg text-faint'
        ].join(' ')}>
          {counts(k)}
        </span>
      </button>
    {/each}

    <input
      type="text"
      bind:value={tickerFilter}
      placeholder="$ticker"
      class="ml-2 w-24 rounded-md border border-border bg-surface-2 px-2 py-1 font-mono text-[12px] text-text placeholder:text-faint/50 focus:border-primary/60 focus:outline-none"
    />

    <span class="ml-auto text-[11px] tabular text-faint">
      {filtered.length} of {merged.length}
    </span>
  </div>
</Card>

<!-- ── feed ──────────────────────────────────────── -->
<div class="mt-3">
  {#if $historyQ.isLoading && !filtered.length}
    <div class="flex justify-center py-12"><Spinner /></div>
  {:else if !filtered.length}
    <EmptyState
      title="No events yet"
      description="Events stream in as the bot ingests news / calls / filings / watch trips. The bell counter also tracks unread."
    />
  {:else}
    <div class="space-y-1">
      {#each filtered as ev (ev.id)}
        {@const Icon = iconFor(ev.kind)}
        <a
          href={hrefFor(ev)}
          class="group flex items-start gap-2.5 rounded-md border border-border-soft bg-surface px-3 py-2 transition-colors hover:border-border-strong hover:bg-white/[0.02]"
        >
          <Icon class={['mt-0.5 h-3.5 w-3.5 shrink-0', colourFor(ev.kind)].join(' ')} />
          <div class="min-w-0 flex-1">
            <div class="flex items-baseline gap-1.5 text-[11px]">
              <Pill variant={pillFor(ev.kind)}>{ev.kind.toUpperCase()}</Pill>
              {#if ev.payload?.ticker}
                <TickerLink ticker={ev.payload.ticker} class="text-[11.5px]" />
              {/if}
              <span class="tabular text-[10px] text-faint">#{ev.id}</span>
              <span class="ml-auto tabular text-[10px] text-faint">{timeAgo(ev.ts)}</span>
            </div>
            <div class="mt-0.5 line-clamp-2 text-[12.5px] leading-snug text-muted">
              {labelFor(ev)}
            </div>
          </div>
        </a>
      {/each}
    </div>
  {/if}
</div>
