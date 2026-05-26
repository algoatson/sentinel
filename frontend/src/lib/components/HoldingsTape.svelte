<script lang="ts">
  /**
   * Compact one-line tape strip of every open position: ticker · mark
   * · % since entry. Clicking a chip jumps to the symbol page; an
   * inline "+" appends the ticker to /compare.
   *
   * Architectural note: reuses the same `positions-open` query the
   * /book page polls (TanStack Query dedupes the cache key) so this
   * costs nothing extra on the wire. Pulled into Overview just under
   * the TodayPulse strip — it's the "what am I in right now" answer
   * a trader otherwise has to click into /book to see.
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { openPositions } from '$api';
  import { base } from '$app/paths';
  import { price } from '$lib/format';
  import { Briefcase, Plus } from 'lucide-svelte';

  const q = createQuery({
    queryKey: ['positions-open'],
    queryFn: openPositions,
    refetchInterval: 30_000
  });

  const rows = $derived($q.data ?? []);
  // Sort: biggest absolute uPnL first, so the most-noteworthy
  // positions are visible without scrolling.
  const sorted = $derived(
    [...rows].sort((a, b) => Math.abs(b.upnl ?? 0) - Math.abs(a.upnl ?? 0))
  );
</script>

{#if rows.length}
  <div class="flex items-center gap-2 overflow-x-auto rounded border border-border bg-surface-2/40 px-3 py-2">
    <span class="flex flex-none items-center gap-1 text-[10px] uppercase tracking-wider text-faint">
      <Briefcase class="h-3 w-3" /> Book
    </span>
    {#each sorted as p (p.id)}
      <a
        href={`${base}/symbol/${encodeURIComponent(p.ticker)}`}
        class={[
          'flex flex-none items-baseline gap-1.5 rounded border px-2 py-1 text-[11px] tabular transition-colors',
          (p.upnl ?? 0) > 0
            ? 'border-good/40 bg-good-soft hover:border-good'
            : (p.upnl ?? 0) < 0
              ? 'border-bad/40 bg-bad-soft hover:border-bad'
              : 'border-border bg-surface-2 hover:border-primary/40'
        ].join(' ')}
        title={`${p.fund} · ${p.side.toUpperCase()} · qty ${p.qty} @ ${price(p.entry)}`}
      >
        <span class="font-mono font-semibold text-text">${p.ticker}</span>
        <span class={[
          'text-[10px] uppercase',
          p.side === 'long' ? 'text-good' : 'text-bad'
        ].join(' ')}>{p.side[0]}</span>
        <span class="text-faint">{price(p.mark)}</span>
        <span class={(p.upnl ?? 0) >= 0 ? 'text-good' : 'text-bad'}>
          {(p.upnl_pct ?? 0) >= 0 ? '+' : ''}{(p.upnl_pct ?? 0).toFixed(2)}%
        </span>
      </a>
    {/each}
    {#if sorted.length}
      <a
        href={`${base}/compare?tickers=${encodeURIComponent(sorted.slice(0, 5).map((p) => p.ticker).join(','))}`}
        class="flex flex-none items-center gap-1 rounded border border-border bg-surface-2 px-2 py-1 text-[10.5px] text-muted transition-colors hover:border-primary/40 hover:text-text"
        title="Open /compare with the top positions preloaded"
      ><Plus class="h-3 w-3" /> Compare</a>
    {/if}
  </div>
{/if}
