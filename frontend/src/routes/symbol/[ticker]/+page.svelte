<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { reactiveQueryOptions } from '$lib/reactive-query.svelte';
  import { page } from '$app/state';
  import { base } from '$app/paths';
  import { symbolProfile, tickerChart } from '$api';
  import Card from '$components/Card.svelte';
  import Pill from '$components/Pill.svelte';
  import Delta from '$components/Delta.svelte';
  import Spinner from '$components/Spinner.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import CandleChart from '$components/CandleChart.svelte';
  import Markdown from '$components/Markdown.svelte';
  import {
    ArrowLeft, ExternalLink, Newspaper, FileText, Target as TargetIcon,
    Brain, MessageCircle, Activity as ActivityIcon, LineChart
  } from 'lucide-svelte';
  import { price, compact, timeAgo, pct } from '$lib/format';

  const ticker = $derived(page.params.ticker?.toUpperCase() ?? '');

  type Range = { label: string; days: number | null };
  const RANGES: Range[] = [
    { label: '1m', days: 30 },
    { label: '3m', days: 90 },
    { label: '1y', days: 365 },
    { label: 'All', days: null }
  ];
  let chartRange: Range = $state(RANGES[1]);

  const profileQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['symbol-profile', ticker],
    queryFn: () => symbolProfile(ticker, 90),
    refetchInterval: 60_000,
    enabled: !!ticker
  })));
  const chartQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['symbol-chart', ticker, chartRange.days],
    queryFn: () => tickerChart(ticker, chartRange.days),
    enabled: !!ticker
  })));

  type Tab = 'news' | 'calls' | 'theses' | 'filings' | 'reddit';
  let tab: Tab = $state('news');

  // Auto-pick a tab that has data on first paint.
  $effect(() => {
    const d = $profileQ.data;
    if (!d) return;
    // pick the first non-empty (only once per ticker change)
  });

  function variantForSentiment(s: number | null): 'pos' | 'neg' | 'info' {
    if (s === null || s === undefined) return 'info';
    if (s > 0.15) return 'pos';
    if (s < -0.15) return 'neg';
    return 'info';
  }

  function variantForMateriality(m: number | null): 'neg' | 'warn' | 'info' | 'neutral' {
    if (m === null || m === undefined) return 'neutral';
    if (m >= 7) return 'neg';
    if (m >= 4) return 'warn';
    return 'info';
  }
</script>

<svelte:head><title>${ticker} · Sentinel</title></svelte:head>

<!-- ── breadcrumb header ───────────────────────────────────────── -->
<div class="mb-3 flex items-center gap-2 text-[11px] text-faint">
  <a href={`${base}/markets`} class="inline-flex items-center gap-1 hover:text-text">
    <ArrowLeft class="h-3 w-3" />
    Markets
  </a>
  <span>·</span>
  <span class="text-muted">Symbol</span>
</div>

{#if $profileQ.isLoading}
  <div class="flex h-64 items-center justify-center"><Spinner /></div>
{:else if !$profileQ.data}
  <EmptyState
    title="Symbol not found"
    description={`No data for $${ticker}. Try a symbol from the Markets watchlist.`}
  />
{:else}
  {@const d = $profileQ.data}

  <!-- ── header: ticker + price hero ───────────────────────── -->
  <div class="mb-4 flex flex-wrap items-end justify-between gap-3 border-b border-border pb-3">
    <div>
      <div class="flex items-baseline gap-3">
        <h1 class="font-mono text-3xl font-bold tracking-tight text-text">${ticker}</h1>
        {#if d.asset_class}
          <Pill variant="neutral">{d.asset_class}</Pill>
        {/if}
        {#if d.in_watchlist}
          <Pill variant="info">watchlist</Pill>
        {/if}
      </div>
      <div class="mt-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        {#if d.context}
          <span class="text-[1.7rem] font-semibold tabular leading-none text-text">
            {price(d.context.last_price)}
          </span>
          <Delta value={d.context.change_1d_pct} label="1d" />
          <Delta value={d.context.change_5d_pct} label="5d" />
          {#if d.context.volume_vs_20d_avg !== null}
            <span class="text-[12px] tabular text-faint">
              vol ×{d.context.volume_vs_20d_avg.toFixed(2)}
            </span>
          {/if}
        {/if}
      </div>
      {#if d.stats && d.stats.high_52w && d.stats.low_52w && d.context?.last_price}
        {@const yPct = ((d.context.last_price - d.stats.low_52w) / (d.stats.high_52w - d.stats.low_52w)) * 100}
        <div class="mt-2 flex items-center gap-2 text-[10.5px] tabular text-faint">
          <span>{price(d.stats.low_52w)}</span>
          <div class="relative h-1.5 w-32 overflow-hidden rounded bg-surface-2">
            <div
              class="absolute top-1/2 h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-primary shadow-[0_0_6px_var(--color-primary)]"
              style:left="{Math.max(0, Math.min(100, yPct))}%"
            ></div>
          </div>
          <span>{price(d.stats.high_52w)}</span>
          <span class="ml-1">52w range</span>
        </div>
      {/if}
    </div>

    <div class="flex items-center gap-1">
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

  <!-- ── candle chart ──────────────────────────────────────── -->
  <Card class="overflow-hidden p-2">
    {#if $chartQ.isLoading}
      <div class="flex h-[420px] items-center justify-center"><Spinner /></div>
    {:else if $chartQ.data?.bars?.length}
      <CandleChart
        bars={$chartQ.data.bars}
        openPosition={$chartQ.data.open_position}
        closedTrades={$chartQ.data.closed}
        height={420}
      />
    {:else}
      <div class="flex h-[420px] items-center justify-center text-[12px] text-faint">
        No price history — likely a thin ticker.
      </div>
    {/if}
  </Card>

  <!-- ── quick stats grid ─────────────────────────────────── -->
  <div class="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
    <div class="rounded-lg border border-border bg-surface px-3 py-2">
      <div class="flex items-center gap-1.5">
        <TargetIcon class="h-3 w-3 text-good" />
        <span class="text-[10px] font-semibold uppercase tracking-wider text-faint">
          Calls
        </span>
      </div>
      <div class="mt-1 text-[18px] font-semibold leading-tight tabular text-text">
        {d.calls.length}
      </div>
      <div class="text-[10.5px] tabular text-faint">
        {d.calls.filter((c) => c.direction === 'long').length} L · {d.calls.filter((c) => c.direction === 'short').length} S
      </div>
    </div>

    <div class="rounded-lg border border-border bg-surface px-3 py-2">
      <div class="flex items-center gap-1.5">
        <Newspaper class="h-3 w-3 text-primary" />
        <span class="text-[10px] font-semibold uppercase tracking-wider text-faint">
          News
        </span>
      </div>
      <div class="mt-1 text-[18px] font-semibold leading-tight tabular text-text">
        {d.news_stats.count}
      </div>
      <div class="text-[10.5px] tabular text-faint">
        <span class="text-good">{d.news_stats.bullish} ↑</span> ·
        <span class="text-bad">{d.news_stats.bearish} ↓</span>
        {#if d.news_stats.sentiment_avg !== null}
          · {d.news_stats.sentiment_avg.toFixed(2)}
        {/if}
      </div>
    </div>

    <div class="rounded-lg border border-border bg-surface px-3 py-2">
      <div class="flex items-center gap-1.5">
        <FileText class="h-3 w-3 text-violet" />
        <span class="text-[10px] font-semibold uppercase tracking-wider text-faint">
          Filings
        </span>
      </div>
      <div class="mt-1 text-[18px] font-semibold leading-tight tabular text-text">
        {d.filings.length}
      </div>
      <div class="text-[10.5px] tabular text-faint">
        {d.filings.filter((f) => (f.materiality_score ?? 0) >= 4).length} material
      </div>
    </div>

    <div class="rounded-lg border border-border bg-surface px-3 py-2">
      <div class="flex items-center gap-1.5">
        <Brain class="h-3 w-3 text-violet" />
        <span class="text-[10px] font-semibold uppercase tracking-wider text-faint">
          Active theses
        </span>
      </div>
      <div class="mt-1 text-[18px] font-semibold leading-tight tabular text-text">
        {d.theses.length}
      </div>
      <div class="text-[10.5px] tabular text-faint">
        {d.theses.filter((t) => t.direction === 'long').length} L · {d.theses.filter((t) => t.direction === 'short').length} S
      </div>
    </div>

    <div class="rounded-lg border border-border bg-surface px-3 py-2">
      <div class="flex items-center gap-1.5">
        <MessageCircle class="h-3 w-3 text-warn" />
        <span class="text-[10px] font-semibold uppercase tracking-wider text-faint">
          Reddit
        </span>
      </div>
      <div class="mt-1 text-[18px] font-semibold leading-tight tabular text-text">
        {d.reddit_stats.count}
      </div>
      <div class="text-[10.5px] tabular text-faint">
        {compact(d.reddit_stats.score_total)} ↑ · {compact(d.reddit_stats.comments_total)} 💬
      </div>
    </div>
  </div>

  <!-- ── active theses inline (always visible — these are the bot's "live thoughts") ─ -->
  {#if d.theses.length > 0}
    <div class="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
      {#each d.theses as t (t.id)}
        <a
          href={`${base}/theses`}
          class="block rounded-xl border border-violet/30 bg-violet-soft/40 px-4 py-3 transition-colors hover:border-violet/50"
        >
          <div class="flex items-center gap-1.5">
            <Pill variant={t.direction === 'long' ? 'pos' : t.direction === 'short' ? 'neg' : 'neutral'}>
              {t.direction.toUpperCase()}
            </Pill>
            <Pill variant="violet">conv {t.conviction}/5</Pill>
            {#if t.target_price !== null}
              <span class="ml-auto text-[10.5px] tabular text-faint">→ {t.target_price.toFixed(2)}</span>
            {/if}
          </div>
          <div class="mt-2 text-[13px] font-medium leading-snug text-text">{t.title}</div>
          {#if t.invalidation_criteria}
            <div class="mt-1.5 line-clamp-2 text-[11px] text-muted">
              <span class="font-semibold text-warn">Kills it:</span>
              {t.invalidation_criteria}
            </div>
          {/if}
        </a>
      {/each}
    </div>
  {/if}

  <!-- ── tabs ────────────────────────────────────────── -->
  <div class="mt-5 flex items-center gap-1 border-b border-border">
    {#each [
      ['news', 'News', Newspaper, d.news.length],
      ['calls', 'Calls', TargetIcon, d.calls.length],
      ['filings', 'Filings', FileText, d.filings.length],
      ['reddit', 'Reddit', MessageCircle, d.reddit.length],
      ['theses', 'Theses', Brain, d.theses.length]
    ] as [k, label, Icon, count] (k)}
      <button
        type="button"
        onclick={() => (tab = k as Tab)}
        class={[
          'group flex items-center gap-1.5 border-b-2 px-3 py-2 text-[12px] transition-colors',
          tab === k
            ? 'border-primary text-text'
            : 'border-transparent text-muted hover:text-text'
        ].join(' ')}
      >
        <Icon class={['h-3.5 w-3.5', tab === k ? 'text-primary' : 'opacity-70'].join(' ')} />
        <span>{label}</span>
        {#if count > 0}
          <span class="rounded bg-surface-2 px-1.5 py-px text-[9px] tabular text-faint">{count}</span>
        {/if}
      </button>
    {/each}
  </div>

  <div class="mt-3">
    {#if tab === 'news'}
      {#if !d.news.length}
        <EmptyState title="No news for ${ticker} in the last 90 days." />
      {:else}
        <div class="space-y-2">
          {#each d.news as n (n.id)}
            <Card class="px-3.5 py-2.5">
              <div class="flex items-center gap-1.5">
                <Pill variant={variantForSentiment(n.sentiment)}>
                  {#if n.sentiment !== null}
                    {n.sentiment > 0.15 ? '↑' : n.sentiment < -0.15 ? '↓' : '·'} {Math.abs(n.sentiment).toFixed(2)}
                  {:else}—{/if}
                </Pill>
                <span class="text-[10px] text-faint">{n.source}</span>
                {#if n.impact_1d_pct !== null}
                  <Delta value={n.impact_1d_pct} label="1d" />
                {/if}
                <span class="ml-auto text-[10px] tabular text-faint">{timeAgo(n.ts)}</span>
              </div>
              <a
                href={n.url}
                target="_blank"
                rel="noopener"
                class="mt-1 block text-[13px] leading-snug text-text hover:text-primary"
              >
                {n.title}
                <ExternalLink class="ml-1 inline h-3 w-3 align-baseline opacity-60" />
              </a>
              {#if n.summary}
                <div class="mt-1 line-clamp-2 text-[11.5px] text-muted">{n.summary}</div>
              {/if}
            </Card>
          {/each}
        </div>
      {/if}

    {:else if tab === 'calls'}
      {#if !d.calls.length}
        <EmptyState title="No calls on ${ticker}." description="The bot hasn't surfaced a thesis on this name yet." />
      {:else}
        <div class="space-y-2">
          {#each d.calls as c (c.id)}
            <Card class="px-4 py-3">
              <div class="flex items-center gap-1.5">
                <Pill variant={c.direction === 'long' ? 'pos' : 'neg'}>{c.direction.toUpperCase()}</Pill>
                <Pill variant={c.conviction >= 4 ? 'pos' : c.conviction <= 2 ? 'neutral' : 'info'}>
                  {c.conviction}/5
                </Pill>
                <span class="text-[10.5px] text-faint">{c.source}</span>
                {#if c.price_at_call !== null}
                  <span class="text-[10.5px] tabular text-faint">@ {price(c.price_at_call)}</span>
                {/if}
                <span class="ml-auto text-[10px] tabular text-faint">{timeAgo(c.ts)}</span>
              </div>
              <div class="mt-1.5 text-[12.5px] leading-snug text-muted">{c.thesis}</div>
              {#if c.ret_1d_pct !== null || c.ret_5d_pct !== null || c.ret_20d_pct !== null}
                <div class="mt-2 flex gap-3 border-t border-border-soft pt-2 text-[11px]">
                  <Delta value={c.ret_1d_pct} label="1d" />
                  <Delta value={c.ret_5d_pct} label="5d" />
                  <Delta value={c.ret_20d_pct} label="20d" />
                  {#if c.settled}<span class="ml-auto text-[10px] text-faint">scored</span>{/if}
                </div>
              {/if}
            </Card>
          {/each}
        </div>
      {/if}

    {:else if tab === 'filings'}
      {#if !d.filings.length}
        <EmptyState title="No filings on ${ticker} in the last 90 days." />
      {:else}
        <div class="space-y-2">
          {#each d.filings as f (f.id)}
            <Card class="px-4 py-3">
              <div class="flex items-center gap-1.5">
                <Pill variant="violet" class="font-mono">{f.form_type}</Pill>
                {#if f.materiality_score !== null}
                  <Pill variant={variantForMateriality(f.materiality_score)}>
                    mat {f.materiality_score}/10
                  </Pill>
                {/if}
                <a
                  href={f.primary_doc_url}
                  target="_blank"
                  rel="noopener"
                  class="inline-flex items-center gap-1 rounded border border-border bg-surface-2 px-1.5 py-0.5 text-[10px] text-muted hover:border-primary/40 hover:text-text"
                >
                  <ExternalLink class="h-2.5 w-2.5" />EDGAR
                </a>
                <span class="ml-auto text-[10px] tabular text-faint">{timeAgo(f.filed_at)}</span>
              </div>
              {#if f.summary}
                <div class="mt-1.5 text-[12px] leading-snug text-muted">{f.summary}</div>
              {/if}
              {#if f.materiality_reason}
                <div class="mt-1 text-[11px] text-warn">
                  <span class="font-semibold">Why:</span> {f.materiality_reason}
                </div>
              {/if}
            </Card>
          {/each}
        </div>
      {/if}

    {:else if tab === 'reddit'}
      {#if !d.reddit.length}
        <EmptyState title={`No Reddit mentions of $${ticker} in 90d.`} />
      {:else}
        <div class="space-y-2">
          {#each d.reddit as r (r.id)}
            <Card class="px-3.5 py-2.5">
              <div class="flex items-center gap-1.5">
                <Pill variant={variantForSentiment(r.sentiment)}>r/{r.subreddit}</Pill>
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
      {/if}

    {:else if tab === 'theses'}
      {#if !d.theses.length}
        <EmptyState title="No active theses for ${ticker}." description="The bot generates theses daily; if it hasn't picked this ticker up, it's because the catalyst stack hasn't hit threshold." />
      {:else}
        <div class="space-y-3">
          {#each d.theses as t (t.id)}
            <Card class="px-4 py-3">
              <div class="flex items-center gap-1.5">
                <Pill variant={t.direction === 'long' ? 'pos' : t.direction === 'short' ? 'neg' : 'neutral'}>
                  {t.direction.toUpperCase()}
                </Pill>
                <Pill variant="violet">conv {t.conviction}/5</Pill>
                {#if t.target_price !== null}<span class="text-[10.5px] tabular text-faint">target {t.target_price.toFixed(2)}</span>{/if}
                {#if t.horizon_days !== null}<span class="text-[10.5px] tabular text-faint">{t.horizon_days}d</span>{/if}
                <span class="ml-auto text-[10px] text-faint">{t.created_at.slice(0, 10)}</span>
              </div>
              <div class="mt-1.5 text-[13.5px] font-medium text-text">{t.title}</div>
              <Markdown source={t.body} class="mt-1.5" />
              {#if t.invalidation_criteria}
                <div class="mt-2 text-[11.5px]">
                  <span class="font-semibold text-warn">Kills it:</span>
                  <span class="ml-1 text-muted">{t.invalidation_criteria}</span>
                </div>
              {/if}
            </Card>
          {/each}
        </div>
      {/if}
    {/if}
  </div>
{/if}
