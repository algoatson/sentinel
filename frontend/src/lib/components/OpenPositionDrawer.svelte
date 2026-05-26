<script lang="ts">
  /**
   * Manual paper-trade open form, mounted from the /book header.
   *
   * Three sizing modes (radio):
   *  - Notional ($): position size in dollars; qty = $ / mark
   *  - Risk ($ at stop): the "1% rule" — qty = (equity × risk%) / |entry−stop|
   *  - Shares: raw qty
   *
   * Risk mode also offers a stop_price input which gets persisted on
   * the new trade (so the auto_exits pipeline can manage it from the
   * moment it opens). Other modes leave the stop blank — set it
   * later via the risk drawer.
   */
  import { createMutation, createQuery, useQueryClient } from '@tanstack/svelte-query';
  import { reactiveQueryOptions } from '$lib/reactive-query.svelte';
  import { openPosition, tickerAtr, type OpenRequest } from '$api';
  import Drawer from './Drawer.svelte';
  import Spinner from './Spinner.svelte';
  import { toast } from '$lib/toast.svelte';
  import { Plus, TrendingUp, TrendingDown, Calculator } from 'lucide-svelte';

  interface Props {
    open: boolean;
    onClose: () => void;
    /** Wallet options to choose from. */
    funds: { name: string; mandate: string; equity: number }[];
    /** Optional preset (e.g. coming from a symbol page). */
    preset?: { ticker?: string; side?: 'long' | 'short' };
  }

  let { open, onClose, funds, preset }: Props = $props();

  type SizeMode = 'notional' | 'risk' | 'qty';

  let fundName = $state('');
  let ticker = $state('');
  let side: 'long' | 'short' = $state('long');
  let sizeMode: SizeMode = $state('notional');
  let notional = $state('1000');
  let qty = $state('');
  let riskPct = $state('1');      // 1% of equity, the classic default
  let stopPrice = $state('');
  let note = $state('');

  // Reset whenever drawer opens.
  $effect(() => {
    if (open) {
      fundName = funds[0]?.name ?? '';
      ticker = preset?.ticker ?? '';
      side = preset?.side ?? 'long';
      notional = '1000';
      qty = '';
      riskPct = '1';
      stopPrice = '';
      note = '';
      sizeMode = 'notional';
    }
  });

  const selectedFund = $derived(funds.find((f) => f.name === fundName));

  // ATR for the entered ticker — drives the "Use ATR stop" suggestions
  // when in risk mode. Only fetches once the user has typed ≥1 char
  // and the drawer is open.
  const atrTicker = $derived(
    ticker.trim().toUpperCase().replace(/^\$/, '')
  );
  const atrQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['atr', atrTicker],
    queryFn: () => tickerAtr(atrTicker),
    enabled: open && atrTicker.length >= 1,
    staleTime: 5 * 60_000
  })));

  const qc = useQueryClient();
  const openM = createMutation({
    mutationFn: (body: OpenRequest) => openPosition(body),
    onSuccess: (res) => {
      const fp =
        res.fill_price !== null ? `@ ${res.fill_price.toFixed(2)}` : '';
      const q = res.qty !== null ? `×${res.qty.toFixed(4)}` : '';
      toast.success(`Opened #${res.trade_id} ${q} ${fp}`);
      qc.invalidateQueries({ queryKey: ['positions-open'] });
      qc.invalidateQueries({ queryKey: ['wallets'] });
      qc.invalidateQueries({ queryKey: ['kpi'] });
      onClose();
    },
    onError: (err) =>
      toast.error(err instanceof Error ? err.message : String(err))
  });

  function submit() {
    const body: OpenRequest = {
      fund_name: fundName,
      ticker: ticker.trim().toUpperCase().replace(/^\$/, ''),
      side
    };
    if (!body.ticker || !body.fund_name) {
      toast.error('Pick a wallet and enter a ticker');
      return;
    }
    if (sizeMode === 'notional') {
      const n = Number(notional);
      if (!(n > 0)) return toast.error('Notional must be a positive number');
      body.notional = n;
    } else if (sizeMode === 'qty') {
      const q = Number(qty);
      if (!(q > 0)) return toast.error('Qty must be a positive number');
      body.qty = q;
    } else {
      const r = Number(riskPct) / 100;
      const sp = Number(stopPrice);
      if (!(r > 0 && r < 0.5))
        return toast.error('Risk % must be between 0 and 50');
      if (!(sp > 0)) return toast.error('Set a stop price for risk sizing');
      body.risk_pct = r;
      body.stop_price = sp;
    }
    if (note.trim()) body.note = note.trim();
    $openM.mutate(body);
  }
</script>

<Drawer {open} {onClose} class="max-w-md">
  {#snippet header()}
    <div class="flex items-center gap-2">
      <Plus class="h-4 w-4 text-good" />
      <span class="text-sm font-semibold text-text">Open paper position</span>
    </div>
  {/snippet}

  <div class="space-y-4">
    <!-- wallet picker -->
    <label class="block">
      <span class="text-[11px] text-muted">Wallet</span>
      <select
        bind:value={fundName}
        class="mt-1 w-full rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-[13px] text-text focus:border-primary/60 focus:outline-none"
      >
        {#each funds as f (f.name)}
          <option value={f.name}>{f.name} · ${f.equity.toLocaleString()}</option>
        {/each}
      </select>
    </label>

    <!-- ticker + side -->
    <div class="grid grid-cols-[1fr_auto] gap-2">
      <label class="block">
        <span class="text-[11px] text-muted">Ticker</span>
        <input
          type="text"
          bind:value={ticker}
          placeholder="$NVDA"
          class="mt-1 w-full rounded-md border border-border bg-surface-2 px-2.5 py-1.5 font-mono text-[13px] text-text placeholder:text-faint/60 focus:border-primary/60 focus:outline-none"
        />
      </label>
      <div>
        <span class="block text-[11px] text-muted">Side</span>
        <div class="mt-1 inline-flex overflow-hidden rounded-md border border-border bg-surface-2 text-[12px]">
          <button
            type="button"
            onclick={() => (side = 'long')}
            class={[
              'flex items-center gap-1 px-3 py-1.5 transition-colors',
              side === 'long' ? 'bg-good-soft text-good' : 'text-muted hover:text-text'
            ].join(' ')}
          ><TrendingUp class="h-3 w-3" /> Long</button>
          <button
            type="button"
            onclick={() => (side = 'short')}
            class={[
              'flex items-center gap-1 px-3 py-1.5 transition-colors',
              side === 'short' ? 'bg-bad-soft text-bad' : 'text-muted hover:text-text'
            ].join(' ')}
          ><TrendingDown class="h-3 w-3" /> Short</button>
        </div>
      </div>
    </div>

    <!-- size mode -->
    <div>
      <div class="mb-1.5 flex items-center justify-between text-[10px] font-semibold uppercase tracking-wider text-faint">
        <span>Sizing</span>
        {#if selectedFund}
          <span class="font-mono text-[10.5px] text-muted">equity ${selectedFund.equity.toLocaleString()}</span>
        {/if}
      </div>
      <div class="flex gap-1.5 text-[11px]">
        {#each [
          ['notional', 'Dollars'],
          ['risk', 'Risk %'],
          ['qty', 'Shares']
        ] as [k, label] (k)}
          <button
            type="button"
            onclick={() => (sizeMode = k as SizeMode)}
            class={[
              'flex-1 rounded-md border px-2 py-1.5 transition-colors',
              sizeMode === k
                ? 'border-primary/50 bg-primary-soft text-primary'
                : 'border-border bg-surface-2 text-muted hover:text-text'
            ].join(' ')}
          >{label}</button>
        {/each}
      </div>

      {#if sizeMode === 'notional'}
        <label class="mt-2 block">
          <span class="text-[11px] text-muted">Notional ($)</span>
          <input
            type="number"
            step="50"
            min="0"
            bind:value={notional}
            class="mt-1 w-full rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-[13px] tabular text-text focus:border-primary/60 focus:outline-none"
          />
        </label>
      {:else if sizeMode === 'qty'}
        <label class="mt-2 block">
          <span class="text-[11px] text-muted">Shares</span>
          <input
            type="number"
            step="0.0001"
            min="0"
            bind:value={qty}
            class="mt-1 w-full rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-[13px] tabular text-text focus:border-primary/60 focus:outline-none"
          />
        </label>
      {:else}
        <div class="mt-2 grid grid-cols-2 gap-2">
          <label class="block">
            <span class="text-[11px] text-muted">Risk %</span>
            <input
              type="number"
              step="0.1"
              min="0.1"
              max="49"
              bind:value={riskPct}
              class="mt-1 w-full rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-[13px] tabular text-text focus:border-primary/60 focus:outline-none"
            />
          </label>
          <label class="block">
            <span class="text-[11px] text-muted">Stop price</span>
            <input
              type="number"
              step="0.01"
              min="0"
              bind:value={stopPrice}
              class="mt-1 w-full rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-[13px] tabular text-text focus:border-primary/60 focus:outline-none"
            />
          </label>
        </div>
        <div class="mt-1 flex items-center gap-1.5 text-[10.5px] text-faint">
          <Calculator class="h-3 w-3" />
          qty = (equity × risk%) / |mark − stop| · stop persists with the trade
        </div>
        {#if $atrQ.data && $atrQ.data.atr !== null}
          {@const a = $atrQ.data}
          {@const tight = side === 'long'
            ? a.suggested_long_stop_tight
            : a.suggested_short_stop_tight}
          {@const wide = side === 'long'
            ? a.suggested_long_stop
            : a.suggested_short_stop}
          <div class="mt-2 flex flex-wrap items-center gap-1.5 rounded-md border border-warn/25 bg-warn-soft/30 px-2 py-1.5 text-[10.5px]">
            <span class="text-faint">ATR{a.period}:</span>
            <span class="tabular text-warn">{a.atr?.toFixed(2)}</span>
            {#if a.atr_pct !== null}
              <span class="text-faint">({a.atr_pct.toFixed(2)}%)</span>
            {/if}
            {#if tight !== null}
              <button
                type="button"
                onclick={() => (stopPrice = tight.toFixed(2))}
                class="ml-auto rounded border border-border bg-surface-2 px-1.5 py-0.5 font-mono tabular text-muted transition-colors hover:border-warn/40 hover:text-text"
                title="1.5× ATR — tighter; gets stopped on more wiggles"
              >1.5× {tight.toFixed(2)}</button>
            {/if}
            {#if wide !== null}
              <button
                type="button"
                onclick={() => (stopPrice = wide.toFixed(2))}
                class="rounded border border-warn/40 bg-warn-soft px-1.5 py-0.5 font-mono tabular text-warn transition-colors hover:bg-warn/20"
                title="2× ATR — default; balances noise rejection vs locked risk"
              >2× {wide.toFixed(2)}</button>
            {/if}
          </div>
        {/if}
      {/if}
    </div>

    <label class="block">
      <span class="text-[11px] text-muted">Note (optional)</span>
      <textarea
        bind:value={note}
        rows="2"
        placeholder="Why this trade? What's the exit plan?"
        class="mt-1 w-full resize-y rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-[12.5px] text-text placeholder:text-faint/60 focus:border-primary/60 focus:outline-none"
      ></textarea>
    </label>

    <button
      type="button"
      onclick={submit}
      disabled={$openM.isPending || !fundName || !ticker.trim()}
      class={[
        'flex w-full items-center justify-center gap-1.5 rounded-md border px-3 py-2 text-[12.5px] font-medium transition-colors disabled:opacity-40',
        side === 'long'
          ? 'border-good/40 bg-good-soft text-good hover:bg-good/15'
          : 'border-bad/40 bg-bad-soft text-bad hover:bg-bad/15'
      ].join(' ')}
    >
      {#if $openM.isPending}<Spinner size={12} />{:else}<Plus class="h-3.5 w-3.5" />{/if}
      Open {side} {ticker || '…'}
    </button>

    <div class="rounded-md border border-border-soft bg-surface-2/40 px-3 py-2 text-[10.5px] text-faint">
      Fills at the latest mark on file. No leverage — sized down if it would
      blow the wallet budget. Use the risk drawer afterwards to add a target
      or trailing stop.
    </div>
  </div>
</Drawer>
