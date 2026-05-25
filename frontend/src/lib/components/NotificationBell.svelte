<script lang="ts">
  import { liveEvents } from '$lib/events.svelte';
  import { Bell, Newspaper, FileText, Target, Sparkles as WatchIcon, ArrowRight } from 'lucide-svelte';
  import { timeAgo } from '$lib/format';
  import TickerLink from './TickerLink.svelte';
  import { base } from '$app/paths';

  let open = $state(false);

  function iconFor(kind: string) {
    switch (kind) {
      case 'news': return Newspaper;
      case 'filing': return FileText;
      case 'call': return Target;
      case 'watch': return WatchIcon;
      default: return Bell;
    }
  }

  function colourFor(kind: string): string {
    switch (kind) {
      case 'news': return 'text-primary';
      case 'filing': return 'text-violet';
      case 'call': return 'text-good';
      case 'watch': return 'text-warn';
      default: return 'text-muted';
    }
  }

  function labelFor(ev: any): string {
    const p = ev.payload || {};
    if (ev.kind === 'news') return p.title || 'news';
    if (ev.kind === 'filing') return p.summary || `${p.form_type} filing`;
    if (ev.kind === 'call') return p.thesis || `${p.direction} call`;
    if (ev.kind === 'watch') return `Watch tripped: ${p.raw_text}`;
    return ev.kind;
  }

  function tickerOf(ev: any): string | null {
    return ev.payload?.ticker ?? null;
  }
</script>

<div class="relative">
  <button
    type="button"
    aria-label="Notifications"
    onclick={() => {
      open = !open;
      if (open) liveEvents.markRead();
    }}
    class={[
      'relative rounded-md p-1.5 transition-colors',
      open ? 'bg-surface-2 text-text' : 'text-muted hover:bg-surface-2 hover:text-text'
    ].join(' ')}
  >
    <Bell class="h-4 w-4" />
    {#if liveEvents.unread > 0}
      <span class="absolute -right-0.5 -top-0.5 inline-flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-primary px-1 text-[8.5px] font-bold text-bg shadow-[0_0_6px_var(--color-primary)]">
        {liveEvents.unread > 99 ? '99+' : liveEvents.unread}
      </span>
    {/if}
    <span
      class={[
        'absolute -bottom-px -right-px h-1.5 w-1.5 rounded-full',
        liveEvents.connected ? 'bg-good' : 'bg-faint'
      ].join(' ')}
      title={liveEvents.connected ? 'Live stream connected' : 'Reconnecting…'}
    ></span>
  </button>

  {#if open}
    <button
      type="button"
      aria-label="Close"
      class="fixed inset-0 z-40 cursor-default"
      onclick={() => (open = false)}
    ></button>
    <div
      class="absolute right-0 top-full z-50 mt-1 w-[22rem] overflow-hidden rounded-xl border border-border bg-surface shadow-2xl animate-[fadeIn_0.12s_ease-out]"
      role="dialog"
    >
      <div class="flex items-center gap-2 border-b border-border px-3 py-2">
        <Bell class="h-3.5 w-3.5 text-muted" />
        <span class="text-[12px] font-semibold text-text">Live activity</span>
        <span
          class={[
            'inline-flex items-center gap-1 text-[10px]',
            liveEvents.connected ? 'text-good' : 'text-faint'
          ].join(' ')}
        >
          <span
            class={[
              'inline-block h-1.5 w-1.5 rounded-full',
              liveEvents.connected ? 'animate-pulse bg-good' : 'bg-faint'
            ].join(' ')}
          ></span>
          {liveEvents.connected ? 'streaming' : 'reconnecting…'}
        </span>
        <span class="ml-auto text-[10px] tabular text-faint">
          {liveEvents.items.length} event{liveEvents.items.length === 1 ? '' : 's'}
        </span>
      </div>

      {#if !liveEvents.items.length}
        <div class="px-3 py-6 text-center text-[12px] text-faint">
          No live events yet. The bot's ingesters publish here as they fire.
        </div>
      {:else}
        <ul class="max-h-[24rem] divide-y divide-border-soft overflow-y-auto">
          {#each liveEvents.items.slice(0, 40) as ev (ev.id)}
            {@const Icon = iconFor(ev.kind)}
            {@const t = tickerOf(ev)}
            <li class="flex items-start gap-2 px-3 py-2 transition-colors hover:bg-white/[0.025]">
              <Icon class={['mt-0.5 h-3.5 w-3.5 shrink-0', colourFor(ev.kind)].join(' ')} />
              <div class="min-w-0 flex-1">
                <div class="flex items-baseline gap-1.5 text-[11px]">
                  <span class={['font-semibold uppercase tracking-wider', colourFor(ev.kind)].join(' ')}>
                    {ev.kind}
                  </span>
                  {#if t}
                    <TickerLink ticker={t} class="text-[11px]" />
                  {/if}
                  <span class="ml-auto tabular text-[9.5px] text-faint">{timeAgo(ev.ts)}</span>
                </div>
                <div class="mt-0.5 line-clamp-2 text-[11.5px] leading-snug text-muted">
                  {labelFor(ev)}
                </div>
              </div>
            </li>
          {/each}
        </ul>
      {/if}

      <div class="border-t border-border bg-surface-2/40 px-3 py-1.5 text-center text-[10.5px] text-faint">
        Last id #{liveEvents.lastSeenId || '—'} ·
        <a href={`${base}/system`} class="text-primary underline hover:text-primary/80">live log</a>
      </div>
    </div>
  {/if}
</div>

<style>
  @keyframes fadeIn {
    from {
      opacity: 0;
      transform: translateY(-4px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
</style>
