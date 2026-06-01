<script lang="ts">
  import { createQuery, useQueryClient } from '@tanstack/svelte-query';
  import { reactiveQueryOptions } from '$lib/reactive-query.svelte';
  import { news, newsDossier, askNews, newsArticle, filings, socialRecent, socialTopTickers } from '$api';
  import Card from '$components/Card.svelte';
  import Pill from '$components/Pill.svelte';
  import Delta from '$components/Delta.svelte';
  import Drawer from '$components/Drawer.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import Spinner from '$components/Spinner.svelte';
  import DossierBlock from '$components/DossierBlock.svelte';
  import AskBox from '$components/AskBox.svelte';
  import TickerLink from '$components/TickerLink.svelte';
  import Markdown from '$components/Markdown.svelte';
  import Pager from '$components/Pager.svelte';
  import { timeAgo, compact, stripMd } from '$lib/format';
  import { Newspaper, ExternalLink, Globe, FileText, MessageCircle } from 'lucide-svelte';

  type Hours = { label: string; value: number };
  const RANGES: Hours[] = [
    { label: '6h', value: 6 },
    { label: '24h', value: 24 },
    { label: '3d', value: 72 },
    { label: '7d', value: 168 }
  ];

  type Mode = 'news' | 'filings' | 'social';
  let mode: Mode = $state('news');
  // 72h default so a freshly-opened tab on a quiet day always shows
  // something — 24h was too narrow when the bot is bursty.
  let hours = $state(72);
  let tickerFilter = $state('');
  let sentimentFilter: 'all' | 'pos' | 'neg' | 'macro' = $state('all');
  let formFilter: string = $state('all');
  let materialityMin = $state(0);
  let textFilter = $state('');
  let selectedNewsId = $state<number | null>(null);
  let selectedFiling = $state<number | null>(null);
  let refreshing = $state(false);
  let dedupeNews = $state(true); // collapse syndicated copies of the same event

  // Pagination — one set of state shared across mode tabs (reset on
  // any meaningful axis change).
  let page = $state(1);
  let pageSize = $state(25);
  $effect(() => {
    mode; hours; tickerFilter; sentimentFilter; formFilter; materialityMin; textFilter;
    page = 1;
  });

  /* ── news queries ────────────────────────── */
  const newsQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['news', hours, tickerFilter, dedupeNews],
    queryFn: () => news(hours, tickerFilter.trim() || undefined, { dedupe: dedupeNews }),
    refetchInterval: mode === 'news' ? 60_000 : false,
    enabled: mode === 'news'
  })));

  const dossierQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['news-dossier', selectedNewsId, refreshing],
    queryFn: () => newsDossier(selectedNewsId!, refreshing),
    enabled: selectedNewsId !== null
  })));

  const articleQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['news-article', selectedNewsId],
    queryFn: () => newsArticle(selectedNewsId!),
    enabled: selectedNewsId !== null,
    staleTime: 60 * 60_000 // article body never changes once cached
  })));

  let articleExpanded = $state(false);
  $effect(() => {
    // Reset collapse state when we switch articles.
    selectedNewsId;
    articleExpanded = false;
  });

  /* ── filings queries ─────────────────────── */
  const filingsQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['filings', hours, tickerFilter, formFilter, materialityMin],
    queryFn: () =>
      filings({
        hours: hours * 2, // filings are sparser than news; widen the window
        ticker: tickerFilter.trim() || undefined,
        form: formFilter !== 'all' ? formFilter : undefined,
        min_materiality: materialityMin
      }),
    refetchInterval: mode === 'filings' ? 60_000 : false,
    enabled: mode === 'filings'
  })));

  /* ── social queries ─────────────────────── */
  const socialQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['social', hours, tickerFilter],
    queryFn: () => socialRecent(hours, tickerFilter.trim() || undefined),
    refetchInterval: mode === 'social' ? 90_000 : false,
    enabled: mode === 'social'
  })));
  const topTickersQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['social-top', hours],
    queryFn: () => socialTopTickers(hours, 10),
    refetchInterval: mode === 'social' ? 5 * 60_000 : false,
    enabled: mode === 'social'
  })));

  function variantForSentimentSimple(s: number | null): 'pos' | 'neg' | 'info' {
    if (s === null || s === undefined) return 'info';
    if (s > 0.15) return 'pos';
    if (s < -0.15) return 'neg';
    return 'info';
  }

  const qc = useQueryClient();

  async function regenerate() {
    if (selectedNewsId === null) return;
    refreshing = true;
    try {
      await newsDossier(selectedNewsId, true);
      await qc.invalidateQueries({ queryKey: ['news-dossier', selectedNewsId] });
    } finally {
      refreshing = false;
    }
  }

  async function askAboutNews(q: string): Promise<string> {
    if (selectedNewsId === null) throw new Error('no item selected');
    const r = await askNews(selectedNewsId, q);
    return r.answer;
  }

  /* ── derived: filtered news ─── */
  const filteredNews = $derived(
    ($newsQ.data ?? []).filter((n) => {
      if (sentimentFilter === 'pos' && !((n.sentiment ?? 0) > 0.15)) return false;
      if (sentimentFilter === 'neg' && !((n.sentiment ?? 0) < -0.15)) return false;
      if (sentimentFilter === 'macro' && !n.is_macro) return false;
      const t = textFilter.trim().toLowerCase();
      if (t && !n.title.toLowerCase().includes(t)) return false;
      return true;
    })
  );

  /* ── derived: filtered filings ─── */
  const filteredFilings = $derived(
    ($filingsQ.data ?? []).filter((f) => {
      const t = textFilter.trim().toLowerCase();
      if (
        t &&
        !(f.summary ?? '').toLowerCase().includes(t) &&
        !f.form_type.toLowerCase().includes(t) &&
        !(f.ticker ?? '').toLowerCase().includes(t)
      )
        return false;
      return true;
    })
  );

  const selectedNewsItem = $derived(
    selectedNewsId !== null
      ? ($newsQ.data ?? []).find((n) => n.id === selectedNewsId)
      : null
  );

  const selectedFilingItem = $derived(
    selectedFiling !== null
      ? ($filingsQ.data ?? []).find((f) => f.id === selectedFiling)
      : null
  );

  function variantForSentiment(s: number | null): 'pos' | 'neg' | 'info' {
    if (s === null || s === undefined) return 'info';
    if (s > 0.15) return 'pos';
    if (s < -0.15) return 'neg';
    return 'info';
  }

  function materialityVariant(m: number | null): 'neg' | 'warn' | 'info' | 'neutral' {
    if (m === null || m === undefined) return 'neutral';
    if (m >= 7) return 'neg';
    if (m >= 4) return 'warn';
    return 'info';
  }

  // Common form types — populated from the data, but always include
  // the well-known set so the chips stay stable across refreshes.
  const KNOWN_FORMS = ['8-K', '10-Q', '10-K', '13D', '13G', '13F', '4', 'S-1'];
</script>

<svelte:head><title>Intel · Sentinel</title></svelte:head>

<!-- Header — caption was a feature description (sentiment, impact,
     dossiers, materiality scores). Once the tab is selected the
     feed below speaks for itself; the caption was just adding a
     paragraph of text between the title and the working surface.
     Icon now follows the active mode so the page still feels
     contextual. -->
<div class="mb-4 flex items-end justify-between gap-3 border-b border-border pb-3">
  <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
    {#if mode === 'news'}
      <Newspaper class="h-5 w-5 text-primary" />
    {:else if mode === 'filings'}
      <FileText class="h-5 w-5 text-violet" />
    {:else}
      <MessageCircle class="h-5 w-5 text-warn" />
    {/if}
    <span>Intel</span>
  </h1>

  <div class="flex rounded-md border border-border bg-surface p-0.5">
    <button
      onclick={() => (mode = 'news')}
      class={[
        'flex items-center gap-1.5 rounded-sm px-3 py-1.5 text-[12px] transition-colors',
        mode === 'news'
          ? 'bg-white/[0.09] font-medium text-text'
          : 'text-muted hover:text-text'
      ].join(' ')}
    >
      <Newspaper class="h-3.5 w-3.5" />
      News
      {#if $newsQ.data?.length}
        <span class="rounded bg-surface-2 px-1.5 py-px text-[9px] text-faint">
          {$newsQ.data.length}
        </span>
      {/if}
    </button>
    <button
      onclick={() => (mode = 'filings')}
      class={[
        'flex items-center gap-1.5 rounded-sm px-3 py-1.5 text-[12px] transition-colors',
        mode === 'filings'
          ? 'bg-white/[0.09] font-medium text-text'
          : 'text-muted hover:text-text'
      ].join(' ')}
    >
      <FileText class="h-3.5 w-3.5" />
      Filings
      {#if $filingsQ.data?.length}
        <span class="rounded bg-surface-2 px-1.5 py-px text-[9px] text-faint">
          {$filingsQ.data.length}
        </span>
      {/if}
    </button>
    <button
      onclick={() => (mode = 'social')}
      class={[
        'flex items-center gap-1.5 rounded-sm px-3 py-1.5 text-[12px] transition-colors',
        mode === 'social'
          ? 'bg-white/[0.09] font-medium text-text'
          : 'text-muted hover:text-text'
      ].join(' ')}
    >
      <MessageCircle class="h-3.5 w-3.5" />
      Reddit
      {#if $socialQ.data?.length}
        <span class="rounded bg-surface-2 px-1.5 py-px text-[9px] text-faint">
          {$socialQ.data.length}
        </span>
      {/if}
    </button>
  </div>
</div>

<!-- ── filter ribbon ───────────────────────────────────────────
     Was: four eyebrow labels ("Window", "Mood", "Form", "Mat ≥") on
     a row of chips — by far the loudest typography on the row, and
     all four are inferable from the chip values (24h vs 7d vs 30d
     are obviously time windows). Now: chip groups separated by a
     thin divider, no labels. Filter inputs are at the right, count
     anchored far right. -->
<Card class="px-3 py-2">
  <div class="flex flex-wrap items-center gap-x-1.5 gap-y-1.5">
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

    <span class="mx-1 h-5 w-px bg-border"></span>

    {#if mode === 'news'}
      {#each [['all', 'All'], ['pos', '↑'], ['neg', '↓'], ['macro', 'Macro']] as [key, label] (key)}
        <button
          onclick={() => (sentimentFilter = key as any)}
          title={key === 'pos' ? 'Bullish only' : key === 'neg' ? 'Bearish only' : key === 'macro' ? 'Macro only' : 'All sentiments'}
          class={[
            'min-w-[28px] rounded-md border px-2 py-1 text-[11px] transition-colors',
            sentimentFilter === key
              ? key === 'pos' ? 'border-good/40 bg-good-soft text-good'
              : key === 'neg' ? 'border-bad/40 bg-bad-soft text-bad'
              : key === 'macro' ? 'border-violet/40 bg-violet-soft text-violet'
              : 'border-primary/50 bg-primary-soft text-primary'
              : 'border-border bg-surface-2 text-muted hover:text-text'
          ].join(' ')}
        >{label}</button>
      {/each}
      <label class="flex cursor-pointer items-center gap-1 rounded-md border border-border bg-surface-2 px-2 py-1 text-[11px]" title="Collapse near-duplicate stories">
        <input type="checkbox" bind:checked={dedupeNews} class="h-3 w-3 cursor-pointer accent-primary" />
        <span class={dedupeNews ? 'text-text' : 'text-muted'}>dedupe</span>
      </label>
    {:else if mode === 'filings'}
      <button
        onclick={() => (formFilter = 'all')}
        class={[
          'rounded-md border px-2 py-1 text-[11px] transition-colors',
          formFilter === 'all' ? 'border-primary/50 bg-primary-soft text-primary' : 'border-border bg-surface-2 text-muted hover:text-text'
        ].join(' ')}
      >All</button>
      {#each KNOWN_FORMS as f (f)}
        <button
          onclick={() => (formFilter = f)}
          class={[
            'rounded-md border px-2 py-1 font-mono text-[11px] transition-colors',
            formFilter === f ? 'border-violet/40 bg-violet-soft text-violet' : 'border-border bg-surface-2 text-muted hover:text-text'
          ].join(' ')}
        >{f}</button>
      {/each}
      <span class="mx-1 h-5 w-px bg-border"></span>
      {#each [0, 4, 7] as m (m)}
        <button
          onclick={() => (materialityMin = m)}
          title={m === 0 ? 'Any materiality' : `Materiality ≥ ${m}`}
          class={[
            'rounded-md border px-2 py-1 text-[11px] transition-colors',
            materialityMin === m ? 'border-primary/50 bg-primary-soft text-primary' : 'border-border bg-surface-2 text-muted hover:text-text'
          ].join(' ')}
        >{m === 0 ? 'mat·any' : `mat≥${m}`}</button>
      {/each}
    {/if}

    <input
      type="text"
      bind:value={tickerFilter}
      placeholder="$ticker"
      class="w-24 rounded-md border border-border bg-surface-2 px-2 py-1 font-mono text-[11.5px] text-text placeholder:text-faint/50 focus:border-primary/60 focus:outline-none"
    />
    <input
      type="text"
      bind:value={textFilter}
      placeholder={mode === 'news' ? 'Title filter…' : mode === 'filings' ? 'Summary filter…' : 'Title filter…'}
      class="w-48 rounded-md border border-border bg-surface-2 px-2 py-1 text-[11.5px] text-text placeholder:text-faint/50 focus:border-primary/60 focus:outline-none"
    />

    <span class="ml-auto text-[11px] tabular text-faint">
      {#if mode === 'news'}
        {filteredNews.length} of {$newsQ.data?.length ?? 0}
      {:else if mode === 'social'}
        {$socialQ.data?.length ?? 0} mentions
      {:else}
        {filteredFilings.length} of {$filingsQ.data?.length ?? 0}
      {/if}
    </span>
  </div>
</Card>

<!-- ── feed ────────────────────────────────────────────────── -->
{#if mode === 'news'}
  <div class="mt-3">
    {#if $newsQ.isLoading}
      <div class="flex justify-center py-12"><Spinner /></div>
    {:else if !filteredNews.length}
      <EmptyState
        icon={Newspaper}
        title="No matching news"
        description={$newsQ.data?.length ? 'Try widening the time window or clearing filters.' : 'Ingesters run every 5min. New items will appear here.'}
      />
    {:else}
      <Pager bind:page bind:pageSize total={filteredNews.length} class="mb-2" />
      <div class="grid grid-cols-1 gap-2.5 md:grid-cols-2 xl:grid-cols-3">
        {#each filteredNews.slice((page - 1) * pageSize, page * pageSize) as n (n.id)}
          <Card interactive onclick={() => (selectedNewsId = n.id)} class="px-4 py-3">
            <div class="flex items-center gap-1.5">
              <Pill variant={variantForSentiment(n.sentiment)}>
                {#if n.sentiment !== null && n.sentiment !== undefined}
                  {n.sentiment > 0.15 ? '↑' : n.sentiment < -0.15 ? '↓' : '·'}
                  {Math.abs(n.sentiment).toFixed(2)}
                {:else}
                  NEWS
                {/if}
              </Pill>
              {#if n.tickers && n.tickers.length > 0}
                {#each n.tickers.slice(0, 3) as t (t)}
                  <TickerLink ticker={t} class="text-[12px]" />
                {/each}
                {#if n.tickers.length > 3}
                  <span class="text-[10px] text-faint">+{n.tickers.length - 3}</span>
                {/if}
              {:else if n.ticker}
                <TickerLink ticker={n.ticker} class="text-[12px]" />
              {/if}
              {#if n.is_macro}
                <Pill variant="violet"><Globe class="h-2.5 w-2.5" /> macro</Pill>
              {/if}
              {#if (n.cluster_size ?? 1) > 1}
                <Pill
                  variant="warn"
                  class="!normal-case"
                >+{(n.cluster_size ?? 1) - 1} dupes</Pill>
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
      <Pager bind:page bind:pageSize total={filteredNews.length} class="mt-3" />
    {/if}
  </div>
{:else if mode === 'filings'}
  <div class="mt-3">
    {#if $filingsQ.isLoading}
      <div class="flex justify-center py-12"><Spinner /></div>
    {:else if !filteredFilings.length}
      <EmptyState
        icon={FileText}
        title="No matching filings"
        description={$filingsQ.data?.length ? 'Try widening the time window, lowering materiality, or clearing filters.' : 'EDGAR poll runs every 10min. New filings will appear here.'}
      />
    {:else}
      <Pager bind:page bind:pageSize total={filteredFilings.length} class="mb-2" />
      <div class="grid grid-cols-1 gap-2.5 md:grid-cols-2 xl:grid-cols-3">
        {#each filteredFilings.slice((page - 1) * pageSize, page * pageSize) as f (f.id)}
          <Card interactive onclick={() => (selectedFiling = f.id)} class="px-4 py-3">
            <div class="flex items-center gap-1.5">
              <Pill variant="violet" class="font-mono">{f.form_type}</Pill>
              {#if f.ticker}
                <TickerLink ticker={f.ticker} class="text-[12px]" />
              {/if}
              {#if f.materiality_score !== null}
                <Pill variant={materialityVariant(f.materiality_score)}>
                  mat {f.materiality_score}/10
                </Pill>
              {/if}
              <span class="ml-auto text-[10px] tabular text-faint">
                {timeAgo(f.filed_at)}
              </span>
            </div>
            {#if f.summary}
              <div class="mt-2 line-clamp-3 text-[12.5px] leading-snug text-muted">
                {stripMd(f.summary)}
              </div>
            {/if}
            <div class="mt-2 text-[10.5px] tabular text-faint">
              cik {f.cik} · {f.accession_number}
            </div>
          </Card>
        {/each}
      </div>
      <Pager bind:page bind:pageSize total={filteredFilings.length} class="mt-3" />
    {/if}
  </div>
{:else}
  <!-- ── social mode ──────────────────────────── -->
  <div class="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-[1fr_15rem]">
    <div>
      {#if $socialQ.isLoading}
        <div class="flex justify-center py-12"><Spinner /></div>
      {:else if !$socialQ.data?.length}
        <EmptyState
          icon={MessageCircle}
          title="No Reddit mentions in window"
          description="The reddit poll runs every 30min across the tracked subreddits."
        />
      {:else}
        <Pager bind:page bind:pageSize total={$socialQ.data.length} class="mb-2" />
        <div class="space-y-2">
          {#each $socialQ.data.slice((page - 1) * pageSize, page * pageSize) as r (r.id)}
            <Card class="px-3.5 py-2.5">
              <div class="flex items-center gap-1.5">
                <Pill variant={variantForSentimentSimple(r.sentiment)}>
                  r/{r.subreddit}
                </Pill>
                {#if r.ticker}
                  <TickerLink ticker={r.ticker} class="text-[12px]" />
                {/if}
                <span class="text-[10.5px] tabular text-faint">↑ {compact(r.score)}</span>
                <span class="text-[10.5px] tabular text-faint">💬 {r.num_comments}</span>
                <span class="ml-auto text-[10px] tabular text-faint">{timeAgo(r.ts)}</span>
              </div>
              <a
                href={`https://www.reddit.com${r.permalink}`}
                target="_blank"
                rel="noopener"
                class="mt-1 block text-[12.5px] leading-snug text-text hover:text-primary"
              >
                {r.title}
                <ExternalLink class="ml-1 inline h-3 w-3 align-baseline opacity-60" />
              </a>
              <div class="mt-1 text-[10.5px] text-faint">u/{r.author}</div>
            </Card>
          {/each}
        </div>
        <Pager bind:page bind:pageSize total={$socialQ.data.length} class="mt-3" />
      {/if}
    </div>

    <aside>
      <div class="rounded-xl border border-border bg-surface px-3.5 py-3">
        <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
          Top tickers (window)
        </div>
        {#if !$topTickersQ.data?.length}
          <div class="mt-2 text-[11.5px] text-faint">No mentions yet.</div>
        {:else}
          <ul class="mt-2 space-y-1">
            {#each $topTickersQ.data as t (t.ticker)}
              <li class="flex items-center gap-2 text-[12px] tabular">
                <TickerLink ticker={t.ticker} class="text-[12px] flex-1" />
                <span class="text-muted">{t.mentions}</span>
                <span class={[
                  'w-12 text-right',
                  t.sentiment_avg > 0.15 ? 'text-good'
                    : t.sentiment_avg < -0.15 ? 'text-bad' : 'text-faint'
                ].join(' ')}>
                  {t.sentiment_avg > 0 ? '+' : ''}{t.sentiment_avg.toFixed(2)}
                </span>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    </aside>
  </div>
{/if}

<!-- ── news drawer ────────────────────────────────────────── -->
<Drawer
  open={selectedNewsId !== null}
  onClose={() => (selectedNewsId = null)}
  class="max-w-2xl"
>
  {#snippet header()}
    {#if selectedNewsItem}
      <div class="min-w-0 flex-1">
        <div class="flex items-center gap-1.5">
          <Pill variant={variantForSentiment(selectedNewsItem.sentiment)}>
            {selectedNewsItem.sentiment !== null
              ? (selectedNewsItem.sentiment > 0.15 ? '↑ bullish' : selectedNewsItem.sentiment < -0.15 ? '↓ bearish' : '· neutral')
              : 'neutral'}
          </Pill>
          {#if selectedNewsItem.tickers && selectedNewsItem.tickers.length > 0}
            {#each selectedNewsItem.tickers as t, i (t)}
              <TickerLink ticker={t} class={i === 0 ? 'text-sm font-bold' : 'text-sm'} />
            {/each}
          {:else if selectedNewsItem.ticker}
            <TickerLink ticker={selectedNewsItem.ticker} class="text-sm font-bold" />
          {/if}
          <span class="text-[11px] text-faint">·</span>
          <span class="text-[11px] text-faint">{selectedNewsItem.source}</span>
          {#if selectedNewsItem.impact_1d_pct !== null}
            <span class="text-[11px] text-faint">·</span>
            <Delta value={selectedNewsItem.impact_1d_pct} label="1d" />
          {/if}
          {#if selectedNewsItem.url}
            <a
              href={selectedNewsItem.url}
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

  {#if selectedNewsItem}
    <div class="mb-3">
      <div class="text-[15px] font-semibold leading-snug text-text">{selectedNewsItem.title}</div>
      {#if selectedNewsItem.summary}
        <div class="mt-1 text-[12px] text-muted">{selectedNewsItem.summary}</div>
      {/if}
    </div>

    <!-- Original article body (cached). Collapsed by default since
         these can be 2-6k chars; expand to read inline. -->
    {#if $articleQ.data?.body}
      {@const art = $articleQ.data}
      <div class="mb-4 rounded-lg border border-border-soft bg-surface-2/40">
        <button
          type="button"
          onclick={() => (articleExpanded = !articleExpanded)}
          class="flex w-full items-center gap-2 px-3 py-2 text-left text-[11px] text-muted transition-colors hover:text-text"
        >
          <span class="text-[10px] font-semibold uppercase tracking-wider text-faint">
            Original article
          </span>
          <span class="tabular text-[10px] text-faint">
            {art.char_count} chars · {art.source}
          </span>
          <span class="ml-auto text-[10px] text-primary">
            {articleExpanded ? '▼ hide' : '▶ show'}
          </span>
        </button>
        {#if articleExpanded}
          <div class="border-t border-border-soft px-4 py-3 text-[12.5px] leading-relaxed text-muted whitespace-pre-wrap">
            {art.body}
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
        placeholder="Ask a follow-up about this story…"
        onAsk={askAboutNews}
      />
    </div>
  {/if}
</Drawer>

<!-- ── filing drawer ──────────────────────────────────────── -->
<Drawer
  open={selectedFiling !== null}
  onClose={() => (selectedFiling = null)}
  class="max-w-2xl"
>
  {#snippet header()}
    {#if selectedFilingItem}
      <div class="flex flex-1 items-baseline gap-1.5">
        <Pill variant="violet" class="font-mono">{selectedFilingItem.form_type}</Pill>
        {#if selectedFilingItem.ticker}
          <TickerLink ticker={selectedFilingItem.ticker} class="text-sm font-bold" />
        {/if}
        {#if selectedFilingItem.materiality_score !== null}
          <Pill variant={materialityVariant(selectedFilingItem.materiality_score)}>
            mat {selectedFilingItem.materiality_score}/10
          </Pill>
        {/if}
        <span class="text-[11px] text-faint">·</span>
        <span class="font-mono text-[10.5px] text-faint">{selectedFilingItem.accession_number}</span>
        <a
          href={selectedFilingItem.primary_doc_url}
          target="_blank"
          rel="noopener"
          class="ml-2 inline-flex items-center gap-1 rounded border border-border bg-surface-2 px-2 py-0.5 text-[10.5px] text-muted transition-colors hover:border-primary/40 hover:text-text"
          onclick={(e) => e.stopPropagation()}
        ><ExternalLink class="h-3 w-3" />EDGAR</a>
      </div>
    {/if}
  {/snippet}

  {#if selectedFilingItem}
    <div class="mb-2 text-[10.5px] tabular text-faint">
      filed {selectedFilingItem.filed_at.slice(0, 19).replace('T', ' ')}Z · cik {selectedFilingItem.cik}
    </div>
    {#if selectedFilingItem.summary}
      <div class="rounded-lg border border-border bg-surface-2 px-4 py-3">
        <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">Summary</div>
        <Markdown source={selectedFilingItem.summary} class="mt-1.5" />
      </div>
    {/if}
    {#if selectedFilingItem.materiality_reason}
      <div class="mt-3 rounded-lg border border-warn/30 bg-warn-soft px-3 py-2">
        <div class="text-[10px] font-semibold uppercase tracking-wider text-warn">
          Why it might matter
        </div>
        <Markdown source={selectedFilingItem.materiality_reason} class="mt-1" />
      </div>
    {/if}
  {/if}
</Drawer>
