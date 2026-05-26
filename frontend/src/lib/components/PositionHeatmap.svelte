<script lang="ts">
  /**
   * Finviz-style treemap of open positions.
   *
   * One tile per open trade. Area ∝ notional (position size), colour
   * ∝ unrealised PnL %. Pure CSS/TS — no d3 dependency.
   *
   * We implement the standard "squarified treemap" algorithm
   * (Bruls/Huijsen/van Wijk 2000): walk rows of tiles, greedily
   * extending the current row while the worst aspect ratio of any
   * tile in it improves, then commit the row when adding the next
   * tile would make things worse. Tiles in a row are then sized
   * along their long axis proportional to value.
   *
   * The container is given an aspect ratio at the call-site (so the
   * grid is stable across re-renders) and the algorithm runs once
   * per render to compute pixel rectangles for each position.
   */
  import { base } from '$app/paths';
  import type { OpenPositionRow } from '$api';
  import { pct, price } from '$lib/format';

  interface Props {
    positions: OpenPositionRow[];
    /** Container height in px. Width is responsive. */
    height?: number;
    /** Aspect ratio used for layout math. Container scales to fit. */
    width?: number;
  }
  let {
    positions,
    height = 360,
    width = 720
  }: Props = $props();

  type Tile = {
    p: OpenPositionRow;
    x: number;
    y: number;
    w: number;
    h: number;
  };

  function colour(upnlPct: number | null): string {
    if (upnlPct === null || upnlPct === undefined) return 'rgba(140,140,150,0.18)';
    // Cap at ±10% for colour intensity scaling.
    const cap = 10;
    const v = Math.max(-cap, Math.min(cap, upnlPct));
    const a = 0.18 + 0.55 * (Math.abs(v) / cap); // 0.18 … 0.73
    if (v >= 0) return `rgba(61, 220, 151, ${a})`;   // good
    return `rgba(255, 107, 107, ${a})`;              // bad
  }

  function textColour(upnlPct: number | null): string {
    if (upnlPct === null || upnlPct === undefined) return 'rgba(255,255,255,0.62)';
    if (Math.abs(upnlPct) >= 4) return 'rgba(255,255,255,0.95)';
    return 'rgba(255,255,255,0.82)';
  }

  // ── Squarified treemap layout ──────────────────────────────────
  function layout(rows: OpenPositionRow[], W: number, H: number): Tile[] {
    if (!rows.length) return [];
    const items = rows
      .map((p) => ({ p, v: Math.max(p.notional ?? 0, 1) }))
      .sort((a, b) => b.v - a.v);
    const total = items.reduce((s, it) => s + it.v, 0);
    // Normalise values to fill W×H exactly.
    const scale = (W * H) / total;
    const normed = items.map((it) => ({ p: it.p, v: it.v * scale }));

    const tiles: Tile[] = [];
    let x = 0, y = 0, rw = W, rh = H;
    let row: { p: OpenPositionRow; v: number }[] = [];

    function worstRatio(rowItems: typeof row, side: number): number {
      const s = rowItems.reduce((s, it) => s + it.v, 0);
      if (!s || !side) return Infinity;
      let worst = 0;
      for (const it of rowItems) {
        const wide = (side * side * it.v) / (s * s);
        const tall = (s * s) / (side * side * it.v);
        worst = Math.max(worst, wide, tall);
      }
      return worst;
    }

    function commit(rowItems: typeof row, side: number, horizontal: boolean) {
      const s = rowItems.reduce((sum, it) => sum + it.v, 0);
      if (!s) return;
      const thick = s / side;
      let cursor = horizontal ? x : y;
      for (const it of rowItems) {
        const len = it.v / thick;
        if (horizontal) {
          tiles.push({ p: it.p, x: cursor, y, w: len, h: thick });
          cursor += len;
        } else {
          tiles.push({ p: it.p, x, y: cursor, w: thick, h: len });
          cursor += len;
        }
      }
      // Shrink the remaining rectangle.
      if (horizontal) {
        y += thick;
        rh -= thick;
      } else {
        x += thick;
        rw -= thick;
      }
    }

    for (const it of normed) {
      const horizontal = rw >= rh;          // place tiles along the long edge
      const side = horizontal ? rw : rh;
      const tentative = [...row, it];
      if (
        row.length === 0 ||
        worstRatio(tentative, side) <= worstRatio(row, side)
      ) {
        row = tentative;
      } else {
        commit(row, side, horizontal);
        // Recompute orientation for the new remaining rect.
        row = [it];
      }
    }
    if (row.length) {
      const horizontal = rw >= rh;
      const side = horizontal ? rw : rh;
      commit(row, side, horizontal);
    }
    return tiles;
  }

  const tiles = $derived(layout(positions, width, height));
</script>

{#if positions.length === 0}
  <div
    class="flex items-center justify-center rounded border border-dashed border-border-soft text-[12px] text-faint"
    style:height="{height}px"
  >
    No open positions.
  </div>
{:else}
  <div
    class="relative overflow-hidden rounded border border-border bg-surface-2"
    style:height="{height}px"
    style:aspect-ratio="{width} / {height}"
  >
    {#each tiles as t (t.p.id)}
      {@const tinyW = t.w < 60}
      {@const tinyH = t.h < 36}
      <a
        href={`${base}/symbol/${encodeURIComponent(t.p.ticker)}`}
        class="absolute flex flex-col justify-between overflow-hidden border border-black/40 px-1.5 py-1 text-[10px] leading-tight transition-[filter] hover:brightness-110"
        style:left="{(t.x / width) * 100}%"
        style:top="{(t.y / height) * 100}%"
        style:width="{(t.w / width) * 100}%"
        style:height="{(t.h / height) * 100}%"
        style:background-color={colour(t.p.upnl_pct)}
        style:color={textColour(t.p.upnl_pct)}
        title={`${t.p.fund} · ${t.p.ticker} · ${t.p.side.toUpperCase()} · qty ${t.p.qty} @ ${price(t.p.entry)} · mark ${price(t.p.mark)} · ${pct(t.p.upnl_pct ?? 0, 2)}`}
      >
        <div class="flex items-baseline justify-between gap-1 tabular">
          <span class="truncate font-mono font-semibold">
            {tinyW ? t.p.ticker.slice(0, 4) : t.p.ticker}
          </span>
          {#if !tinyW}
            <span class="text-[8.5px] uppercase opacity-70">{t.p.side[0]}</span>
          {/if}
        </div>
        {#if !tinyH}
          <div class="flex items-baseline justify-between gap-1 tabular">
            <span class="font-semibold">
              {pct(t.p.upnl_pct ?? 0, 2)}
            </span>
            {#if t.w >= 90}
              <span class="text-[9px] opacity-75">{price(t.p.notional)}</span>
            {/if}
          </div>
        {/if}
      </a>
    {/each}
  </div>
{/if}
