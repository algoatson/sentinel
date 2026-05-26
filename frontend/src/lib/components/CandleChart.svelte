<script lang="ts">
  /**
   * TradingView-style candle chart for the Symbol page.
   *
   * Rendering layers (back to front):
   *   1. Candles + volume histogram (always).
   *   2. Closed-trade entry/exit dot markers, coloured by PnL.
   *   3. For each currently-open position (one per wallet holding the
   *      ticker):
   *        - Entry price line (solid, per-fund colour, axis label).
   *        - Stop price line (red, dashed) when stop_price set.
   *        - Target price line (green, dashed) when target_price set.
   *        - Trailing-stop watermark line (dotted) when armed.
   *        - Entry-time arrow marker labeled with the fund name.
   *   4. Overlay legend showing every open position with side, qty,
   *      and live PnL. Click a row to scroll the time-scale to its
   *      entry.
   *
   * Multiple positions on the same ticker (e.g. degen + catalyst both
   * long $NVDA) render as separate lines + markers so the trader can
   * see who's holding what and where each was entered.
   *
   * lightweight-charts gotchas baked in:
   *   - Times are Unix seconds (not "YYYY-MM-DD") so intraday bars
   *     don't collapse onto one bucket.
   *   - Colours use the comma-form `rgba(R, G, B, A)` — the lib's
   *     parser doesn't speak the modern `rgb(R G B / A)` syntax.
   *   - Series + price-lines are recreated, not mutated, on data
   *     change to dodge the "ranges out of order" edge case.
   */
  import { onMount, onDestroy } from 'svelte';
  import {
    createChart,
    type IChartApi,
    type IPriceLine,
    type ISeriesApi,
    type Time
  } from 'lightweight-charts';
  import type { OHLC, OpenPosition, ClosedTrade } from '../types';

  interface Props {
    bars: OHLC[];
    openPositions?: OpenPosition[];
    /** Legacy single-position prop — folded into openPositions when present. */
    openPosition?: OpenPosition | null;
    closedTrades?: ClosedTrade[];
    height?: number;
  }

  let {
    bars,
    openPositions = [],
    openPosition = null,
    closedTrades = [],
    height = 480
  }: Props = $props();

  // Resolve the effective list once: prefer the array prop, fall back to the
  // singular legacy prop for older callers.
  const positions = $derived(
    openPositions.length > 0
      ? openPositions
      : openPosition
        ? [openPosition]
        : []
  );

  let container: HTMLDivElement;
  let chart: IChartApi | undefined;
  let candleSeries: ISeriesApi<'Candlestick'> | undefined;
  let volumeSeries: ISeriesApi<'Histogram'> | undefined;
  /** PriceLines created from openPositions, tracked so we can clean
   *  them up on re-render — lightweight-charts has `removePriceLine`
   *  but not "remove all", so we keep the handles ourselves. */
  let priceLines: IPriceLine[] = [];

  const PALETTE = {
    bg: 'transparent',
    text: 'rgba(255, 255, 255, 0.62)',
    grid: 'rgba(255, 255, 255, 0.05)',
    border: 'rgba(255, 255, 255, 0.085)',
    up: '#3ddc97',
    down: '#ff6b6b',
    upVol: 'rgba(61, 220, 151, 0.45)',
    downVol: 'rgba(255, 107, 107, 0.45)',
    stop: 'rgba(255, 107, 107, 0.95)',
    target: 'rgba(61, 220, 151, 0.95)',
    watermark: 'rgba(255, 255, 255, 0.55)',
  };

  // Fund → colour. Same palette as PortfolioCard and WalletAllocation
  // so the user reads "degen blue" the same everywhere.
  const FUND_COLOURS: Record<string, string> = {
    catalyst:   '#6699ff',
    contrarian: '#a78bfa',
    crypto:     '#fbbf24',
    degen:      '#ff6b6b',
    hype:       '#f2c0ff',
    macro:      '#3ddc97',
    research:   '#8ed1fc',
    sniper:     '#a3e635',
  };
  function fundColour(name: string | null | undefined): string {
    if (!name) return '#6699ff';
    return FUND_COLOURS[name] ?? '#6699ff';
  }

  /** Unix-seconds Time — intraday bars otherwise collapse onto a date. */
  const toUnix = (iso: string): Time =>
    Math.floor(new Date(iso).getTime() / 1000) as unknown as Time;

  /** Drop strict-monotonicity violations (lightweight-charts refuses
   *  unsorted feeds). Sort, then collapse equal timestamps to the
   *  latest bar at that time. */
  function normalize<T extends { time: Time }>(rows: T[]): T[] {
    const sorted = [...rows].sort(
      (a, b) => (a.time as number) - (b.time as number)
    );
    const out: T[] = [];
    for (const r of sorted) {
      const prev = out[out.length - 1];
      if (prev && (prev.time as number) === (r.time as number)) {
        out[out.length - 1] = r;
      } else {
        out.push(r);
      }
    }
    return out;
  }

  function mount() {
    if (!container) return;
    chart = createChart(container, {
      width: container.clientWidth,
      height,
      layout: {
        background: { color: 'transparent' },
        textColor: PALETTE.text,
        fontFamily: 'Inter, system-ui, sans-serif'
      },
      grid: {
        vertLines: { color: PALETTE.grid },
        horzLines: { color: PALETTE.grid }
      },
      rightPriceScale: {
        borderColor: PALETTE.border,
        scaleMargins: { top: 0.08, bottom: 0.28 }
      },
      timeScale: {
        borderColor: PALETTE.border,
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 6,
      },
      crosshair: {
        vertLine: { color: PALETTE.border, width: 1 },
        horzLine: { color: PALETTE.border, width: 1 }
      }
    });

    candleSeries = chart.addCandlestickSeries({
      upColor: PALETTE.up,
      downColor: PALETTE.down,
      borderUpColor: PALETTE.up,
      borderDownColor: PALETTE.down,
      wickUpColor: PALETTE.up,
      wickDownColor: PALETTE.down
    });

    volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: '',
      color: PALETTE.upVol
    });
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.78, bottom: 0 }
    });

    syncData();
    chart.timeScale().fitContent();
  }

  function clearPriceLines() {
    if (!candleSeries) return;
    for (const pl of priceLines) {
      try {
        candleSeries.removePriceLine(pl);
      } catch (_) {
        /* the series may have been recreated — ignore */
      }
    }
    priceLines = [];
  }

  function addPositionLines() {
    if (!candleSeries) return;
    for (const p of positions) {
      const colour = fundColour(p.fund);
      const label = p.fund ? p.fund : p.side;
      // Entry — solid, axis-labelled with the fund.
      priceLines.push(
        candleSeries.createPriceLine({
          price: p.entry,
          color: colour,
          lineWidth: 1,
          lineStyle: 0, // solid
          axisLabelVisible: true,
          title: `${label} · entry`
        })
      );
      if (p.stop_price && p.stop_price > 0) {
        priceLines.push(
          candleSeries.createPriceLine({
            price: p.stop_price,
            color: PALETTE.stop,
            lineWidth: 1,
            lineStyle: 2, // dashed
            axisLabelVisible: true,
            title: `${label} · stop`
          })
        );
      }
      if (p.target_price && p.target_price > 0) {
        priceLines.push(
          candleSeries.createPriceLine({
            price: p.target_price,
            color: PALETTE.target,
            lineWidth: 1,
            lineStyle: 2, // dashed
            axisLabelVisible: true,
            title: `${label} · target`
          })
        );
      }
      if (p.trailing_stop_pct && p.watermark_price && p.watermark_price > 0) {
        const trail = p.side === 'long'
          ? p.watermark_price * (1 - p.trailing_stop_pct)
          : p.watermark_price * (1 + p.trailing_stop_pct);
        priceLines.push(
          candleSeries.createPriceLine({
            price: trail,
            color: PALETTE.watermark,
            lineWidth: 1,
            lineStyle: 3, // dotted
            axisLabelVisible: true,
            title: `${label} · trail`
          })
        );
      }
    }
  }

  function syncData() {
    if (!candleSeries || !volumeSeries) return;
    const candles = normalize(
      bars.map((b) => ({
        time: toUnix(b.ts),
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close
      }))
    );
    candleSeries.setData(candles);

    const vols = normalize(
      bars.map((b) => ({
        time: toUnix(b.ts),
        value: b.volume,
        color: b.close >= b.open ? PALETTE.upVol : PALETTE.downVol
      }))
    );
    volumeSeries.setData(vols);

    // Price lines — entry/stop/target/trail per open position.
    clearPriceLines();
    addPositionLines();

    // Time-axis markers — one per closed trade (entry + exit) and
    // one arrow per open position labeled with the fund.
    const markers = normalize([
      ...(closedTrades || []).flatMap((t) => {
        const out: any[] = [
          {
            time: toUnix(t.entry_at),
            position: 'belowBar' as const,
            color: 'rgba(255,255,255,0.55)',
            shape: 'circle' as const,
            text: t.fund ? `${t.fund}·in` : 'in'
          }
        ];
        if (t.exit_at) {
          out.push({
            time: toUnix(t.exit_at),
            position: 'aboveBar' as const,
            color: (t.pnl ?? 0) >= 0 ? PALETTE.up : PALETTE.down,
            shape: 'circle' as const,
            text: t.fund ? `${t.fund}·out` : 'out'
          });
        }
        return out;
      }),
      ...positions.map((p) => ({
        time: toUnix(p.entry_at),
        position:
          p.side === 'long'
            ? ('belowBar' as const)
            : ('aboveBar' as const),
        color: fundColour(p.fund),
        shape:
          p.side === 'long'
            ? ('arrowUp' as const)
            : ('arrowDown' as const),
        text: p.fund ? `${p.fund} ${p.side}` : p.side.toUpperCase()
      }))
    ]);
    candleSeries.setMarkers(markers);
  }

  function jumpToEntry(p: OpenPosition) {
    if (!chart) return;
    const t = toUnix(p.entry_at) as number;
    // Show ~10% of the visible range to each side of the entry.
    chart.timeScale().setVisibleRange({
      from: (t - 7 * 24 * 3600) as Time,
      to: (t + 7 * 24 * 3600) as Time,
    });
  }

  function resize() {
    if (chart && container) {
      chart.applyOptions({ width: container.clientWidth });
    }
  }

  onMount(() => {
    mount();
    window.addEventListener('resize', resize);
  });

  onDestroy(() => {
    window.removeEventListener('resize', resize);
    try {
      chart?.remove();
    } catch (_) {
      /* ignore */
    }
  });

  // Re-sync when any data prop changes.
  $effect(() => {
    bars;
    positions;
    closedTrades;
    if (candleSeries) syncData();
  });
</script>

<div class="relative">
  <div bind:this={container} style="height: {height}px; width: 100%"></div>

  {#if positions.length > 0}
    <div class="pointer-events-none absolute left-2 top-2 max-w-[18rem] rounded-md border border-border bg-surface/95 px-2 py-1.5 text-[10.5px] tabular shadow-lg backdrop-blur">
      <div class="mb-0.5 text-[9.5px] uppercase tracking-wider text-faint">
        Open positions · {positions.length}
      </div>
      <ul class="space-y-0.5">
        {#each positions as p (p.id ?? p.entry_at)}
          <li class="pointer-events-auto">
            <button
              type="button"
              onclick={() => jumpToEntry(p)}
              class="flex w-full items-center gap-1.5 rounded px-1 py-0.5 text-left hover:bg-surface-2/60"
              title="Jump chart to entry"
            >
              <span
                class="inline-block h-1.5 w-1.5 rounded-full"
                style:background-color={fundColour(p.fund)}
              ></span>
              <span class="capitalize text-muted">{p.fund ?? '—'}</span>
              <span class={[
                'rounded px-1 text-[9px] uppercase',
                p.side === 'long' ? 'bg-good-soft text-good' : 'bg-bad-soft text-bad'
              ].join(' ')}>{p.side[0]}</span>
              <span class="text-faint">{p.qty}</span>
              <span class="text-faint">@</span>
              <span class="text-text">{p.entry.toFixed(2)}</span>
              {#if p.pnl_pct !== null && p.pnl_pct !== undefined}
                <span class={[
                  'ml-auto',
                  (p.pnl_pct ?? 0) >= 0 ? 'text-good' : 'text-bad'
                ].join(' ')}>
                  {(p.pnl_pct ?? 0) >= 0 ? '+' : ''}{(p.pnl_pct ?? 0).toFixed(2)}%
                </span>
              {/if}
            </button>
          </li>
        {/each}
      </ul>
    </div>
  {/if}
</div>
