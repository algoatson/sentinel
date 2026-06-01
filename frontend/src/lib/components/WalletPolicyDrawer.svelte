<script lang="ts">
  /**
   * Editor drawer for a single wallet's autonomous policy.
   *
   * Pulls the resolved policy (code defaults overlaid with DB
   * overrides) from /api/wallets/{name}/policy, lets the user retune
   * the per-cycle knobs (size, max positions, stop %, take %, hold
   * days, min conviction, daily-opens cap) or pause the wallet
   * entirely. Saves via PATCH; engine picks the new values up on the
   * next cycle without a restart.
   *
   * Per-knob "reset to default" sends `clear=[knob]` so the DB column
   * clears and the wallet falls back to the seed default from
   * funds._POLICIES.
   */
  import { createQuery, createMutation, useQueryClient } from '@tanstack/svelte-query';
  import { reactiveQueryOptions } from '$lib/reactive-query.svelte';
  import {
    walletPolicy, updateWalletPolicy, walletHistory,
    type WalletKnobKey, type WalletPolicy,
  } from '$api';
  import Drawer from './Drawer.svelte';
  import Spinner from './Spinner.svelte';
  import { toast } from '$lib/toast.svelte';
  import { Save, RotateCcw, PauseCircle, PlayCircle } from 'lucide-svelte';
  import { usd } from '$lib/format';

  interface Props {
    name: string | null;
    open: boolean;
    onClose: () => void;
  }
  let { name, open, onClose }: Props = $props();

  const qc = useQueryClient();
  // reactiveQueryOptions so `enabled`/`queryKey` track name+open — a plain
  // object evaluates `enabled` once at mount (name=null → false) and the
  // fetch never fires, which is why the drawer read "No policy loaded".
  const q = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['wallet-policy', name],
    queryFn: () => walletPolicy(name!),
    enabled: !!name && open,
    refetchInterval: false,
    staleTime: 30_000,
  })));

  // Recent closed trades — turns the drawer into a wallet cockpit. Separate
  // query (same enable gate) so the policy editor renders instantly while
  // the trade tape streams in behind it.
  const hist = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['wallet-policy-history', name],
    queryFn: () => walletHistory(name!, 90),
    enabled: !!name && open,
    refetchInterval: false,
    staleTime: 60_000,
  })));
  const recentCloses = $derived.by(() => {
    const c = $hist.data?.closed ?? [];
    return [...c]
      .filter((t) => t.exit_at)
      .sort((a, b) => (a.exit_at! < b.exit_at! ? 1 : -1))
      .slice(0, 5);
  });

  // Draft fields — populated when the policy loads. Strings so the
  // input boxes don't fight us on leading minus / empty values.
  let dMandate = $state('');
  let dSize = $state('');
  let dMaxPos = $state('');
  let dStop = $state('');
  let dTake = $state('');
  let dHold = $state('');
  let dMinConv = $state('');
  let dMaxOpens = $state('');

  // Mark a clean (no-edit) state until the user touches a field.
  let initialised = $state(false);

  $effect(() => {
    const d = $q.data;
    if (d && !initialised) {
      dMandate = d.mandate ?? '';
      dSize    = d.knobs.size_pct.value      != null ? String(d.knobs.size_pct.value)      : '';
      dMaxPos  = d.knobs.max_positions.value != null ? String(d.knobs.max_positions.value) : '';
      dStop    = d.knobs.stop_pct.value      != null ? String(d.knobs.stop_pct.value)      : '';
      dTake    = d.knobs.take_pct.value      != null ? String(d.knobs.take_pct.value)      : '';
      dHold    = d.knobs.max_hold_days.value != null ? String(d.knobs.max_hold_days.value) : '';
      dMinConv = d.knobs.min_conviction.value!= null ? String(d.knobs.min_conviction.value): '';
      dMaxOpens= d.knobs.max_opens_per_day.value != null ? String(d.knobs.max_opens_per_day.value) : '';
      initialised = true;
    }
    if (!open) initialised = false;
  });

  const m = createMutation({
    mutationFn: (body: Parameters<typeof updateWalletPolicy>[1]) =>
      updateWalletPolicy(name!, body),
    onSuccess: () => {
      toast.success(`Policy saved · ${name}`);
      qc.invalidateQueries({ queryKey: ['wallet-policy', name] });
      qc.invalidateQueries({ queryKey: ['wallets'] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
  });

  function parseNum(s: string): number | null {
    const t = s.trim();
    if (!t) return null;
    const n = Number(t);
    return Number.isFinite(n) ? n : null;
  }

  function save() {
    if (!name) return;
    const body: Parameters<typeof updateWalletPolicy>[1] = {};
    if (dMandate.trim() && dMandate !== ($q.data?.mandate ?? '')) {
      body.mandate = dMandate.trim();
    }
    const numericMap: Array<[WalletKnobKey, string]> = [
      ['size_pct', dSize],
      ['max_positions', dMaxPos],
      ['stop_pct', dStop],
      ['take_pct', dTake],
      ['max_hold_days', dHold],
      ['min_conviction', dMinConv],
      ['max_opens_per_day', dMaxOpens],
    ];
    const clear: WalletKnobKey[] = [];
    for (const [key, val] of numericMap) {
      const parsed = parseNum(val);
      const wasOverridden = $q.data?.knobs[key]?.overridden ?? false;
      if (parsed === null) {
        // Empty field → reset to default if it was overridden before.
        if (wasOverridden) clear.push(key);
      } else {
        (body as Record<string, unknown>)[key] = parsed;
      }
    }
    if (clear.length) body.clear = clear;
    if (Object.keys(body).length === 0) {
      toast.success('Nothing to save');
      return;
    }
    $m.mutate(body);
  }

  function togglePause() {
    if (!name || !$q.data) return;
    $m.mutate({ active: !$q.data.active });
  }

  function resetKnob(key: WalletKnobKey) {
    if (!name) return;
    $m.mutate({ clear: [key] });
    initialised = false; // re-hydrate drafts after the patch
  }
</script>

<Drawer {open} {onClose} title={name ? `${name} · policy` : 'Wallet policy'}>
  {#snippet header()}
    <div class="flex items-center gap-2">
      <span class="text-sm font-semibold capitalize">{name ?? '—'}</span>
      {#if $q.data}
        <span class={[
          'rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider',
          $q.data.active
            ? 'border-good/40 bg-good-soft text-good'
            : 'border-warn/40 bg-warn-soft text-warn',
        ].join(' ')}>
          {$q.data.active ? 'active' : 'paused'}
        </span>
      {/if}
    </div>
  {/snippet}

  {#if $q.isLoading}
    <div class="flex items-center justify-center py-8"><Spinner /></div>
  {:else if !$q.data}
    <div class="py-6 text-center text-[12px] text-faint">No policy loaded.</div>
  {:else}
    {@const d = $q.data}
    <div class="space-y-3">
      <!-- Live stats — the drawer doubles as a wallet cockpit -->
      {#if d.stats}
        {@const s = d.stats}
        {#snippet stat(label: string, val: string, cls = 'text-text')}
          <div class="rounded-md border border-border bg-surface-2/40 px-2 py-1.5">
            <div class="text-[9px] uppercase tracking-wider text-faint">{label}</div>
            <div class={['mt-0.5 text-[13.5px] font-semibold tabular leading-tight', cls].join(' ')}>{val}</div>
          </div>
        {/snippet}
        <div class="grid grid-cols-3 gap-2">
          {@render stat('Return', `${s.return_pct > 0 ? '+' : ''}${s.return_pct.toFixed(2)}%`, s.return_pct >= 0 ? 'text-good' : 'text-bad')}
          {@render stat('Equity', usd(s.equity))}
          {@render stat('uPnL', usd(s.upnl, true), s.upnl > 0 ? 'text-good' : s.upnl < 0 ? 'text-bad' : 'text-muted')}
          {@render stat('Open', String(s.open))}
          {@render stat('Closed', String(s.closed))}
          {@render stat('Win rate', s.win_rate_pct != null ? `${s.win_rate_pct.toFixed(0)}%` : '—')}
        </div>
      {/if}

      <!-- What this wallet listens to (read-only) -->
      <div class="flex flex-wrap items-center gap-1 text-[10.5px]">
        <span class="uppercase tracking-wider text-faint">listens</span>
        {#if d.sources.length}
          {#each d.sources as src (src)}
            <span class="rounded border border-border bg-surface-2 px-1.5 py-0.5 text-muted">{src}</span>
          {/each}
        {:else}
          <span class="rounded border border-border bg-surface-2 px-1.5 py-0.5 text-faint">user-driven</span>
        {/if}
        {#if d.asset_classes}
          {#each d.asset_classes as ac (ac)}
            <span class="rounded border border-primary/30 bg-primary-soft px-1.5 py-0.5 capitalize text-primary">{ac}</span>
          {/each}
        {:else}
          <span class="rounded border border-primary/20 bg-primary-soft/50 px-1.5 py-0.5 text-primary/70">any asset</span>
        {/if}
      </div>

      <!-- Recent closes — the wallet's live trade tape -->
      {#if recentCloses.length}
        <div class="rounded-md border border-border bg-surface-2/40 px-2.5 py-2">
          <div class="mb-1.5 flex items-center justify-between">
            <span class="text-[9.5px] uppercase tracking-wider text-faint">Recent closes</span>
            {#if d.stats}
              <span class="text-[9.5px] tabular text-faint">{d.stats.closed} total</span>
            {/if}
          </div>
          <div class="space-y-1">
            {#each recentCloses as t (t.id)}
              <div class="flex items-center gap-2 text-[11px]">
                <span class={['h-1.5 w-1.5 shrink-0 rounded-full', t.side === 'long' ? 'bg-good' : 'bg-bad'].join(' ')}></span>
                <span class="font-mono font-medium text-text">{t.ticker}</span>
                {#if t.close_reason}
                  <span class="truncate text-faint">{t.close_reason}</span>
                {/if}
                <span class={['ml-auto shrink-0 tabular font-semibold', t.realized_pct >= 0 ? 'text-good' : 'text-bad'].join(' ')}>
                  {t.realized_pct > 0 ? '+' : ''}{t.realized_pct.toFixed(1)}%
                </span>
              </div>
            {/each}
          </div>
        </div>
      {/if}

      <!-- Active toggle -->
      <div class="flex items-center gap-2 rounded-md border border-border bg-surface-2/40 px-3 py-2">
        <span class="flex-1 text-[12px] text-muted">
          Autonomous trading {d.active ? 'enabled' : 'paused'} —
          {d.active
            ? 'wallet will open new positions per its policy'
            : 'wallet won\'t open new positions but existing positions still risk-manage'}.
        </span>
        <button
          type="button"
          onclick={togglePause}
          disabled={$m.isPending}
          class={[
            'inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-medium',
            d.active
              ? 'border-warn/40 bg-warn-soft text-warn hover:bg-warn/15'
              : 'border-good/40 bg-good-soft text-good hover:bg-good/15',
          ].join(' ')}
        >
          {#if d.active}
            <PauseCircle class="h-3.5 w-3.5" /> Pause
          {:else}
            <PlayCircle class="h-3.5 w-3.5" /> Resume
          {/if}
        </button>
      </div>

      <!-- Mandate -->
      <label class="block">
        <span class="text-[11px] text-muted">Mandate</span>
        <textarea
          bind:value={dMandate}
          rows="2"
          class="mt-1 w-full resize-y rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-[12px] text-text placeholder:text-faint/60 focus:border-primary/60 focus:outline-none"
          placeholder="Human-readable description of this wallet"
        ></textarea>
      </label>

      <!-- Knobs -->
      <div class="grid grid-cols-2 gap-2">
        {#snippet knob(label: string, key: WalletKnobKey, val: string, setVal: (v: string) => void, hint: string)}
          {@const knob = d.knobs[key]}
          {@const overridden = knob.overridden}
          <label class="block rounded-md border border-border bg-surface-2/40 px-2 py-1.5">
            <div class="flex items-center justify-between text-[10px] uppercase tracking-wider text-faint">
              <span>{label}</span>
              {#if overridden}
                <button
                  type="button"
                  onclick={() => resetKnob(key)}
                  class="text-[9.5px] text-primary hover:underline"
                  title="Reset to default ({knob.default ?? '—'})"
                >reset</button>
              {/if}
            </div>
            <input
              type="text"
              value={val}
              oninput={(e) => setVal((e.currentTarget as HTMLInputElement).value)}
              placeholder={knob.default !== null ? String(knob.default) : '—'}
              class="mt-1 w-full bg-transparent text-[13px] tabular text-text placeholder:text-faint/50 focus:outline-none"
            />
            <div class="text-[9.5px] tabular text-faint">{hint}</div>
          </label>
        {/snippet}
        {@render knob(
          'Size %', 'size_pct', dSize, (v) => (dSize = v),
          '0..1 — fraction of equity per top-conviction trade',
        )}
        {@render knob(
          'Max positions', 'max_positions', dMaxPos, (v) => (dMaxPos = v),
          'soft cap on concurrent open positions',
        )}
        {@render knob(
          'Stop %', 'stop_pct', dStop, (v) => (dStop = v),
          'negative — e.g. -0.08 cuts a long at -8%',
        )}
        {@render knob(
          'Take %', 'take_pct', dTake, (v) => (dTake = v),
          'positive — e.g. 0.25 closes a long at +25%',
        )}
        {@render knob(
          'Max hold (days)', 'max_hold_days', dHold, (v) => (dHold = v),
          'time stop — force close after N days',
        )}
        {@render knob(
          'Min conviction', 'min_conviction', dMinConv, (v) => (dMinConv = v),
          '1..5 — drop calls below this',
        )}
        {@render knob(
          'Max opens / day', 'max_opens_per_day', dMaxOpens, (v) => (dMaxOpens = v),
          'caps news-cascade churn',
        )}
      </div>

      <button
        type="button"
        onclick={save}
        disabled={$m.isPending}
        class="mt-2 inline-flex w-full items-center justify-center gap-1.5 rounded-md border border-primary/40 bg-primary-soft px-3 py-2 text-[12.5px] font-medium text-primary transition-colors hover:bg-primary/15 disabled:opacity-50"
      >
        {#if $m.isPending}<Spinner size={12} />{:else}<Save class="h-3.5 w-3.5" />{/if}
        Save policy
      </button>
    </div>
  {/if}
</Drawer>
