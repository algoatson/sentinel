<script lang="ts">
  /**
   * Watchlist heatmap — cells colored by 1d% change, intensity by
   * magnitude. Hover for full row data; click navigates to the
   * symbol detail page (via the parent's onPick).
   *
   * Layout is auto-fit on min cell width; works from phone (3
   * cols) to wide desktop (10+ cols). Same data the watchlist
   * table consumes, no extra fetch.
   */
  import { base } from '$app/paths';
  import { price } from '$lib/format';
  import type { WatchlistRow } from '$lib/types';

  interface Props {
    rows: WatchlistRow[];
    /** Cap to avoid 200-cell walls; sorted by abs(1d%) first. */
    limit?: number;
  }

  let { rows, limit = 80 }: Props = $props();

  const visible = $derived(
    [...rows]
      .filter((r) => r.change_1d_pct !== null)
      .sort(
        (a, b) =>
          Math.abs(b.change_1d_pct ?? 0) - Math.abs(a.change_1d_pct ?? 0)
      )
      .slice(0, limit)
  );

  /** Pick a colour for a % move. Uses HSL stops, capped at ±5%. */
  function bg(pct: number): string {
    const clamped = Math.max(-5, Math.min(5, pct));
    const intensity = Math.abs(clamped) / 5; // 0..1
    // Mint for up, red for down. Mix with surface so neutral isn't black.
    if (clamped > 0) {
      const alpha = 0.15 + intensity * 0.55;
      return `rgba(61, 220, 151, ${alpha.toFixed(2)})`;
    } else if (clamped < 0) {
      const alpha = 0.15 + intensity * 0.55;
      return `rgba(255, 107, 107, ${alpha.toFixed(2)})`;
    }
    return 'rgba(255, 255, 255, 0.04)';
  }
  function txt(pct: number): string {
    // Bright text on dark cells, dim on neutral.
    return Math.abs(pct) > 0.2 ? '#ffffff' : 'var(--color-muted)';
  }
</script>

{#if !visible.length}
  <div class="py-12 text-center text-[12px] text-faint">
    No price data — wait for the next bar poll.
  </div>
{:else}
  <div class="grid grid-cols-[repeat(auto-fit,minmax(8.5rem,1fr))] gap-1.5">
    {#each visible as r (r.ticker)}
      <a
        href={`${base}/symbol/${encodeURIComponent(r.ticker)}`}
        class="flex aspect-[5/3] flex-col justify-between rounded-md border border-border-soft p-2 transition-all hover:border-border-strong hover:-translate-y-px"
        style:background-color={bg(r.change_1d_pct ?? 0)}
        style:color={txt(r.change_1d_pct ?? 0)}
        title={`${r.ticker} · ${(r.change_1d_pct ?? 0).toFixed(2)}% 1d · ${price(r.last_price)}`}
      >
        <div class="flex items-baseline justify-between">
          <span class="font-mono text-[13px] font-bold leading-none">{r.ticker}</span>
          <span class="text-[9.5px] uppercase tracking-wider opacity-60">
            {r.asset_class}
          </span>
        </div>
        <div class="flex items-baseline justify-between">
          <span class="text-[10px] tabular opacity-80">{price(r.last_price)}</span>
          <span class="tabular text-[13.5px] font-semibold leading-none">
            {(r.change_1d_pct ?? 0) >= 0 ? '+' : ''}{(r.change_1d_pct ?? 0).toFixed(2)}%
          </span>
        </div>
      </a>
    {/each}
  </div>
  <div class="mt-2 flex items-center justify-end gap-3 text-[10px] tabular text-faint">
    <span>−5%</span>
    <span class="inline-block h-2 w-32 rounded" style="background: linear-gradient(to right, rgba(255,107,107,0.7), rgba(255,255,255,0.05), rgba(61,220,151,0.7));"></span>
    <span>+5%</span>
    <span class="ml-2">cell color = 1d % change · top {visible.length} by magnitude</span>
  </div>
{/if}
