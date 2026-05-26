<script lang="ts">
  /**
   * Multi-line equity curve, one line per fund, normalised to
   * starting cash = 0% so different fund sizes are visually
   * comparable.
   *
   * Built on lightweight-charts (already in the bundle for the
   * candle view). Properly handles:
   *  - aspect ratio + responsive width via the lib's resize
   *  - shared y-axis across series so the 0% baseline lines up
   *  - hover crosshair + per-series value tooltip
   *  - time axis with sensible date formatting
   *
   * Previous SVG implementation had two scale calculations
   * (one for the lines, one for the baseline) that drifted, plus
   * `preserveAspectRatio="none"` distorted the inline text. Both
   * fixed by handing off to the charting lib.
   */
  import { onMount, onDestroy } from 'svelte';
  import {
    createChart,
    type IChartApi,
    type ISeriesApi,
    type Time
  } from 'lightweight-charts';
  import type { EquityCurve } from '$lib/types';

  interface Props {
    series: EquityCurve[];
    height?: number;
  }

  let { series, height = 240 }: Props = $props();

  const PALETTE = [
    '#6699ff', // primary
    '#3ddc97', // good (mint)
    '#a78bfa', // violet
    '#fbbf24', // warn (amber)
    '#ff6b6b', // bad
    '#8ed1fc', // sky
    '#f2c0ff', // lavender
    '#a3e635'  // lime
  ];

  let container: HTMLDivElement;
  let chart: IChartApi | undefined;
  let lineSeries: Array<{ name: string; color: string; api: ISeriesApi<'Line'>; last: number }> = [];

  // Hover readout: the chart's `subscribeCrosshairMove` populates these.
  let hoverDate = $state<string | null>(null);
  let hoverValues = $state<Array<{ name: string; color: string; pct: number | null }>>([]);

  function makeData(s: EquityCurve): { time: Time; value: number }[] {
    return s.points
      .map((p) => {
        const pct = s.starting ? ((p.equity - s.starting) / s.starting) * 100 : 0;
        // lightweight-charts wants either Unix seconds or "YYYY-MM-DD".
        // We have minute-level marks, so use seconds.
        const t = Math.floor(new Date(p.ts).getTime() / 1000) as unknown as Time;
        return { time: t, value: pct };
      })
      // Dedupe + sort by time (lib requires strictly increasing).
      .sort((a, b) => (a.time as number) - (b.time as number))
      .reduce<{ time: Time; value: number }[]>((acc, pt) => {
        const prev = acc[acc.length - 1];
        if (!prev || (prev.time as number) < (pt.time as number)) acc.push(pt);
        return acc;
      }, []);
  }

  function build() {
    if (!container) return;
    chart = createChart(container, {
      width: container.clientWidth,
      height,
      layout: {
        background: { color: 'transparent' },
        textColor: 'rgba(255,255,255,0.62)',
        fontFamily: 'Inter, system-ui, sans-serif'
      },
      grid: {
        vertLines: { color: 'rgba(255,255,255,0.05)' },
        horzLines: { color: 'rgba(255,255,255,0.05)' }
      },
      rightPriceScale: {
        borderColor: 'rgba(255,255,255,0.085)',
        scaleMargins: { top: 0.08, bottom: 0.08 }
      },
      timeScale: {
        borderColor: 'rgba(255,255,255,0.085)',
        timeVisible: false,
        secondsVisible: false
      },
      crosshair: {
        vertLine: { color: 'rgba(255,255,255,0.18)', width: 1 },
        horzLine: { color: 'rgba(255,255,255,0.18)', width: 1 }
      },
      handleScroll: false,   // dashboard panel; no need for scroll/zoom
      handleScale: false
    });

    // Dashed 0% baseline via a constant-line price line on the first
    // series (price-lines belong to a series in lightweight-charts).
    // We add it once series exist below.

    syncSeries();
    chart.timeScale().fitContent();

    // Hover crosshair → tooltip.
    chart.subscribeCrosshairMove((param) => {
      if (!param || !param.time || !param.seriesData) {
        hoverDate = null;
        hoverValues = [];
        return;
      }
      const tsec = param.time as number;
      hoverDate = new Date(tsec * 1000).toISOString().slice(0, 10);
      hoverValues = lineSeries.map((l) => {
        const v = param.seriesData.get(l.api) as
          | { value: number }
          | undefined;
        return { name: l.name, color: l.color, pct: v ? v.value : null };
      });
    });
  }

  function syncSeries() {
    if (!chart) return;
    // Tear down any previous series before rebuilding.
    for (const l of lineSeries) {
      try {
        chart.removeSeries(l.api);
      } catch (_) {
        /* ignore */
      }
    }
    lineSeries = [];

    series.forEach((s, i) => {
      const data = makeData(s);
      if (!data.length) return;
      const color = PALETTE[i % PALETTE.length];
      // No `title` and `lastValueVisible: false` — the in-chart
      // series labels covered the right gutter (which also hides
      // the right-edge of the lines). The HTML legend below the
      // chart already shows fund · last % per line, and the
      // floating hover overlay shows the per-fund value at the
      // crosshair, so the inline label was redundant + intrusive.
      const api = chart!.addLineSeries({
        color,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        priceFormat: {
          type: 'custom',
          minMove: 0.01,
          formatter: (v: number) => `${v.toFixed(2)}%`
        }
      });
      api.setData(data);

      // Cluster trade markers per UTC day so a busy session doesn't
      // paint a wall of overlapping dots on one line. Each daily
      // cluster is one marker: net-PnL-coloured, labelled with the
      // tickers (truncated). lightweight-charts requires monotonic
      // time order so we anchor each cluster at noon UTC of its day
      // (one unique time per day).
      type DayBucket = {
        pnl: number;
        tickers: string[];
        count: number;
      };
      const byDay = new Map<string, DayBucket>();
      for (const t of s.trades || []) {
        const day = new Date(t.ts).toISOString().slice(0, 10);
        const bucket = byDay.get(day) ?? { pnl: 0, tickers: [], count: 0 };
        bucket.pnl += t.pnl ?? 0;
        if (!bucket.tickers.includes(t.ticker)) bucket.tickers.push(t.ticker);
        bucket.count += 1;
        byDay.set(day, bucket);
      }
      const trades = Array.from(byDay.entries())
        .map(([day, b]) => {
          // Anchor at 12:00 UTC of the day so the marker sits cleanly
          // on the daily equity tick around the same time.
          const ts = Math.floor(new Date(day + 'T12:00:00Z').getTime() / 1000);
          const label =
            b.tickers.length === 1
              ? b.tickers[0]
              : b.count > 1
                ? `${b.tickers.slice(0, 2).join(',')}${b.count > 2 ? '+' + (b.count - 2) : ''}`
                : b.tickers[0];
          return {
            time: ts as unknown as Time,
            position: (b.pnl >= 0 ? 'aboveBar' : 'belowBar') as
              'aboveBar' | 'belowBar',
            color: b.pnl >= 0 ? '#3ddc97' : '#ff6b6b',
            shape: 'circle' as const,
            text: label,
          };
        })
        .sort((a, b) => (a.time as number) - (b.time as number));
      if (trades.length) api.setMarkers(trades);

      lineSeries.push({
        name: s.fund,
        color,
        api,
        last: data.length ? data[data.length - 1].value : 0
      });
    });

    // 0% baseline as a price line on the first series so the lib
    // does the scale math for us. Dashed grey, no label.
    if (lineSeries.length) {
      lineSeries[0].api.createPriceLine({
        price: 0,
        color: 'rgba(255,255,255,0.22)',
        lineWidth: 1,
        lineStyle: 2,  // dashed
        axisLabelVisible: true,
        title: '0%'
      });
    }

    if (lineSeries.length) chart.timeScale().fitContent();
  }

  function resize() {
    if (chart && container) {
      chart.applyOptions({ width: container.clientWidth });
    }
  }

  onMount(() => {
    build();
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

  // Re-sync if the parent passes a new series (e.g. range chip
  // toggled 30d → 90d → 1y).
  $effect(() => {
    series;
    if (chart) syncSeries();
  });
</script>

<div class="relative">
  <div bind:this={container} style:height="{height}px"></div>

  <!-- Hover readout overlay: small floating panel in the top-left. -->
  {#if hoverDate && hoverValues.length}
    <!-- Hover readout pinned top-left (per user preference — the
         earlier attempt at top-right was worse). -->
    <div
      class="pointer-events-none absolute left-2 top-2 rounded-md border border-border bg-surface/95 px-2 py-1.5 text-[10.5px] tabular shadow-lg backdrop-blur"
    >
      <div class="font-mono text-faint">{hoverDate}</div>
      <div class="mt-0.5 space-y-0.5">
        {#each hoverValues as v (v.name)}
          <div class="flex items-center gap-1.5">
            <span
              class="inline-block h-1.5 w-1.5 rounded-full"
              style:background-color={v.color}
            ></span>
            <span class="capitalize text-muted">{v.name}</span>
            {#if v.pct !== null}
              <span class={['ml-1 tabular', v.pct >= 0 ? 'text-good' : 'text-bad'].join(' ')}>
                {v.pct >= 0 ? '+' : ''}{v.pct.toFixed(2)}%
              </span>
            {:else}
              <span class="ml-1 text-faint">—</span>
            {/if}
          </div>
        {/each}
      </div>
    </div>
  {/if}

  <!-- Legend strip below the chart: fund · last % · mandate (clamped). -->
  {#if lineSeries.length}
    <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] tabular">
      {#each lineSeries as l, i (l.name)}
        {@const mandate = series[i]?.mandate ?? ''}
        <div class="flex items-center gap-1.5">
          <span
            class="inline-block h-2 w-2 rounded-full"
            style:background-color={l.color}
          ></span>
          <span class="capitalize text-muted">{l.name}</span>
          <span class={l.last >= 0 ? 'text-good' : 'text-bad'}>
            {l.last >= 0 ? '+' : ''}{l.last.toFixed(2)}%
          </span>
          {#if mandate}
            <span class="hidden truncate max-w-[10rem] text-faint md:inline" title={mandate}>
              · {mandate}
            </span>
          {/if}
        </div>
      {/each}
    </div>
  {:else}
    <div class="py-8 text-center text-[12px] text-faint">
      No equity history yet — funds need at least one daily mark.
    </div>
  {/if}
</div>
