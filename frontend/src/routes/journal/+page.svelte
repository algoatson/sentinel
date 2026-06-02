<script lang="ts">
  /**
   * /journal — closed-trade journal.
   *
   * One row per closed trade, newest first, grouped by month for
   * scanability. Each row shows the lifecycle stats (entry/exit, hold
   * time, realised PnL %, R-multiple, close reason) plus an inline
   * reflection editor that PATCHes /positions/{id}/journal.
   *
   * Use the search box to grep the notes/close-reason for ad-hoc
   * post-mortems ("how did I do on $TSLA over the last six months?").
   * Filter chips trim the list to winners/losers/large-R only.
   *
   * No new ingest — closed_trades_recent() reads existing FundTrade
   * rows. The notes column already exists in the schema; we're just
   * giving the trader a place to actually write in it.
   */
  import { createQuery, createMutation, useQueryClient } from '@tanstack/svelte-query';
  import { closedPositions, updateJournal, type ClosedTradeRow } from '$api';
  import { base } from '$app/paths';
  import Card from '$components/Card.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import Skeleton from '$components/Skeleton.svelte';
  import TickerLink from '$components/TickerLink.svelte';
  import TradeLifecycle from '$components/TradeLifecycle.svelte';
  import { toast } from '$lib/toast.svelte';
  import { price, pct, timeAgo } from '$lib/format';
  import {
    BookText, Search, TrendingUp, TrendingDown, Award, Save, RotateCcw,
    Clock, Target as TargetIcon, ShieldAlert, ChevronDown, ChevronUp
  } from 'lucide-svelte';

  type Quick = 'all' | 'winners' | 'losers' | 'big_r' | 'no_notes';
  let quick: Quick = $state('all');
  let search = $state('');
  let walletFilter = $state<string>('all');
  let limit = $state(100);
  let expanded = $state(new Set<number>());

  const q = createQuery({
    queryKey: ['positions-closed'],
    queryFn: () => closedPositions({ limit: 250 }),
    refetchInterval: 90_000
  });
  const qc = useQueryClient();

  /** Edit-draft state, keyed by trade id. */
  const drafts = $state<Record<number, string>>({});

  const saveM = createMutation({
    mutationFn: ({ id, notes }: { id: number; notes: string | null }) =>
      updateJournal(id, notes),
    onSuccess: (_res, vars) => {
      toast.success(`Journal saved · #${vars.id}`);
      delete drafts[vars.id];
      qc.invalidateQueries({ queryKey: ['positions-closed'] });
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : String(err))
  });

  const rows = $derived($q.data ?? []);
  const wallets = $derived(
    Array.from(new Set(rows.map((r) => r.fund))).sort()
  );
  const filtered = $derived.by(() => {
    const s = search.trim().toLowerCase();
    return rows.filter((r) => {
      if (walletFilter !== 'all' && r.fund !== walletFilter) return false;
      switch (quick) {
        case 'winners': if ((r.realized_pnl ?? 0) <= 0) return false; break;
        case 'losers':  if ((r.realized_pnl ?? 0) >= 0) return false; break;
        case 'big_r':   if ((r.r_multiple ?? 0) === 0 || Math.abs(r.r_multiple ?? 0) < 2) return false; break;
        case 'no_notes': if ((r.notes ?? '').trim()) return false; break;
      }
      if (!s) return true;
      const hay = [
        r.ticker, r.fund, r.open_reason ?? '', r.close_reason ?? '', r.notes ?? ''
      ].join(' ').toLowerCase();
      return hay.includes(s);
    }).slice(0, limit);
  });

  /** Group trades by YYYY-MM so the user can scan month over month. */
  const grouped = $derived.by(() => {
    const out: { label: string; trades: ClosedTradeRow[] }[] = [];
    let curLabel: string | null = null;
    for (const t of filtered) {
      const dt = t.exit_at ? new Date(t.exit_at) : null;
      const label = dt
        ? dt.toLocaleString(undefined, { month: 'long', year: 'numeric' })
        : 'Unknown';
      if (label !== curLabel) {
        out.push({ label, trades: [] });
        curLabel = label;
      }
      out[out.length - 1].trades.push(t);
    }
    return out;
  });

  // Best / worst trades over the loaded window. Computed on the
  // raw rows (not the active filter) so the leaderboard answers
  // "all-time" not "what filter is active right now".
  const leaderboard = $derived.by(() => {
    if (!rows.length) return null;
    const sortedByPnl = [...rows].sort(
      (a, b) => (b.realized_pnl ?? 0) - (a.realized_pnl ?? 0)
    );
    const winners = sortedByPnl.filter(
      (t) => (t.realized_pnl ?? 0) > 0
    ).slice(0, 3);
    const losers = sortedByPnl.filter(
      (t) => (t.realized_pnl ?? 0) < 0
    ).slice(-3).reverse();
    return { winners, losers };
  });

  // Aggregate stats for the active filter set.
  const stats = $derived.by(() => {
    const ts = filtered;
    if (!ts.length) return null;
    const wins = ts.filter((t) => (t.realized_pnl ?? 0) > 0).length;
    const losses = ts.filter((t) => (t.realized_pnl ?? 0) < 0).length;
    const totalPnl = ts.reduce((s, t) => s + (t.realized_pnl ?? 0), 0);
    const rs = ts.map((t) => t.r_multiple).filter((r): r is number => r !== null);
    const avgR = rs.length ? rs.reduce((s, r) => s + r, 0) / rs.length : null;
    const expectancy = ts.length
      ? ts.reduce((s, t) => s + (t.realized_pct ?? 0), 0) / ts.length
      : 0;
    const noNotes = ts.filter((t) => !(t.notes ?? '').trim()).length;
    return {
      count: ts.length,
      wins,
      losses,
      winRate: ts.length ? (wins / ts.length) * 100 : 0,
      totalPnl,
      avgR,
      expectancy,
      noNotes
    };
  });

  function startEdit(t: ClosedTradeRow) {
    expanded.add(t.id);
    expanded = new Set(expanded);
    if (drafts[t.id] === undefined) drafts[t.id] = t.notes ?? '';
  }
  function cancel(t: ClosedTradeRow) {
    delete drafts[t.id];
  }
  function save(t: ClosedTradeRow) {
    const v = (drafts[t.id] ?? '').trim();
    $saveM.mutate({ id: t.id, notes: v ? v : null });
  }
  function toggle(id: number) {
    if (expanded.has(id)) expanded.delete(id);
    else expanded.add(id);
    expanded = new Set(expanded);
  }
</script>

<svelte:head><title>Journal · Sentinel</title></svelte:head>

<div class="mb-4 flex items-end justify-between gap-3 border-b border-border pb-3">
  <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
    <BookText class="h-5 w-5 text-primary" /><span>Journal</span>
  </h1>
  {#if stats}
    <div class="flex items-baseline gap-3 text-[11px] tabular text-faint">
      <span><span class="text-text">{stats.count}</span> trades</span>
      <span>·</span>
      <span class={stats.totalPnl >= 0 ? 'text-good' : 'text-bad'}>
        {stats.totalPnl >= 0 ? '+' : ''}{stats.totalPnl.toFixed(2)} realised
      </span>
      <span>·</span>
      <span>
        win <span class={stats.winRate >= 50 ? 'text-good' : 'text-bad'}>{stats.winRate.toFixed(0)}%</span>
        ({stats.wins}W/{stats.losses}L)
      </span>
      {#if stats.avgR !== null}
        <span>·</span>
        <span class={stats.avgR >= 0 ? 'text-good' : 'text-bad'}>
          avg {stats.avgR >= 0 ? '+' : ''}{stats.avgR.toFixed(2)}R
        </span>
      {/if}
      {#if stats.expectancy !== 0}
        <span>·</span>
        <span class={stats.expectancy >= 0 ? 'text-good' : 'text-bad'}>
          expectancy {pct(stats.expectancy, 2)}
        </span>
      {/if}
      {#if stats.noNotes > 0}
        <span>·</span>
        <span class="text-warn">{stats.noNotes} unreflected</span>
      {/if}
    </div>
  {/if}
</div>

{#if leaderboard && (leaderboard.winners.length || leaderboard.losers.length)}
  <Card class="mb-3 px-4 py-3">
    <div class="mb-2 flex items-center gap-2">
      <Award class="h-3.5 w-3.5 text-primary" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Leaderboard · last {rows.length} closed trades
      </div>
    </div>
    <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
      <div>
        <div class="mb-1 flex items-center gap-1 text-[9.5px] uppercase tracking-wider text-good">
          <TrendingUp class="h-2.5 w-2.5" /> Top winners
        </div>
        <ul class="space-y-0.5">
          {#each leaderboard.winners as t (t.id)}
            <li class="flex items-center gap-2 rounded border border-border-soft bg-surface-2/40 px-2 py-1 text-[11.5px] tabular">
              <TickerLink ticker={t.ticker} class="font-mono font-semibold text-text" />
              <span class="text-[10px] capitalize text-faint">{t.fund}</span>
              {#if t.hold_h !== null}
                <span class="text-[10px] text-faint">
                  {t.hold_h < 24 ? `${t.hold_h.toFixed(1)}h` : `${(t.hold_h / 24).toFixed(1)}d`}
                </span>
              {/if}
              {#if t.r_multiple !== null}
                <span class={[
                  'rounded px-1 text-[10px]',
                  t.r_multiple >= 0 ? 'bg-good-soft text-good' : 'bg-bad-soft text-bad'
                ].join(' ')}>
                  {t.r_multiple >= 0 ? '+' : ''}{t.r_multiple.toFixed(2)}R
                </span>
              {/if}
              <span class="ml-auto text-[12px] font-semibold text-good">
                +{t.realized_pnl?.toFixed(2)}
              </span>
              <span class="text-[10.5px] text-good">{pct(t.realized_pct, 2)}</span>
            </li>
          {:else}
            <li class="text-[11px] italic text-faint">No winning trades yet.</li>
          {/each}
        </ul>
      </div>
      <div>
        <div class="mb-1 flex items-center gap-1 text-[9.5px] uppercase tracking-wider text-bad">
          <TrendingDown class="h-2.5 w-2.5" /> Worst losers
        </div>
        <ul class="space-y-0.5">
          {#each leaderboard.losers as t (t.id)}
            <li class="flex items-center gap-2 rounded border border-border-soft bg-surface-2/40 px-2 py-1 text-[11.5px] tabular">
              <TickerLink ticker={t.ticker} class="font-mono font-semibold text-text" />
              <span class="text-[10px] capitalize text-faint">{t.fund}</span>
              {#if t.hold_h !== null}
                <span class="text-[10px] text-faint">
                  {t.hold_h < 24 ? `${t.hold_h.toFixed(1)}h` : `${(t.hold_h / 24).toFixed(1)}d`}
                </span>
              {/if}
              {#if t.r_multiple !== null}
                <span class={[
                  'rounded px-1 text-[10px]',
                  t.r_multiple >= 0 ? 'bg-good-soft text-good' : 'bg-bad-soft text-bad'
                ].join(' ')}>
                  {t.r_multiple >= 0 ? '+' : ''}{t.r_multiple.toFixed(2)}R
                </span>
              {/if}
              <span class="ml-auto text-[12px] font-semibold text-bad">
                {t.realized_pnl?.toFixed(2)}
              </span>
              <span class="text-[10.5px] text-bad">{pct(t.realized_pct, 2)}</span>
            </li>
          {:else}
            <li class="text-[11px] italic text-faint">No losing trades yet.</li>
          {/each}
        </ul>
      </div>
    </div>
  </Card>
{/if}

<!-- Filter strip -->
<Card class="flex flex-wrap items-center gap-2 px-4 py-2">
  {#each [
    ['all',      'All',         null],
    ['winners',  'Winners',     TrendingUp],
    ['losers',   'Losers',      TrendingDown],
    ['big_r',    '|R| ≥ 2',     Award],
    ['no_notes', 'No notes yet', BookText]
  ] as [k, label, Icon] (k)}
    <button
      type="button"
      onclick={() => (quick = k as Quick)}
      class={[
        'inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11.5px] transition-colors',
        quick === k
          ? k === 'winners'
            ? 'border-good/40 bg-good-soft text-good'
            : k === 'losers'
              ? 'border-bad/40 bg-bad-soft text-bad'
              : k === 'big_r'
                ? 'border-primary/50 bg-primary-soft text-primary'
                : k === 'no_notes'
                  ? 'border-warn/40 bg-warn-soft text-warn'
                  : 'border-primary/50 bg-primary-soft text-primary'
          : 'border-border bg-surface-2 text-muted hover:text-text'
      ].join(' ')}
    >
      {#if Icon}<Icon class="h-3 w-3" />{/if}
      {label}
    </button>
  {/each}

  {#if wallets.length}
    <span class="ml-2 text-[10px] font-semibold uppercase tracking-wider text-faint">Wallet</span>
    <select
      bind:value={walletFilter}
      class="rounded-md border border-border bg-surface-2 px-2 py-1 text-[11.5px] text-text focus:border-primary/60 focus:outline-none"
    >
      <option value="all">all</option>
      {#each wallets as w (w)}
        <option value={w}>{w}</option>
      {/each}
    </select>
  {/if}

  <div class="ml-auto inline-flex items-center gap-1.5 rounded-md border border-border bg-surface-2 px-2 py-1">
    <Search class="h-3 w-3 text-faint" />
    <input
      type="text"
      bind:value={search}
      placeholder="search notes / reasons / ticker"
      class="w-56 bg-transparent text-[11.5px] text-text placeholder:text-faint/60 focus:outline-none"
    />
  </div>
</Card>

{#if $q.isLoading}
  <Card class="mt-4 px-3 py-3"><Skeleton class="h-9 w-full rounded" lines={9} /></Card>
{:else if !rows.length}
  <Card class="mt-4">
    <EmptyState
      icon={BookText}
      title="No closed trades yet"
      description="As soon as a position closes, it'll appear here for reflection."
    />
  </Card>
{:else if !filtered.length}
  <Card class="mt-4">
    <EmptyState
      icon={BookText}
      title="No trades match this filter"
      description="Try widening the filter or clearing the search."
    />
  </Card>
{:else}
  <div class="mt-3 space-y-4">
    {#each grouped as group (group.label)}
      <div>
        <div class="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-faint">
          {group.label} <span class="ml-1 normal-case tracking-normal">· {group.trades.length} trade{group.trades.length === 1 ? '' : 's'}</span>
        </div>
        <Card class="divide-y divide-border-soft">
          {#each group.trades as t (t.id)}
            {@const isExpanded = expanded.has(t.id)}
            {@const isEditing = drafts[t.id] !== undefined}
            {@const win = (t.realized_pnl ?? 0) > 0}
            <div class="px-3 py-2 transition-colors even:bg-white/[0.015] hover:bg-white/[0.03]">
              <!-- Row header -->
              <button
                type="button"
                onclick={() => toggle(t.id)}
                class="flex w-full items-center gap-3 text-left"
              >
                <span class="flex w-2 justify-center">
                  {#if isExpanded}
                    <ChevronUp class="h-3 w-3 text-faint" />
                  {:else}
                    <ChevronDown class="h-3 w-3 text-faint" />
                  {/if}
                </span>
                <span class="w-12 flex-none text-[10px] uppercase text-faint">{t.fund}</span>
                <TickerLink ticker={t.ticker} class="w-20 flex-none text-[13px]" />
                <span class={[
                  'w-10 flex-none rounded px-1 py-0.5 text-center text-[9.5px] font-semibold uppercase tracking-wider',
                  t.side === 'long' ? 'bg-good-soft text-good' : 'bg-bad-soft text-bad'
                ].join(' ')}>{t.side}</span>
                <span class="flex-1 truncate text-[11px] text-muted">
                  {price(t.entry)} → {price(t.exit)}
                  <span class="ml-2 text-faint">
                    {#if t.hold_h !== null}
                      <Clock class="mr-0.5 inline h-2.5 w-2.5" />
                      {t.hold_h < 24 ? `${t.hold_h.toFixed(1)}h` : `${(t.hold_h / 24).toFixed(1)}d`}
                    {/if}
                  </span>
                  {#if t.close_reason}
                    <span class="ml-2 text-faint italic">· {t.close_reason}</span>
                  {/if}
                </span>
                {#if t.r_multiple !== null}
                  <span class={[
                    'w-16 flex-none text-right text-[12px] tabular',
                    t.r_multiple >= 0 ? 'text-good' : 'text-bad'
                  ].join(' ')}>
                    {t.r_multiple >= 0 ? '+' : ''}{t.r_multiple.toFixed(2)}R
                  </span>
                {:else}
                  <span class="w-16 flex-none text-right text-[10.5px] text-faint">—</span>
                {/if}
                <span class={[
                  'w-20 flex-none text-right text-[12.5px] tabular font-semibold',
                  win ? 'text-good' : 'text-bad'
                ].join(' ')}>
                  {win ? '+' : ''}{t.realized_pnl?.toFixed(2) ?? '—'}
                </span>
                <span class={[
                  'w-14 flex-none text-right text-[11px] tabular',
                  win ? 'text-good' : 'text-bad'
                ].join(' ')}>
                  {pct(t.realized_pct, 2)}
                </span>
                {#if t.notes && t.notes.trim()}
                  <BookText class="h-3 w-3 text-primary" />
                {/if}
              </button>

              {#if isExpanded}
                <div class="mt-2 grid grid-cols-1 gap-3 pl-5 md:grid-cols-2">
                  <!-- Lifecycle details -->
                  <div class="rounded-md border border-border bg-surface-2/40 p-2 text-[11px]">
                    <div class="mb-1 text-[9.5px] font-semibold uppercase tracking-wider text-faint">
                      Lifecycle
                    </div>
                    <div class="grid grid-cols-2 gap-x-3 gap-y-1 tabular text-muted">
                      <div>opened</div>
                      <div class="text-right text-text">
                        {timeAgo(t.entry_at)}
                        <span class="ml-1 text-[9.5px] text-faint">
                          {new Date(t.entry_at).toLocaleDateString()}
                        </span>
                      </div>
                      <div>closed</div>
                      <div class="text-right text-text">
                        {t.exit_at ? timeAgo(t.exit_at) : '—'}
                        {#if t.exit_at}
                          <span class="ml-1 text-[9.5px] text-faint">
                            {new Date(t.exit_at).toLocaleDateString()}
                          </span>
                        {/if}
                      </div>
                      <div>qty</div>
                      <div class="text-right text-text">{t.qty}</div>
                      <div>notional</div>
                      <div class="text-right text-text">{price(t.notional)}</div>
                      {#if t.stop_price}
                        <div class="flex items-center gap-1"><ShieldAlert class="h-2.5 w-2.5 text-warn" /> stop</div>
                        <div class="text-right text-text">{price(t.stop_price)}</div>
                      {/if}
                      {#if t.target_price}
                        <div class="flex items-center gap-1"><TargetIcon class="h-2.5 w-2.5 text-good" /> target</div>
                        <div class="text-right text-text">{price(t.target_price)}</div>
                      {/if}
                      {#if t.open_reason}
                        <div>open reason</div>
                        <div class="text-right text-faint italic">{t.open_reason}</div>
                      {/if}
                    </div>
                    {#if t.call_id !== null}
                      <a
                        href={`${base}/calls?id=${t.call_id}`}
                        class="mt-1 inline-block text-[10.5px] text-primary hover:underline"
                      >→ Originating call #{t.call_id}</a>
                    {/if}
                  </div>

                  <!-- Reflection editor -->
                  <div class="rounded-md border border-border bg-surface-2/40 p-2 text-[11px]">
                    <div class="mb-1 flex items-center gap-2">
                      <span class="text-[9.5px] font-semibold uppercase tracking-wider text-faint">
                        Reflection
                      </span>
                      {#if !isEditing}
                        <button
                          type="button"
                          onclick={() => startEdit(t)}
                          class="ml-auto text-[10px] text-primary hover:underline"
                        >{t.notes ? 'Edit' : 'Add'}</button>
                      {/if}
                    </div>
                    {#if isEditing}
                      <textarea
                        bind:value={drafts[t.id]}
                        rows="4"
                        placeholder="What went right? What went wrong? What's the lesson?"
                        class="w-full resize-y rounded border border-border bg-surface-2 px-2 py-1.5 text-[11.5px] text-text placeholder:text-faint/60 focus:border-primary/60 focus:outline-none"
                      ></textarea>
                      <div class="mt-1 flex items-center gap-2">
                        <button
                          type="button"
                          onclick={() => save(t)}
                          disabled={$saveM.isPending}
                          class="inline-flex items-center gap-1 rounded border border-primary/40 bg-primary-soft px-2 py-1 text-[10.5px] text-primary hover:bg-primary/15 disabled:opacity-50"
                        ><Save class="h-3 w-3" /> Save</button>
                        <button
                          type="button"
                          onclick={() => cancel(t)}
                          class="inline-flex items-center gap-1 rounded border border-border bg-bg px-2 py-1 text-[10.5px] text-muted hover:text-text"
                        ><RotateCcw class="h-3 w-3" /> Cancel</button>
                      </div>
                    {:else if t.notes}
                      <div class="whitespace-pre-wrap text-[11.5px] leading-snug text-text">
                        {t.notes}
                      </div>
                    {:else}
                      <div class="text-[11px] italic text-faint">
                        No reflection yet — click "Add" to write one.
                      </div>
                    {/if}
                  </div>
                </div>

                <!-- What the bot saw during this trade -->
                <div class="mt-2 pl-5">
                  <TradeLifecycle tradeId={t.id} />
                </div>
              {/if}
            </div>
          {/each}
        </Card>
      </div>
    {/each}

    {#if filtered.length === limit && rows.length > limit}
      <div class="flex justify-center">
        <button
          type="button"
          onclick={() => (limit += 100)}
          class="rounded-md border border-border bg-surface-2 px-3 py-1.5 text-[11px] text-muted hover:text-text"
        >Show more</button>
      </div>
    {/if}
  </div>
{/if}
