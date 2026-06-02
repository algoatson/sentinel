<script lang="ts">
  /**
   * /book — pro-grade position view across every wallet.
   *
   * Inspired by ThinkOrSwim's Monitor tab + TradingView's Portfolio
   * widget. Features that exist on real platforms and are now here:
   *  - Aggregate header (count, uPnL, winners/losers, longs/shorts,
   *    notional, average R-multiple)
   *  - Wallet / side / ticker filters
   *  - Sortable 12-column table with R-multiple + %-equity
   *  - Visual risk-reward bar per row (red distance-to-stop +
   *    green distance-to-target)
   *  - Per-position drawer: set stop / target / trailing stop / notes,
   *    plus inline close
   *  - Multi-select checkboxes + Bulk Close in the header
   *  - CSV export (1-click)
   *
   * Refreshes every 30s + invalidated by SSE trade events. The
   * auto_exits pipeline closes positions when stops/targets fire.
   */
  import { createQuery, createMutation, useQueryClient } from '@tanstack/svelte-query';
  import { reactiveQueryOptions } from '$lib/reactive-query.svelte';
  import { openPositions, closePosition, updateRisk, bulkClose, csvExportUrl, wallets as walletsApi, tickerAtr } from '$api';
  import type { OpenPositionRow } from '$api';
  import OpenPositionDrawer from '$components/OpenPositionDrawer.svelte';
  import PositionHeatmap from '$components/PositionHeatmap.svelte';
  import TradeLifecycle from '$components/TradeLifecycle.svelte';
  import Card from '$components/Card.svelte';
  import Pill from '$components/Pill.svelte';
  import TickerLink from '$components/TickerLink.svelte';
  import Spinner from '$components/Spinner.svelte';
  import Skeleton from '$components/Skeleton.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import Drawer from '$components/Drawer.svelte';
  import { toast } from '$lib/toast.svelte';
  import { usd, price, timeAgo } from '$lib/format';
  import {
    Briefcase, AlertCircle, X, Download, Shield, Target as TargetIcon,
    Edit3, TrendingUp, TrendingDown, Save, Layers, MoreVertical, Plus,
    LayoutGrid, List, AlertTriangle, Wand2
  } from 'lucide-svelte';

  type SortKey =
    | 'fund' | 'ticker' | 'side' | 'age' | 'entry' | 'mark'
    | 'upnl' | 'upnl_pct' | 'r' | 'pct_eq' | 'notional';

  let sortKey: SortKey = $state('upnl');
  let sortDir: 'asc' | 'desc' = $state('desc');
  let fundFilter = $state('all');
  let sideFilter: 'all' | 'long' | 'short' = $state('all');
  let tickerFilter = $state('');
  let onlyHasRisk = $state(false);
  type QuickFilter = 'all' | 'winners' | 'losers' | 'near_stop' | 'naked';
  let quick: QuickFilter = $state('all');
  let viewMode: 'table' | 'heatmap' = $state('table');
  /** "Near stop" threshold matches the Risk Monitor card. */
  const NEAR_STOP_PCT = 1.5;

  let confirmId = $state<number | null>(null);
  let bulkConfirm = $state(false);
  // "Cut losers" / "Lock winners" route through an explicit confirm modal
  // (previously they silently armed the toolbar bulk-confirm far away, which
  // read as "the button does nothing").
  let bulkAction = $state<{ kind: 'losers' | 'winners'; ids: number[] } | null>(null);
  /** When true, clicking a row toggles selection. When false, click
   * opens the drawer. Mirrors how IBKR TWS / TOS work — a "select
   * mode" pill keeps the day-to-day click-to-inspect flow clean. */
  let selectMode = $state(false);
  const selected = $state(new Set<number>());

  let drawerId = $state<number | null>(null);
  let openTradeOpen = $state(false);

  const walletsQ = createQuery({
    queryKey: ['wallets'],
    queryFn: walletsApi,
    refetchInterval: 60_000
  });
  // Risk-form draft. The numeric fields bind to <input type="number">, which
  // writes a `number` back (or `null` when the field is empty) — NOT a string.
  // (The old `string` typing here was the bug: saveRisk called `.trim()` on the
  // value, which throws on a number/null, so editing a stop/target then saving
  // silently did nothing.) `null` = field cleared.
  let dStop = $state<number | null>(null);
  let dTarget = $state<number | null>(null);
  let dTrail = $state<number | null>(null); // whole percent, e.g. 10 = 10%
  let dNotes = $state<string>('');

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
      drawerId = null;
    },
    onError: (err) =>
      toast.error(err instanceof Error ? err.message : String(err))
  });

  const bulkM = createMutation({
    mutationFn: ({ ids, reason }: { ids: number[]; reason: string }) =>
      bulkClose(ids, reason),
    onSuccess: (res) => {
      const pnl = res.total_realized_pnl;
      toast.success(
        `Closed ${res.closed} / ${res.attempted} · realised ${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}`
      );
      selected.clear();
      bulkConfirm = false;
      bulkAction = null;
      qc.invalidateQueries({ queryKey: ['positions-open'] });
      qc.invalidateQueries({ queryKey: ['wallets'] });
      qc.invalidateQueries({ queryKey: ['kpi'] });
      qc.invalidateQueries({ queryKey: ['risk-monitor'] });
    },
    onError: (err) =>
      toast.error(err instanceof Error ? err.message : String(err))
  });

  const riskM = createMutation({
    mutationFn: ({ id, body }: { id: number; body: Parameters<typeof updateRisk>[1] }) =>
      updateRisk(id, body),
    onSuccess: () => {
      toast.success('Risk settings saved');
      qc.invalidateQueries({ queryKey: ['positions-open'] });
      qc.invalidateQueries({ queryKey: ['risk-monitor'] });
    },
    onError: (err) =>
      toast.error(err instanceof Error ? err.message : String(err))
  });

  // Inline "2×ATR" quick stop on naked rows — fetch ATR for the
  // ticker, pick the stop on the correct side of entry, PATCH.
  // Tracks per-row pending state so the button shows a spinner
  // without freezing other rows.
  let atrPending = $state(new Set<number>());
  async function setAtrStop(p: OpenPositionRow) {
    atrPending.add(p.id);
    atrPending = new Set(atrPending);
    try {
      const atr = await tickerAtr(p.ticker, 14);
      const stop = p.side === 'long'
        ? atr.suggested_long_stop
        : atr.suggested_short_stop;
      if (stop === null || !Number.isFinite(stop) || stop <= 0) {
        toast.error(`No usable ATR for $${p.ticker}`);
        return;
      }
      await updateRisk(p.id, { stop_price: stop });
      toast.success(
        `Stop set on $${p.ticker} @ ${stop.toFixed(2)} (2×ATR)`
      );
      qc.invalidateQueries({ queryKey: ['positions-open'] });
      qc.invalidateQueries({ queryKey: ['risk-monitor'] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      atrPending.delete(p.id);
      atrPending = new Set(atrPending);
    }
  }

  const drawerRow = $derived(
    drawerId !== null
      ? ($positionsQ.data ?? []).find((p) => p.id === drawerId) ?? null
      : null
  );

  // Pre-seed the risk-form draft when a row opens.
  $effect(() => {
    if (drawerRow) {
      dStop = drawerRow.stop_price ?? null;
      dTarget = drawerRow.target_price ?? null;
      dTrail =
        drawerRow.trailing_stop_pct != null
          ? Math.round(drawerRow.trailing_stop_pct * 100)
          : null;
      dNotes = drawerRow.notes ?? '';
    }
  });

  function saveRisk() {
    if (drawerId === null) return;
    const body: Parameters<typeof updateRisk>[1] = { clear: [] };
    if (typeof dStop === 'number' && dStop > 0) body.stop_price = dStop;
    else body.clear!.push('stop_price');
    if (typeof dTarget === 'number' && dTarget > 0) body.target_price = dTarget;
    else body.clear!.push('target_price');
    const trail = typeof dTrail === 'number' ? dTrail / 100 : NaN;
    if (Number.isFinite(trail) && trail > 0 && trail < 1)
      body.trailing_stop_pct = trail;
    else body.clear!.push('trailing_stop_pct');
    if (dNotes.trim()) body.notes = dNotes.trim();
    else body.clear!.push('notes');
    $riskM.mutate({ id: drawerId, body });
  }

  // ── drawer risk helpers (quick presets + live R:R preview) ──────────────
  /** Stop price `pct`% on the loss side of entry, rounded to a cent. */
  function stopFromPct(pct: number): number {
    const e = drawerRow!.entry;
    const px = drawerRow!.side === 'long' ? e * (1 - pct / 100) : e * (1 + pct / 100);
    return Math.round(px * 100) / 100;
  }
  /** Target at `r` × (entry→stop distance) on the profit side. Needs a stop. */
  function targetFromR(r: number): number | null {
    if (typeof dStop !== 'number' || dStop <= 0) return null;
    const risk = Math.abs(drawerRow!.entry - dStop);
    if (!risk) return null;
    const px = drawerRow!.side === 'long'
      ? drawerRow!.entry + r * risk
      : drawerRow!.entry - r * risk;
    return px > 0 ? Math.round(px * 100) / 100 : null;
  }
  /** Live risk/reward preview from the current draft, in $ and R:R. */
  const rrPreview = $derived.by(() => {
    if (!drawerRow) return null;
    const qty = drawerRow.qty;
    const entry = drawerRow.entry;
    const riskPx = typeof dStop === 'number' && dStop > 0 ? Math.abs(entry - dStop) : null;
    const rewardPx =
      typeof dTarget === 'number' && dTarget > 0 ? Math.abs(dTarget - entry) : null;
    return {
      risk$: riskPx !== null ? riskPx * qty : null,
      riskPct: riskPx !== null ? (riskPx / entry) * 100 : null,
      reward$: rewardPx !== null ? rewardPx * qty : null,
      rr: riskPx && rewardPx ? rewardPx / riskPx : null
    };
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
      case 'r':        return p.r_multiple ?? -Infinity;
      case 'pct_eq':   return p.pct_of_equity;
      case 'notional': return p.notional;
    }
  }

  const sorted = $derived(
    [...($positionsQ.data ?? [])]
      .filter((p) => {
        if (fundFilter !== 'all' && p.fund !== fundFilter) return false;
        if (sideFilter !== 'all' && p.side !== sideFilter) return false;
        if (onlyHasRisk && p.stop_price == null && p.target_price == null && p.trailing_stop_pct == null) return false;
        const t = tickerFilter.trim().toUpperCase().replace(/^\$/, '');
        if (t && p.ticker !== t) return false;
        switch (quick) {
          case 'winners':
            if ((p.upnl ?? 0) <= 0) return false;
            break;
          case 'losers':
            if ((p.upnl ?? 0) >= 0) return false;
            break;
          case 'near_stop':
            if (
              p.dist_to_stop_pct == null ||
              p.dist_to_stop_pct < 0 ||
              p.dist_to_stop_pct > NEAR_STOP_PCT
            ) return false;
            break;
          case 'naked':
            if (p.stop_price != null) return false;
            break;
        }
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
    const notional = ps.reduce((s, p) => s + p.notional, 0);
    const wins = ps.filter((p) => p.upnl > 0).length;
    const losses = ps.filter((p) => p.upnl < 0).length;
    const longs = ps.filter((p) => p.side === 'long').length;
    const shorts = ps.filter((p) => p.side === 'short').length;
    const rs = ps.map((p) => p.r_multiple).filter((r): r is number => r !== null);
    const avgR = rs.length ? rs.reduce((s, r) => s + r, 0) / rs.length : null;
    const stopped = ps.filter((p) => p.stop_price != null).length;
    return {
      upnl, notional, wins, losses, longs, shorts,
      count: ps.length, avgR, stopped
    };
  });

  function toggleAll() {
    if (selected.size === sorted.length) {
      selected.clear();
    } else {
      sorted.forEach((p) => selected.add(p.id));
    }
  }
  function toggle(id: number) {
    if (selected.has(id)) selected.delete(id);
    else selected.add(id);
  }
</script>

<svelte:head><title>Book · Sentinel</title></svelte:head>

<!-- Page header — was a 4-line subtitle of feature-list docs ("CSV
     export, bulk close, R-multiple, risk-reward bars"). That belongs
     in the README, not above the working surface. Header now: title +
     primary action (Open trade). Cut/Lock/Export demoted into a kebab
     overflow so the row stays uncluttered. -->
<div class="mb-4 flex items-end justify-between gap-3 border-b border-border pb-3">
  <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
    <Briefcase class="h-5 w-5 text-primary" /><span>Book</span>
  </h1>

  <div class="relative flex items-center gap-1.5">
    <button
      type="button"
      onclick={() => (openTradeOpen = true)}
      class="flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary-soft px-2.5 py-1.5 text-[11.5px] font-medium text-primary transition-colors hover:bg-primary/15"
    >
      <Plus class="h-3 w-3" />
      Open trade
    </button>

    <details class="relative">
      <summary
        class="flex cursor-pointer list-none items-center gap-1 rounded-md border border-border bg-surface-2 px-2 py-1.5 text-[11px] text-muted transition-colors hover:border-primary/40 hover:text-text [&::-webkit-details-marker]:hidden"
        title="More book actions"
      >
        <MoreVertical class="h-3.5 w-3.5" />
      </summary>
      <div
        class="absolute right-0 top-full z-30 mt-1.5 w-44 rounded-md border border-border bg-surface shadow-xl"
      >
        <button
          type="button"
          onclick={(e) => {
            (e.currentTarget.closest('details') as HTMLDetailsElement | null)?.removeAttribute('open');
            const losers = ($positionsQ.data ?? []).filter((p) => p.upnl < 0).map((p) => p.id);
            if (!losers.length) { toast.info('No losing positions to close'); return; }
            bulkAction = { kind: 'losers', ids: losers };
          }}
          class="flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] text-bad hover:bg-bad-soft"
        >
          <TrendingDown class="h-3.5 w-3.5" />
          Cut losers
        </button>
        <button
          type="button"
          onclick={(e) => {
            (e.currentTarget.closest('details') as HTMLDetailsElement | null)?.removeAttribute('open');
            const winners = ($positionsQ.data ?? []).filter((p) => p.upnl > 0).map((p) => p.id);
            if (!winners.length) { toast.info('No winning positions to lock in'); return; }
            bulkAction = { kind: 'winners', ids: winners };
          }}
          class="flex w-full items-center gap-2 border-t border-border px-3 py-2 text-left text-[12px] text-good hover:bg-good-soft"
        >
          <TrendingUp class="h-3.5 w-3.5" />
          Lock winners
        </button>
        <a
          href={csvExportUrl}
          download
          class="flex items-center gap-2 border-t border-border px-3 py-2 text-[12px] text-muted hover:bg-surface-2 hover:text-text"
        >
          <Download class="h-3.5 w-3.5" />
          Export CSV
        </a>
      </div>
    </details>
  </div>
</div>

<!-- ── headline aggregates ───────────────────────────
     Was 6. Dropped Winners/Losers — its two numbers now ride along
     in the uPnL tile's secondary line as "X up · Y down" (no number
     lost, just compacted). Kept Notional: this is the one page in
     the app where total $ exposure is visible at a glance — there's
     no other surface for it. Final five tiles are all decision-
     relevant: open count + L/S split, where uPnL sits, the avg R of
     risk-defined trades (quality reading), how much is deployed,
     and whether stops are set everywhere. -->
<div class="grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-5">
  <div class="card-lift rounded-lg border border-border bg-surface px-3 py-2">
    <div class="text-[10px] uppercase tracking-wider text-faint">Open positions</div>
    <div class="mt-0.5 text-[18px] font-semibold tabular text-text">{totals.count}</div>
    <div class="text-[10.5px] tabular text-faint">
      <span class="text-good">{totals.longs} L</span> · <span class="text-bad">{totals.shorts} S</span>
    </div>
  </div>

  <div class={[
    'card-lift rounded-lg border px-3 py-2',
    totals.upnl > 0 ? 'border-good/25 bg-gradient-to-b from-good-soft/30 to-surface'
    : totals.upnl < 0 ? 'border-bad/25 bg-gradient-to-b from-bad-soft/30 to-surface'
    : 'border-border bg-surface'
  ].join(' ')}>
    <div class="text-[10px] uppercase tracking-wider text-faint">Aggregate uPnL</div>
    <div class={[
      'mt-0.5 text-[18px] font-semibold tabular',
      totals.upnl > 0 ? 'text-good' : totals.upnl < 0 ? 'text-bad' : 'text-text'
    ].join(' ')}>
      {usd(totals.upnl, true)}
    </div>
    <div class="text-[10.5px] tabular text-faint">
      <span class="text-good">{totals.wins} up</span> · <span class="text-bad">{totals.losses} down</span>
    </div>
  </div>

  <div class={[
    'card-lift rounded-lg border px-3 py-2',
    (totals.avgR ?? 0) > 0 ? 'border-good/25 bg-gradient-to-b from-good-soft/30 to-surface'
    : (totals.avgR ?? 0) < 0 ? 'border-bad/25 bg-gradient-to-b from-bad-soft/30 to-surface'
    : 'border-border bg-surface'
  ].join(' ')}>
    <div class="text-[10px] uppercase tracking-wider text-faint">Avg R</div>
    <div class={[
      'mt-0.5 text-[18px] font-semibold tabular',
      (totals.avgR ?? 0) > 0 ? 'text-good' : (totals.avgR ?? 0) < 0 ? 'text-bad' : 'text-text'
    ].join(' ')}>
      {totals.avgR !== null ? (totals.avgR >= 0 ? '+' : '') + totals.avgR.toFixed(2) + 'R' : '—'}
    </div>
    <div class="text-[10.5px] tabular text-faint">across stop-set positions</div>
  </div>

  <div class="card-lift rounded-lg border border-border bg-surface px-3 py-2">
    <div class="text-[10px] uppercase tracking-wider text-faint">Notional</div>
    <div class="mt-0.5 text-[18px] font-semibold tabular text-text">{usd(totals.notional)}</div>
    <div class="text-[10.5px] tabular text-faint">total exposure</div>
  </div>

  <div class={[
    'card-lift rounded-lg border px-3 py-2',
    totals.count === 0 ? 'border-border bg-surface'
    : totals.stopped === totals.count ? 'border-good/25 bg-gradient-to-b from-good-soft/30 to-surface'
    : totals.stopped >= totals.count * 0.5 ? 'border-warn/25 bg-gradient-to-b from-warn-soft/30 to-surface'
    : 'border-bad/25 bg-gradient-to-b from-bad-soft/30 to-surface'
  ].join(' ')}>
    <div class="text-[10px] uppercase tracking-wider text-faint">Risk-defined</div>
    <div class={[
      'mt-0.5 text-[18px] font-semibold tabular',
      totals.count === 0 ? 'text-text' :
      totals.stopped === totals.count ? 'text-good' :
      totals.stopped >= totals.count * 0.5 ? 'text-warn' : 'text-bad'
    ].join(' ')}>
      {totals.stopped}<span class="text-[12px] text-faint"> / {totals.count}</span>
    </div>
    <div class="text-[10.5px] tabular text-faint">have stop set</div>
  </div>
</div>

<!-- ── unified toolbar ─────────────────────────────
     Was two separate Card rows ("quick filter strip" + "filter
     ribbon") which together held: 5 quick-filter chips, view toggle,
     wallet select, side chips, has-stop checkbox, select-mode chip,
     ticker input, bulk-action area. Two Cards stacked on top of each
     other with the same kind of content is the worst kind of clutter
     — equal weight, nothing leads.

     Now: one row, grouped left→right by intent:
       1. Quick filters (the most common day-to-day cut)
       2. Wallet · Side · Search · Has-stop (refine the cut)
       3. View toggle + bulk-action area (right-anchored)
     Eyebrow labels removed — the buttons are self-explanatory and
     the labels were the loudest thing on the row. -->
<Card class="mt-3 px-3 py-2">
  <div class="flex flex-wrap items-center gap-x-1.5 gap-y-1.5">
    {#each [
      ['all',       'All',       null],
      ['winners',   'Winners',   TrendingUp],
      ['losers',    'Losers',    TrendingDown],
      ['near_stop', 'Near stop', AlertTriangle],
      ['naked',     'No stop',   Shield]
    ] as [k, label, Icon] (k)}
      <button
        type="button"
        onclick={() => (quick = k as QuickFilter)}
        class={[
          'inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] transition-colors',
          quick === k
            ? k === 'winners'
              ? 'border-good/40 bg-good-soft text-good'
              : k === 'losers' || k === 'near_stop'
                ? 'border-bad/40 bg-bad-soft text-bad'
                : k === 'naked'
                  ? 'border-warn/40 bg-warn-soft text-warn'
                  : 'border-primary/50 bg-primary-soft text-primary'
            : 'border-border bg-surface-2 text-muted hover:text-text'
        ].join(' ')}
      >
        {#if Icon}<Icon class="h-3 w-3" />{/if}
        {label}
      </button>
    {/each}

    <span class="mx-1 h-5 w-px bg-border"></span>

    <select
      bind:value={fundFilter}
      title="Filter by wallet"
      class="rounded-md border border-border bg-surface-2 px-2 py-1 text-[11.5px] text-text focus:border-primary/60 focus:outline-none"
    >
      <option value="all">all wallets</option>
      {#each funds as f (f)}<option value={f}>{f}</option>{/each}
    </select>

    {#each [['all', 'All'], ['long', 'L'], ['short', 'S']] as [k, label] (k)}
      <button
        type="button"
        onclick={() => (sideFilter = k as any)}
        title={k === 'long' ? 'Longs only' : k === 'short' ? 'Shorts only' : 'Both sides'}
        class={[
          'w-8 rounded-md border px-1.5 py-1 text-[11px] transition-colors',
          sideFilter === k
            ? k === 'long' ? 'border-good/40 bg-good-soft text-good'
            : k === 'short' ? 'border-bad/40 bg-bad-soft text-bad'
            : 'border-primary/50 bg-primary-soft text-primary'
            : 'border-border bg-surface-2 text-muted hover:text-text'
        ].join(' ')}
      >{label}</button>
    {/each}

    <input
      type="text"
      bind:value={tickerFilter}
      placeholder="$ticker"
      class="w-24 rounded-md border border-border bg-surface-2 px-2 py-1 font-mono text-[11.5px] text-text placeholder:text-faint/50 focus:border-primary/60 focus:outline-none"
    />

    <label class="flex cursor-pointer items-center gap-1 rounded-md border border-border bg-surface-2 px-2 py-1 text-[11px]" title="Show only positions with a stop OR target set">
      <input type="checkbox" bind:checked={onlyHasRisk} class="h-3 w-3 cursor-pointer accent-primary" />
      <Shield class="h-3 w-3 text-faint" />
      <span class={onlyHasRisk ? 'text-text' : 'text-muted'}>risk-set</span>
    </label>

    <button
      type="button"
      onclick={() => { selectMode = !selectMode; if (!selectMode) selected.clear(); }}
      title="Toggle multi-select rows for bulk actions"
      class={[
        'flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] transition-colors',
        selectMode ? 'border-primary/50 bg-primary-soft text-primary' : 'border-border bg-surface-2 text-muted hover:text-text'
      ].join(' ')}
    >
      <Layers class="h-3 w-3" />
      {selectMode ? 'Selecting' : 'Select'}
    </button>

    <div class="ml-auto flex items-center gap-2">
      {#if selected.size > 0}
        <div class="flex items-center gap-1.5 rounded-md border border-bad/40 bg-bad-soft px-2 py-1 text-[11px] text-bad">
          <Layers class="h-3 w-3" />
          <span>{selected.size} selected</span>
          {#if bulkConfirm}
            <button
              type="button"
              onclick={() => $bulkM.mutate({ ids: [...selected], reason: 'bulk via /book' })}
              disabled={$bulkM.isPending}
              class="rounded border border-bad bg-bad/30 px-2 py-0.5 font-medium text-text hover:bg-bad/40 disabled:opacity-50"
            >Confirm × {selected.size}</button>
            <button type="button" onclick={() => (bulkConfirm = false)} class="rounded border border-border bg-bg px-2 py-0.5 text-muted hover:text-text">Cancel</button>
          {:else}
            <button type="button" onclick={() => (bulkConfirm = true)} class="rounded border border-bad bg-bad/20 px-2 py-0.5 font-medium hover:bg-bad/30">Close</button>
            <button type="button" onclick={() => selected.clear()} class="text-muted hover:text-text" title="Clear selection"><X class="h-3 w-3" /></button>
          {/if}
        </div>
      {:else}
        <span class="text-[11px] tabular text-faint">{sorted.length} of {$positionsQ.data?.length ?? 0}</span>
      {/if}

      <div class="inline-flex overflow-hidden rounded-md border border-border">
        <button type="button" onclick={() => (viewMode = 'table')} title="Table" class={['inline-flex items-center gap-1 px-2 py-1 text-[11px] transition-colors', viewMode === 'table' ? 'bg-primary-soft text-primary' : 'bg-surface-2 text-muted hover:text-text'].join(' ')}><List class="h-3 w-3" /></button>
        <button type="button" onclick={() => (viewMode = 'heatmap')} title="Heatmap" class={['inline-flex items-center gap-1 border-l border-border px-2 py-1 text-[11px] transition-colors', viewMode === 'heatmap' ? 'bg-primary-soft text-primary' : 'bg-surface-2 text-muted hover:text-text'].join(' ')}><LayoutGrid class="h-3 w-3" /></button>
      </div>
    </div>
  </div>
</Card>

{#if viewMode === 'heatmap'}
  <Card class="mt-3 px-3 py-3">
    {#if $positionsQ.isLoading}
      <div class="space-y-2 py-3"><Skeleton class="h-6 w-full rounded" lines={8} /></div>
    {:else}
      <div class="mb-2 flex items-baseline gap-3">
        <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
          Heatmap · area = notional, colour = uPnL %
        </div>
        <span class="text-[10.5px] text-faint">
          {sorted.length} position{sorted.length === 1 ? '' : 's'} shown
        </span>
      </div>
      <PositionHeatmap positions={sorted} height={420} />
    {/if}
  </Card>
{/if}

<!-- ── table ───────────────────────────────── -->
<Card class={['mt-3 overflow-hidden', viewMode === 'heatmap' ? 'hidden' : ''].join(' ')}>
  {#if $positionsQ.isLoading}
    <div class="space-y-2 py-3"><Skeleton class="h-6 w-full rounded" lines={10} /></div>
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
            {#if selectMode}
              <th class="w-8 px-2 py-2 text-center">
                <input
                  type="checkbox"
                  checked={selected.size === sorted.length && sorted.length > 0}
                  indeterminate={selected.size > 0 && selected.size < sorted.length}
                  onchange={toggleAll}
                  class="h-3 w-3 cursor-pointer accent-primary"
                />
              </th>
            {/if}
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
            {@render th('Entry', 'entry')}
            {@render th('Mark', 'mark')}
            {@render th('uPnL', 'upnl')}
            {@render th('%', 'upnl_pct')}
            {@render th('R', 'r')}
            {@render th('% Eq', 'pct_eq')}
            <th class="px-3 py-2 text-left font-semibold">Risk · Note</th>
            {@render th('Age', 'age')}
          </tr>
        </thead>
        <tbody>
          {#each sorted as p (p.id)}
            {@const checked = selected.has(p.id)}
            {@const hasRisk =
              p.stop_price !== null || p.target_price !== null || p.trailing_stop_pct !== null}
            {@const stopWarn =
              p.dist_to_stop_pct !== null && p.dist_to_stop_pct < 5}
            <tr
              class={[
                'group cursor-pointer border-b border-border-soft transition-colors',
                checked
                  ? 'bg-primary-soft/40 hover:bg-primary-soft/50'
                  : 'hover:bg-white/[0.03]'
              ].join(' ')}
              onclick={(e) => {
                // Don't hijack the explicit Close confirmation flow
                if (confirmId === p.id) return;
                if (selectMode) toggle(p.id);
                else drawerId = p.id;
              }}
            >
              {#if selectMode}
                <td class="px-2 py-1.5 text-center" onclick={(e) => e.stopPropagation()}>
                  <input
                    type="checkbox"
                    {checked}
                    onchange={() => toggle(p.id)}
                    class="h-3 w-3 cursor-pointer accent-primary"
                  />
                </td>
              {/if}
              <td class="px-3 py-2 text-left text-muted capitalize" title={p.fund_mandate}>{p.fund}</td>
              <td class="px-3 py-2 text-left" onclick={(e) => e.stopPropagation()}>
                <TickerLink ticker={p.ticker} />
              </td>
              <td class="px-3 py-2 text-left">
                <Pill variant={p.side === 'long' ? 'pos' : 'neg'}>{p.side.toUpperCase()}</Pill>
              </td>
              <td class="px-3 py-2 text-right">{price(p.entry)}</td>
              <td class={['px-3 py-2 text-right', p.mark_live ? '' : 'text-faint'].join(' ')}
                  title={p.mark_live ? '' : 'No live quote — falls back to entry'}>
                {price(p.mark)}{p.mark_live ? '' : '*'}
              </td>
              <td class={['px-3 py-2 text-right font-medium', p.upnl >= 0 ? 'text-good' : 'text-bad'].join(' ')}>
                {usd(p.upnl, true)}
              </td>
              <td class={['px-3 py-2 text-right', p.upnl_pct >= 0 ? 'text-good' : 'text-bad'].join(' ')}>
                {p.upnl_pct >= 0 ? '+' : ''}{p.upnl_pct.toFixed(2)}%
              </td>
              <td class={[
                'px-3 py-2 text-right font-medium',
                p.r_multiple === null ? 'text-faint'
                  : p.r_multiple >= 1 ? 'text-good'
                  : p.r_multiple <= -0.7 ? 'text-bad' : 'text-text'
              ].join(' ')}>
                {p.r_multiple !== null
                  ? (p.r_multiple >= 0 ? '+' : '') + p.r_multiple.toFixed(2) + 'R'
                  : '—'}
              </td>
              <td class={[
                'px-3 py-2 text-right',
                p.pct_of_equity >= 50 ? 'text-bad' :
                p.pct_of_equity >= 25 ? 'text-warn' : 'text-muted'
              ].join(' ')}
                  title="Position notional as % of wallet equity">
                {p.pct_of_equity.toFixed(1)}%
              </td>
              <td class="px-3 py-2 text-left">
                <!-- Risk/note: compact icon pills. Stop chip glows red
                     when within 5% of the trigger; target chip green;
                     trail chip warn; note chip clickable for full text -->
                <span class="inline-flex items-center gap-1 text-[10.5px] tabular">
                  {#if p.stop_price !== null}
                    <span
                      class={[
                        'inline-flex items-center gap-0.5 rounded border px-1.5 py-0.5',
                        stopWarn
                          ? 'border-bad/60 bg-bad-soft text-bad font-semibold'
                          : 'border-bad/25 bg-bad-soft/40 text-bad/90'
                      ].join(' ')}
                      title={stopWarn
                        ? `WARNING: ${p.dist_to_stop_pct!.toFixed(1)}% from stop`
                        : `Stop at ${price(p.stop_price)} (${p.dist_to_stop_pct?.toFixed(1)}% away)`}
                    >↓ {price(p.stop_price)}</span>
                  {/if}
                  {#if p.target_price !== null}
                    <span
                      class="inline-flex items-center gap-0.5 rounded border border-good/25 bg-good-soft/40 px-1.5 py-0.5 text-good/90"
                      title={`Target ${price(p.target_price)} (${p.dist_to_target_pct?.toFixed(1)}% away)`}
                    >↑ {price(p.target_price)}</span>
                  {/if}
                  {#if p.trailing_stop_pct !== null}
                    <span
                      class="inline-flex items-center gap-0.5 rounded border border-warn/25 bg-warn-soft/40 px-1.5 py-0.5 text-warn/90"
                      title={`Trailing ${(p.trailing_stop_pct * 100).toFixed(0)}% off watermark ${p.watermark_price !== null ? price(p.watermark_price) : '—'}`}
                    >⇣{(p.trailing_stop_pct * 100).toFixed(0)}%</span>
                  {/if}
                  {#if p.notes}
                    <Edit3
                      class="h-3 w-3 text-muted"
                      aria-label="Has notes"
                    />
                  {/if}
                  {#if !hasRisk && !p.notes}
                    <span class="text-faint italic">no rules</span>
                  {/if}
                  {#if p.stop_price === null}
                    {@const isPending = atrPending.has(p.id)}
                    <button
                      type="button"
                      onclick={(e) => { e.stopPropagation(); setAtrStop(p); }}
                      disabled={isPending}
                      class="ml-1 inline-flex items-center gap-1 rounded border border-warn/40 bg-warn-soft px-1.5 py-0 text-[10px] text-warn transition-colors hover:bg-warn/15 disabled:opacity-50"
                      title="Set 2×ATR(14) stop on the correct side of entry"
                    >
                      {#if isPending}
                        <Spinner size={9} />
                      {:else}
                        <Wand2 class="h-2.5 w-2.5" />
                      {/if}
                      2×ATR
                    </button>
                  {/if}
                </span>
              </td>
              <td class="px-3 py-2 text-right text-[11px] text-faint">
                {p.age_h < 24 ? `${p.age_h.toFixed(1)}h` : `${Math.round(p.age_h / 24)}d`}
              </td>
            </tr>
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

<!-- ── risk-mgmt drawer ─────────────────────────── -->
<Drawer
  open={drawerId !== null}
  onClose={() => (drawerId = null)}
  class="max-w-md"
>
  {#snippet header()}
    {#if drawerRow}
      <div class="flex flex-1 items-center gap-2">
        <Pill variant={drawerRow.side === 'long' ? 'pos' : 'neg'}>
          {drawerRow.side.toUpperCase()}
        </Pill>
        <TickerLink ticker={drawerRow.ticker} class="text-sm font-bold" />
        <span class="text-[11px] text-faint">·</span>
        <span class="text-[11px] text-muted capitalize">{drawerRow.fund}</span>
        <span class="text-[11px] text-faint">#{drawerRow.id}</span>
      </div>
    {/if}
  {/snippet}

  {#if drawerRow}
    <div class="space-y-4">
      <!-- snapshot strip -->
      <div class="grid grid-cols-3 gap-2 text-center">
        <div class="rounded-md border border-border bg-surface-2 px-2 py-1.5">
          <div class="text-[10px] uppercase tracking-wider text-faint">Entry</div>
          <div class="mt-0.5 text-[13px] tabular">{price(drawerRow.entry)}</div>
        </div>
        <div class="rounded-md border border-border bg-surface-2 px-2 py-1.5">
          <div class="text-[10px] uppercase tracking-wider text-faint">Mark</div>
          <div class="mt-0.5 text-[13px] tabular">{price(drawerRow.mark)}{drawerRow.mark_live ? '' : '*'}</div>
        </div>
        <div class="rounded-md border border-border bg-surface-2 px-2 py-1.5">
          <div class="text-[10px] uppercase tracking-wider text-faint">uPnL</div>
          <div class={[
            'mt-0.5 text-[13px] tabular font-medium',
            drawerRow.upnl >= 0 ? 'text-good' : 'text-bad'
          ].join(' ')}>
            {usd(drawerRow.upnl, true)}
          </div>
        </div>
      </div>

      <!-- secondary metrics -->
      <div class="grid grid-cols-3 gap-2 text-center">
        <div class="rounded-md border border-border bg-surface-2 px-2 py-1.5">
          <div class="text-[10px] uppercase tracking-wider text-faint">R-multiple</div>
          <div class={[
            'mt-0.5 text-[13px] tabular font-medium',
            drawerRow.r_multiple === null ? 'text-faint'
              : drawerRow.r_multiple >= 1 ? 'text-good'
              : drawerRow.r_multiple <= -0.7 ? 'text-bad' : 'text-text'
          ].join(' ')}>
            {drawerRow.r_multiple !== null
              ? (drawerRow.r_multiple >= 0 ? '+' : '') + drawerRow.r_multiple.toFixed(2) + 'R'
              : '—'}
          </div>
        </div>
        <div class="rounded-md border border-border bg-surface-2 px-2 py-1.5">
          <div class="text-[10px] uppercase tracking-wider text-faint">% Equity</div>
          <div class={[
            'mt-0.5 text-[13px] tabular',
            drawerRow.pct_of_equity >= 50 ? 'text-bad' :
            drawerRow.pct_of_equity >= 25 ? 'text-warn' : 'text-text'
          ].join(' ')}>{drawerRow.pct_of_equity.toFixed(1)}%</div>
        </div>
        <div class="rounded-md border border-border bg-surface-2 px-2 py-1.5">
          <div class="text-[10px] uppercase tracking-wider text-faint">Held</div>
          <div class="mt-0.5 text-[13px] tabular text-text">
            {drawerRow.age_h < 24 ? `${drawerRow.age_h.toFixed(1)}h` : `${Math.round(drawerRow.age_h / 24)}d`}
          </div>
        </div>
      </div>

      <!-- distance bars when stops/targets set -->
      {#if drawerRow.dist_to_stop_pct !== null || drawerRow.dist_to_target_pct !== null}
        <div class="space-y-1.5">
          {#if drawerRow.dist_to_stop_pct !== null}
            <div class="flex items-center gap-2 text-[11px]">
              <Shield class="h-3 w-3 text-bad" />
              <span class={[
                'flex-1 tabular',
                drawerRow.dist_to_stop_pct < 5 ? 'text-bad font-medium'
                  : drawerRow.dist_to_stop_pct < 10 ? 'text-warn' : 'text-muted'
              ].join(' ')}>
                {drawerRow.dist_to_stop_pct.toFixed(2)}% to stop
                {#if drawerRow.dist_to_stop_pct < 5}<span class="ml-1">⚠ close!</span>{/if}
              </span>
            </div>
          {/if}
          {#if drawerRow.dist_to_target_pct !== null}
            <div class="flex items-center gap-2 text-[11px]">
              <TargetIcon class="h-3 w-3 text-good" />
              <span class="flex-1 tabular text-muted">
                {drawerRow.dist_to_target_pct.toFixed(2)}% to target
              </span>
            </div>
          {/if}
          {#if drawerRow.watermark_price !== null}
            <div class="flex items-center gap-2 text-[11px]">
              <TrendingUp class="h-3 w-3 text-warn" />
              <span class="flex-1 tabular text-muted">
                Trailing watermark @ {price(drawerRow.watermark_price)}
              </span>
            </div>
          {/if}
        </div>
      {/if}

      <!-- risk form -->
      <div>
        <div class="mb-2 text-[10px] font-semibold uppercase tracking-wider text-faint">
          Risk management
        </div>
        <div class="space-y-2.5">
          <label class="block">
            <span class="flex items-center gap-1.5 text-[11px] text-muted">
              <Shield class="h-3 w-3 text-bad" />
              Stop price
              {#if drawerRow.side === 'long'}
                <span class="text-faint">(below entry)</span>
              {:else}
                <span class="text-faint">(above entry)</span>
              {/if}
            </span>
            <input
              type="number"
              step="0.01"
              bind:value={dStop}
              placeholder="—"
              class="mt-1 w-full rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-[13px] tabular text-text placeholder:text-faint/60 focus:border-primary/60 focus:outline-none"
            />
            <div class="mt-1.5 flex flex-wrap items-center gap-1">
              {#each [3, 5, 8, 10] as pct (pct)}
                <button
                  type="button"
                  onclick={() => (dStop = stopFromPct(pct))}
                  class="rounded border border-border bg-surface-2 px-1.5 py-0.5 text-[10.5px] tabular text-muted transition-colors hover:border-bad/50 hover:text-bad"
                  title={`Stop ${pct}% ${drawerRow.side === 'long' ? 'below' : 'above'} entry → ${price(stopFromPct(pct))}`}
                >−{pct}%</button>
              {/each}
              {#if dStop !== null}
                <button type="button" onclick={() => (dStop = null)} class="ml-auto text-[10.5px] text-faint hover:text-text">clear</button>
              {/if}
            </div>
          </label>

          <label class="block">
            <span class="flex items-center gap-1.5 text-[11px] text-muted">
              <TargetIcon class="h-3 w-3 text-good" />
              Target price
              {#if drawerRow.side === 'long'}
                <span class="text-faint">(above entry)</span>
              {:else}
                <span class="text-faint">(below entry)</span>
              {/if}
            </span>
            <input
              type="number"
              step="0.01"
              bind:value={dTarget}
              placeholder="—"
              class="mt-1 w-full rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-[13px] tabular text-text placeholder:text-faint/60 focus:border-primary/60 focus:outline-none"
            />
            <div class="mt-1.5 flex flex-wrap items-center gap-1">
              {#if typeof dStop === 'number' && dStop > 0}
                {#each [1, 2, 3] as r (r)}
                  {@const tp = targetFromR(r)}
                  {#if tp !== null}
                    <button
                      type="button"
                      onclick={() => (dTarget = tp)}
                      class="rounded border border-border bg-surface-2 px-1.5 py-0.5 text-[10.5px] tabular text-muted transition-colors hover:border-good/50 hover:text-good"
                      title={`${r}R target (${r}× the entry→stop distance) → ${price(tp)}`}
                    >{r}R</button>
                  {/if}
                {/each}
              {:else}
                <span class="text-[10.5px] text-faint italic">set a stop for R-based targets</span>
              {/if}
              {#if dTarget !== null}
                <button type="button" onclick={() => (dTarget = null)} class="ml-auto text-[10.5px] text-faint hover:text-text">clear</button>
              {/if}
            </div>
          </label>

          <label class="block">
            <span class="flex items-center gap-1.5 text-[11px] text-muted">
              {#if drawerRow.side === 'long'}
                <TrendingUp class="h-3 w-3 text-warn" />
              {:else}
                <TrendingDown class="h-3 w-3 text-warn" />
              {/if}
              Trailing stop %
              <span class="text-faint">(0–99; ratchets off the best price)</span>
            </span>
            <input
              type="number"
              step="0.5"
              min="0"
              max="99"
              bind:value={dTrail}
              placeholder="—"
              class="mt-1 w-full rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-[13px] tabular text-text placeholder:text-faint/60 focus:border-primary/60 focus:outline-none"
            />
            {#if drawerRow.watermark_price !== null}
              <div class="mt-1 text-[10.5px] text-faint">
                watermark: {price(drawerRow.watermark_price)}
              </div>
            {/if}
          </label>

          <label class="block">
            <span class="text-[11px] text-muted">Notes (journal)</span>
            <textarea
              bind:value={dNotes}
              rows="3"
              placeholder="Why did you open this? What's the exit plan? What would invalidate?"
              class="mt-1 w-full resize-y rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-[12.5px] text-text placeholder:text-faint/60 focus:border-primary/60 focus:outline-none"
            ></textarea>
          </label>
        </div>

        <!-- live risk / reward preview from the current draft -->
        {#if rrPreview && (rrPreview.risk$ !== null || rrPreview.reward$ !== null)}
          <div class="mt-2.5 flex items-center justify-between gap-2 rounded-md border border-border-soft bg-surface-2/50 px-2.5 py-1.5 text-[11px] tabular">
            <span class="flex items-center gap-1 text-bad">
              <Shield class="h-3 w-3" />
              {rrPreview.risk$ !== null
                ? `−${usd(rrPreview.risk$)}${rrPreview.riskPct !== null ? ` (${rrPreview.riskPct.toFixed(1)}%)` : ''}`
                : 'no stop'}
            </span>
            <span class="flex items-center gap-1 text-good">
              <TargetIcon class="h-3 w-3" />
              {rrPreview.reward$ !== null ? `+${usd(rrPreview.reward$)}` : 'no target'}
            </span>
            <span class={[
              'rounded px-1.5 py-0.5 font-semibold',
              rrPreview.rr === null ? 'text-faint'
                : rrPreview.rr >= 2 ? 'bg-good-soft text-good'
                : rrPreview.rr >= 1 ? 'bg-warn-soft text-warn'
                : 'bg-bad-soft text-bad'
            ].join(' ')}>
              {rrPreview.rr !== null ? `1 : ${rrPreview.rr.toFixed(2)} R:R` : '— R:R'}
            </span>
          </div>
        {/if}

        <button
          type="button"
          onclick={saveRisk}
          disabled={$riskM.isPending}
          class="mt-2.5 flex w-full items-center justify-center gap-1.5 rounded-md border border-primary/40 bg-primary-soft px-3 py-2 text-[12.5px] font-medium text-primary transition-colors hover:bg-primary/15 disabled:opacity-50"
        >
          {#if $riskM.isPending}<Spinner size={12} />{:else}<Save class="h-3.5 w-3.5" />{/if}
          Save risk settings
        </button>
      </div>

      <!-- close-position -->
      <div class="border-t border-border pt-3">
        <button
          type="button"
          onclick={() => $closeM.mutate(drawerRow!.id)}
          disabled={$closeM.isPending}
          class="flex w-full items-center justify-center gap-1.5 rounded-md border border-bad/40 bg-bad-soft px-3 py-2 text-[12.5px] font-medium text-bad transition-colors hover:bg-bad/15 disabled:opacity-50"
        >
          {#if $closeM.isPending}<Spinner size={12} />{:else}<X class="h-3.5 w-3.5" />{/if}
          Close position at mark
        </button>
      </div>

      {#if drawerRow.open_reason}
        <div class="rounded-md border border-border-soft bg-surface-2/40 px-3 py-2 text-[11.5px] text-muted">
          <span class="font-semibold text-faint">Opened:</span> {drawerRow.open_reason}
        </div>
      {/if}

      <!-- News / filings / bot calls about this ticker since entry -->
      <TradeLifecycle tradeId={drawerRow.id} />
    </div>
  {/if}
</Drawer>

<!-- ── Cut losers / Lock winners confirmation ───────────── -->
{#if bulkAction}
  {@const isLosers = bulkAction.kind === 'losers'}
  <div class="fixed inset-0 z-50 flex items-center justify-center p-4">
    <button
      type="button"
      aria-label="Cancel"
      class="absolute inset-0 cursor-default bg-black/55 backdrop-blur-sm"
      onclick={() => (bulkAction = null)}
    ></button>
    <div class="relative w-full max-w-sm rounded-lg border border-border bg-surface p-4 shadow-2xl">
      <div class="flex items-center gap-2 text-sm font-semibold">
        {#if isLosers}
          <TrendingDown class="h-4 w-4 text-bad" /><span>Cut losing positions</span>
        {:else}
          <TrendingUp class="h-4 w-4 text-good" /><span>Lock in winners</span>
        {/if}
      </div>
      <p class="mt-2 text-[12.5px] leading-relaxed text-muted">
        Close <span class="font-semibold text-text">{bulkAction.ids.length}</span>
        {isLosers ? 'losing' : 'winning'} position{bulkAction.ids.length === 1 ? '' : 's'}
        at the current mark across all wallets. This realises their P&L and can't be undone.
      </p>
      <div class="mt-4 flex justify-end gap-2">
        <button
          type="button"
          onclick={() => (bulkAction = null)}
          class="rounded-md border border-border bg-surface-2 px-3 py-1.5 text-[12px] text-muted transition-colors hover:text-text"
        >Cancel</button>
        <button
          type="button"
          disabled={$bulkM.isPending}
          onclick={() => $bulkM.mutate({
            ids: bulkAction!.ids,
            reason: isLosers ? 'cut losers via /book' : 'lock winners via /book'
          })}
          class={[
            'flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-[12px] font-medium transition-colors disabled:opacity-50',
            isLosers
              ? 'border-bad/50 bg-bad-soft text-bad hover:bg-bad/20'
              : 'border-good/50 bg-good-soft text-good hover:bg-good/20'
          ].join(' ')}
        >
          {#if $bulkM.isPending}<Spinner size={12} />{/if}
          Close {bulkAction.ids.length}
        </button>
      </div>
    </div>
  </div>
{/if}

<!-- ── manual paper-trade open drawer ───────────────────── -->
<OpenPositionDrawer
  open={openTradeOpen}
  onClose={() => (openTradeOpen = false)}
  funds={($walletsQ.data ?? []).map((w) => ({
    name: w.name, mandate: w.mandate, equity: w.equity
  }))}
/>
