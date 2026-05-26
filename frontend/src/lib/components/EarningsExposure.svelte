<script lang="ts">
  /**
   * Upcoming earnings reports for currently-held tickers.
   *
   * Compact card for Overview: summary tiles for "this week" / "this
   * month" + a horizontally-scrolling list of upcoming prints, each
   * with countdown + notional + uPnL.  Tickers held without a known
   * print date show in a secondary list so the trader can research.
   *
   * Polls /api/analytics/earnings-exposure once a minute.
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { earningsExposure } from '$api';
  import { base } from '$app/paths';
  import Card from './Card.svelte';
  import { CalendarClock, AlertTriangle } from 'lucide-svelte';
  import { price, pct } from '$lib/format';

  const q = createQuery({
    queryKey: ['earnings-exposure', 30],
    queryFn: () => earningsExposure(30),
    refetchInterval: 60_000
  });

  const data = $derived($q.data);

  function dayColour(d: number): string {
    if (d <= 1) return 'border-bad/50 bg-bad-soft text-bad';
    if (d <= 7) return 'border-warn/50 bg-warn-soft text-warn';
    return 'border-border bg-surface-2 text-muted';
  }
</script>

<Card class="flex h-full flex-col px-4 py-3">
  <div class="mb-2 flex items-center gap-2">
    <CalendarClock class="h-3.5 w-3.5 text-warn" />
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Earnings exposure
    </div>
    {#if data}
      <span class="text-[10.5px] text-faint">
        · next {data.window_days}d, {data.upcoming.length} held
      </span>
    {/if}
  </div>

  {#if !data}
    <div class="flex flex-1 items-center justify-center text-[12px] text-faint">Loading…</div>
  {:else if data.upcoming.length === 0 && data.unknown.length === 0}
    <div class="flex flex-1 items-center justify-center text-center text-[12px] text-faint">
      No open positions — nothing to track.
    </div>
  {:else}
    <div class="mb-2 grid grid-cols-2 gap-1.5">
      <div class="rounded-md border border-border bg-surface-2 px-2 py-1.5">
        <div class="text-[9.5px] uppercase tracking-wider text-faint">This week</div>
        <div class="mt-0.5 text-[16px] font-semibold tabular {data.this_week ? 'text-bad' : 'text-text'}">
          {data.this_week}
        </div>
      </div>
      <div class="rounded-md border border-border bg-surface-2 px-2 py-1.5">
        <div class="text-[9.5px] uppercase tracking-wider text-faint">This month</div>
        <div class="mt-0.5 text-[16px] font-semibold tabular {data.this_month ? 'text-warn' : 'text-text'}">
          {data.this_month}
        </div>
      </div>
    </div>

    {#if data.upcoming.length}
      <ul class="flex-1 space-y-1 overflow-y-auto">
        {#each data.upcoming as r (r.ticker)}
          <li class="flex items-center gap-2 rounded border border-border-soft bg-surface-2/40 px-2 py-1 text-[11.5px] tabular">
            <span class={[
              'rounded border px-1.5 py-0.5 text-[10px] font-medium',
              dayColour(r.days_until)
            ].join(' ')}>
              {r.days_until === 0 ? 'today' : r.days_until === 1 ? '1d' : `${r.days_until}d`}
            </span>
            <a
              href={`${base}/symbol/${encodeURIComponent(r.ticker)}`}
              class="font-mono font-semibold text-text hover:text-primary"
            >${r.ticker}</a>
            <span class="text-[10.5px] text-faint">{r.report_date}</span>
            <span class="ml-auto text-faint">{price(r.notional)}</span>
            <span class={(r.upnl ?? 0) >= 0 ? 'text-good' : 'text-bad'}>
              {(r.upnl ?? 0) >= 0 ? '+' : ''}{r.upnl.toFixed(2)}
            </span>
          </li>
        {/each}
      </ul>
    {/if}

    {#if data.unknown.length}
      <div class="mt-2">
        <div class="flex items-center gap-1 text-[9.5px] font-semibold uppercase tracking-wider text-faint">
          <AlertTriangle class="h-2.5 w-2.5 text-warn" /> No date known ({data.unknown.length})
        </div>
        <div class="mt-1 flex flex-wrap gap-1.5">
          {#each data.unknown as r (r.ticker)}
            <a
              href={`${base}/symbol/${encodeURIComponent(r.ticker)}`}
              class="rounded border border-border bg-surface-2/40 px-1.5 py-0.5 text-[10.5px] font-mono text-muted hover:border-warn/40 hover:text-warn"
            >${r.ticker}</a>
          {/each}
        </div>
      </div>
    {/if}
  {/if}
</Card>
