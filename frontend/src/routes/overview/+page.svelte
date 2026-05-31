<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { reactiveQueryOptions } from '$lib/reactive-query.svelte';
  import { kpi, activity, realizedCurve, equityCurve, calls, news, filings, catalysts, hotTickers, topMovers, todayPulse } from '$api';
  import Card from '$components/Card.svelte';
  import Pill from '$components/Pill.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import Spinner from '$components/Spinner.svelte';
  import TickerLink from '$components/TickerLink.svelte';
  import EquityCurveChart from '$components/EquityCurveChart.svelte';
  import Sparkline from '$components/Sparkline.svelte';
  import TodayPulse from '$components/TodayPulse.svelte';
  import RiskMonitor from '$components/RiskMonitor.svelte';
  import EarningsExposure from '$components/EarningsExposure.svelte';
  import HoldingsNews from '$components/HoldingsNews.svelte';
  import HoldingsTape from '$components/HoldingsTape.svelte';
  import StreakCard from '$components/StreakCard.svelte';
  import DailyPlanCard from '$components/DailyPlanCard.svelte';
  import LiveEvents from '$components/LiveEvents.svelte';
  import WalletAllocation from '$components/WalletAllocation.svelte';
  import ConvergingNow from '$components/ConvergingNow.svelte';
  import { base } from '$app/paths';
  import { usd, timeAgo, pct, tone, stripMd } from '$lib/format';
  import {
    Newspaper, FileText, Target as TargetIcon, ArrowUpRight, ArrowDownRight,
    TrendingUp, Zap, Flame
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
  // Same query key the <TodayPulse /> component already uses — TanStack
  // dedupes by key so this isn't a second wire call. We just want the
  // already-fetched payload up here in the hero too.
  const pulseQ = createQuery({
    queryKey: ['today-pulse'],
    queryFn: todayPulse,
    refetchInterval: 60_000,
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
  const moversQ = createQuery({
    queryKey: ['top-movers', 6],
    queryFn: () => topMovers(6),
    refetchInterval: 60_000
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

<!-- ── HERO: equity headline + the three numbers that matter ──────────── -->
<!-- One row, four chips, no eyebrow. The TopBar already shows equity +
     return + health, so this stays focused on the WIDER context: total
     equity (big), inception return, today's realised, open uPnL, hit
     rate. Anything that was duplicated with TopBar or the KPI ribbon
     below has been removed. -->
<div class="mb-5">
  <div class="flex flex-wrap items-end gap-x-4 gap-y-2">
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
        <span class="ml-1 text-[10.5px] font-normal text-faint">since inception</span>
      </span>
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

  <!-- Single chip-row: today + open uPnL + open count. Dropped the
       "calls hit-rate" chip — it lives in the KPI ribbon directly
       below, so the same number is no longer rendered twice. -->
  <div class="mt-2.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11.5px] tabular">
    {#if $pulseQ.data}
      {@const today = $pulseQ.data.realized_today ?? 0}
      <span class={[
        'inline-flex items-baseline gap-1 rounded border px-2 py-0.5',
        today > 0 ? 'border-good/40 bg-good-soft text-good' :
        today < 0 ? 'border-bad/40 bg-bad-soft text-bad' :
        'border-border bg-surface-2 text-muted'
      ].join(' ')}>
        <span class="text-[9.5px] uppercase tracking-wider opacity-80">today</span>
        <span class="font-semibold">
          {today >= 0 ? '+' : ''}{usd(today, true)} realised
        </span>
        {#if $pulseQ.data.trades_closed > 0}
          <span class="text-[9.5px] opacity-70">· {$pulseQ.data.trades_closed} closed</span>
        {/if}
      </span>
    {/if}
    {#if $kpiQ.data?.unrealized_pnl !== null && $kpiQ.data?.unrealized_pnl !== undefined}
      {@const up = $kpiQ.data.unrealized_pnl}
      <span class={[
        'inline-flex items-baseline gap-1 rounded border px-2 py-0.5',
        up > 0 ? 'border-good/40 bg-good-soft text-good' :
        up < 0 ? 'border-bad/40 bg-bad-soft text-bad' :
        'border-border bg-surface-2 text-muted'
      ].join(' ')}>
        <span class="text-[9.5px] uppercase tracking-wider opacity-80">open</span>
        <span class="font-semibold">{up >= 0 ? '+' : ''}{usd(up, true)} uPnL</span>
        {#if $kpiQ.data?.open_positions !== null && $kpiQ.data?.open_positions !== undefined}
          <span class="text-[9.5px] opacity-70">· {$kpiQ.data.open_positions} pos</span>
        {/if}
      </span>
    {/if}
  </div>
</div>


<!-- ── TODAY + HOLDINGS strips · then DAILY PLAN full-width ──────────
     Earlier this paired TodayPulse and DailyPlanCard side-by-side. The
     problem: TodayPulse is a fundamentally horizontal one-row strip
     (~60px tall), while DailyPlanCard is itself a wide two-pane card
     (left plan textarea, right multi-paragraph briefing markdown —
     easily 400px tall). Pairing them in a 2-col grid forces the row to
     stretch to the briefing's height, leaving a giant blank zone next
     to the strip and making the briefing pane crammed at half-width.

     Now: both strip components stack as full-width strips, then the
     DailyPlanCard sits full-width on its own row. The card is already
     internally split — plan on the left, bot briefing on the right —
     so it has its OWN two-column layout when it's the full width of
     the page. Strip-stack → full-width-card is the only shape that
     lets each component breathe at its natural height. -->
<div class="mb-3">
  <TodayPulse />
</div>
<div class="mb-3">
  <HoldingsTape />
</div>
<div class="mb-4">
  <DailyPlanCard />
</div>

<!-- ── KPI ribbon ──────────────────────────────────────────────────────
     Trimmed from 6 → 3 tiles. Dropped: Wallets (TopBar shows count via
     the hero subtitle and Portfolio page covers detail), LLM (lives on
     /system + TopBar pill), Closed trades (already the denominator on
     the Realised P&L tile). -->
<div class="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
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
  {:else if $kpiQ.isLoading}
    {#each Array(3) as _, i (i)}
      <div class="h-[5.4rem] animate-pulse rounded-lg border border-border bg-surface" />
    {/each}
  {/if}
</div>

<!-- ── EQUITY CURVE + RISK MONITOR (side-by-side, 7/5 split) ────── -->
<div class="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-12">
  <Card class="px-4 py-3 lg:col-span-7">
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
      <div class="flex h-[340px] items-center justify-center"><Spinner /></div>
    {:else}
      <EquityCurveChart series={$equityQ.data ?? []} height={340} />
    {/if}
  </Card>
  <div class="lg:col-span-5">
    <RiskMonitor />
  </div>
</div>

<!-- ── ALLOCATION (full width) ──────────────────────── -->
<div class="mt-4">
  <WalletAllocation />
</div>

<!-- ── STREAKS + EARNINGS (split row) ───────────────── -->
<div class="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-12">
  <div class="lg:col-span-7">
    <StreakCard />
  </div>
  <div class="lg:col-span-5">
    <EarningsExposure />
  </div>
</div>

<!-- ── HOLDINGS NEWS (full width) ───────────────────── -->
<div class="mt-4">
  <HoldingsNews />
</div>

<!-- ── CONVERGING NOW (full width) ──────────────────── -->
<div class="mt-4">
  <ConvergingNow />
</div>

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

<!-- ── TOP MOVERS ─────────────────────────────────── -->
{#if $moversQ.data && ($moversQ.data.gainers.length || $moversQ.data.losers.length)}
  <Card class="mt-4 px-4 py-3">
    <div class="mb-2 flex items-baseline gap-2">
      <ArrowUpRight class="h-3.5 w-3.5 text-good" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Top movers (1d)
      </div>
      <span class="ml-auto text-[10.5px] text-faint">
        watchlist · sorted by % change
      </span>
    </div>
    <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
      <!-- gainers -->
      <div>
        <div class="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-good">
          <ArrowUpRight class="h-3 w-3" />
          Gainers
        </div>
        <div class="space-y-1">
          {#each $moversQ.data.gainers as m (m.ticker)}
            <a
              href={`${base}/symbol/${encodeURIComponent(m.ticker)}`}
              class="flex items-center gap-2 rounded-md border border-border-soft bg-surface-2/40 px-2 py-1 transition-colors hover:border-good/40"
            >
              <TickerLink ticker={m.ticker} class="text-[12px]" />
              <span class="text-[10px] text-faint">{m.asset_class}</span>
              <span class="ml-auto text-[11.5px] tabular text-muted">
                {m.last_price !== null ? m.last_price.toLocaleString('en-US', { maximumFractionDigits: 2 }) : '—'}
              </span>
              <span class="w-16 text-right text-[12px] tabular font-semibold text-good">
                +{m.change_1d_pct.toFixed(2)}%
              </span>
            </a>
          {/each}
        </div>
      </div>

      <!-- losers -->
      <div>
        <div class="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-bad">
          <ArrowDownRight class="h-3 w-3" />
          Losers
        </div>
        {#if !$moversQ.data.losers.length}
          <div class="rounded-md border border-border-soft bg-surface-2/40 px-2.5 py-3 text-center text-[11px] text-faint">
            No watchlist tickers in the red today 🎉
          </div>
        {:else}
          <div class="space-y-1">
            {#each $moversQ.data.losers as m (m.ticker)}
              <a
                href={`${base}/symbol/${encodeURIComponent(m.ticker)}`}
                class="flex items-center gap-2 rounded-md border border-border-soft bg-surface-2/40 px-2 py-1 transition-colors hover:border-bad/40"
              >
                <TickerLink ticker={m.ticker} class="text-[12px]" />
                <span class="text-[10px] text-faint">{m.asset_class}</span>
                <span class="ml-auto text-[11.5px] tabular text-muted">
                  {m.last_price !== null ? m.last_price.toLocaleString('en-US', { maximumFractionDigits: 2 }) : '—'}
                </span>
                <span class="w-16 text-right text-[12px] tabular font-semibold text-bad">
                  {m.change_1d_pct.toFixed(2)}%
                </span>
              </a>
            {/each}
          </div>
        {/if}
      </div>
    </div>
  </Card>
{/if}

<!-- ── upcoming catalysts ──────────────────────────────────
     Was: each event in a fat bordered chip with two pills, 20 of
     them wrapping across the row. Now: a compact horizontal strip,
     one line, with a small earnings/macro marker before each entry.
     Reads like a calendar bar instead of a sticker collection. -->
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
    <div class="flex flex-wrap gap-x-3 gap-y-1.5 text-[11.5px]">
      {#each $catalystsQ.data.events.slice(0, 24) as e (e.date + (e.ticker ?? e.label ?? ''))}
        <div class="inline-flex items-center gap-1.5">
          <span class="font-mono tabular text-faint">{e.date.slice(5)}</span>
          {#if e.ticker}
            <span class="h-1.5 w-1.5 rounded-full bg-warn" title="earnings"></span>
            <TickerLink ticker={e.ticker} class="text-[11.5px]" />
          {:else if e.label}
            <span class="h-1.5 w-1.5 rounded-full bg-primary" title="macro"></span>
            <span class="text-muted">{e.label}</span>
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
      <div class="flex flex-1 items-center justify-center rounded-md border border-border-soft bg-surface-2/40 px-3 py-4 text-center text-[11.5px] text-faint">
        No calls in the last 7 days.
      </div>
    {:else}
      <ul class="divide-soft -mx-1 flex-1">
        {#each $callsQ.data.slice(0, 5) as c (c.id)}
          <li>
            <a
              href={`${base}/calls`}
              class="flex h-[3.25rem] items-start gap-2 rounded-md px-1.5 py-1.5 transition-colors hover:bg-white/[0.025]"
            >
              <Pill variant={c.direction === 'long' ? 'pos' : 'neg'}>
                {c.direction[0].toUpperCase()}
              </Pill>
              <div class="min-w-0 flex-1 overflow-hidden">
                <div class="flex items-baseline gap-1.5 text-[12px] whitespace-nowrap">
                  <TickerLink ticker={c.ticker} class="text-[12px]" />
                  <span class="shrink-0 text-[10px] text-muted">{c.conviction}/5</span>
                  <span class="ml-auto shrink-0 text-[10px] tabular text-faint">{timeAgo(c.ts)}</span>
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
      <div class="flex flex-1 items-center justify-center rounded-md border border-border-soft bg-surface-2/40 px-3 py-4 text-center text-[11.5px] text-faint">
        No filings in the last 7 days.
      </div>
    {:else}
      <ul class="divide-soft -mx-1 flex-1">
        {#each $filingsQ.data.slice(0, 5) as f (f.id)}
          <li>
            <a
              href={`${base}/intel`}
              class="flex h-[3.25rem] items-start gap-2 rounded-md px-1.5 py-1.5 transition-colors hover:bg-white/[0.025]"
            >
              <Pill variant="violet" class="font-mono">{f.form_type}</Pill>
              <div class="min-w-0 flex-1 overflow-hidden">
                <div class="flex items-baseline gap-1.5 text-[12px] whitespace-nowrap">
                  {#if f.ticker}<TickerLink ticker={f.ticker} class="text-[12px]" />{/if}
                  {#if f.materiality_score !== null}
                    <span class={[
                      'shrink-0 text-[10px] tabular',
                      f.materiality_score >= 7 ? 'text-bad' :
                      f.materiality_score >= 4 ? 'text-warn' : 'text-faint'
                    ].join(' ')}>mat {f.materiality_score}/10</span>
                  {/if}
                  <span class="ml-auto shrink-0 text-[10px] tabular text-faint">{timeAgo(f.filed_at)}</span>
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
      <div class="flex flex-1 items-center justify-center rounded-md border border-border-soft bg-surface-2/40 px-3 py-4 text-center text-[11.5px] text-faint">
        No news in the last 7 days.
      </div>
    {:else}
      <ul class="divide-soft -mx-1 flex-1">
        {#each $newsQ.data.slice(0, 5) as n (n.id)}
          <li>
            <a
              href={`${base}/intel`}
              class="flex h-[3.25rem] items-start gap-2 rounded-md px-1.5 py-1.5 transition-colors hover:bg-white/[0.025]"
            >
              <Pill variant={variantForSentiment(n.sentiment)}>
                {n.sentiment !== null && n.sentiment !== undefined
                  ? (n.sentiment > 0.15 ? '↑' : n.sentiment < -0.15 ? '↓' : '·')
                  : '·'}
              </Pill>
              <div class="min-w-0 flex-1 overflow-hidden">
                <div class="flex items-baseline gap-1.5 text-[12px] whitespace-nowrap">
                  {#if n.ticker}<TickerLink ticker={n.ticker} class="text-[12px]" />{/if}
                  <span class="truncate text-[10px] text-faint">{n.source}</span>
                  <span class="ml-auto shrink-0 text-[10px] tabular text-faint">{timeAgo(n.ts)}</span>
                </div>
                <div class="mt-0.5 line-clamp-1 text-[11.5px] leading-snug text-muted">{n.title}</div>
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

<!-- ── live events (footer) ──────────────────────────────────
     The SSE stream lives at the bottom now, not as a hero element —
     it's a passive log, not something to land on. Useful as
     ambient confirmation that the bot is alive. -->
<div class="mt-4">
  <LiveEvents />
</div>
