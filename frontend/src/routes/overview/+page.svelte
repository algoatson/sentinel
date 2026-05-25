<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { reactiveQueryOptions } from '$lib/reactive-query.svelte';
  import { kpi, activity, realizedCurve, equityCurve, calls, news, filings, catalysts, hotTickers } from '$api';
  import Card from '$components/Card.svelte';
  import Pill from '$components/Pill.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import Spinner from '$components/Spinner.svelte';
  import TickerLink from '$components/TickerLink.svelte';
  import EquityCurveChart from '$components/EquityCurveChart.svelte';
  import Sparkline from '$components/Sparkline.svelte';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { usd, compact, timeAgo, pct, tone, stripMd } from '$lib/format';
  import {
    Newspaper, FileText, Target as TargetIcon, ArrowUpRight, ArrowDownRight,
    Wallet, TrendingUp, Activity as ActivityIcon, Brain, Sparkles, Zap, Flame
  } from 'lucide-svelte';

  type Range = { label: string; days: number };
  const RANGES: Range[] = [
    { label: '7d', days: 7 },
    { label: '30d', days: 30 },
    { label: '90d', days: 90 },
    { label: '1y', days: 365 }
  ];
  let equityRange: Range = $state(RANGES[1]);

  const kpiQ = createQuery({
    queryKey: ['kpi'],
    queryFn: kpi,
    refetchInterval: 45_000
  });
  const equityQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['equity-curve', equityRange.days],
    queryFn: () => equityCurve(equityRange.days),
    refetchInterval: 60_000
  })));
  const realQ = createQuery({
    queryKey: ['realized-curve'],
    queryFn: realizedCurve,
    refetchInterval: 60_000
  });
  // 1 week so even a quiet bot has something to show on a fresh open.
  const callsQ = createQuery({
    queryKey: ['calls', 7],
    queryFn: () => calls(7),
    refetchInterval: 60_000
  });
  const newsQ = createQuery({
    queryKey: ['news', 168],
    queryFn: () => news(168),
    refetchInterval: 60_000
  });
  const filingsQ = createQuery({
    queryKey: ['filings', 168],
    queryFn: () => filings({ hours: 168 }),
    refetchInterval: 60_000
  });
  const catalystsQ = createQuery({
    queryKey: ['catalysts'],
    queryFn: catalysts,
    refetchInterval: 5 * 60_000
  });
  const hotQ = createQuery({
    queryKey: ['hot', 24],
    queryFn: () => hotTickers(24, 8),
    refetchInterval: 90_000
  });

  const realCum = $derived(($realQ.data ?? []).map((p) => p.cumulative));
  const sparkColour = $derived(
    realCum.length > 0 && realCum[realCum.length - 1] >= 0
      ? 'var(--color-good)'
      : 'var(--color-bad)'
  );

  function variantForSentiment(s: number | null | undefined): 'pos' | 'neg' | 'info' {
    if (s === null || s === undefined) return 'info';
    if (s > 0.15) return 'pos';
    if (s < -0.15) return 'neg';
    return 'info';
  }
</script>

<svelte:head><title>Overview · Sentinel</title></svelte:head>

<!-- ── HERO: equity number + return + sparkline ───────────────────────── -->
<div class="mb-6">
  <div class="text-[11px] font-semibold uppercase tracking-[0.13em] text-faint">
    Aggregate equity
  </div>
  <div class="mt-1 flex flex-wrap items-end gap-x-4 gap-y-1">
    <span class="text-[2.6rem] font-semibold leading-none tracking-tight tabular text-text">
      {$kpiQ.data ? usd($kpiQ.data.equity) : '—'}
    </span>
    {#if $kpiQ.data && $kpiQ.data.return_pct !== null}
      {@const r = $kpiQ.data.return_pct}
      {@const t = tone(r)}
      <span class={[
        'inline-flex items-baseline gap-1 text-[18px] font-semibold tabular',
        t === 'pos' ? 'text-good' : t === 'neg' ? 'text-bad' : 'text-muted'
      ].join(' ')}>
        {#if t === 'pos'}<ArrowUpRight class="h-4 w-4 self-center" />
        {:else if t === 'neg'}<ArrowDownRight class="h-4 w-4 self-center" />{/if}
        {pct(r, 2)}
      </span>
      <span class="text-[12px] text-faint">since inception</span>
    {/if}
    {#if realCum.length > 1}
      <div class="ml-auto w-40 shrink-0">
        <Sparkline values={realCum} width={160} height={36} color={sparkColour} />
        <div class="mt-0.5 text-right text-[10px] tabular text-faint">
          {realCum.length} closed · realised {usd(realCum[realCum.length - 1], true)}
        </div>
      </div>
    {/if}
  </div>
</div>

<!-- ── KPI ribbon ────────────────────────────────────────────────────── -->
<div class="grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-6">
  {#if $kpiQ.data}
    {@const k = $kpiQ.data}
    {#snippet kpi(label: string, value: string, sub: string, icon: any, accent: 'pos' | 'neg' | 'none' = 'none')}
      <div class="rounded-lg border border-border bg-surface px-3 py-2.5">
        <div class="flex items-center gap-1.5">
          <svelte:component this={icon} class={[
            'h-3 w-3',
            accent === 'pos' ? 'text-good' : accent === 'neg' ? 'text-bad' : 'text-faint'
          ].join(' ')} />
          <span class="text-[10px] font-semibold uppercase tracking-wider text-faint">
            {label}
          </span>
        </div>
        <div class={[
          'mt-1 text-[18px] font-semibold leading-tight tabular',
          accent === 'pos' ? 'text-good' : accent === 'neg' ? 'text-bad' : 'text-text'
        ].join(' ')}>
          {value}
        </div>
        <div class="text-[10.5px] text-faint tabular leading-tight">{sub}</div>
      </div>
    {/snippet}

    {@render kpi('Wallets', String(k.wallets ?? '—'), 'active funds', Wallet)}
    {@render kpi(
      'Open positions',
      k.open_positions !== null ? String(k.open_positions) : '—',
      k.unrealized_pnl !== null && k.open_positions ? `uPnL ${usd(k.unrealized_pnl, true)}` : 'flat',
      TrendingUp,
      k.unrealized_pnl !== null && k.unrealized_pnl > 0 ? 'pos' : k.unrealized_pnl !== null && k.unrealized_pnl < 0 ? 'neg' : 'none'
    )}
    {@render kpi(
      'Realised P&L',
      k.realized_pnl !== null ? usd(k.realized_pnl, true) : '—',
      k.closed ? `${k.wins ?? 0}/${k.closed} closed won` : 'no closed trades',
      Zap,
      k.realized_pnl !== null && k.realized_pnl > 0 ? 'pos' : k.realized_pnl !== null && k.realized_pnl < 0 ? 'neg' : 'none'
    )}
    {@render kpi(
      'Hit rate',
      k.hit_rate_pct !== null ? `${k.hit_rate_pct.toFixed(0)}%` : '—',
      k.calls_scored ? `${k.hits ?? 0}/${k.calls_scored} scored` : 'none yet',
      TargetIcon
    )}
    {@render kpi(
      'LLM',
      k.llm_reliability_pct !== null ? `${k.llm_reliability_pct.toFixed(1)}%` : '—',
      k.llm_calls ? `${compact(k.llm_calls)} calls · ${k.llm_errors ?? 0} fail` : 'idle',
      Sparkles,
      (k.llm_reliability_pct ?? 100) < 90 ? 'neg' : 'none'
    )}
    {@render kpi('Closed trades', String(k.closed ?? 0), 'all time', ActivityIcon)}
  {:else if $kpiQ.isLoading}
    {#each Array(6) as _, i (i)}
      <div class="h-[5.4rem] animate-pulse rounded-lg border border-border bg-surface" />
    {/each}
  {/if}
</div>

<!-- ── EQUITY CURVE chart ────────────────────────────────────────── -->
<Card class="mt-4 px-4 py-3">
  <div class="mb-2 flex items-baseline gap-3">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Equity vs inception
    </div>
    <div class="ml-auto flex items-center gap-1">
      {#each RANGES as r (r.label)}
        <button
          onclick={() => (equityRange = r)}
          class={[
            'rounded-md border px-2 py-0.5 text-[10.5px] transition-colors',
            equityRange.label === r.label
              ? 'border-primary/50 bg-primary-soft text-primary'
              : 'border-border bg-surface-2 text-muted hover:text-text'
          ].join(' ')}
        >{r.label}</button>
      {/each}
    </div>
  </div>
  {#if $equityQ.isLoading}
    <div class="flex h-[200px] items-center justify-center"><Spinner /></div>
  {:else}
    <EquityCurveChart series={$equityQ.data ?? []} />
  {/if}
</Card>

<!-- ── HOT NOW ──────────────────────────────────────── -->
{#if $hotQ.data && $hotQ.data.length > 0}
  <Card class="mt-4 px-4 py-3">
    <div class="mb-2 flex items-baseline gap-2">
      <Flame class="h-3.5 w-3.5 text-warn" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Hot now (24h composite signal)
      </div>
      <a
        href={`${base}/analytics`}
        class="ml-auto text-[10.5px] text-primary underline hover:text-primary/80"
      >see all →</a>
    </div>
    <div class="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
      {#each $hotQ.data as h (h.ticker)}
        <a
          href={`${base}/symbol/${encodeURIComponent(h.ticker)}`}
          class="rounded-md border border-border bg-surface-2 px-2.5 py-1.5 transition-colors hover:border-warn/40"
        >
          <div class="flex items-baseline gap-1.5">
            <TickerLink ticker={h.ticker} class="text-[12.5px]" />
            <span class={[
              'tabular text-[10.5px] font-bold',
              h.score >= 50 ? 'text-bad' : h.score >= 30 ? 'text-warn' : 'text-good'
            ].join(' ')}>{h.score.toFixed(0)}</span>
          </div>
          <div class="mt-1 flex h-1 overflow-hidden rounded-full bg-bg/40">
            {#each [
              [h.components.news, 'var(--color-primary)'],
              [h.components.reddit, 'var(--color-warn)'],
              [h.components.filings, 'var(--color-violet)'],
              [h.components.call, 'var(--color-good)'],
              [h.components.price, 'var(--color-bad)']
            ] as [v, c] (c)}
              {#if v > 0}<div style:flex-grow={v} style:background-color={c}></div>{/if}
            {/each}
          </div>
          <div class="mt-1 text-[10px] tabular text-faint">
            {#if h.news_count > 0}{h.news_count}N{/if}
            {#if h.reddit_count > 0} {h.reddit_count}R{/if}
            {#if h.filings_material > 0} {h.filings_material}F{/if}
            {#if h.price_move_pct !== null && Math.abs(h.price_move_pct) > 2}
              <span class={h.price_move_pct > 0 ? 'text-good' : 'text-bad'}>
                {h.price_move_pct > 0 ? '+' : ''}{h.price_move_pct.toFixed(1)}%
              </span>
            {/if}
          </div>
        </a>
      {/each}
    </div>
  </Card>
{/if}

<!-- ── upcoming catalysts ────────────────────────────────── -->
{#if $catalystsQ.data?.events?.length}
  <Card class="mt-4 px-4 py-3">
    <div class="mb-2 flex items-baseline gap-2">
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Upcoming catalysts (next 14d)
      </div>
      <span class="ml-auto text-[10px] tabular text-faint">
        {$catalystsQ.data.events.length}
      </span>
    </div>
    <div class="flex flex-wrap gap-2">
      {#each $catalystsQ.data.events.slice(0, 20) as e (e.date + (e.ticker ?? e.label ?? ''))}
        <div class="flex items-center gap-2 rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-[11.5px]">
          <span class="font-mono tabular text-faint">{e.date.slice(5)}</span>
          {#if e.ticker}
            <TickerLink ticker={e.ticker} class="text-[11.5px]" />
            <Pill variant="warn">earnings</Pill>
          {:else if e.label}
            <span class="text-muted">{e.label}</span>
            <Pill variant="info">macro</Pill>
          {/if}
        </div>
      {/each}
    </div>
  </Card>
{/if}

<!-- ── 3-column feed grid: Calls / Filings / News ────────────────── -->
<div class="mt-4 grid grid-cols-1 gap-3 lg:grid-cols-3">
  <!-- CALLS column -->
  <Card class="flex flex-col px-4 py-3">
    <div class="mb-2 flex items-baseline gap-2">
      <TargetIcon class="h-3.5 w-3.5 text-primary" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Recent calls (7d)
      </div>
      <span class="ml-auto text-[10px] tabular text-faint">
        {$callsQ.data?.length ?? 0}
      </span>
    </div>
    {#if $callsQ.isLoading}
      <div class="flex justify-center py-6"><Spinner size={14} /></div>
    {:else if !$callsQ.data?.length}
      <div class="rounded-md border border-border-soft bg-surface-2/40 px-3 py-4 text-center text-[11.5px] text-faint">
        No calls in the last 7 days.
      </div>
    {:else}
      <ul class="divide-soft -mx-1">
        {#each $callsQ.data.slice(0, 8) as c (c.id)}
          <li>
            <a
              href={`${base}/calls`}
              class="flex items-start gap-2 rounded-md px-1.5 py-1.5 transition-colors hover:bg-white/[0.025]"
            >
              <Pill variant={c.direction === 'long' ? 'pos' : 'neg'}>
                {c.direction[0].toUpperCase()}
              </Pill>
              <div class="min-w-0 flex-1">
                <div class="flex items-baseline gap-1.5 text-[12px]">
                  <TickerLink ticker={c.ticker} class="text-[12px]" />
                  <span class="text-[10px] text-muted">{c.conviction}/5</span>
                  <span class="ml-auto text-[10px] tabular text-faint">{timeAgo(c.ts)}</span>
                </div>
                <div class="mt-0.5 line-clamp-1 text-[11.5px] text-muted">{stripMd(c.thesis)}</div>
              </div>
            </a>
          </li>
        {/each}
      </ul>
      <a
        href={`${base}/calls`}
        class="mt-2 block rounded-md border border-border bg-surface-2 px-2 py-1 text-center text-[10.5px] text-muted transition-colors hover:border-primary/40 hover:text-text"
      >View all calls →</a>
    {/if}
  </Card>

  <!-- FILINGS column -->
  <Card class="flex flex-col px-4 py-3">
    <div class="mb-2 flex items-baseline gap-2">
      <FileText class="h-3.5 w-3.5 text-violet" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Recent filings (7d)
      </div>
      <span class="ml-auto text-[10px] tabular text-faint">
        {$filingsQ.data?.length ?? 0}
      </span>
    </div>
    {#if $filingsQ.isLoading}
      <div class="flex justify-center py-6"><Spinner size={14} /></div>
    {:else if !$filingsQ.data?.length}
      <div class="rounded-md border border-border-soft bg-surface-2/40 px-3 py-4 text-center text-[11.5px] text-faint">
        No filings in the last 7 days.
      </div>
    {:else}
      <ul class="divide-soft -mx-1">
        {#each $filingsQ.data.slice(0, 8) as f (f.id)}
          <li>
            <a
              href={`${base}/intel`}
              class="flex items-start gap-2 rounded-md px-1.5 py-1.5 transition-colors hover:bg-white/[0.025]"
            >
              <Pill variant="violet" class="font-mono">{f.form_type}</Pill>
              <div class="min-w-0 flex-1">
                <div class="flex items-baseline gap-1.5 text-[12px]">
                  {#if f.ticker}<TickerLink ticker={f.ticker} class="text-[12px]" />{/if}
                  {#if f.materiality_score !== null}
                    <span class={[
                      'text-[10px] tabular',
                      f.materiality_score >= 7 ? 'text-bad' :
                      f.materiality_score >= 4 ? 'text-warn' : 'text-faint'
                    ].join(' ')}>mat {f.materiality_score}/10</span>
                  {/if}
                  <span class="ml-auto text-[10px] tabular text-faint">{timeAgo(f.filed_at)}</span>
                </div>
                {#if f.summary}
                  <div class="mt-0.5 line-clamp-1 text-[11.5px] text-muted">{stripMd(f.summary)}</div>
                {/if}
              </div>
            </a>
          </li>
        {/each}
      </ul>
      <a
        href={`${base}/intel`}
        class="mt-2 block rounded-md border border-border bg-surface-2 px-2 py-1 text-center text-[10.5px] text-muted transition-colors hover:border-violet/40 hover:text-text"
      >View all filings →</a>
    {/if}
  </Card>

  <!-- NEWS column -->
  <Card class="flex flex-col px-4 py-3">
    <div class="mb-2 flex items-baseline gap-2">
      <Newspaper class="h-3.5 w-3.5 text-primary" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Recent news (7d)
      </div>
      <span class="ml-auto text-[10px] tabular text-faint">
        {$newsQ.data?.length ?? 0}
      </span>
    </div>
    {#if $newsQ.isLoading}
      <div class="flex justify-center py-6"><Spinner size={14} /></div>
    {:else if !$newsQ.data?.length}
      <div class="rounded-md border border-border-soft bg-surface-2/40 px-3 py-4 text-center text-[11.5px] text-faint">
        No news in the last 7 days.
      </div>
    {:else}
      <ul class="divide-soft -mx-1">
        {#each $newsQ.data.slice(0, 8) as n (n.id)}
          <li>
            <a
              href={`${base}/intel`}
              class="flex items-start gap-2 rounded-md px-1.5 py-1.5 transition-colors hover:bg-white/[0.025]"
            >
              <Pill variant={variantForSentiment(n.sentiment)}>
                {n.sentiment !== null && n.sentiment !== undefined
                  ? (n.sentiment > 0.15 ? '↑' : n.sentiment < -0.15 ? '↓' : '·')
                  : '·'}
              </Pill>
              <div class="min-w-0 flex-1">
                <div class="flex items-baseline gap-1.5 text-[12px]">
                  {#if n.ticker}<TickerLink ticker={n.ticker} class="text-[12px]" />{/if}
                  <span class="text-[10px] text-faint">{n.source}</span>
                  <span class="ml-auto text-[10px] tabular text-faint">{timeAgo(n.ts)}</span>
                </div>
                <div class="mt-0.5 line-clamp-2 text-[11.5px] leading-snug text-muted">{n.title}</div>
              </div>
            </a>
          </li>
        {/each}
      </ul>
      <a
        href={`${base}/intel`}
        class="mt-2 block rounded-md border border-border bg-surface-2 px-2 py-1 text-center text-[10.5px] text-muted transition-colors hover:border-primary/40 hover:text-text"
      >View all news →</a>
    {/if}
  </Card>
</div>
