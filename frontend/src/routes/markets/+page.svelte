<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { reactiveQueryOptions } from '$lib/reactive-query.svelte';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { watchlist, tickerChart, tickerStats } from '$api';
  import Card from '$components/Card.svelte';
  import Pill from '$components/Pill.svelte';
  import Delta from '$components/Delta.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import Spinner from '$components/Spinner.svelte';
  import CandleChart from '$components/CandleChart.svelte';
  import Sparkline from '$components/Sparkline.svelte';
  import Heatmap from '$components/Heatmap.svelte';
  import { usd, price, compact } from '$lib/format';

  type Range = { label: string; days: number | null };
  const RANGES: Range[] = [
    { label: '1w', days: 7 },
    { label: '1m', days: 30 },
    { label: '3m', days: 90 },
    { label: '6m', days: 180 },
    { label: '1y', days: 365 },
    { label: 'All', days: null }
  ];

  // ?ticker=NVDA → preselect that symbol so deep-links from TickerLink
  // anywhere in the app land on the chart already loaded.
  const queryTicker = $derived(
    (page.url.searchParams.get('ticker') ?? '').toUpperCase().replace(/^\$/, '')
  );
  let selectedTicker = $state(queryTicker || 'SPY');
  let selectedRange: Range = $state(RANGES[1]);
  let searchInput = $state('');

  $effect(() => {
    if (queryTicker && queryTicker !== selectedTicker) {
      selectedTicker = queryTicker;
    }
  });

  function pick(t: string) {
    const sym = t.toUpperCase().replace(/^\$/, '');
    selectedTicker = sym;
    // Push the picked symbol into the URL so refresh / share works.
    // `replaceState: true` keeps the back button useful — multiple
    // ticker hops in a row collapse to a single history entry.
    goto(`${base}/markets?ticker=${encodeURIComponent(sym)}`, {
      replaceState: true,
      keepFocus: true,
      noScroll: true
    });
  }
  function submitSearch() {
    if (searchInput.trim()) {
      pick(searchInput.trim());
      searchInput = '';
    }
  }

  const wlQ = createQuery({
    queryKey: ['watchlist'],
    queryFn: watchlist,
    refetchInterval: 45_000
  });
  // svelte-query 5.x: pass a function returning options so the query
  // re-evaluates whenever the reactive dependencies (selectedTicker/Range) change.
  const chartQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['ticker-chart', selectedTicker, selectedRange.days],
    queryFn: () => tickerChart(selectedTicker, selectedRange.days),
    enabled: !!selectedTicker
  })));
  const statsQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['ticker-stats', selectedTicker],
    queryFn: () => tickerStats(selectedTicker),
    enabled: !!selectedTicker
  })));

  type WlView = 'table' | 'heatmap';
  let wlView: WlView = $state('table');

  // Watchlist column-sort state
  type SortKey =
    | 'ticker'
    | 'last_price'
    | 'change_1d_pct'
    | 'change_1w_pct'
    | 'change_1m_pct'
    | 'change_1y_pct'
    | 'volume_vs_avg';
  let sortKey: SortKey = $state('change_1d_pct');
  let sortDir: 'asc' | 'desc' = $state('desc');
  let filter = $state('');

  function setSort(k: SortKey) {
    if (sortKey === k) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    else {
      sortKey = k;
      sortDir = k === 'ticker' ? 'asc' : 'desc';
    }
  }

  const sortedRows = $derived(
    [...($wlQ.data ?? [])]
      .filter((r) => {
        const f = filter.trim().toLowerCase();
        if (!f) return true;
        return (
          r.ticker.toLowerCase().includes(f) ||
          (r.asset_class ?? '').toLowerCase().includes(f)
        );
      })
      .sort((a, b) => {
        const va = (a as any)[sortKey];
        const vb = (b as any)[sortKey];
        if (sortKey === 'ticker') {
          return sortDir === 'asc'
            ? String(va).localeCompare(String(vb))
            : String(vb).localeCompare(String(va));
        }
        const na = typeof va === 'number' ? va : -Infinity;
        const nb = typeof vb === 'number' ? vb : -Infinity;
        return sortDir === 'asc' ? na - nb : nb - na;
      })
  );
</script>

<svelte:head><title>Markets · Sentinel</title></svelte:head>

<div class="mb-4 flex items-end justify-between border-b border-border pb-3">
  <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
    <span>📈</span><span>Markets</span>
  </h1>
</div>

<!-- ── ticker picker + range chips ─────────────────────────────────── -->
<Card class="px-4 py-3">
  <div class="flex flex-wrap items-center gap-2">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">Ticker</div>
    <form onsubmit={(e) => { e.preventDefault(); submitSearch(); }} class="flex items-center gap-1">
      <input
        type="text"
        placeholder="$NVDA"
        bind:value={searchInput}
        class="w-28 rounded-md border border-border bg-surface-2 px-2.5 py-1 font-mono text-sm tabular text-text placeholder:text-faint/50 focus:border-primary/60 focus:outline-none"
      />
      <button
        type="submit"
        class="rounded-md border border-border bg-surface-2 px-2 py-1 text-[11px] text-muted transition-colors hover:border-primary/40 hover:text-text"
      >Go</button>
    </form>
    <span class="ml-2 font-mono text-base font-semibold text-primary">${selectedTicker}</span>

    <div class="ml-auto flex items-center gap-1">
      <span class="mr-2 text-[10px] font-semibold uppercase tracking-wider text-faint">Range</span>
      {#each RANGES as r (r.label)}
        <button
          onclick={() => (selectedRange = r)}
          class={[
            'rounded-md border px-2 py-1 text-[11px] transition-colors',
            selectedRange.label === r.label
              ? 'border-primary/50 bg-primary-soft text-primary'
              : 'border-border bg-surface-2 text-muted hover:text-text'
          ].join(' ')}
        >{r.label}</button>
      {/each}
    </div>
  </div>
</Card>

<!-- ── chart + stats ───────────────────────────────────────────────── -->
<div class="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-[1fr_18rem]">
  <Card class="overflow-hidden p-2">
    {#if $chartQ?.isLoading}
      <div class="flex h-[480px] items-center justify-center"><Spinner /></div>
    {:else if !$chartQ?.data?.bars?.length}
      <div class="h-[480px]"><EmptyState title={`No price history for $${selectedTicker}`} description="The bot needs at least one PriceBar to draw — try a more liquid ticker (SPY/QQQ/NVDA) or wait for the next daily bar poll." /></div>
    {:else}
      <CandleChart
        bars={$chartQ.data.bars}
        openPosition={$chartQ.data.open_position}
        closedTrades={$chartQ.data.closed}
      />
    {/if}
  </Card>

  <Card class="px-4 py-3">
    {#if $statsQ?.data}
      {@const s = $statsQ.data}
      <div class="flex items-baseline gap-2">
        <div class="font-mono text-lg font-bold tracking-tight">${selectedTicker}</div>
        {#if s.bars_count}
          <Pill variant="neutral">{s.bars_count}b</Pill>
        {/if}
      </div>
      {#if s.last_price !== null}
        <div class="mt-1 text-[1.7rem] font-semibold tabular leading-none">
          {price(s.last_price)}
        </div>
        <div class="mt-1 flex gap-2.5 text-[11.5px]">
          {#if s.change_1d_pct !== null}<Delta value={s.change_1d_pct} label="1d" />{/if}
          {#if s.change_5d_pct !== null}<Delta value={s.change_5d_pct} label="5d" />{/if}
        </div>
      {:else}
        <div class="mt-2 text-[12px] text-faint">No price context.</div>
      {/if}

      {#if s.day_high && s.day_low && s.last_price}
        {@const dPct =
          ((s.last_price - s.day_low) / (s.day_high - s.day_low)) * 100}
        <div class="mt-4 space-y-1">
          <div class="flex justify-between text-[10.5px] text-faint">
            <span>Day range</span>
          </div>
          <div class="flex items-center gap-2 text-[11px] tabular">
            <span class="text-faint">{price(s.day_low)}</span>
            <div class="relative h-1.5 flex-1 overflow-hidden rounded bg-surface-2">
              <div
                class="absolute top-1/2 h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-primary shadow-[0_0_6px_var(--color-primary)]"
                style:left="{Math.max(0, Math.min(100, dPct))}%"
              ></div>
            </div>
            <span>{price(s.day_high)}</span>
          </div>
        </div>
      {/if}

      {#if s.high_52w && s.low_52w && s.last_price}
        {@const yPct =
          ((s.last_price - s.low_52w) / (s.high_52w - s.low_52w)) * 100}
        <div class="mt-3 space-y-1">
          <div class="flex justify-between text-[10.5px] text-faint">
            <span>52w range</span>
          </div>
          <div class="flex items-center gap-2 text-[11px] tabular">
            <span class="text-faint">{price(s.low_52w)}</span>
            <div class="relative h-1.5 flex-1 overflow-hidden rounded bg-surface-2">
              <div
                class="absolute top-1/2 h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-primary shadow-[0_0_6px_var(--color-primary)]"
                style:left="{Math.max(0, Math.min(100, yPct))}%"
              ></div>
            </div>
            <span>{price(s.high_52w)}</span>
          </div>
        </div>
      {/if}

      {#if s.volume !== null || s.avg_volume_20d !== null}
        <div class="mt-4 space-y-1 border-t border-border pt-3 text-[12px]">
          {#if s.volume !== null}
            <div class="flex justify-between">
              <span class="text-faint">Volume</span>
              <span class="tabular">{compact(s.volume)}</span>
            </div>
          {/if}
          {#if s.avg_volume_20d !== null}
            <div class="flex justify-between">
              <span class="text-faint">Avg 20d</span>
              <span class="tabular">{compact(s.avg_volume_20d)}</span>
            </div>
          {/if}
        </div>
      {/if}

      {#if $chartQ?.data?.open_position}
        {@const op = $chartQ.data.open_position}
        <div class="mt-4 border-t border-border pt-3">
          <div class="text-[10.5px] font-semibold uppercase tracking-wider text-faint">
            Open position
          </div>
          <div class="mt-1.5 flex items-baseline gap-2">
            <Pill variant={op.side === 'long' ? 'pos' : 'neg'}>{op.side.toUpperCase()}</Pill>
            <span class="font-mono text-sm">{op.qty}</span>
            <span class="text-[11px] text-faint">@ {price(op.entry)}</span>
          </div>
          {#if op.pnl !== null}
            <div class="mt-1 text-[12px]">
              <span class="text-faint">uPnL</span>
              <span class={['ml-2 tabular font-semibold', op.pnl >= 0 ? 'text-good' : 'text-bad'].join(' ')}>
                {usd(op.pnl, true)}{#if op.pnl_pct !== null} ({op.pnl_pct.toFixed(2)}%){/if}
              </span>
            </div>
          {/if}
        </div>
      {/if}
    {:else if $statsQ?.isLoading}
      <Spinner />
    {/if}
  </Card>
</div>

<!-- ── watchlist ───────────────────────────────────────────────────── -->
<Card class="mt-3 overflow-hidden">
  <div class="flex items-center gap-3 border-b border-border px-4 py-2.5">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">Watchlist</div>
    <input
      type="text"
      bind:value={filter}
      placeholder="Filter…"
      class="w-48 rounded-md border border-border bg-surface-2 px-2 py-1 text-[12px] text-text placeholder:text-faint/50 focus:border-primary/60 focus:outline-none"
    />

    <div class="ml-auto flex items-center gap-1">
      <button
        onclick={() => (wlView = 'table')}
        class={[
          'rounded-md border px-2 py-1 text-[10.5px] transition-colors',
          wlView === 'table'
            ? 'border-primary/50 bg-primary-soft text-primary'
            : 'border-border bg-surface-2 text-muted hover:text-text'
        ].join(' ')}
      >Table</button>
      <button
        onclick={() => (wlView = 'heatmap')}
        class={[
          'rounded-md border px-2 py-1 text-[10.5px] transition-colors',
          wlView === 'heatmap'
            ? 'border-primary/50 bg-primary-soft text-primary'
            : 'border-border bg-surface-2 text-muted hover:text-text'
        ].join(' ')}
      >Heatmap</button>
    </div>

    <span class="text-[11px] text-faint">
      {sortedRows.length} symbol{sortedRows.length === 1 ? '' : 's'}
    </span>
  </div>

  {#if $wlQ.isLoading}
    <div class="flex items-center justify-center py-12"><Spinner /></div>
  {:else if !sortedRows.length}
    <EmptyState title="Watchlist empty" />
  {:else if wlView === 'heatmap'}
    <div class="p-4">
      <Heatmap rows={sortedRows} limit={80} />
    </div>
  {:else}
    <div class="overflow-x-auto">
      <table class="w-full text-[12.5px] tabular">
        <thead>
          <tr class="border-b border-border text-[10px] uppercase tracking-wider text-faint">
            {#snippet th(label: string, key: SortKey, align: 'left' | 'right' = 'right')}
              <th
                class={[
                  'px-3 py-2 font-semibold transition-colors hover:text-text',
                  align === 'left' ? 'text-left' : 'text-right',
                  'cursor-pointer select-none'
                ].join(' ')}
                onclick={() => setSort(key)}
              >
                <span>{label}</span>
                {#if sortKey === key}
                  <span class="ml-0.5 text-primary">{sortDir === 'asc' ? '▲' : '▼'}</span>
                {/if}
              </th>
            {/snippet}
            {@render th('Ticker', 'ticker', 'left')}
            <th class="px-3 py-2 text-right font-semibold">Class</th>
            {@render th('Last', 'last_price')}
            {@render th('1d %', 'change_1d_pct')}
            {@render th('1w %', 'change_1w_pct')}
            {@render th('1m %', 'change_1m_pct')}
            {@render th('1y %', 'change_1y_pct')}
            <th class="hidden px-3 py-2 text-center font-semibold lg:table-cell">30d</th>
            {@render th('Vol×', 'volume_vs_avg')}
          </tr>
        </thead>
        <tbody>
          {#each sortedRows.slice(0, 80) as r (r.ticker)}
            <tr
              class="cursor-pointer border-b border-border-soft transition-colors hover:bg-white/[0.025]"
              onclick={() => pick(r.ticker)}
            >
              <td class="px-3 py-1.5 text-left">
                <span class="font-mono font-semibold text-text">${r.ticker}</span>
              </td>
              <td class="px-3 py-1.5 text-right text-faint">{r.asset_class}</td>
              <td class="px-3 py-1.5 text-right">{price(r.last_price)}</td>
              <td class="px-3 py-1.5 text-right"><Delta value={r.change_1d_pct} /></td>
              <td class="px-3 py-1.5 text-right"><Delta value={r.change_1w_pct} /></td>
              <td class="px-3 py-1.5 text-right"><Delta value={r.change_1m_pct} /></td>
              <td class="px-3 py-1.5 text-right"><Delta value={r.change_1y_pct} /></td>
              <td class="hidden px-3 py-1 align-middle lg:table-cell">
                <div class="flex justify-center">
                  <Sparkline values={r.spark_30d} width={80} height={20} />
                </div>
              </td>
              <td
                class={[
                  'px-3 py-1.5 text-right',
                  (r.volume_vs_avg ?? 0) >= 1.8
                    ? 'text-good'
                    : (r.volume_vs_avg ?? 0) < 0.5
                      ? 'text-faint'
                      : 'text-muted'
                ].join(' ')}
              >
                {r.volume_vs_avg !== null ? `×${r.volume_vs_avg.toFixed(2)}` : '—'}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</Card>
