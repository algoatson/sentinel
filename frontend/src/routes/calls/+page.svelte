<script lang="ts">
  import { createQuery, useQueryClient } from '@tanstack/svelte-query';
  import { reactiveQueryOptions } from '$lib/reactive-query.svelte';
  import { calls, callDossier, askCall, scorecard } from '$api';
  import Card from '$components/Card.svelte';
  import Pill from '$components/Pill.svelte';
  import Delta from '$components/Delta.svelte';
  import StatTile from '$components/StatTile.svelte';
  import Drawer from '$components/Drawer.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import Spinner from '$components/Spinner.svelte';
  import Pager from '$components/Pager.svelte';
  import DossierBlock from '$components/DossierBlock.svelte';
  import AskBox from '$components/AskBox.svelte';
  import TickerLink from '$components/TickerLink.svelte';
  import Markdown from '$components/Markdown.svelte';
  import { price, timeAgo, stripMd } from '$lib/format';
  import { Target } from 'lucide-svelte';

  type SortKey = 'date' | 'conv' | 'ret_1d' | 'ret_5d' | 'ret_20d';
  type Dir = 'all' | 'long' | 'short';

  let days = $state(7);
  let tickerFilter = $state('');
  let directionFilter: Dir = $state('all');
  let convictionMin = $state(0);
  let sortKey: SortKey = $state('date');
  let selected = $state<number | null>(null);
  let refreshing = $state(false);

  const callsQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['calls', days],
    queryFn: () => calls(days),
    refetchInterval: 60_000
  })));
  const scorecardQ = createQuery({
    queryKey: ['scorecard'],
    queryFn: scorecard,
    refetchInterval: 90_000
  });
  const dossierQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['call-dossier', selected, refreshing],
    queryFn: () => callDossier(selected!, refreshing),
    enabled: selected !== null
  })));

  const qc = useQueryClient();

  async function regenerate() {
    if (selected === null) return;
    refreshing = true;
    try {
      await callDossier(selected, true);
      await qc.invalidateQueries({ queryKey: ['call-dossier', selected] });
    } finally {
      refreshing = false;
    }
  }

  async function askAboutSelected(q: string): Promise<string> {
    if (selected === null) throw new Error('no item selected');
    const r = await askCall(selected, q);
    return r.answer;
  }

  function pickField(c: any, key: SortKey): number {
    switch (key) {
      case 'date':   return new Date(c.ts).getTime();
      case 'conv':   return c.conviction ?? 0;
      case 'ret_1d': return c.ret_1d_pct ?? -Infinity;
      case 'ret_5d': return c.ret_5d_pct ?? -Infinity;
      case 'ret_20d':return c.ret_20d_pct ?? -Infinity;
    }
  }

  const filtered = $derived(
    ($callsQ.data ?? [])
      .filter((c) => {
        const t = tickerFilter.trim().toUpperCase().replace(/^\$/, '');
        if (t && c.ticker !== t) return false;
        if (directionFilter !== 'all' && c.direction !== directionFilter) return false;
        if (c.conviction < convictionMin) return false;
        return true;
      })
      .sort((a, b) => pickField(b, sortKey) - pickField(a, sortKey))
  );

  let page = $state(1);
  let pageSize = $state(25);
  $effect(() => {
    // reset to first page whenever the filter/sort axes shake the list
    days; tickerFilter; directionFilter; convictionMin; sortKey;
    page = 1;
  });
  const paged = $derived(filtered.slice((page - 1) * pageSize, page * pageSize));

  const selectedItem = $derived(
    selected !== null
      ? ($callsQ.data ?? []).find((c) => c.id === selected)
      : null
  );

  // Compose source/conviction matrix from scorecard for the ribbon.
  const sourceRows = $derived(
    Object.entries($scorecardQ.data?.by_source ?? {})
      .map(([k, v]) => ({
        source: k,
        n: v.n,
        hits: v.hits,
        rate: v.n ? (v.hits / v.n) * 100 : 0
      }))
      .sort((a, b) => b.n - a.n)
  );
  // Keyed by the backend's low/med/high buckets; the template renders them
  // in a fixed order, so no sort needed here.
  const convictionRows = $derived(
    Object.entries($scorecardQ.data?.by_conviction ?? {})
      .map(([k, v]) => ({
        bucket: k,
        n: v.n,
        hits: v.hits,
        rate: v.n ? (v.hits / v.n) * 100 : 0
      }))
  );

  function rateColor(rate: number, n: number): string {
    if (n < 5) return 'text-faint';
    if (rate >= 60) return 'text-good';
    if (rate >= 40) return 'text-muted';
    return 'text-bad';
  }
</script>

<svelte:head><title>Calls · Sentinel</title></svelte:head>

<div class="mb-4 flex items-end justify-between border-b border-border pb-3">
  <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
    <Target class="h-5 w-5 text-primary" /><span>Calls</span>
  </h1>
</div>

<!-- ── scorecard ribbon ────────────────────────────────────────── -->
{#if $scorecardQ.data}
  {@const s = $scorecardQ.data.overall}
  <div class="grid grid-cols-1 gap-3 lg:grid-cols-[12rem_1fr_1fr]">
    <StatTile
      label="Overall hit rate"
      value={s.n ? `${((s.hits / s.n) * 100).toFixed(0)}%` : '—'}
      sub={`${s.hits}/${s.n} scored`}
      accent={s.n >= 10 ? (s.hits / s.n >= 0.55 ? 'pos' : s.hits / s.n < 0.4 ? 'neg' : 'none') : 'none'}
    />

    <Card class="px-4 py-3">
      <div class="mb-2 text-[10px] font-semibold uppercase tracking-wider text-faint">
        By source
      </div>
      {#if !sourceRows.length}
        <div class="text-[11px] text-faint">No data yet.</div>
      {:else}
        <div class="grid grid-cols-2 gap-x-3 gap-y-1 text-[11.5px] tabular md:grid-cols-3">
          {#each sourceRows.slice(0, 9) as r (r.source)}
            <div class="flex items-center justify-between">
              <span class="truncate text-muted" title={r.source}>{r.source}</span>
              <span class={['ml-2 font-medium', rateColor(r.rate, r.n)].join(' ')}>
                {r.rate.toFixed(0)}%
                <span class="ml-0.5 text-[10px] text-faint">({r.hits}/{r.n})</span>
              </span>
            </div>
          {/each}
        </div>
      {/if}
    </Card>

    <Card class="px-4 py-3">
      <div class="mb-2 text-[10px] font-semibold uppercase tracking-wider text-faint">
        By conviction
      </div>
      {#if !convictionRows.length}
        <div class="text-[11px] text-faint">No data yet.</div>
      {:else}
        <!-- Backend buckets conviction into low (≤2) / med (3) / high (≥4)
             — same split _calibration_note uses. Render those three, not
             five levels (the old 5-tile grid matched on Number("low")=NaN,
             so every tile read 0/0 no matter the data). -->
        <div class="grid grid-cols-3 gap-2 text-center text-[11.5px] tabular">
          {#each [['low', '≤2'], ['med', '3'], ['high', '≥4']] as [key, lbl] (key)}
            {@const r = convictionRows.find((x) => x.bucket === key)}
            <div class="rounded-md border border-border bg-surface-2 px-2 py-1.5">
              <div class="text-[10px] uppercase tracking-wider text-faint">conv {lbl}</div>
              {#if r && r.n}
                <div class={['mt-0.5 text-[14px] font-semibold', rateColor(r.rate, r.n)].join(' ')}>
                  {r.rate.toFixed(0)}%
                </div>
                <div class="text-[10px] text-faint">{r.hits}/{r.n}</div>
              {:else}
                <div class="mt-0.5 text-[14px] font-semibold text-faint">—</div>
                <div class="text-[10px] text-faint">0/0</div>
              {/if}
            </div>
          {/each}
        </div>
      {/if}
    </Card>
  </div>
{/if}

<!-- ── filter ribbon ────────────────────────────────────────── -->
<Card class="mt-3 px-4 py-3">
  <div class="flex flex-wrap items-center gap-3">
    <div class="flex items-center gap-1">
      <span class="mr-2 text-[10px] font-semibold uppercase tracking-wider text-faint">Window</span>
      {#each [1, 3, 7, 30] as d (d)}
        <button
          onclick={() => (days = d)}
          class={[
            'rounded-md border px-2 py-1 text-[11px] transition-colors',
            days === d
              ? 'border-primary/50 bg-primary-soft text-primary'
              : 'border-border bg-surface-2 text-muted hover:text-text'
          ].join(' ')}
        >{d}d</button>
      {/each}
    </div>
    <div class="flex items-center gap-1">
      <span class="mr-2 text-[10px] font-semibold uppercase tracking-wider text-faint">Side</span>
      {#each [['all', 'All'], ['long', 'Long'], ['short', 'Short']] as [k, label] (k)}
        <button
          onclick={() => (directionFilter = k as any)}
          class={[
            'rounded-md border px-2 py-1 text-[11px] transition-colors',
            directionFilter === k
              ? k === 'long'
                ? 'border-good/40 bg-good-soft text-good'
                : k === 'short'
                  ? 'border-bad/40 bg-bad-soft text-bad'
                  : 'border-primary/50 bg-primary-soft text-primary'
              : 'border-border bg-surface-2 text-muted hover:text-text'
          ].join(' ')}
        >{label}</button>
      {/each}
    </div>
    <div class="flex items-center gap-1">
      <span class="mr-2 text-[10px] font-semibold uppercase tracking-wider text-faint">
        Conv ≥
      </span>
      {#each [0, 3, 4, 5] as c (c)}
        <button
          onclick={() => (convictionMin = c)}
          class={[
            'rounded-md border px-2 py-1 text-[11px] transition-colors',
            convictionMin === c
              ? 'border-primary/50 bg-primary-soft text-primary'
              : 'border-border bg-surface-2 text-muted hover:text-text'
          ].join(' ')}
        >{c === 0 ? 'any' : c + '+'}</button>
      {/each}
    </div>
    <input
      type="text"
      bind:value={tickerFilter}
      placeholder="$ticker"
      class="w-24 rounded-md border border-border bg-surface-2 px-2 py-1 font-mono text-[12px] text-text placeholder:text-faint/50 focus:border-primary/60 focus:outline-none"
    />

    <div class="flex items-center gap-1">
      <span class="mr-2 text-[10px] font-semibold uppercase tracking-wider text-faint">
        Sort
      </span>
      {#each [
        ['date', 'Newest'],
        ['conv', 'Conv'],
        ['ret_1d', '1d ↓'],
        ['ret_5d', '5d ↓'],
        ['ret_20d', '20d ↓']
      ] as [k, label] (k)}
        <button
          onclick={() => (sortKey = k as SortKey)}
          class={[
            'rounded-md border px-2 py-1 text-[11px] transition-colors',
            sortKey === k
              ? 'border-primary/50 bg-primary-soft text-primary'
              : 'border-border bg-surface-2 text-muted hover:text-text'
          ].join(' ')}
        >{label}</button>
      {/each}
    </div>

    <span class="ml-auto text-[11px] tabular text-faint">
      {filtered.length} of {$callsQ.data?.length ?? 0}
    </span>
  </div>
</Card>

<div class="mt-3">
  {#if $callsQ.isLoading}
    <div class="flex justify-center py-12"><Spinner /></div>
  {:else if !filtered.length}
    <EmptyState
      title="No matching calls"
      description={$callsQ.data?.length ? 'Try widening the time window or clearing filters.' : 'The bot produces calls from filings, news and synthesis cycles.'}
    />
  {:else}
    <Pager bind:page bind:pageSize total={filtered.length} class="mb-2" />
    <div class="grid grid-cols-1 gap-2.5 md:grid-cols-2 xl:grid-cols-3">
      {#each paged as c (c.id)}
        <Card interactive onclick={() => (selected = c.id)} class="px-4 py-3">
          <div class="flex items-center gap-1.5">
            <Pill variant={c.direction === 'long' ? 'pos' : 'neg'}>
              {c.direction.toUpperCase()}
            </Pill>
            <TickerLink ticker={c.ticker} class="text-[12.5px]" />
            <Pill variant={c.conviction >= 4 ? 'pos' : c.conviction <= 2 ? 'neutral' : 'info'}>
              {c.conviction}/5
            </Pill>
            <span class="ml-auto text-[10px] tabular text-faint">{timeAgo(c.ts)}</span>
          </div>
          {#if c.thesis}
            <div class="mt-2 line-clamp-3 text-[12.5px] leading-snug text-text">{stripMd(c.thesis)}</div>
          {/if}
          <div class="mt-2 flex items-center gap-x-3 gap-y-1 text-[10.5px] tabular text-faint">
            <span class="font-medium">{c.source}</span>
            {#if c.price_at_call}
              <span>@ {price(c.price_at_call)}</span>
            {/if}
          </div>
          {#if c.ret_1d_pct !== null || c.ret_5d_pct !== null || c.ret_20d_pct !== null}
            <div class="mt-2 flex gap-3 border-t border-border-soft pt-2 text-[11px]">
              <Delta value={c.ret_1d_pct} label="1d" />
              <Delta value={c.ret_5d_pct} label="5d" />
              <Delta value={c.ret_20d_pct} label="20d" />
              {#if c.settled}
                <span class="ml-auto rounded bg-surface-2 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-faint">
                  scored
                </span>
              {/if}
            </div>
          {/if}
        </Card>
      {/each}
    </div>
    <Pager bind:page bind:pageSize total={filtered.length} class="mt-3" />
  {/if}
</div>

<Drawer
  open={selected !== null}
  onClose={() => (selected = null)}
  class="max-w-2xl"
>
  {#snippet header()}
    {#if selectedItem}
      <div class="flex flex-1 items-baseline gap-1.5">
        <Pill variant={selectedItem.direction === 'long' ? 'pos' : 'neg'}>
          {selectedItem.direction.toUpperCase()}
        </Pill>
        <TickerLink ticker={selectedItem.ticker} class="text-sm font-bold" />
        <Pill variant={selectedItem.conviction >= 4 ? 'pos' : 'info'}>
          conv {selectedItem.conviction}/5
        </Pill>
        <span class="text-[11px] text-faint">·</span>
        <span class="text-[11px] text-muted">{selectedItem.source}</span>
      </div>
    {/if}
  {/snippet}

  {#if selectedItem}
    {#if selectedItem.thesis}
      <div class="mb-3 rounded-lg border border-border bg-surface-2 px-3 py-2">
        <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">Thesis</div>
        <Markdown source={selectedItem.thesis} class="mt-1" />
        <div class="mt-2 flex flex-wrap gap-x-3 text-[10.5px] tabular text-faint">
          {#if selectedItem.price_at_call}<span>price @ call: {price(selectedItem.price_at_call)}</span>{/if}
          <span>filed {timeAgo(selectedItem.ts)} ago</span>
        </div>
        {#if selectedItem.ret_1d_pct !== null || selectedItem.ret_5d_pct !== null || selectedItem.ret_20d_pct !== null}
          <div class="mt-2 flex gap-3 border-t border-border-soft pt-2 text-[11.5px]">
            <Delta value={selectedItem.ret_1d_pct} label="1d" />
            <Delta value={selectedItem.ret_5d_pct} label="5d" />
            <Delta value={selectedItem.ret_20d_pct} label="20d" />
          </div>
        {/if}
      </div>
    {/if}

    <DossierBlock
      body={$dossierQ.data?.body}
      meta={$dossierQ.data?.meta}
      isLoading={$dossierQ.isLoading || $dossierQ.isFetching}
      onRefresh={regenerate}
      {refreshing}
    />

    <div class="mt-5">
      <AskBox
        placeholder="Ask a follow-up about this call…"
        onAsk={askAboutSelected}
      />
    </div>
  {/if}
</Drawer>
