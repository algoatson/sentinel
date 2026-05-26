<script lang="ts">
  /**
   * Compact live event tape on Overview. Subscribes to the SSE bus
   * (/api/events) the bot already publishes to whenever a pipeline
   * notably fires — trade open/close, drawdown_trip, breaking-news
   * alert, filings hit, etc. Last N events shown newest-first.
   *
   * Loads a backlog via /api/events/recent on mount so the strip
   * isn't empty for the first 30 seconds after page load.
   *
   * Browser EventSource handles reconnect + the Last-Event-ID header,
   * so a transient disconnect doesn't lose events.
   */
  import { onMount, onDestroy } from 'svelte';
  import { base } from '$app/paths';
  import { Activity, AlertTriangle, ArrowDownRight, ArrowUpRight, Newspaper, FileText, Megaphone } from 'lucide-svelte';
  import { timeAgo } from '$lib/format';

  type EvKind =
    | 'trade'
    | 'drawdown_trip'
    | 'filing'
    | 'news'
    | 'convergence'
    | 'why_moved'
    | string;

  interface BotEvent {
    id: number;
    kind: EvKind;
    payload: Record<string, any>;
    ts: string;
  }

  const MAX = 30;
  let events: BotEvent[] = $state([]);
  let connected = $state(false);
  let es: EventSource | null = null;

  function ingest(ev: BotEvent) {
    // newest first, dedupe by id, keep MAX
    events = [ev, ...events.filter((e) => e.id !== ev.id)].slice(0, MAX);
  }

  async function backfill() {
    try {
      const r = await fetch('/api/events/recent');
      if (!r.ok) return;
      const d = (await r.json()) as { events: BotEvent[] };
      events = (d.events || []).slice(-MAX).reverse();
    } catch (_) {
      /* offline — SSE will catch up */
    }
  }

  function connect() {
    try {
      es?.close();
      es = new EventSource('/api/events');
      es.onopen = () => (connected = true);
      es.onerror = () => (connected = false);
      // The server emits typed events (event: trade, event: filing, …)
      // — use a generic message handler that listens for any event.
      const handler = (e: MessageEvent) => {
        try {
          const ev = JSON.parse(e.data) as BotEvent;
          ingest(ev);
        } catch (_) { /* skip malformed */ }
      };
      ['trade', 'drawdown_trip', 'filing', 'news', 'convergence', 'why_moved'].forEach((k) =>
        es!.addEventListener(k, handler as EventListener)
      );
      // Fallback: some clients deliver as message
      es.addEventListener('message', handler as EventListener);
    } catch (_) {
      /* EventSource unsupported / blocked — degrade silently */
    }
  }

  onMount(async () => {
    await backfill();
    connect();
  });

  onDestroy(() => {
    try { es?.close(); } catch (_) { /* ignore */ }
  });

  // ── render helpers ──
  function iconFor(kind: EvKind) {
    switch (kind) {
      case 'trade':         return ArrowUpRight;
      case 'drawdown_trip': return AlertTriangle;
      case 'filing':        return FileText;
      case 'news':          return Newspaper;
      case 'convergence':
      case 'why_moved':     return Megaphone;
      default:              return Activity;
    }
  }
  function toneFor(ev: BotEvent): 'pos' | 'neg' | 'warn' | 'mute' {
    if (ev.kind === 'drawdown_trip') return 'warn';
    if (ev.kind === 'trade') {
      const pnl = ev.payload?.realized_pnl;
      if (typeof pnl === 'number') return pnl >= 0 ? 'pos' : 'neg';
      return 'mute';
    }
    if (ev.kind === 'filing') {
      const s = ev.payload?.materiality_score ?? 0;
      if (s >= 7) return 'warn';
    }
    return 'mute';
  }
  function summary(ev: BotEvent): string {
    const p = ev.payload || {};
    switch (ev.kind) {
      case 'trade':
        if (p.summary) return p.summary;
        if (p.ticker && typeof p.realized_pnl === 'number') {
          const sign = p.realized_pnl >= 0 ? '+' : '';
          return `closed ${p.side ?? ''} $${p.ticker} · ${sign}${p.realized_pnl.toFixed(2)}`;
        }
        return p.ticker ? `$${p.ticker} trade` : 'trade';
      case 'drawdown_trip':
        return p.summary || `${p.fund ?? 'wallet'} drawdown trip`;
      case 'filing':
        return p.ticker ? `$${p.ticker} ${p.form_type ?? 'filing'}` : 'filing';
      case 'news':
        return p.ticker ? `$${p.ticker} news` : (p.title?.slice(0, 60) ?? 'news');
      case 'convergence':
      case 'why_moved':
        return p.ticker ? `$${p.ticker} ${ev.kind.replace('_', ' ')}` : ev.kind;
      default:
        return p.summary || p.ticker || ev.kind;
    }
  }
  function tickerLink(ev: BotEvent): string | null {
    const t = ev.payload?.ticker;
    return t ? `${base}/symbol/${encodeURIComponent(t)}` : null;
  }
  const TONE: Record<string, string> = {
    pos:  'border-good/40 bg-good-soft text-good',
    neg:  'border-bad/40 bg-bad-soft text-bad',
    warn: 'border-warn/40 bg-warn-soft text-warn',
    mute: 'border-border bg-surface-2 text-muted',
  };
</script>

<div class="flex items-center gap-2 overflow-x-auto rounded border border-border bg-surface-2/40 px-3 py-2">
  <span class="flex flex-none items-center gap-1 text-[10px] uppercase tracking-wider text-faint">
    <Activity class="h-3 w-3" />
    Live
    <span class={['inline-block h-1.5 w-1.5 rounded-full', connected ? 'bg-good' : 'bg-bad'].join(' ')}></span>
  </span>
  {#if events.length === 0}
    <span class="text-[11px] italic text-faint">No events yet — waiting for the next pipeline tick.</span>
  {:else}
    {#each events as ev (ev.id)}
      {@const Icon = iconFor(ev.kind)}
      {@const tone = toneFor(ev)}
      {@const link = tickerLink(ev)}
      <svelte:element
        this={link ? 'a' : 'span'}
        href={link ?? undefined}
        class={[
          'flex flex-none items-center gap-1.5 rounded border px-2 py-1 text-[11px] tabular',
          TONE[tone],
          link ? 'transition-colors hover:brightness-110' : ''
        ].join(' ')}
        title={`${ev.kind} · ${timeAgo(ev.ts)}`}
      >
        <Icon class="h-3 w-3" />
        <span class="font-medium">{summary(ev)}</span>
        <span class="text-[9.5px] opacity-70">{timeAgo(ev.ts)}</span>
      </svelte:element>
    {/each}
  {/if}
</div>
