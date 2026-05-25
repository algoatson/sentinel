<script lang="ts">
  import { createQuery, useQueryClient } from '@tanstack/svelte-query';
  import { news, newsDossier, askNews } from '$api';
  import Card from '$components/Card.svelte';
  import Pill from '$components/Pill.svelte';
  import Delta from '$components/Delta.svelte';
  import Drawer from '$components/Drawer.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import Spinner from '$components/Spinner.svelte';
  import DossierBlock from '$components/DossierBlock.svelte';
  import AskBox from '$components/AskBox.svelte';
  import TickerLink from '$components/TickerLink.svelte';
  import { timeAgo } from '$lib/format';
  import { Newspaper, ExternalLink, Globe } from 'lucide-svelte';

  type Hours = { label: string; value: number };
  const RANGES: Hours[] = [
    { label: '6h', value: 6 },
    { label: '24h', value: 24 },
    { label: '3d', value: 72 },
    { label: '7d', value: 168 }
  ];

  let hours = $state(24);
  let tickerFilter = $state('');
  let sentimentFilter: 'all' | 'pos' | 'neg' | 'macro' = $state('all');
  let textFilter = $state('');
  let selected = $state<number | null>(null);
  let refreshing = $state(false);

  const newsQ = createQuery(() => ({
    queryKey: ['news', hours, tickerFilter],
    queryFn: () => news(hours, tickerFilter.trim() || undefined),
    refetchInterval: 60_000
  }));

  const dossierQ = createQuery(() => ({
    queryKey: ['news-dossier', selected, refreshing],
    queryFn: () => newsDossier(selected!, refreshing),
    enabled: selected !== null
  }));

  const qc = useQueryClient();

  async function regenerate() {
    if (selected === null) return;
    refreshing = true;
    try {
      await newsDossier(selected, true);
      await qc.invalidateQueries({ queryKey: ['news-dossier', selected] });
    } finally {
      refreshing = false;
    }
  }

  async function askAboutSelected(q: string): Promise<string> {
    if (selected === null) throw new Error('no item selected');
    const r = await askNews(selected, q);
    return r.answer;
  }

  const filtered = $derived(
    ($newsQ.data ?? []).filter((n) => {
      if (sentimentFilter === 'pos' && !((n.sentiment ?? 0) > 0.15)) return false;
      if (sentimentFilter === 'neg' && !((n.sentiment ?? 0) < -0.15)) return false;
      if (sentimentFilter === 'macro' && !n.is_macro) return false;
      const t = textFilter.trim().toLowerCase();
      if (t && !n.title.toLowerCase().includes(t)) return false;
      return true;
    })
  );

  const selectedItem = $derived(
    selected !== null
      ? ($newsQ.data ?? []).find((n) => n.id === selected)
      : null
  );

  function variantForSentiment(s: number | null): 'pos' | 'neg' | 'info' {
    if (s === null || s === undefined) return 'info';
    if (s > 0.15) return 'pos';
    if (s < -0.15) return 'neg';
    return 'info';
  }
</script>

<svelte:head><title>Intel · Sentinel</title></svelte:head>

<div class="mb-4 flex items-end justify-between border-b border-border pb-3">
  <div>
    <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
      <Newspaper class="h-5 w-5 text-primary" /><span>Intel</span>
    </h1>
    <div class="mt-0.5 text-[11.5px] text-faint">
      Ingested news with sentiment, 1-day price impact, and on-demand LLM dossiers.
    </div>
  </div>
</div>

<Card class="px-4 py-3">
  <div class="flex flex-wrap items-center gap-3">
    <div class="flex items-center gap-1">
      <span class="mr-2 text-[10px] font-semibold uppercase tracking-wider text-faint">
        Window
      </span>
      {#each RANGES as r (r.value)}
        <button
          onclick={() => (hours = r.value)}
          class={[
            'rounded-md border px-2 py-1 text-[11px] transition-colors',
            hours === r.value
              ? 'border-primary/50 bg-primary-soft text-primary'
              : 'border-border bg-surface-2 text-muted hover:text-text'
          ].join(' ')}
        >{r.label}</button>
      {/each}
    </div>

    <div class="flex items-center gap-1">
      <span class="mr-2 text-[10px] font-semibold uppercase tracking-wider text-faint">
        Mood
      </span>
      {#each [['all', 'All'], ['pos', '↑ Bullish'], ['neg', '↓ Bearish'], ['macro', 'Macro']] as [key, label] (key)}
        <button
          onclick={() => (sentimentFilter = key as any)}
          class={[
            'rounded-md border px-2 py-1 text-[11px] transition-colors',
            sentimentFilter === key
              ? key === 'pos'
                ? 'border-good/40 bg-good-soft text-good'
                : key === 'neg'
                  ? 'border-bad/40 bg-bad-soft text-bad'
                  : key === 'macro'
                    ? 'border-violet/40 bg-violet-soft text-violet'
                    : 'border-primary/50 bg-primary-soft text-primary'
              : 'border-border bg-surface-2 text-muted hover:text-text'
          ].join(' ')}
        >{label}</button>
      {/each}
    </div>

    <input
      type="text"
      bind:value={tickerFilter}
      placeholder="$ticker"
      class="w-24 rounded-md border border-border bg-surface-2 px-2 py-1 font-mono text-[12px] text-text placeholder:text-faint/50 focus:border-primary/60 focus:outline-none"
    />
    <input
      type="text"
      bind:value={textFilter}
      placeholder="Title filter…"
      class="w-56 rounded-md border border-border bg-surface-2 px-2 py-1 text-[12px] text-text placeholder:text-faint/50 focus:border-primary/60 focus:outline-none"
    />

    <span class="ml-auto text-[11px] tabular text-faint">
      {filtered.length} of {$newsQ.data?.length ?? 0}
    </span>
  </div>
</Card>

<div class="mt-3">
  {#if $newsQ.isLoading}
    <div class="flex justify-center py-12"><Spinner /></div>
  {:else if !filtered.length}
    <EmptyState
      title="No matching news"
      description={$newsQ.data?.length ? 'Try widening the time window or clearing filters.' : 'Ingesters run every 5min. New items will appear here.'}
    />
  {:else}
    <div class="grid grid-cols-1 gap-2.5 md:grid-cols-2 xl:grid-cols-3">
      {#each filtered as n (n.id)}
        <Card interactive onclick={() => (selected = n.id)} class="px-4 py-3">
          <div class="flex items-center gap-1.5">
            <Pill variant={variantForSentiment(n.sentiment)}>
              {#if n.sentiment !== null && n.sentiment !== undefined}
                {n.sentiment > 0.15 ? '↑' : n.sentiment < -0.15 ? '↓' : '·'}
                {Math.abs(n.sentiment).toFixed(2)}
              {:else}
                NEWS
              {/if}
            </Pill>
            {#if n.ticker}
              <TickerLink ticker={n.ticker} class="text-[12px]" />
            {/if}
            {#if n.is_macro}
              <Pill variant="violet"><Globe class="h-2.5 w-2.5" /> macro</Pill>
            {/if}
            {#if n.impact_1d_pct !== null}
              <div class="ml-auto"><Delta value={n.impact_1d_pct} label="1d" /></div>
            {/if}
          </div>
          <div class="mt-1.5 line-clamp-3 text-[13px] leading-snug text-text">{n.title}</div>
          {#if n.summary}
            <div class="mt-1 line-clamp-2 text-[11.5px] text-muted">{n.summary}</div>
          {/if}
          <div class="mt-2 flex items-center gap-2 text-[10.5px] tabular text-faint">
            <span class="font-medium">{n.source}</span>
            <span>·</span>
            <span>{timeAgo(n.ts)}</span>
          </div>
        </Card>
      {/each}
    </div>
  {/if}
</div>

<Drawer
  open={selected !== null}
  onClose={() => (selected = null)}
  class="max-w-2xl"
>
  {#snippet header()}
    {#if selectedItem}
      <div class="min-w-0 flex-1">
        <div class="flex items-center gap-1.5">
          <Pill variant={variantForSentiment(selectedItem.sentiment)}>
            {selectedItem.sentiment !== null
              ? (selectedItem.sentiment > 0.15 ? '↑ bullish' : selectedItem.sentiment < -0.15 ? '↓ bearish' : '· neutral')
              : 'neutral'}
          </Pill>
          {#if selectedItem.ticker}
            <TickerLink ticker={selectedItem.ticker} class="text-sm font-bold" />
          {/if}
          <span class="text-[11px] text-faint">·</span>
          <span class="text-[11px] text-faint">{selectedItem.source}</span>
          {#if selectedItem.impact_1d_pct !== null}
            <span class="text-[11px] text-faint">·</span>
            <Delta value={selectedItem.impact_1d_pct} label="1d" />
          {/if}
          {#if selectedItem.url}
            <a
              href={selectedItem.url}
              target="_blank"
              rel="noopener"
              class="ml-2 inline-flex items-center gap-1 rounded border border-border bg-surface-2 px-2 py-0.5 text-[10.5px] text-muted transition-colors hover:border-primary/40 hover:text-text"
              onclick={(e) => e.stopPropagation()}
            ><ExternalLink class="h-3 w-3" />open</a>
          {/if}
        </div>
      </div>
    {/if}
  {/snippet}

  {#if selectedItem}
    <div class="mb-3">
      <div class="text-[15px] font-semibold leading-snug text-text">{selectedItem.title}</div>
      {#if selectedItem.summary}
        <div class="mt-1 text-[12px] text-muted">{selectedItem.summary}</div>
      {/if}
    </div>

    <DossierBlock
      body={$dossierQ.data?.body}
      meta={$dossierQ.data?.meta}
      isLoading={$dossierQ.isLoading || $dossierQ.isFetching}
      onRefresh={regenerate}
      {refreshing}
    />

    <div class="mt-5">
      <AskBox
        placeholder="Ask a follow-up about this story…"
        onAsk={askAboutSelected}
      />
    </div>
  {/if}
</Drawer>
