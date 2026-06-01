<script lang="ts">
  /**
   * Crypto cockpit — one place for the autonomous crypto leg: the BTC
   * regime that gates entries, open crypto positions, the funding-squeeze
   * setups the detector is firing, and a microstructure screener (funding /
   * OI / book) across every tracked coin. Consolidates what was scattered
   * across Overview, Markets and the symbol pages.
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { cryptoScreener, openPositions } from '$api';
  import Card from '$components/Card.svelte';
  import CryptoSignals from '$components/CryptoSignals.svelte';
  import TickerLink from '$components/TickerLink.svelte';
  import Delta from '$components/Delta.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import Skeleton from '$components/Skeleton.svelte';
  import { base } from '$app/paths';
  import { usd, price, timeAgo } from '$lib/format';
  import { Bitcoin, ArrowUpRight, ArrowDownRight, BarChart3 } from 'lucide-svelte';

  const scrQ = createQuery({
    queryKey: ['crypto-screener'],
    queryFn: cryptoScreener,
    refetchInterval: 60_000
  });
  const posQ = createQuery({
    queryKey: ['positions-open'],
    queryFn: openPositions,
    refetchInterval: 60_000
  });

  const regime = $derived($scrQ.data?.regime ?? null);
  const cryptoPos = $derived(($posQ.data ?? []).filter((p) => p.asset_class === 'crypto'));
  const cryptoUpnl = $derived(cryptoPos.reduce((s, p) => s + (p.upnl ?? 0), 0));

  const REGIME: Record<string, { label: string; cls: string; glow: string; gate: string }> = {
    risk_on: {
      label: 'RISK-ON',
      cls: 'border-good/40 text-good',
      glow: 'rgba(61,220,151,0.16)',
      gate: 'New shorts are gated — the crypto leg won’t fade a rising tape. Longs & mean-reversion run normally.'
    },
    risk_off: {
      label: 'RISK-OFF',
      cls: 'border-bad/40 text-bad',
      glow: 'rgba(255,107,107,0.15)',
      gate: 'New longs are gated — the crypto leg won’t add long exposure into a falling tape. Shorts & fades still allowed.'
    },
    neutral: {
      label: 'NEUTRAL',
      cls: 'border-border text-muted',
      glow: 'rgba(102,153,255,0.12)',
      gate: 'No directional gate — both-side setups (squeeze longs, crowded-long fades) run normally.'
    }
  };
  const rm = $derived(REGIME[regime?.state ?? 'neutral'] ?? REGIME.neutral);

  // ── screener sort ──────────────────────────────────────────────
  type SortKey = 'ticker' | 'change_1d_pct' | 'funding_pct' | 'oi_change_24h_pct' | 'orderbook_imbalance';
  let sortKey: SortKey = $state('funding_pct');
  // funding ascending = most-negative first (deepest squeeze fuel up top)
  let sortDir: 'asc' | 'desc' = $state('asc');
  function setSort(k: SortKey) {
    if (sortKey === k) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    else { sortKey = k; sortDir = k === 'ticker' ? 'asc' : 'asc'; }
  }
  const coins = $derived(
    [...($scrQ.data?.coins ?? [])].sort((a, b) => {
      const va = (a as any)[sortKey], vb = (b as any)[sortKey];
      if (sortKey === 'ticker') {
        return sortDir === 'asc' ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
      }
      const na = typeof va === 'number' ? va : (sortDir === 'asc' ? Infinity : -Infinity);
      const nb = typeof vb === 'number' ? vb : (sortDir === 'asc' ? Infinity : -Infinity);
      return sortDir === 'asc' ? na - nb : nb - na;
    })
  );
  const arrow = (k: SortKey) => (sortKey !== k ? '' : sortDir === 'asc' ? ' ↑' : ' ↓');

  function fundCls(v: number | null): string {
    if (v === null || v === undefined) return 'text-faint';
    return v < 0 ? 'text-good' : v >= 0.05 ? 'text-bad' : 'text-muted';
  }
  function bookCls(v: number | null): string {
    if (v === null || v === undefined) return 'text-faint';
    return v > 0.1 ? 'text-good' : v < -0.1 ? 'text-bad' : 'text-muted';
  }
</script>

<svelte:head><title>Crypto · Sentinel</title></svelte:head>

<div class="mb-4 flex items-end justify-between border-b border-border pb-3">
  <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
    <Bitcoin class="h-5 w-5 text-warn" /><span>Crypto</span>
  </h1>
  {#if cryptoPos.length}
    <div class="text-[11.5px] tabular text-faint">
      {cryptoPos.length} open ·
      <span class={cryptoUpnl >= 0 ? 'text-good' : 'text-bad'}>{usd(cryptoUpnl, true)} uPnL</span>
    </div>
  {/if}
</div>

<!-- ── REGIME BANNER ─────────────────────────────────────────── -->
<div class={['relative mb-4 overflow-hidden rounded-2xl border bg-gradient-to-br from-surface to-bg px-5 py-4', rm.cls].join(' ')}>
  <div
    class="pointer-events-none absolute -right-20 -top-24 h-56 w-56 rounded-full opacity-60 blur-3xl"
    style:background={`radial-gradient(circle, ${rm.glow}, transparent 70%)`}
  ></div>
  <div class="relative flex flex-wrap items-center gap-x-5 gap-y-2">
    <div>
      <div class="text-[10px] font-semibold uppercase tracking-[0.14em] text-faint">Crypto tape · BTC regime</div>
      <div class={['text-[1.9rem] font-bold leading-none tracking-tight', rm.cls.split(' ').pop()].join(' ')}>{rm.label}</div>
    </div>
    {#if regime}
      <div class="flex items-end gap-4 tabular">
        <div>
          <div class="text-[9.5px] uppercase tracking-wider text-faint">BTC 1d</div>
          <div class={['text-[15px] font-semibold', (regime.btc_1d_pct ?? 0) >= 0 ? 'text-good' : 'text-bad'].join(' ')}>
            {regime.btc_1d_pct !== null ? `${regime.btc_1d_pct > 0 ? '+' : ''}${regime.btc_1d_pct.toFixed(1)}%` : '—'}
          </div>
        </div>
        <div>
          <div class="text-[9.5px] uppercase tracking-wider text-faint">BTC 5d</div>
          <div class={['text-[15px] font-semibold', (regime.btc_5d_pct ?? 0) >= 0 ? 'text-good' : 'text-bad'].join(' ')}>
            {regime.btc_5d_pct !== null ? `${regime.btc_5d_pct > 0 ? '+' : ''}${regime.btc_5d_pct.toFixed(1)}%` : '—'}
          </div>
        </div>
      </div>
    {/if}
    <div class="ml-auto max-w-md text-[11.5px] leading-snug text-muted">{rm.gate}</div>
  </div>
</div>

<!-- ── OPEN CRYPTO POSITIONS ─────────────────────────────────── -->
<Card class="mb-4 px-4 py-3">
  <div class="mb-2 flex items-baseline gap-2">
    <div class="sect-accent text-[10px] font-semibold uppercase tracking-wider text-faint">Open crypto positions</div>
    <span class="text-[10.5px] text-faint">{cryptoPos.length}</span>
  </div>
  {#if $posQ.isLoading}
    <div class="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
      {#each Array(3) as _, i (i)}<Skeleton class="h-10 w-full rounded" />{/each}
    </div>
  {:else if !cryptoPos.length}
    <EmptyState icon={Bitcoin} title="No open crypto positions" description="The crypto wallet opens positions off funding-squeeze, why-moved and convergence calls — gated by the BTC regime above." />
  {:else}
    <div class="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
      {#each cryptoPos as p (p.id)}
        <a href={`${base}/symbol/${encodeURIComponent(p.ticker)}`}
           class="flex items-center gap-2 rounded border border-border-soft bg-surface-2/40 px-2.5 py-1.5 hover:border-warn/40">
          <span class={['flex-none', p.side === 'long' ? 'text-good' : 'text-bad'].join(' ')}>
            {#if p.side === 'long'}<ArrowUpRight class="h-3.5 w-3.5" />{:else}<ArrowDownRight class="h-3.5 w-3.5" />{/if}
          </span>
          <TickerLink ticker={p.ticker} class="text-[12.5px] font-semibold" />
          <span class="text-[10px] uppercase tracking-wider text-faint">{p.fund}</span>
          <span class="ml-auto text-right tabular">
            <span class={['block text-[12px] font-semibold', (p.upnl ?? 0) >= 0 ? 'text-good' : 'text-bad'].join(' ')}>
              {usd(p.upnl, true)}
            </span>
            <span class={['block text-[9.5px]', (p.upnl_pct ?? 0) >= 0 ? 'text-good' : 'text-bad'].join(' ')}>
              {(p.upnl_pct ?? 0) > 0 ? '+' : ''}{(p.upnl_pct ?? 0).toFixed(1)}%
            </span>
          </span>
        </a>
      {/each}
    </div>
  {/if}
</Card>

<!-- ── FUNDING-SQUEEZE SIGNALS (reused) ──────────────────────── -->
<div class="mb-4">
  <CryptoSignals />
</div>

<!-- ── MICROSTRUCTURE SCREENER ───────────────────────────────── -->
<Card class="overflow-hidden">
  <div class="flex items-center gap-2 border-b border-border px-4 py-2.5">
    <div class="sect-accent text-[10px] font-semibold uppercase tracking-wider text-faint">Microstructure screener</div>
    <span class="text-[10.5px] text-faint">funding · OI · book, across {coins.length} coins</span>
    <span class="ml-auto text-[10px] text-faint">sorted by {sortKey.replace('_pct', '').replace('_24h', '')}{arrow(sortKey)}</span>
  </div>
  {#if $scrQ.isLoading}
    <div class="space-y-2 p-3">
      {#each Array(10) as _, i (i)}<Skeleton class="h-6 w-full rounded" />{/each}
    </div>
  {:else if !coins.length}
    <EmptyState icon={BarChart3} title="No crypto microstructure yet" description="The crypto_micro ingester polls Binance/OKX every 20 min." />
  {:else}
    <div class="overflow-x-auto">
      <table class="w-full text-[12.5px] tabular">
        <thead>
          <tr class="border-b border-border text-[10px] uppercase tracking-wider text-faint">
            {#snippet th(label: string, key: SortKey, align = 'right')}
              <th class={['cursor-pointer select-none px-3 py-2 font-semibold hover:text-text', align === 'left' ? 'text-left' : 'text-right'].join(' ')}
                  onclick={() => setSort(key)}>{label}{arrow(key)}</th>
            {/snippet}
            {@render th('Coin', 'ticker', 'left')}
            {@render th('Last', 'ticker')}
            {@render th('1d %', 'change_1d_pct')}
            {@render th('Funding 8h', 'funding_pct')}
            {@render th('OI 24h', 'oi_change_24h_pct')}
            {@render th('Book', 'orderbook_imbalance')}
            <th class="px-3 py-2 text-right font-semibold">Upd</th>
          </tr>
        </thead>
        <tbody>
          {#each coins as c (c.ticker)}
            <tr class="border-b border-border-soft transition-colors even:bg-white/[0.018] hover:bg-white/[0.045]">
              <td class="px-3 py-1.5 text-left"><TickerLink ticker={c.ticker} class="text-[12.5px] font-semibold" /></td>
              <td class="px-3 py-1.5 text-right text-muted">{price(c.last_price)}</td>
              <td class="px-3 py-1.5 text-right"><Delta value={c.change_1d_pct} /></td>
              <td class={['px-3 py-1.5 text-right font-medium', fundCls(c.funding_pct)].join(' ')}>
                {c.funding_pct !== null ? `${c.funding_pct > 0 ? '+' : ''}${c.funding_pct.toFixed(3)}%` : '—'}
              </td>
              <td class="px-3 py-1.5 text-right text-muted">
                {c.oi_change_24h_pct !== null ? `${c.oi_change_24h_pct > 0 ? '+' : ''}${c.oi_change_24h_pct.toFixed(1)}%` : '—'}
              </td>
              <td class={['px-3 py-1.5 text-right', bookCls(c.orderbook_imbalance)].join(' ')}>
                {c.orderbook_imbalance !== null ? `${c.orderbook_imbalance > 0 ? '+' : ''}${c.orderbook_imbalance.toFixed(2)}` : '—'}
              </td>
              <td class="px-3 py-1.5 text-right text-[10.5px] text-faint">{c.updated_at ? timeAgo(c.updated_at) : '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</Card>
