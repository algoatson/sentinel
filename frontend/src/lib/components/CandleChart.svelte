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

  const PALETTE = {
    bg: 'transparent',
    text: 'rgb(255 255 255 / 0.62)',
    grid: 'rgb(255 255 255 / 0.05)',
    border: 'rgb(255 255 255 / 0.085)',
    up: '#3ddc97',
    down: '#ff6b6b',
    upVol: 'rgba(61,220,151,0.45)',
    downVol: 'rgba(255,107,107,0.45)'
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

  function syncData() {
    if (!candleSeries || !volumeSeries) return;
    const candles = bars.map((b) => ({
      time: b.ts.slice(0, 10) as Time,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close
    }));
    candleSeries.setData(candles);

    const vols = bars.map((b) => ({
      time: b.ts.slice(0, 10) as Time,
      value: b.volume,
      color: b.close >= b.open ? PALETTE.upVol : PALETTE.downVol
    }));
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

    // Closed-trade markers
    const markers = [
      ...(closedTrades || []).flatMap((t) => {
        const out = [
          {
            time: t.entry_at.slice(0, 10) as Time,
            position: 'belowBar' as const,
            color: 'rgba(255,255,255,0.55)',
            shape: 'circle' as const,
            text: 'in'
          }
        ];
        if (t.exit_at) {
          out.push({
            time: t.exit_at.slice(0, 10) as Time,
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
              time: openPosition.entry_at.slice(0, 10) as Time,
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
    ];
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
