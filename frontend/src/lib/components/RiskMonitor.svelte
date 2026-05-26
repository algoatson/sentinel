<script lang="ts">
  /**
   * Book-level risk snapshot. Single dense card. Sits next to the
   * equity curve on Overview so the trader sees, at a glance:
   *
   *   - how many open positions
   *   - how many are within N% of stop / target
   *   - $ at risk if every stop hits
   *   - which trades to look at first (biggest winner/loser, naked)
   *
   * Polls /api/analytics/risk-monitor every 60s, same cadence as the
   * other Overview panels.
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { riskMonitor, type RiskRowSlim } from '$api';
  import { base } from '$app/paths';
  import {
    ShieldAlert,
    Target as TargetIcon,
    AlertTriangle,
    TrendingUp,
    TrendingDown,
    Activity
  } from 'lucide-svelte';
  import Card from './Card.svelte';
  import { pct, price } from '$lib/format';

  const q = createQuery({
    queryKey: ['risk-monitor'],
    queryFn: riskMonitor,
    refetchInterval: 60_000
  });

  const data = $derived($q.data);

  function rowColour(d: number | null): string {
    if (d === null) return 'text-faint';
    if (d <= 0.5) return 'text-bad';
    if (d <= 1.0) return 'text-warn';
    return 'text-muted';
  }
</script>

<Card class="flex h-full flex-col px-4 py-3">
  <div class="mb-2 flex items-center gap-2">
    <ShieldAlert class="h-3.5 w-3.5 text-warn" />
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Risk monitor
    </div>
    {#if data}
      <span class="text-[10.5px] text-faint">· near = ≤{data.near_pct}% from stop/target</span>
    {/if}
    <a
      href={`${base}/book`}
      class="ml-auto text-[10.5px] text-muted hover:text-primary hover:underline"
    >Open book →</a>
  </div>

  {#if !data}
    <div class="flex flex-1 items-center justify-center text-[12px] text-faint">
      Loading…
    </div>
  {:else if data.n_open === 0}
    <div class="flex flex-1 items-center justify-center text-center text-[12px] text-faint">
      No open positions — book is flat.
    </div>
  {:else}
    <!-- Summary tiles row -->
    <div class="grid grid-cols-4 gap-1.5">
      <div class="rounded-md border border-border bg-surface-2 px-2 py-1.5">
        <div class="text-[9.5px] uppercase tracking-wider text-faint">Open</div>
        <div class="mt-0.5 flex items-baseline gap-1 tabular">
          <span class="text-[16px] font-semibold text-text">{data.n_open}</span>
          <span class="text-[10px] text-faint">·</span>
          <span class="text-[10.5px] text-good">{data.n_in_profit}↑</span>
          <span class="text-[10.5px] text-bad">{data.n_underwater}↓</span>
        </div>
      </div>
      <div class="rounded-md border border-border bg-surface-2 px-2 py-1.5">
        <div class="flex items-center gap-1 text-[9.5px] uppercase tracking-wider text-faint">
          <AlertTriangle class="h-2.5 w-2.5 text-bad" /> Near stop
        </div>
        <div class="mt-0.5 text-[16px] font-semibold tabular {data.n_near_stop ? 'text-bad' : 'text-text'}">
          {data.n_near_stop}
        </div>
      </div>
      <div class="rounded-md border border-border bg-surface-2 px-2 py-1.5">
        <div class="flex items-center gap-1 text-[9.5px] uppercase tracking-wider text-faint">
          <TargetIcon class="h-2.5 w-2.5 text-good" /> Near target
        </div>
        <div class="mt-0.5 text-[16px] font-semibold tabular {data.n_near_target ? 'text-good' : 'text-text'}">
          {data.n_near_target}
        </div>
      </div>
      <div class="rounded-md border border-border bg-surface-2 px-2 py-1.5">
        <div class="text-[9.5px] uppercase tracking-wider text-faint">@ risk</div>
        <div class="mt-0.5 flex items-baseline gap-1 tabular">
          <span class="text-[13.5px] font-semibold text-text">{price(data.dollar_at_risk)}</span>
          <span class="text-[10.5px] text-faint">·</span>
          <span class="text-[11px] {data.pct_book_at_risk >= 5 ? 'text-bad' : 'text-muted'}">
            {data.pct_book_at_risk.toFixed(1)}%
          </span>
        </div>
      </div>
    </div>

    <!-- Detail strip: biggest winner / loser + avg R -->
    <div class="mt-2 grid grid-cols-3 gap-1.5">
      {#snippet movePill(title: string, row: RiskRowSlim | null, isWinner: boolean)}
        <div class="rounded-md border border-border bg-surface-2 px-2 py-1.5">
          <div class="flex items-center gap-1 text-[9.5px] uppercase tracking-wider text-faint">
            {#if isWinner}
              <TrendingUp class="h-2.5 w-2.5 text-good" />
            {:else}
              <TrendingDown class="h-2.5 w-2.5 text-bad" />
            {/if}
            {title}
          </div>
          {#if row}
            <a
              href={`${base}/symbol/${encodeURIComponent(row.ticker)}`}
              class="mt-0.5 flex items-baseline gap-1.5 tabular hover:text-primary"
            >
              <span class="font-mono text-[12.5px] font-semibold text-text">${row.ticker}</span>
              <span class="text-[10.5px] {(row.upnl ?? 0) >= 0 ? 'text-good' : 'text-bad'}">
                {pct(row.upnl_pct ?? 0, 2)}
              </span>
            </a>
          {:else}
            <div class="mt-0.5 text-[12px] text-faint">—</div>
          {/if}
        </div>
      {/snippet}
      {@render movePill('Top winner', data.biggest_winner, true)}
      {@render movePill('Top loser', data.biggest_loser, false)}
      <div class="rounded-md border border-border bg-surface-2 px-2 py-1.5">
        <div class="flex items-center gap-1 text-[9.5px] uppercase tracking-wider text-faint">
          <Activity class="h-2.5 w-2.5 text-primary" />
          Avg R
        </div>
        <div class="mt-0.5 flex items-baseline gap-1 tabular">
          {#if data.avg_r_multiple !== null}
            <span class={[
              'text-[13.5px] font-semibold',
              data.avg_r_multiple >= 0 ? 'text-good' : 'text-bad'
            ].join(' ')}>
              {data.avg_r_multiple >= 0 ? '+' : ''}{data.avg_r_multiple.toFixed(2)}R
            </span>
            {#if data.median_dist_to_stop_pct !== null}
              <span class="text-[10.5px] text-faint">· median {data.median_dist_to_stop_pct.toFixed(1)}% to stop</span>
            {/if}
          {:else}
            <span class="text-[12px] text-faint">— (no stops set)</span>
          {/if}
        </div>
      </div>
    </div>

    <!-- Near stop / naked stops sections, only if non-empty -->
    {#if data.near_stop.length || data.naked.length}
      <div class="mt-2 flex-1 overflow-y-auto">
        {#if data.near_stop.length}
          <div class="text-[9.5px] font-semibold uppercase tracking-wider text-bad">
            Within {data.near_pct}% of stop ({data.near_stop.length})
          </div>
          <ul class="mt-1 space-y-0.5">
            {#each data.near_stop as r (r.id)}
              <li class="flex items-center gap-2 rounded border border-border-soft bg-surface-2/40 px-2 py-1 text-[11.5px] tabular">
                <a
                  href={`${base}/symbol/${encodeURIComponent(r.ticker)}`}
                  class="font-mono font-semibold text-text hover:text-primary"
                >${r.ticker}</a>
                <span class="text-[10px] uppercase text-faint">{r.side}</span>
                <span class="ml-auto text-faint">
                  mark <span class="text-muted">{price(r.mark)}</span>
                </span>
                <span class={rowColour(r.dist_to_stop_pct)}>
                  ↓ {r.dist_to_stop_pct?.toFixed(2)}% to {price(r.stop_price)}
                </span>
              </li>
            {/each}
          </ul>
        {/if}
        {#if data.naked.length}
          <div class="mt-2 text-[9.5px] font-semibold uppercase tracking-wider text-warn">
            No stop set ({data.naked.length})
          </div>
          <ul class="mt-1 space-y-0.5">
            {#each data.naked.slice(0, 4) as r (r.id)}
              <li class="flex items-center gap-2 rounded border border-border-soft bg-surface-2/40 px-2 py-1 text-[11.5px] tabular">
                <a
                  href={`${base}/symbol/${encodeURIComponent(r.ticker)}`}
                  class="font-mono font-semibold text-text hover:text-primary"
                >${r.ticker}</a>
                <span class="text-[10px] uppercase text-faint">{r.side}</span>
                <span class="text-[10.5px] capitalize text-faint">{r.fund}</span>
                <span class="ml-auto {(r.upnl ?? 0) >= 0 ? 'text-good' : 'text-bad'}">
                  {pct(r.upnl_pct ?? 0, 2)}
                </span>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    {/if}
  {/if}
</Card>
