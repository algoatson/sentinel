<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import {
    createChart,
    type IChartApi,
    type ISeriesApi,
    type Time
  } from 'lightweight-charts';
  import type { OHLC, OpenPosition, ClosedTrade } from '../types';

  interface Props {
    bars: OHLC[];
    openPosition?: OpenPosition | null;
    closedTrades?: ClosedTrade[];
    height?: number;
  }

  let { bars, openPosition = null, closedTrades = [], height = 480 }: Props =
    $props();

  let container: HTMLDivElement;
  let chart: IChartApi | undefined;
  let candleSeries: ISeriesApi<'Candlestick'> | undefined;
  let volumeSeries: ISeriesApi<'Histogram'> | undefined;
  let entryLine: ISeriesApi<'Line'> | undefined;

  // lightweight-charts uses an older colour parser that rejects the
  // modern space-separated `rgb(R G B / A)` syntax — must be the
  // classic comma form `rgba(R, G, B, A)`. Crashes with
  // "Cannot parse color: rgb(255 255 255 / 0.62)" otherwise.
  const PALETTE = {
    bg: 'transparent',
    text: 'rgba(255, 255, 255, 0.62)',
    grid: 'rgba(255, 255, 255, 0.05)',
    border: 'rgba(255, 255, 255, 0.085)',
    up: '#3ddc97',
    down: '#ff6b6b',
    upVol: 'rgba(61, 220, 151, 0.45)',
    downVol: 'rgba(255, 107, 107, 0.45)'
  };

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
        secondsVisible: false
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

  /** Unix-seconds Time. The bot writes intraday PriceBars (multiple
   * per day) so the previous date-string key (`YYYY-MM-DD`) collapsed
   * many bars onto the same time and lightweight-charts silently
   * dropped all but the first. Unix-seconds is monotonic and matches
   * the lib's `UTCTimestamp` type. */
  const toUnix = (iso: string): Time =>
    Math.floor(new Date(iso).getTime() / 1000) as unknown as Time;

  /** Drop strict-monotonicity violations (duplicate or backwards
   * timestamps) — the lib refuses an unsorted feed. Sort first, then
   * collapse equal timestamps to the latest bar at that time. */
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

    // Entry line for the open position
    if (entryLine) {
      try {
        chart?.removeSeries(entryLine);
      } catch (_) {
        /* ignore */
      }
      entryLine = undefined;
    }
    if (openPosition && candles.length > 0) {
      entryLine = chart!.addLineSeries({
        color: openPosition.side === 'long' ? PALETTE.up : PALETTE.down,
        lineWidth: 1,
        lineStyle: 2, // dashed
        priceLineVisible: false,
        lastValueVisible: false
      });
      entryLine.setData(
        candles.map((c) => ({ time: c.time, value: openPosition!.entry }))
      );
    }

    // Closed-trade markers (also need Unix seconds + monotonicity).
    const markers = normalize([
      ...(closedTrades || []).flatMap((t) => {
        const out: any[] = [
          {
            time: toUnix(t.entry_at),
            position: 'belowBar' as const,
            color: 'rgba(255,255,255,0.55)',
            shape: 'circle' as const,
            text: 'in'
          }
        ];
        if (t.exit_at) {
          out.push({
            time: toUnix(t.exit_at),
            position: 'aboveBar' as const,
            color: (t.pnl ?? 0) >= 0 ? PALETTE.up : PALETTE.down,
            shape: 'circle' as const,
            text: 'out'
          });
        }
        return out;
      }),
      ...(openPosition
        ? [
            {
              time: toUnix(openPosition.entry_at),
              position:
                openPosition.side === 'long'
                  ? ('belowBar' as const)
                  : ('aboveBar' as const),
              color: openPosition.side === 'long' ? PALETTE.up : PALETTE.down,
              shape:
                openPosition.side === 'long'
                  ? ('arrowUp' as const)
                  : ('arrowDown' as const),
              text: openPosition.side.toUpperCase()
            }
          ]
        : [])
    ]);
    candleSeries.setMarkers(markers);
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

  // Re-sync whenever the data props change.
  $effect(() => {
    bars;
    openPosition;
    closedTrades;
    if (candleSeries) syncData();
  });
</script>

<div bind:this={container} style="height: {height}px; width: 100%"></div>
