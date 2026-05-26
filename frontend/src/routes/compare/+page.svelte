<script lang="ts">
  /**
   * /compare?tickers=NVDA,AMD,TSLA — head-to-head ticker view.
   * Multi-line normalised-return overlay (each series starts at
   * 0% on the first shared date) + side-by-side stats grid.
   *
   * Inspired by TradingView's compare overlay + Yahoo Finance's
   * "compare against" feature. Limit 5 tickers to keep the chart
   * readable.
   */
  import { onMount, onDestroy } from 'svelte';
  import { createQueries } from '@tanstack/svelte-query';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { tickerChart, tickerStats } from '$api';
  import {
    createChart, type IChartApi, type ISeriesApi, type Time
  } from 'lightweight-charts';
  import Card from '$components/Card.svelte';
  import TickerLink from '$components/TickerLink.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import { GitCompareArrows, X, Plus } from 'lucide-svelte';
  import { price, pct } from '$lib/format';

  const PALETTE = [
    '#6699ff', '#3ddc97', '#a78bfa', '#fbbf24', '#ff6b6b'
  ];
  const MAX = 5;

  type Range = { label: string; days: number };
  const RANGES: Range[] = [
    { label: '1m', days: 30 },
    { label: '3m', days: 90 },
    { label: '6m', days: 180 },
    { label: '1y', days: 365 }
  ];

  // Reactive ticker list — sync ?tickers= ↔ state
  const userTickers = $derived(
    (page.url.searchParams.get('tickers') || '')
      .split(',')
      .map((t) => t.trim().toUpperCase().replace(/^\$/, ''))
      .filter(Boolean)
      .slice(0, MAX)
  );
  // Effective list rendered into the chart + stats grid. SPY is
  // *pinned* (not part of the MAX 5 user slots) so the benchmark
  // doesn't consume a slot the trader wanted for a real ticker.
  const tickers = $derived(
    showSpy && !userTickers.includes('SPY')
      ? ['SPY', ...userTickers]
      : userTickers
  );
  let chartRange: Range = $state(RANGES[1]);
  let input = $state('');
  let showSpy = $state(false);
  const LS_TICKERS = 'sentinel:compare:tickers';
  const LS_SPY = 'sentinel:compare:spy';

  // On first load, if the URL has no `?tickers=`, restore the last
  // saved list from localStorage so the page picks up where the user
  // left off. Persist on every change.
  if (typeof window !== 'undefined') {
    if (!page.url.searchParams.get('tickers')) {
      try {
        const saved = localStorage.getItem(LS_TICKERS);
        if (saved) {
          const params = new URLSearchParams(page.url.searchParams);
          params.set('tickers', saved);
          // Replace state without scrolling — initial bootstrap, no
          // animation.
          history.replaceState({}, '', `${base}/compare?${params}`);
        }
      } catch (_) { /* localStorage may be disabled */ }
      try {
        showSpy = localStorage.getItem(LS_SPY) === '1';
      } catch (_) { /* ignore */ }
    }
  }

  function setTickers(next: string[]) {
    const params = new URLSearchParams(page.url.searchParams);
    if (next.length) params.set('tickers', next.join(','));
    else params.delete('tickers');
    goto(`${base}/compare?${params}`, { keepFocus: true, noScroll: true });
    try {
      if (next.length) localStorage.setItem(LS_TICKERS, next.join(','));
      else localStorage.removeItem(LS_TICKERS);
    } catch (_) { /* ignore */ }
  }
  function toggleSpy() {
    showSpy = !showSpy;
    try { localStorage.setItem(LS_SPY, showSpy ? '1' : '0'); } catch (_) { /* ignore */ }
  }
  function addTicker() {
    const t = input.trim().toUpperCase().replace(/^\$/, '');
    if (!t) return;
    if (userTickers.includes(t) || userTickers.length >= MAX) {
      input = '';
      return;
    }
    setTickers([...userTickers, t]);
    input = '';
  }
  function removeTicker(t: string) {
    // SPY is controlled by the benchmark toggle, not the chip list.
    if (t === 'SPY' && showSpy && !userTickers.includes('SPY')) {
      toggleSpy();
      return;
    }
    setTickers(userTickers.filter((x) => x !== t));
  }

  // Per-ticker chart query, parallel.
  const chartQueries = $derived(
    createQueries({
      queries: tickers.map((t) => ({
        queryKey: ['compare-chart', t, chartRange.days],
        queryFn: () => tickerChart(t, chartRange.days),
        enabled: !!t
      }))
    })
  );
  const statsQueries = $derived(
    createQueries({
      queries: tickers.map((t) => ({
        queryKey: ['compare-stats', t],
        queryFn: () => tickerStats(t, 365),
        enabled: !!t
      }))
    })
  );

  let container: HTMLDivElement;
  let chart: IChartApi | undefined;
  let seriesByTicker: Map<string, ISeriesApi<'Line'>> = new Map();

  function build() {
    if (!container) return;
    chart = createChart(container, {
      width: container.clientWidth,
      height: 420,
      layout: {
        background: { color: 'transparent' },
        textColor: 'rgba(255, 255, 255, 0.62)',
        fontFamily: 'Inter, system-ui, sans-serif'
      },
      grid: {
        vertLines: { color: 'rgba(255, 255, 255, 0.05)' },
        horzLines: { color: 'rgba(255, 255, 255, 0.05)' }
      },
      rightPriceScale: {
        borderColor: 'rgba(255, 255, 255, 0.085)',
        scaleMargins: { top: 0.08, bottom: 0.08 }
      },
      timeScale: {
        borderColor: 'rgba(255, 255, 255, 0.085)',
        timeVisible: false,
        secondsVisible: false
      },
      crosshair: {
        vertLine: { color: 'rgba(255, 255, 255, 0.18)', width: 1 },
        horzLine: { color: 'rgba(255, 255, 255, 0.18)', width: 1 }
      },
      handleScroll: false,
      handleScale: false
    });
  }

  function rebuild() {
    if (!chart) return;
    for (const s of seriesByTicker.values()) {
      try { chart.removeSeries(s); } catch (_) { /* ignore */ }
    }
    seriesByTicker.clear();

    const charts = $chartQueries.map((q) => q.data);
    // Find the LATEST first-bar timestamp across all series — every
    // line should start at the same moment so the % normalisation
    // is comparable.
    let alignStart = -Infinity;
    for (const d of charts) {
      if (!d?.bars?.length) continue;
      const t0 = Math.floor(new Date(d.bars[0].ts).getTime() / 1000);
      if (t0 > alignStart) alignStart = t0;
    }
    if (!isFinite(alignStart)) return;

    tickers.forEach((tk, i) => {
      const d = charts[i];
      if (!d?.bars?.length) return;
      // Find the first bar at or after alignStart — its close is
      // the 0% reference for this series.
      const refIdx = d.bars.findIndex(
        (b) => Math.floor(new Date(b.ts).getTime() / 1000) >= alignStart
      );
      if (refIdx < 0) return;
      const refClose = d.bars[refIdx].close;
      if (!refClose) return;

      const data: { time: Time; value: number }[] = [];
      for (let j = refIdx; j < d.bars.length; j++) {
        const b = d.bars[j];
        const t = Math.floor(new Date(b.ts).getTime() / 1000) as unknown as Time;
        const v = ((b.close - refClose) / refClose) * 100;
        data.push({ time: t, value: v });
      }
      // Dedupe + sort
      data.sort((a, b) => (a.time as number) - (b.time as number));
      const dedup: typeof data = [];
      for (const pt of data) {
        const prev = dedup[dedup.length - 1];
        if (prev && (prev.time as number) === (pt.time as number)) {
          dedup[dedup.length - 1] = pt;
        } else {
          dedup.push(pt);
        }
      }

      const series = chart!.addLineSeries({
        color: PALETTE[i % PALETTE.length],
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        priceFormat: {
          type: 'custom', minMove: 0.01,
          formatter: (v: number) => `${v.toFixed(2)}%`
        }
      });
      series.setData(dedup);
      seriesByTicker.set(tk, series);
    });

    // 0% baseline as a price line on the first series.
    const first = seriesByTicker.get(tickers[0]);
    if (first) {
      first.createPriceLine({
        price: 0,
        color: 'rgba(255, 255, 255, 0.22)',
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: '0%'
      });
    }
    chart.timeScale().fitContent();
  }

  function resize() {
    if (chart && container) chart.applyOptions({ width: container.clientWidth });
  }

  onMount(() => {
    build();
    window.addEventListener('resize', resize);
  });
  onDestroy(() => {
    window.removeEventListener('resize', resize);
    try { chart?.remove(); } catch (_) { /* ignore */ }
  });

  $effect(() => {
    tickers;
    $chartQueries;
    chartRange;
    if (chart) rebuild();
  });

  // Latest return per ticker for the stats row.
  function latestPct(idx: number): number | null {
    const d = $chartQueries[idx]?.data;
    if (!d?.bars?.length) return null;
    const first = d.bars[0]?.close;
    const last = d.bars[d.bars.length - 1]?.close;
    if (!first || !last) return null;
    return ((last - first) / first) * 100;
  }
</script>

<svelte:head><title>Compare · Sentinel</title></svelte:head>

<div class="mb-4 flex items-end justify-between gap-3 border-b border-border pb-3">
  <div>
    <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
      <GitCompareArrows class="h-5 w-5 text-primary" /><span>Compare</span>
    </h1>
    <div class="mt-0.5 text-[11.5px] text-faint">
      Head-to-head multi-ticker chart with normalised returns. Up to {MAX} tickers.
    </div>
  </div>
  <div class="flex items-center gap-2">
    <button
      type="button"
      onclick={toggleSpy}
      title="Overlay the S&P 500 (SPY) as a benchmark"
      class={[
        'rounded-md border px-2.5 py-1 text-[11.5px] transition-colors',
        showSpy
          ? 'border-good/40 bg-good-soft text-good'
          : 'border-border bg-surface-2 text-muted hover:text-text'
      ].join(' ')}
    >
      vs SPY
    </button>
    <div class="flex items-center gap-1 border-l border-border pl-2">
      {#each RANGES as r (r.label)}
        <button
          onclick={() => (chartRange = r)}
          class={[
            'rounded-md border px-2.5 py-1 text-[11.5px] transition-colors',
            chartRange.label === r.label
              ? 'border-primary/50 bg-primary-soft text-primary'
              : 'border-border bg-surface-2 text-muted hover:text-text'
          ].join(' ')}
        >{r.label}</button>
      {/each}
    </div>
  </div>
</div>

<Card class="px-4 py-3">
  <div class="flex flex-wrap items-center gap-2">
    <span class="text-[10px] font-semibold uppercase tracking-wider text-faint">Tickers</span>
    {#each tickers as t, i (t)}
      {@const isBenchmark = t === 'SPY' && showSpy && !userTickers.includes('SPY')}
      <span
        class="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[12px] font-mono tabular"
        style:border-color="rgba(102, 153, 255, 0.35)"
        style:background-color="{PALETTE[i % PALETTE.length]}22"
        title={isBenchmark ? 'Benchmark · toggle "vs SPY" to hide' : ''}
      >
        <span
          class="inline-block h-2 w-2 rounded-full"
          style:background-color={PALETTE[i % PALETTE.length]}
        ></span>
        <a
          href={`${base}/symbol/${encodeURIComponent(t)}`}
          class="font-semibold text-text hover:text-primary hover:underline"
        >${t}</a>
        {#if isBenchmark}
          <span class="text-[9px] uppercase tracking-wider text-faint">bench</span>
        {/if}
        <button
          type="button"
          onclick={() => removeTicker(t)}
          class="-mr-0.5 ml-0.5 rounded p-0.5 text-faint transition-colors hover:bg-surface-2 hover:text-bad"
          aria-label="Remove"
        ><X class="h-3 w-3" /></button>
      </span>
    {/each}
    {#if userTickers.length < MAX}
      <form
        onsubmit={(e) => { e.preventDefault(); addTicker(); }}
        class="inline-flex items-center"
      >
        <input
          type="text"
          bind:value={input}
          placeholder="$AAPL"
          class="w-24 rounded-l-md border border-border bg-surface-2 px-2 py-1 font-mono text-[12px] text-text placeholder:text-faint/50 focus:border-primary/60 focus:outline-none"
        />
        <button
          type="submit"
          class="rounded-r-md border border-l-0 border-border bg-surface-2 px-2 py-1 text-[11px] text-muted hover:border-primary/40 hover:text-text"
        ><Plus class="h-3 w-3" /></button>
      </form>
    {/if}
    {#if !userTickers.length}
      <span class="text-[11px] italic text-faint">Add one to start; try $NVDA, $AMD, $TSLA</span>
    {/if}
  </div>
</Card>

{#if !tickers.length}
  <Card class="mt-4 px-4 py-8">
    <EmptyState
      title="Pick tickers to compare"
      description="Normalised return overlay — every series starts at 0% on the latest common start date. Click any ticker tag to remove it."
    />
  </Card>
{:else}
  <Card class="mt-4 overflow-hidden p-2">
    <div bind:this={container} style="height: 420px; width: 100%"></div>
  </Card>

  <!-- per-ticker stats grid -->
  <Card class="mt-3 overflow-hidden">
    <div class="overflow-x-auto">
      <table class="w-full text-[12.5px] tabular">
        <thead>
          <tr class="border-b border-border bg-surface-2/40 text-[10px] uppercase tracking-wider text-faint">
            <th class="px-3 py-2 text-left">Ticker</th>
            <th class="px-3 py-2 text-right">Last</th>
            <th class="px-3 py-2 text-right">1d</th>
            <th class="px-3 py-2 text-right">5d</th>
            <th class="px-3 py-2 text-right">Window</th>
            <th class="px-3 py-2 text-right">52w high</th>
            <th class="px-3 py-2 text-right">52w low</th>
          </tr>
        </thead>
        <tbody>
          {#each tickers as t, i (t)}
            {@const s = $statsQueries[i]?.data}
            {@const ret = latestPct(i)}
            <tr class="border-b border-border-soft">
              <td class="px-3 py-2 text-left">
                <span
                  class="mr-2 inline-block h-2 w-2 rounded-full"
                  style:background-color={PALETTE[i % PALETTE.length]}
                ></span>
                <TickerLink ticker={t} class="text-[13px]" />
              </td>
              <td class="px-3 py-2 text-right">{s ? price(s.last_price) : '—'}</td>
              <td class={[
                'px-3 py-2 text-right tabular',
                s && (s.change_1d_pct ?? 0) >= 0 ? 'text-good' : 'text-bad'
              ].join(' ')}>
                {s?.change_1d_pct !== null && s?.change_1d_pct !== undefined
                  ? pct(s.change_1d_pct, 2)
                  : '—'}
              </td>
              <td class={[
                'px-3 py-2 text-right tabular',
                s && (s.change_5d_pct ?? 0) >= 0 ? 'text-good' : 'text-bad'
              ].join(' ')}>
                {s?.change_5d_pct !== null && s?.change_5d_pct !== undefined
                  ? pct(s.change_5d_pct, 2)
                  : '—'}
              </td>
              <td class={[
                'px-3 py-2 text-right tabular font-medium',
                ret === null ? 'text-faint' : ret >= 0 ? 'text-good' : 'text-bad'
              ].join(' ')}>
                {ret !== null ? pct(ret, 2) : '—'}
              </td>
              <td class="px-3 py-2 text-right text-faint">{s ? price(s.high_52w) : '—'}</td>
              <td class="px-3 py-2 text-right text-faint">{s ? price(s.low_52w) : '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </Card>
{/if}
