<script lang="ts">
  /**
   * /book — unified open-positions view across every wallet.
   *
   * One sortable table, with inline "Close" buttons that fire
   * POST /api/positions/{id}/close. Live SSE invalidates the list
   * on any trade event.
   */
  import { createQuery, createMutation, useQueryClient } from '@tanstack/svelte-query';
  import { openPositions, closePosition } from '$api';
  import type { OpenPositionRow } from '$api';
  import Card from '$components/Card.svelte';
  import Pill from '$components/Pill.svelte';
  import TickerLink from '$components/TickerLink.svelte';
  import Spinner from '$components/Spinner.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import { toast } from '$lib/toast.svelte';
  import { usd, price, timeAgo } from '$lib/format';
  import { Briefcase, AlertCircle, X } from 'lucide-svelte';

  type SortKey = 'fund' | 'ticker' | 'side' | 'age' | 'entry' | 'mark' | 'upnl' | 'upnl_pct';
  let sortKey: SortKey = $state('upnl');
  let sortDir: 'asc' | 'desc' = $state('desc');
  let fundFilter = $state('all');
  let sideFilter: 'all' | 'long' | 'short' = $state('all');
  let tickerFilter = $state('');
  let confirmId = $state<number | null>(null);

  const positionsQ = createQuery({
    queryKey: ['positions-open'],
    queryFn: openPositions,
    refetchInterval: 30_000
  });

  const qc = useQueryClient();
  const closeM = createMutation({
    mutationFn: (id: number) => closePosition(id, 'manual via /book'),
    onSuccess: (res) => {
      toast.success(
        res.realized_pnl !== null
          ? `Closed #${res.trade_id} · pnl ${res.realized_pnl >= 0 ? '+' : ''}${res.realized_pnl.toFixed(2)}`
          : `Closed #${res.trade_id}`
      );
      qc.invalidateQueries({ queryKey: ['positions-open'] });
      qc.invalidateQueries({ queryKey: ['wallets'] });
      qc.invalidateQueries({ queryKey: ['kpi'] });
      qc.invalidateQueries({ queryKey: ['realized-curve'] });
      confirmId = null;
    },
    onError: (err) =>
      toast.error(err instanceof Error ? err.message : String(err))
  });

  const funds = $derived(
    Array.from(new Set(($positionsQ.data ?? []).map((p) => p.fund))).sort()
  );

  function setSort(k: SortKey) {
    if (sortKey === k) {
      sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      sortKey = k;
      sortDir = k === 'ticker' || k === 'fund' || k === 'side' ? 'asc' : 'desc';
    }
  }

  function field(p: OpenPositionRow, k: SortKey): number | string {
    switch (k) {
      case 'fund':     return p.fund;
      case 'ticker':   return p.ticker;
      case 'side':     return p.side;
      case 'age':      return p.age_h;
      case 'entry':    return p.entry;
      case 'mark':     return p.mark;
      case 'upnl':     return p.upnl;
      case 'upnl_pct': return p.upnl_pct;
    }
  }

  const sorted = $derived(
    [...($positionsQ.data ?? [])]
      .filter((p) => {
        if (fundFilter !== 'all' && p.fund !== fundFilter) return false;
        if (sideFilter !== 'all' && p.side !== sideFilter) return false;
        const t = tickerFilter.trim().toUpperCase().replace(/^\$/, '');
        if (t && p.ticker !== t) return false;
        return true;
      })
      .sort((a, b) => {
        const va = field(a, sortKey);
        const vb = field(b, sortKey);
        if (typeof va === 'string') {
          return sortDir === 'asc'
            ? String(va).localeCompare(String(vb))
            : String(vb).localeCompare(String(va));
        }
        const na = typeof va === 'number' ? va : -Infinity;
        const nb = typeof vb === 'number' ? vb : -Infinity;
        return sortDir === 'asc' ? na - nb : nb - na;
      })
  );

  const totals = $derived.by(() => {
    const ps = $positionsQ.data ?? [];
    const upnl = ps.reduce((s, p) => s + p.upnl, 0);
    const wins = ps.filter((p) => p.upnl > 0).length;
    const losses = ps.filter((p) => p.upnl < 0).length;
    const longs = ps.filter((p) => p.side === 'long').length;
    const shorts = ps.filter((p) => p.side === 'short').length;
    return { upnl, wins, losses, longs, shorts, count: ps.length };
  });
</script>

<svelte:head><title>Book · Sentinel</title></svelte:head>

<div class="mb-4 flex items-end justify-between border-b border-border pb-3">
  <div>
    <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
      <Briefcase class="h-5 w-5 text-primary" /><span>Book</span>
    </h1>
    <div class="mt-0.5 text-[11.5px] text-faint">
      Every open position across every wallet — sortable, filterable,
      with inline close. Auto-refreshes; trade events stream in via SSE.
    </div>
  </div>
</div>

<!-- ── headline aggregates ─────────────────────────── -->
<div class="grid grid-cols-2 gap-2.5 md:grid-cols-3 lg:grid-cols-5">
  <div class="rounded-lg border border-border bg-surface px-3 py-2">
    <div class="text-[10px] uppercase tracking-wider text-faint">Open positions</div>
    <div class="mt-0.5 text-[18px] font-semibold tabular text-text">{totals.count}</div>
    <div class="text-[10.5px] tabular text-faint">
      <span class="text-good">{totals.longs} L</span> · <span class="text-bad">{totals.shorts} S</span>
    </div>
  </div>

  <div class="rounded-lg border border-border bg-surface px-3 py-2">
    <div class="text-[10px] uppercase tracking-wider text-faint">Aggregate uPnL</div>
    <div class={[
      'mt-0.5 text-[18px] font-semibold tabular',
      totals.upnl > 0 ? 'text-good' : totals.upnl < 0 ? 'text-bad' : 'text-text'
    ].join(' ')}>
      {usd(totals.upnl, true)}
    </div>
    <div class="text-[10.5px] tabular text-faint">across all wallets</div>
  </div>

  <div class="rounded-lg border border-border bg-surface px-3 py-2">
    <div class="text-[10px] uppercase tracking-wider text-faint">Winners</div>
    <div class="mt-0.5 text-[18px] font-semibold tabular text-good">{totals.wins}</div>
    <div class="text-[10.5px] tabular text-faint">positions in profit</div>
  </div>

  <div class="rounded-lg border border-border bg-surface px-3 py-2">
    <div class="text-[10px] uppercase tracking-wider text-faint">Losers</div>
    <div class="mt-0.5 text-[18px] font-semibold tabular text-bad">{totals.losses}</div>
    <div class="text-[10.5px] tabular text-faint">positions underwater</div>
  </div>

  <div class="rounded-lg border border-border bg-surface px-3 py-2">
    <div class="text-[10px] uppercase tracking-wider text-faint">Wallets active</div>
    <div class="mt-0.5 text-[18px] font-semibold tabular text-text">{funds.length}</div>
    <div class="text-[10.5px] tabular text-faint">with open trades</div>
  </div>
</div>

<!-- ── filter ribbon ─────────────────────────────── -->
<Card class="mt-3 px-4 py-2.5">
  <div class="flex flex-wrap items-center gap-2">
    <span class="text-[10px] font-semibold uppercase tracking-wider text-faint">Wallet</span>
    <select
      bind:value={fundFilter}
      class="rounded-md border border-border bg-surface-2 px-2 py-1 text-[12px] text-text focus:border-primary/60 focus:outline-none"
    >
      <option value="all">all</option>
      {#each funds as f (f)}
        <option value={f}>{f}</option>
      {/each}
    </select>

    <span class="ml-2 text-[10px] font-semibold uppercase tracking-wider text-faint">Side</span>
    {#each [['all', 'All'], ['long', 'Long'], ['short', 'Short']] as [k, label] (k)}
      <button
        type="button"
        onclick={() => (sideFilter = k as any)}
        class={[
          'rounded-md border px-2 py-1 text-[11px] transition-colors',
          sideFilter === k
            ? k === 'long'
              ? 'border-good/40 bg-good-soft text-good'
              : k === 'short'
                ? 'border-bad/40 bg-bad-soft text-bad'
                : 'border-primary/50 bg-primary-soft text-primary'
            : 'border-border bg-surface-2 text-muted hover:text-text'
        ].join(' ')}
      >{label}</button>
    {/each}

    <input
      type="text"
      bind:value={tickerFilter}
      placeholder="$ticker"
      class="ml-2 w-24 rounded-md border border-border bg-surface-2 px-2 py-1 font-mono text-[12px] text-text placeholder:text-faint/50 focus:border-primary/60 focus:outline-none"
    />

    <span class="ml-auto text-[11px] tabular text-faint">
      {sorted.length} of {$positionsQ.data?.length ?? 0}
    </span>
  </div>
</Card>

<!-- ── table ───────────────────────────────────── -->
<Card class="mt-3 overflow-hidden">
  {#if $positionsQ.isLoading}
    <div class="flex items-center justify-center py-12"><Spinner /></div>
  {:else if !$positionsQ.data?.length}
    <EmptyState
      title="No open positions"
      description="Every autonomous wallet is flat. Open positions appear here as soon as the bot enters."
    />
  {:else}
    <div class="overflow-x-auto">
      <table class="w-full text-[12.5px] tabular">
        <thead>
          <tr class="border-b border-border bg-surface-2/40 text-[10px] uppercase tracking-wider text-faint">
            {#snippet th(label: string, k: SortKey, align: 'left' | 'right' = 'right')}
              <th
                class={[
                  'px-3 py-2 font-semibold cursor-pointer select-none transition-colors hover:text-text',
                  align === 'left' ? 'text-left' : 'text-right'
                ].join(' ')}
                onclick={() => setSort(k)}
              >
                <span>{label}</span>
                {#if sortKey === k}
                  <span class="ml-0.5 text-primary">{sortDir === 'asc' ? '▲' : '▼'}</span>
                {/if}
              </th>
            {/snippet}
            {@render th('Wallet', 'fund', 'left')}
            {@render th('Ticker', 'ticker', 'left')}
            {@render th('Side', 'side', 'left')}
            {@render th('Qty', 'entry')}
            {@render th('Entry', 'entry')}
            {@render th('Mark', 'mark')}
            {@render th('uPnL', 'upnl')}
            {@render th('%', 'upnl_pct')}
            {@render th('Age', 'age')}
            <th class="px-3 py-2 text-right font-semibold">Action</th>
          </tr>
        </thead>
        <tbody>
          {#each sorted as p (p.id)}
            <tr class="border-b border-border-soft hover:bg-white/[0.02]">
              <td class="px-3 py-1.5 text-left text-muted capitalize" title={p.fund_mandate}>{p.fund}</td>
              <td class="px-3 py-1.5 text-left"><TickerLink ticker={p.ticker} /></td>
              <td class="px-3 py-1.5 text-left">
                <Pill variant={p.side === 'long' ? 'pos' : 'neg'}>{p.side.toUpperCase()}</Pill>
              </td>
              <td class="px-3 py-1.5 text-right">{p.qty}</td>
              <td class="px-3 py-1.5 text-right">{price(p.entry)}</td>
              <td class={['px-3 py-1.5 text-right', p.mark_live ? '' : 'text-faint'].join(' ')}>
                {price(p.mark)}{p.mark_live ? '' : '*'}
              </td>
              <td class={['px-3 py-1.5 text-right font-medium', p.upnl >= 0 ? 'text-good' : 'text-bad'].join(' ')}>
                {usd(p.upnl, true)}
              </td>
              <td class={['px-3 py-1.5 text-right', p.upnl_pct >= 0 ? 'text-good' : 'text-bad'].join(' ')}>
                {p.upnl_pct >= 0 ? '+' : ''}{p.upnl_pct.toFixed(2)}%
              </td>
              <td class="px-3 py-1.5 text-right text-[10.5px] text-faint">
                {p.age_h < 24 ? `${p.age_h.toFixed(1)}h` : `${Math.round(p.age_h / 24)}d`}
              </td>
              <td class="px-3 py-1.5 text-right">
                {#if confirmId === p.id}
                  <span class="inline-flex items-center gap-1">
                    <button
                      type="button"
                      onclick={() => $closeM.mutate(p.id)}
                      disabled={$closeM.isPending}
                      class="rounded-md border border-bad/40 bg-bad-soft px-2 py-0.5 text-[10.5px] font-medium text-bad hover:bg-bad/15 disabled:opacity-50"
                    >Confirm</button>
                    <button
                      type="button"
                      onclick={() => (confirmId = null)}
                      class="rounded-md border border-border bg-surface-2 px-2 py-0.5 text-[10.5px] text-muted hover:text-text"
                    >Cancel</button>
                  </span>
                {:else}
                  <button
                    type="button"
                    onclick={() => (confirmId = p.id)}
                    class="inline-flex items-center gap-1 rounded-md border border-border bg-surface-2 px-2 py-0.5 text-[10.5px] text-muted hover:border-bad/30 hover:text-bad"
                    title="Close this position at the current mark"
                  >
                    <X class="h-2.5 w-2.5" />
                    Close
                  </button>
                {/if}
              </td>
            </tr>
            {#if p.open_reason}
              <tr class="border-b border-border-soft">
                <td colspan="10" class="bg-surface-2/30 px-3 py-1 text-[10.5px] text-faint">
                  <span class="text-muted">opened:</span> {p.open_reason}
                </td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    </div>
    {#if sorted.some((p) => !p.mark_live)}
      <div class="border-t border-border-soft bg-surface-2/30 px-3 py-1.5 text-[10.5px] text-faint">
        <AlertCircle class="mr-1 inline h-3 w-3 align-baseline" />
        Asterisk (*) on Mark = no live quote available; falls back to entry price.
      </div>
    {/if}
  {/if}
</Card>
