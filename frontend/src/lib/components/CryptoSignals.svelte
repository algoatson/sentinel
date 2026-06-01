<script lang="ts">
  /**
   * Funding-squeeze findings — the crypto leg's deterministic setups
   * (deep funding extremes, OI surges, crowded-long fades) recorded as
   * source="funding_squeeze" calls. Surfaces what the crypto detector is
   * seeing, each coin's CURRENT microstructure, and the BTC regime that
   * gates entries. Click → /symbol/$X.
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { cryptoSignals } from '$api';
  import { base } from '$app/paths';
  import Card from './Card.svelte';
  import EmptyState from './EmptyState.svelte';
  import Skeleton from './Skeleton.svelte';
  import TickerLink from './TickerLink.svelte';
  import { Activity, ArrowUpRight, ArrowDownRight } from 'lucide-svelte';
  import { timeAgo } from '$lib/format';

  const q = createQuery({
    queryKey: ['crypto-signals'],
    queryFn: () => cryptoSignals(72, 8),
    refetchInterval: 90_000,
  });

  const REGIME_META: Record<string, { label: string; cls: string }> = {
    risk_on:  { label: 'risk-on',  cls: 'border-good/40 bg-good-soft text-good' },
    risk_off: { label: 'risk-off', cls: 'border-bad/40 bg-bad-soft text-bad' },
    neutral:  { label: 'neutral',  cls: 'border-border bg-surface-2 text-muted' }
  };
</script>

<Card class="px-4 py-3">
  <div class="mb-2 flex items-baseline gap-2">
    <Activity class="h-3.5 w-3.5 text-violet" />
    <div class="sect-accent text-[10px] font-semibold uppercase tracking-wider text-faint">
      Funding &amp; OI setups · 72h
    </div>
    {#if $q.data}
      <span class="text-[10.5px] text-faint">{$q.data.signals.length} firing</span>
      {@const rm = REGIME_META[$q.data.regime.state] ?? REGIME_META.neutral}
      <span
        class={['ml-auto inline-flex items-baseline gap-1 rounded border px-2 py-0.5 text-[10px]', rm.cls].join(' ')}
        title={`Crypto tape — ${$q.data.regime.reason}`}
      >
        <span class="uppercase tracking-wider opacity-80">tape</span>
        <span class="font-semibold">{rm.label}</span>
      </span>
    {/if}
  </div>

  {#if !$q.data}
    <div class="space-y-1.5 py-1">
      {#each Array(3) as _, i (i)}
        <Skeleton class="h-9 w-full rounded" />
      {/each}
    </div>
  {:else if $q.data.signals.length === 0}
    <EmptyState
      title="No funding/OI setups firing"
      description={$q.data.regime.state === 'risk_off'
        ? 'Detector is quiet — and with the crypto tape risk-off, new longs are gated anyway. Squeeze / fade setups land here on a funding extreme.'
        : 'The funding-squeeze detector posts here on a deep funding extreme, an OI surge, or a crowded-long fade. None right now.'}
    />
  {:else}
    <ul class="space-y-1">
      {#each $q.data.signals as s (s.id)}
        {@const long = s.direction === 'long'}
        <li>
          <a
            href={`${base}/symbol/${encodeURIComponent(s.ticker)}`}
            class="flex items-center gap-2 rounded border border-border-soft bg-surface-2/40 px-2 py-1.5 hover:border-violet/40"
          >
            <span class={['flex-none', long ? 'text-good' : 'text-bad'].join(' ')}>
              {#if long}<ArrowUpRight class="h-3.5 w-3.5" />{:else}<ArrowDownRight class="h-3.5 w-3.5" />{/if}
            </span>
            <TickerLink ticker={s.ticker} class="w-20 flex-none text-[12.5px] font-semibold" />
            <span class="min-w-0 flex-1">
              <span class="block truncate text-[12px] text-text">{s.headline}</span>
              {#if s.micro}
                <span class="flex flex-wrap items-center gap-x-2 text-[10px] tabular text-faint">
                  {#if s.micro.funding_rate_pct !== null}
                    <span><span class="opacity-70">fund</span>
                      <span class={s.micro.funding_rate_pct < 0 ? 'text-good' : s.micro.funding_rate_pct >= 0.05 ? 'text-bad' : ''}>
                        {s.micro.funding_rate_pct > 0 ? '+' : ''}{s.micro.funding_rate_pct.toFixed(3)}%
                      </span></span>
                  {/if}
                  {#if s.micro.oi_change_24h_pct !== null}
                    <span><span class="opacity-70">OI</span> {s.micro.oi_change_24h_pct > 0 ? '+' : ''}{s.micro.oi_change_24h_pct.toFixed(1)}%</span>
                  {/if}
                  {#if s.micro.orderbook_imbalance !== null}
                    <span><span class="opacity-70">book</span> {s.micro.orderbook_imbalance > 0 ? '+' : ''}{s.micro.orderbook_imbalance.toFixed(2)}</span>
                  {/if}
                </span>
              {/if}
            </span>
            <span class="ml-auto flex flex-none items-center gap-1.5 text-[10.5px] tabular text-faint">
              {#if s.settled && s.ret_1d_pct !== null}
                <span class={s.ret_1d_pct >= 0 ? 'text-good' : 'text-bad'}>
                  {s.ret_1d_pct > 0 ? '+' : ''}{s.ret_1d_pct.toFixed(1)}%
                </span>
              {/if}
              <span class="rounded border border-border bg-surface-2 px-1 text-[9.5px] font-semibold text-muted">c{s.conviction}</span>
              {#if s.ts}<span>{timeAgo(s.ts)}</span>{/if}
            </span>
          </a>
        </li>
      {/each}
    </ul>
  {/if}
</Card>
