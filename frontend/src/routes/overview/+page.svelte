<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { kpi, activity, realizedCurve } from '$api';
  import StatTile from '$components/StatTile.svelte';
  import Card from '$components/Card.svelte';
  import Pill from '$components/Pill.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import Spinner from '$components/Spinner.svelte';
  import TickerLink from '$components/TickerLink.svelte';
  import { usd, compact, timeAgo } from '$lib/format';
  import { Newspaper, FileText, Target as TargetIcon } from 'lucide-svelte';

  const kpiQ = createQuery({
    queryKey: ['kpi'],
    queryFn: kpi,
    refetchInterval: 45_000
  });
  const actQ = createQuery({
    queryKey: ['activity', 48],
    queryFn: () => activity(48),
    refetchInterval: 60_000
  });
  const realQ = createQuery({
    queryKey: ['realized-curve'],
    queryFn: realizedCurve,
    refetchInterval: 60_000
  });

  // Tiny inline equity-trend sparkline from the realized P&L points.
  // Simple SVG, no chart lib for this — it's a 60px-tall summary.
  function sparkPath(values: number[], width: number, height: number): string {
    if (!values.length) return '';
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const step = width / Math.max(1, values.length - 1);
    return values
      .map((v, i) => {
        const x = i * step;
        const y = height - ((v - min) / range) * height;
        return `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`;
      })
      .join(' ');
  }

  const cumValues = $derived(($realQ.data ?? []).map((p) => p.cumulative));
  const sparkColour = $derived(
    cumValues.length > 0 && cumValues[cumValues.length - 1] >= 0
      ? 'var(--color-good)'
      : 'var(--color-bad)'
  );
</script>

<svelte:head><title>Overview · Sentinel</title></svelte:head>

<!-- ── header ─────────────────────────────────────────────────────────── -->
<div class="mb-4 flex items-end justify-between border-b border-border pb-3">
  <div>
    <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
      <span>📊</span><span>Overview</span>
    </h1>
    <div class="mt-0.5 text-[11.5px] text-faint">
      At-a-glance — KPIs, realised P&L, recent activity.
    </div>
  </div>
</div>

<!-- ── KPI ribbon ────────────────────────────────────────────────────── -->
<div class="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
  {#if $kpiQ.data}
    {@const k = $kpiQ.data}
    <StatTile
      label="Wallet equity"
      value={usd(k.equity)}
      sub={k.wallets ? `${k.wallets} wallet${k.wallets === 1 ? '' : 's'}` : '—'}
    />
    <StatTile
      label="Aggregate return"
      value={k.return_pct !== null ? `${k.return_pct.toFixed(1)}%` : '—'}
      toneValue={k.return_pct}
      sub="since inception"
    />
    <StatTile
      label="Open positions"
      value={k.open_positions !== null ? String(k.open_positions) : '—'}
      sub={k.unrealized_pnl !== null && k.open_positions
        ? `uPnL ${usd(k.unrealized_pnl, true)}`
        : 'flat'}
    />
    <StatTile
      label="Realized P&L"
      value={k.realized_pnl !== null ? usd(k.realized_pnl, true) : '—'}
      toneValue={k.realized_pnl}
      sub={k.closed
        ? `${k.wins ?? 0}/${k.closed} closed won`
        : 'no closed trades'}
    />
    <StatTile
      label="Call hit rate"
      value={k.hit_rate_pct !== null ? `${k.hit_rate_pct.toFixed(0)}%` : '—'}
      sub={k.calls_scored
        ? `${k.hits ?? 0}/${k.calls_scored} scored`
        : 'none yet'}
    />
    <StatTile
      label="LLM reliability"
      value={k.llm_reliability_pct !== null
        ? `${k.llm_reliability_pct.toFixed(1)}%`
        : '—'}
      accent={(k.llm_reliability_pct ?? 100) < 90 ? 'neg' : 'none'}
      sub={k.llm_calls
        ? `${compact(k.llm_calls)} calls · ${k.llm_errors ?? 0} failed`
        : 'no calls yet'}
    />
  {:else if $kpiQ.isLoading}
    {#each Array(6) as _, i (i)}
      <div class="h-[5.5rem] animate-pulse rounded-xl border border-border bg-surface" />
    {/each}
  {/if}
</div>

<!-- ── realised P&L sparkline + activity feed ──────────────────────────── -->
<div class="mt-4 grid grid-cols-1 gap-3 lg:grid-cols-3">
  <Card class="px-4 py-3 lg:col-span-1">
    <div class="flex items-baseline justify-between">
      <div>
        <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
          Realised P&L
        </div>
        <div class="mt-0.5 text-[1.4rem] font-semibold tabular">
          {#if cumValues.length > 0}
            <span class={cumValues[cumValues.length - 1] >= 0 ? 'text-good' : 'text-bad'}>
              {usd(cumValues[cumValues.length - 1], true)}
            </span>
          {:else}
            <span class="text-muted">—</span>
          {/if}
        </div>
        <div class="text-[11px] text-faint">
          {cumValues.length} closed trade{cumValues.length === 1 ? '' : 's'}
        </div>
      </div>
    </div>
    {#if cumValues.length > 1}
      <div class="mt-3">
        <svg viewBox="0 0 200 60" class="w-full" preserveAspectRatio="none">
          <path
            d={sparkPath(cumValues, 200, 60)}
            fill="none"
            stroke={sparkColour}
            stroke-width="1.5"
            stroke-linejoin="round"
            stroke-linecap="round"
          />
          <path
            d={sparkPath(cumValues, 200, 60) + ` L 200 60 L 0 60 Z`}
            fill={sparkColour}
            fill-opacity="0.12"
            stroke="none"
          />
        </svg>
      </div>
    {/if}
  </Card>

  <Card class="px-4 py-3 lg:col-span-2">
    <div class="mb-2 flex items-baseline justify-between">
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Recent activity (48h)
      </div>
      <div class="text-[11px] text-faint">
        {$actQ.data?.length ?? 0} item{($actQ.data?.length ?? 0) === 1 ? '' : 's'}
      </div>
    </div>
    {#if $actQ.isLoading}
      <div class="flex items-center justify-center py-8"><Spinner /></div>
    {:else if !$actQ.data?.length}
      <EmptyState
        title="No activity in last 48h"
        description="Calls, filings and news will appear here as the bot ingests them."
      />
    {:else}
      <ul class="divide-soft">
        {#each $actQ.data.slice(0, 18) as item (item.kind + '-' + item.id)}
          <li class="grid grid-cols-[auto_1fr_auto] items-start gap-3 py-2 text-[12.5px]">
            <Pill
              variant={item.kind === 'call'
                ? item.side === 'short'
                  ? 'neg'
                  : 'pos'
                : item.kind === 'filing'
                  ? 'violet'
                  : item.sentiment && item.sentiment > 0
                    ? 'pos'
                    : item.sentiment && item.sentiment < 0
                      ? 'neg'
                      : 'info'}
            >
              {#if item.kind === 'call'}
                <TargetIcon class="h-3 w-3" />
                {(item.side ?? '').toUpperCase()}
              {:else if item.kind === 'filing'}
                <FileText class="h-3 w-3" />
                {(item.form ?? 'FILING').slice(0, 8)}
              {:else}
                <Newspaper class="h-3 w-3" />
                NEWS
              {/if}
            </Pill>
            <div class="min-w-0">
              {#if item.url}
                <a
                  href={item.url}
                  target="_blank"
                  rel="noopener"
                  class="block truncate text-text hover:text-primary"
                  title={item.title}>{item.title}</a
                >
              {:else}
                <div class="truncate text-text" title={item.title}>{item.title}</div>
              {/if}
              <div class="mt-0.5 flex flex-wrap items-center gap-x-2 text-[10.5px] text-faint">
                {#if item.ticker}
                  <TickerLink ticker={item.ticker} class="text-muted hover:!text-primary" />
                {/if}
                {#if item.src}
                  <span>{item.src}</span>
                {/if}
                {#if item.conviction}
                  <span>conv {item.conviction}/5</span>
                {/if}
              </div>
            </div>
            <span class="tabular text-[10px] text-faint">{timeAgo(item.ts)}</span>
          </li>
        {/each}
      </ul>
    {/if}
  </Card>
</div>
