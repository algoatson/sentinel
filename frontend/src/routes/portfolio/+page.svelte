<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { wallets, walletHistory, equityCurve } from '$api';
  import Card from '$components/Card.svelte';
  import Pill from '$components/Pill.svelte';
  import StatTile from '$components/StatTile.svelte';
  import Drawer from '$components/Drawer.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import Spinner from '$components/Spinner.svelte';
  import { usd, price, timeAgo, tone } from '$lib/format';

  const walletsQ = createQuery({
    queryKey: ['wallets'],
    queryFn: wallets,
    refetchInterval: 30_000
  });
  const curveQ = createQuery({
    queryKey: ['equity-curve', 30],
    queryFn: () => equityCurve(30),
    refetchInterval: 60_000
  });

  let selected = $state<string | null>(null);
  const historyQ = createQuery(() => ({
    queryKey: ['wallet-history', selected],
    queryFn: () => walletHistory(selected!, 90),
    enabled: !!selected
  }));

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

  function pointsFor(fund: string): number[] {
    const c = $curveQ.data?.find((c) => c.fund === fund);
    return (c?.points ?? []).map((p) => p.equity);
  }

  const aggregate = $derived.by(() => {
    const ws = $walletsQ.data ?? [];
    if (!ws.length) return null;
    const equity = ws.reduce((s, w) => s + w.equity, 0);
    const start = ws.reduce((s, w) => s + w.start, 0);
    const upnl = ws.reduce((s, w) => s + w.upnl, 0);
    const open = ws.reduce((s, w) => s + w.open, 0);
    const closed = ws.reduce((s, w) => s + w.closed, 0);
    const wins = ws.reduce((s, w) => s + w.wins, 0);
    return {
      equity,
      start,
      upnl,
      open,
      closed,
      wins,
      ret_pct: start ? ((equity - start) / start) * 100 : 0,
      win_rate: closed ? (wins / closed) * 100 : null
    };
  });
</script>

<svelte:head><title>Portfolio · Sentinel</title></svelte:head>

<div class="mb-4 flex items-end justify-between border-b border-border pb-3">
  <div>
    <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
      <span>💼</span><span>Portfolio</span>
    </h1>
    <div class="mt-0.5 text-[11.5px] text-faint">
      Autonomous wallets — each runs its own mandate. Click a card for open positions and 90-day trade history.
    </div>
  </div>
</div>

{#if aggregate}
  <div class="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
    <StatTile label="Aggregate equity" value={usd(aggregate.equity)} sub={`from ${usd(aggregate.start)}`} />
    <StatTile label="Aggregate return" value={`${aggregate.ret_pct.toFixed(1)}%`} toneValue={aggregate.ret_pct} sub="since inception" />
    <StatTile label="Unrealized P&L" value={usd(aggregate.upnl, true)} toneValue={aggregate.upnl} sub={`${aggregate.open} open`} />
    <StatTile label="Win rate" value={aggregate.win_rate !== null ? `${aggregate.win_rate.toFixed(0)}%` : '—'} sub={`${aggregate.wins}/${aggregate.closed} closed`} />
    <StatTile label="Wallets" value={String($walletsQ.data?.length ?? 0)} sub="active funds" />
    <StatTile label="Closed trades" value={String(aggregate.closed)} sub="all time" />
  </div>
{/if}

<div class="mt-5">
  {#if $walletsQ.isLoading}
    <div class="flex justify-center py-12"><Spinner /></div>
  {:else if !$walletsQ.data?.length}
    <EmptyState title="No wallets seeded yet" description="The bot seeds wallets on first run. Try restarting the bot." />
  {:else}
    <div class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
      {#each $walletsQ.data as w (w.name)}
        {@const points = pointsFor(w.name)}
        <Card interactive onclick={() => (selected = w.name)} class="px-4 py-3.5">
          <div class="flex items-baseline justify-between">
            <div>
              <div class="flex items-center gap-2">
                <span class="text-base font-semibold tracking-tight capitalize">{w.name}</span>
                <Pill variant={w.name === 'research' ? 'violet' : 'neutral'}>{w.mandate}</Pill>
              </div>
              <div class="mt-1 flex items-baseline gap-2">
                <span class="text-[1.5rem] font-semibold tabular leading-none">{usd(w.equity)}</span>
                <span class={['tabular text-[12.5px]', tone(w.ret_pct) === 'pos' ? 'text-good' : tone(w.ret_pct) === 'neg' ? 'text-bad' : 'text-muted'].join(' ')}>
                  {w.ret_pct >= 0 ? '+' : ''}{w.ret_pct.toFixed(2)}%
                </span>
              </div>
              <div class="text-[10.5px] text-faint tabular">from {usd(w.start)}</div>
            </div>
            <div class="text-right">
              <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">uPnL</div>
              <div class={['mt-0.5 tabular text-[14px] font-semibold', w.upnl >= 0 ? 'text-good' : 'text-bad'].join(' ')}>
                {usd(w.upnl, true)}
              </div>
            </div>
          </div>

          {#if points.length > 1}
            <div class="mt-3">
              <svg viewBox="0 0 240 48" class="w-full" preserveAspectRatio="none">
                <path
                  d={sparkPath(points, 240, 48)}
                  fill="none"
                  stroke={w.ret_pct >= 0 ? 'var(--color-good)' : 'var(--color-bad)'}
                  stroke-width="1.4"
                  stroke-linejoin="round"
                  stroke-linecap="round"
                />
                <path
                  d={sparkPath(points, 240, 48) + ' L 240 48 L 0 48 Z'}
                  fill={w.ret_pct >= 0 ? 'var(--color-good)' : 'var(--color-bad)'}
                  fill-opacity="0.10"
                  stroke="none"
                />
              </svg>
            </div>
          {/if}

          <div class="mt-3 grid grid-cols-3 gap-2 border-t border-border-soft pt-2.5 text-[11px] tabular">
            <div>
              <div class="text-faint">Open</div>
              <div class="text-text">{w.open}</div>
            </div>
            <div>
              <div class="text-faint">Closed</div>
              <div class="text-text">{w.closed}</div>
            </div>
            <div>
              <div class="text-faint">Wins</div>
              <div class={w.closed > 0 && w.wins / w.closed >= 0.55 ? 'text-good' : 'text-muted'}>
                {w.wins}{w.closed ? ` / ${w.closed}` : ''}
              </div>
            </div>
          </div>
        </Card>
      {/each}
    </div>
  {/if}
</div>

<Drawer
  open={selected !== null}
  onClose={() => (selected = null)}
  class="max-w-3xl"
>
  {#snippet header()}
    {#if $historyQ.data}
      {@const h = $historyQ.data}
      <div class="flex flex-1 items-baseline gap-2">
        <span class="text-base font-semibold capitalize text-text">{h.name}</span>
        <Pill variant={h.name === 'research' ? 'violet' : 'neutral'}>{h.mandate}</Pill>
        <span class="ml-3 text-[11px] text-faint">·</span>
        <span class={['ml-1 tabular text-sm font-semibold', tone(h.ret_pct) === 'pos' ? 'text-good' : tone(h.ret_pct) === 'neg' ? 'text-bad' : 'text-muted'].join(' ')}>
          {h.ret_pct >= 0 ? '+' : ''}{h.ret_pct.toFixed(2)}%
        </span>
        <span class="text-[11px] text-faint">since inception</span>
      </div>
    {:else}
      <span class="text-sm font-semibold text-text">Wallet</span>
    {/if}
  {/snippet}

  {#if $historyQ.isLoading}
    <div class="flex justify-center py-12"><Spinner /></div>
  {:else if $historyQ.data}
    {@const h = $historyQ.data}
    <div class="grid grid-cols-3 gap-2 text-[12px] tabular">
      <div class="rounded-md border border-border bg-surface-2 px-3 py-2">
        <div class="text-[10px] uppercase tracking-wider text-faint">Equity</div>
        <div class="mt-0.5 text-[15px] font-semibold">{usd(h.equity)}</div>
      </div>
      <div class="rounded-md border border-border bg-surface-2 px-3 py-2">
        <div class="text-[10px] uppercase tracking-wider text-faint">Cash</div>
        <div class="mt-0.5 text-[15px] font-semibold">{usd(h.cash)}</div>
      </div>
      <div class="rounded-md border border-border bg-surface-2 px-3 py-2">
        <div class="text-[10px] uppercase tracking-wider text-faint">Starting</div>
        <div class="mt-0.5 text-[15px] font-semibold text-muted">{usd(h.starting)}</div>
      </div>
    </div>

    <div class="mt-5">
      <div class="mb-2 flex items-baseline gap-2">
        <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
          Open positions
        </div>
        <div class="text-[11px] text-faint">{h.open.length}</div>
      </div>
      {#if !h.open.length}
        <div class="rounded-md border border-border-soft bg-surface-2 px-3 py-2 text-[11.5px] text-faint">
          No open positions.
        </div>
      {:else}
        <div class="overflow-hidden rounded-lg border border-border">
          <table class="w-full text-[12px] tabular">
            <thead class="bg-surface-2 text-[10px] uppercase tracking-wider text-faint">
              <tr>
                <th class="px-3 py-1.5 text-left">Ticker</th>
                <th class="px-3 py-1.5 text-left">Side</th>
                <th class="px-3 py-1.5 text-right">Qty</th>
                <th class="px-3 py-1.5 text-right">Entry</th>
                <th class="px-3 py-1.5 text-right">Mark</th>
                <th class="px-3 py-1.5 text-right">uPnL</th>
                <th class="px-3 py-1.5 text-right">Opened</th>
              </tr>
            </thead>
            <tbody>
              {#each h.open as p (p.id)}
                <tr class="border-t border-border-soft">
                  <td class="px-3 py-1.5 font-mono font-semibold">${p.ticker}</td>
                  <td class="px-3 py-1.5">
                    <Pill variant={p.side === 'long' ? 'pos' : 'neg'}>{p.side.toUpperCase()}</Pill>
                  </td>
                  <td class="px-3 py-1.5 text-right">{p.qty}</td>
                  <td class="px-3 py-1.5 text-right">{price(p.entry)}</td>
                  <td class={['px-3 py-1.5 text-right', p.mark_live ? '' : 'text-faint'].join(' ')}>
                    {price(p.mark)}{p.mark_live ? '' : '*'}
                  </td>
                  <td class={['px-3 py-1.5 text-right font-medium', p.upnl >= 0 ? 'text-good' : 'text-bad'].join(' ')}>
                    {usd(p.upnl, true)}<span class="ml-1 text-[10px] text-faint">({p.upnl_pct.toFixed(1)}%)</span>
                  </td>
                  <td class="px-3 py-1.5 text-right text-[10.5px] text-faint">{timeAgo(p.entry_at)}</td>
                </tr>
                {#if p.open_reason}
                  <tr class="border-t border-border-soft">
                    <td colspan="7" class="bg-surface-2/40 px-3 py-1 text-[10.5px] text-muted">
                      <span class="text-faint">opened:</span> {p.open_reason}
                    </td>
                  </tr>
                {/if}
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </div>

    <div class="mt-5">
      <div class="mb-2 flex items-baseline gap-2">
        <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
          Closed (90d)
        </div>
        <div class="text-[11px] text-faint">{h.closed.length}</div>
      </div>
      {#if !h.closed.length}
        <div class="rounded-md border border-border-soft bg-surface-2 px-3 py-2 text-[11.5px] text-faint">
          No closed trades in the last 90 days.
        </div>
      {:else}
        <div class="overflow-hidden rounded-lg border border-border">
          <table class="w-full text-[11.5px] tabular">
            <thead class="bg-surface-2 text-[10px] uppercase tracking-wider text-faint">
              <tr>
                <th class="px-3 py-1.5 text-left">Ticker</th>
                <th class="px-3 py-1.5 text-left">Side</th>
                <th class="px-3 py-1.5 text-right">In → Out</th>
                <th class="px-3 py-1.5 text-right">P&L</th>
                <th class="px-3 py-1.5 text-right">Closed</th>
              </tr>
            </thead>
            <tbody>
              {#each h.closed as t (t.id)}
                <tr class="border-t border-border-soft">
                  <td class="px-3 py-1.5 font-mono font-semibold">${t.ticker}</td>
                  <td class="px-3 py-1.5">
                    <Pill variant={t.side === 'long' ? 'pos' : 'neg'}>{t.side.toUpperCase()}</Pill>
                  </td>
                  <td class="px-3 py-1.5 text-right">
                    {price(t.entry)} → {t.exit !== null ? price(t.exit) : '—'}
                  </td>
                  <td class={['px-3 py-1.5 text-right font-medium', (t.realized_pnl ?? 0) >= 0 ? 'text-good' : 'text-bad'].join(' ')}>
                    {usd(t.realized_pnl, true)}<span class="ml-1 text-[10px] text-faint">({t.realized_pct.toFixed(1)}%)</span>
                  </td>
                  <td class="px-3 py-1.5 text-right text-[10.5px] text-faint">{timeAgo(t.exit_at)}</td>
                </tr>
                {#if t.close_reason}
                  <tr class="border-t border-border-soft">
                    <td colspan="5" class="bg-surface-2/40 px-3 py-1 text-[10.5px] text-muted">
                      <span class="text-faint">closed:</span> {t.close_reason}
                    </td>
                  </tr>
                {/if}
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </div>
  {/if}
</Drawer>
