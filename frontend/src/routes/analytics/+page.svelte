<script lang="ts">
  /**
   * /analytics — the bot's "performance brain" view.
   *
   * Three sections, each backed by a pure-read endpoint:
   *  - Hot tickers (last 24h composite signal)
   *  - Calibration (reliability curve + Brier per source)
   *  - Attribution (per-source / per-conviction / per-direction P&L)
   *
   * Sections share a `days` window control so the user can toggle
   * 30d / 90d / 365d on one place.
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { reactiveQueryOptions } from '$lib/reactive-query.svelte';
  import { hotTickers, calibration, attribution } from '$api';
  import Card from '$components/Card.svelte';
  import Pill from '$components/Pill.svelte';
  import Spinner from '$components/Spinner.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import TickerLink from '$components/TickerLink.svelte';
  import { Flame, Target as TargetIcon, BarChart3, Brain, MessageCircle, Newspaper, FileText, TrendingUp } from 'lucide-svelte';

  let days = $state(90);
  let hotHours = $state(24);

  const hotQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['hot', hotHours],
    queryFn: () => hotTickers(hotHours, 12),
    refetchInterval: 60_000
  })));
  const calQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['calibration', days],
    queryFn: () => calibration(days),
    refetchInterval: 5 * 60_000
  })));
  const attQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['attribution', days],
    queryFn: () => attribution(days),
    refetchInterval: 5 * 60_000
  })));

  /** Colour-code by hit-rate vs predicted prob: green when realised ≥
   * predicted, red when realised much lower (overconfident). */
  function reliabilityClass(predicted: number, realised: number | null): string {
    if (realised === null) return 'text-faint';
    const diff = realised - predicted;
    if (diff >= -0.05) return 'text-good';
    if (diff >= -0.15) return 'text-warn';
    return 'text-bad';
  }
</script>

<svelte:head><title>Analytics · Sentinel</title></svelte:head>

<div class="mb-4 flex flex-wrap items-end justify-between gap-3 border-b border-border pb-3">
  <div>
    <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
      <BarChart3 class="h-5 w-5 text-primary" /><span>Analytics</span>
    </h1>
    <div class="mt-0.5 text-[11.5px] text-faint">
      Hot tickers · calibration (Brier + reliability) · realised-P&L
      attribution. The bot's signal layer, audited.
    </div>
  </div>

  <div class="flex items-center gap-1">
    <span class="mr-1 text-[10px] font-semibold uppercase tracking-wider text-faint">Window</span>
    {#each [30, 90, 365] as d (d)}
      <button
        onclick={() => (days = d)}
        class={[
          'rounded-md border px-2 py-0.5 text-[10.5px] transition-colors',
          days === d
            ? 'border-primary/50 bg-primary-soft text-primary'
            : 'border-border bg-surface-2 text-muted hover:text-text'
        ].join(' ')}
      >{d}d</button>
    {/each}
  </div>
</div>

<!-- ── HOT TICKERS ──────────────────────────────────────── -->
<Card class="px-4 py-3">
  <div class="mb-2 flex items-center gap-3">
    <div class="flex items-center gap-1.5">
      <Flame class="h-3.5 w-3.5 text-warn" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Hot tickers
      </div>
    </div>
    <span class="text-[10.5px] text-faint">
      composite signal across news, reddit, filings, calls, price
    </span>
    <div class="ml-auto flex items-center gap-1">
      {#each [6, 24, 72] as h (h)}
        <button
          onclick={() => (hotHours = h)}
          class={[
            'rounded-md border px-2 py-0.5 text-[10.5px] transition-colors',
            hotHours === h
              ? 'border-warn/40 bg-warn-soft text-warn'
              : 'border-border bg-surface-2 text-muted hover:text-text'
          ].join(' ')}
        >{h}h</button>
      {/each}
    </div>
  </div>

  {#if $hotQ.isLoading}
    <div class="flex justify-center py-6"><Spinner size={14} /></div>
  {:else if !$hotQ.data?.length}
    <EmptyState
      title="Nothing hot in this window"
      description="No tickers with enough multi-stream activity to cross the noise floor."
    />
  {:else}
    <div class="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
      {#each $hotQ.data as h (h.ticker)}
        <a
          href={`/app/symbol/${encodeURIComponent(h.ticker)}`}
          class="group rounded-lg border border-border bg-surface-2/40 px-3 py-2.5 transition-colors hover:border-warn/40"
        >
          <div class="flex items-center gap-2">
            <TickerLink ticker={h.ticker} class="text-[14px]" />
            <span class={[
              'tabular text-[11px] font-bold',
              h.score >= 60 ? 'text-bad' :
              h.score >= 40 ? 'text-warn' :
              h.score >= 25 ? 'text-good' : 'text-muted'
            ].join(' ')}>
              {h.score.toFixed(0)}
            </span>
            {#if h.in_watchlist}
              <Pill variant="info">WL</Pill>
            {/if}
            {#if h.best_call_direction}
              <Pill variant={h.best_call_direction === 'long' ? 'pos' : 'neg'}>
                {h.best_call_direction[0].toUpperCase()}{h.best_call_conv}
              </Pill>
            {/if}
            {#if h.price_move_pct !== null && Math.abs(h.price_move_pct) > 3}
              <span class="ml-auto text-[10.5px] tabular text-warn">
                {h.price_move_pct >= 0 ? '+' : '-'}{Math.abs(h.price_move_pct).toFixed(1)}%
              </span>
            {/if}
          </div>

          <!-- breakdown chips -->
          <div class="mt-2 flex flex-wrap gap-x-3 gap-y-0.5 text-[10.5px] tabular text-faint">
            {#if h.news_count > 0}
              <span class="flex items-center gap-1">
                <Newspaper class="h-2.5 w-2.5 text-primary" />
                {h.news_count}{#if h.news_sentiment_avg !== null} · {h.news_sentiment_avg > 0 ? '+' : ''}{h.news_sentiment_avg.toFixed(2)}{/if}
              </span>
            {/if}
            {#if h.reddit_count > 0}
              <span class="flex items-center gap-1">
                <MessageCircle class="h-2.5 w-2.5 text-warn" />
                {h.reddit_count} · ↑{h.reddit_score}
              </span>
            {/if}
            {#if h.filings_material > 0}
              <span class="flex items-center gap-1">
                <FileText class="h-2.5 w-2.5 text-violet" />
                {h.filings_material} mat{h.filings_max_mat ? ` ≤${h.filings_max_mat}` : ''}
              </span>
            {/if}
          </div>

          <!-- component stack bar -->
          <div class="mt-2 flex h-1 overflow-hidden rounded-full bg-bg/40">
            {#each [
              ['news', h.components.news, 'var(--color-primary)'],
              ['sent', h.components.sent, 'var(--color-primary)'],
              ['reddit', h.components.reddit, 'var(--color-warn)'],
              ['filings', h.components.filings, 'var(--color-violet)'],
              ['call', h.components.call, 'var(--color-good)'],
              ['price', h.components.price, 'var(--color-bad)'],
              ['spread', h.components.spread, 'var(--color-muted)']
            ] as [k, v, c] (k)}
              {#if v > 0}
                <div style:flex-grow={v} style:background-color={c} title={`${k}: ${v}`}></div>
              {/if}
            {/each}
          </div>
        </a>
      {/each}
    </div>
  {/if}
</Card>

<!-- ── CALIBRATION ────────────────────────────────────── -->
<Card class="mt-4 px-4 py-3">
  <div class="mb-2 flex items-baseline gap-3">
    <div class="flex items-center gap-1.5">
      <Brain class="h-3.5 w-3.5 text-violet" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Calibration
      </div>
    </div>
    <span class="text-[10.5px] text-faint">
      reliability curve · Brier score · per source
    </span>
  </div>

  {#if $calQ.isLoading}
    <div class="flex justify-center py-6"><Spinner size={14} /></div>
  {:else if !$calQ.data || !$calQ.data.n}
    <EmptyState
      title="No settled calls in this window"
      description="Calibration needs scored 5d returns; widen the window or wait for more calls to mature."
    />
  {:else}
    {@const c = $calQ.data}
    <div class="grid grid-cols-2 gap-3 md:grid-cols-4">
      <div class="rounded-lg border border-border bg-surface-2 px-3 py-2">
        <div class="text-[10px] uppercase tracking-wider text-faint">Sample</div>
        <div class="mt-0.5 text-[18px] font-semibold tabular text-text">{c.n}</div>
        <div class="text-[10.5px] text-faint">scored calls · {c.window_days}d</div>
      </div>
      <div class="rounded-lg border border-border bg-surface-2 px-3 py-2">
        <div class="text-[10px] uppercase tracking-wider text-faint">Hit rate</div>
        <div class={[
          'mt-0.5 text-[18px] font-semibold tabular',
          (c.hit_rate ?? 0) >= 0.55 ? 'text-good' :
          (c.hit_rate ?? 0) < 0.45 ? 'text-bad' : 'text-text'
        ].join(' ')}>
          {c.hit_rate !== null ? `${(c.hit_rate * 100).toFixed(0)}%` : '—'}
        </div>
        <div class="text-[10.5px] text-faint">{c.wins} winners</div>
      </div>
      <div class="rounded-lg border border-border bg-surface-2 px-3 py-2">
        <div class="text-[10px] uppercase tracking-wider text-faint">Brier</div>
        <div class={[
          'mt-0.5 text-[18px] font-semibold tabular',
          (c.brier ?? 1) <= 0.20 ? 'text-good' :
          (c.brier ?? 1) <= 0.27 ? 'text-text' : 'text-bad'
        ].join(' ')}>
          {c.brier !== null ? c.brier.toFixed(3) : '—'}
        </div>
        <div class="text-[10.5px] text-faint">0 = perfect, 0.25 = coinflip</div>
      </div>
      <div class="rounded-lg border border-border bg-surface-2 px-3 py-2">
        <div class="text-[10px] uppercase tracking-wider text-faint">Buckets</div>
        <div class="mt-0.5 text-[18px] font-semibold tabular text-text">{c.buckets.length}</div>
        <div class="text-[10.5px] text-faint">conviction tiers with data</div>
      </div>
    </div>

    <!-- reliability table -->
    <div class="mt-4 overflow-hidden rounded-lg border border-border">
      <table class="w-full text-[12px] tabular">
        <thead class="bg-surface-2 text-[10px] uppercase tracking-wider text-faint">
          <tr>
            <th class="px-3 py-1.5 text-left">Conv</th>
            <th class="px-3 py-1.5 text-right">Predicted</th>
            <th class="px-3 py-1.5 text-right">Realised</th>
            <th class="px-3 py-1.5 text-left">Diff</th>
            <th class="px-3 py-1.5 text-right">Brier</th>
            <th class="px-3 py-1.5 text-right">n</th>
          </tr>
        </thead>
        <tbody>
          {#each c.buckets as b (b.conviction)}
            {@const diff = (b.hit_rate ?? 0) - b.predicted_prob}
            <tr class="border-t border-border-soft">
              <td class="px-3 py-1.5 font-medium">{b.conviction}/5</td>
              <td class="px-3 py-1.5 text-right">{(b.predicted_prob * 100).toFixed(0)}%</td>
              <td class={['px-3 py-1.5 text-right', reliabilityClass(b.predicted_prob, b.hit_rate)].join(' ')}>
                {b.hit_rate !== null ? `${(b.hit_rate * 100).toFixed(0)}%` : '—'}
              </td>
              <td class={['px-3 py-1.5', reliabilityClass(b.predicted_prob, b.hit_rate)].join(' ')}>
                {diff >= 0 ? '+' : ''}{(diff * 100).toFixed(1)}%
              </td>
              <td class="px-3 py-1.5 text-right">{b.brier !== null ? b.brier.toFixed(3) : '—'}</td>
              <td class="px-3 py-1.5 text-right text-faint">{b.n}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <!-- per-source brier -->
    {#if Object.keys(c.by_source).length > 0}
      <div class="mt-3">
        <div class="mb-1 text-[10px] font-semibold uppercase tracking-wider text-faint">
          By source
        </div>
        <div class="grid grid-cols-2 gap-x-3 gap-y-1 text-[11.5px] tabular md:grid-cols-3 lg:grid-cols-4">
          {#each Object.values(c.by_source) as bs (bs.source)}
            <div class="flex items-center justify-between rounded border border-border-soft bg-surface-2/40 px-2 py-1">
              <span class="truncate text-muted" title={bs.source}>{bs.source}</span>
              <span class={[
                'ml-2 font-medium',
                (bs.brier ?? 1) <= 0.22 ? 'text-good' :
                (bs.brier ?? 1) <= 0.28 ? 'text-text' : 'text-bad'
              ].join(' ')}>
                B {bs.brier !== null ? bs.brier.toFixed(2) : '—'}
                <span class="ml-1 text-[10px] text-faint">({bs.wins}/{bs.n})</span>
              </span>
            </div>
          {/each}
        </div>
      </div>
    {/if}
  {/if}
</Card>

<!-- ── ATTRIBUTION ──────────────────────────────────── -->
<Card class="mt-4 px-4 py-3">
  <div class="mb-2 flex items-baseline gap-3">
    <div class="flex items-center gap-1.5">
      <TrendingUp class="h-3.5 w-3.5 text-good" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Signal attribution
      </div>
    </div>
    <span class="text-[10.5px] text-faint">
      who made the money? 5d-return basis, 1-unit notional per call
    </span>
  </div>

  {#if $attQ.isLoading}
    <div class="flex justify-center py-6"><Spinner size={14} /></div>
  {:else if !$attQ.data || (!$attQ.data.by_source.length && !$attQ.data.by_conviction.length)}
    <EmptyState title="No settled calls in window" />
  {:else}
    {@const a = $attQ.data}
    <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <!-- by source -->
      <div>
        <div class="mb-1 text-[10px] font-semibold uppercase tracking-wider text-faint">
          By source (sorted by total return)
        </div>
        {#if !a.by_source.length}
          <div class="text-[11.5px] text-faint">No data.</div>
        {:else}
          <div class="space-y-1">
            {#each a.by_source as r (r.source)}
              {@const positive = (r.ret_sum_pct ?? 0) >= 0}
              <div class="rounded-md border border-border-soft bg-surface-2/40 px-2.5 py-1.5">
                <div class="flex items-center gap-2 text-[12px]">
                  <span class="flex-1 truncate text-muted">{r.source}</span>
                  <span class={['tabular text-[12.5px] font-semibold', positive ? 'text-good' : 'text-bad'].join(' ')}>
                    {positive ? '+' : ''}{r.ret_sum_pct.toFixed(1)}%
                  </span>
                  <span class="text-[10.5px] tabular text-faint">{r.wins}/{r.n}</span>
                </div>
                <div class="mt-1 flex items-center gap-2 text-[10.5px] tabular text-faint">
                  <span>avg {r.ret_avg_pct?.toFixed(2)}%</span>
                  {#if r.best_pct !== undefined}<span class="text-good">best {r.best_pct.toFixed(1)}%</span>{/if}
                  {#if r.worst_pct !== undefined}<span class="text-bad">worst {r.worst_pct.toFixed(1)}%</span>{/if}
                </div>
              </div>
            {/each}
          </div>
        {/if}
      </div>

      <!-- by conviction + direction -->
      <div class="space-y-3">
        <div>
          <div class="mb-1 text-[10px] font-semibold uppercase tracking-wider text-faint">
            By conviction
          </div>
          {#if !a.by_conviction.length}
            <div class="text-[11.5px] text-faint">No data.</div>
          {:else}
            <div class="grid grid-cols-5 gap-1.5 text-center text-[11.5px] tabular">
              {#each [5, 4, 3, 2, 1] as bucket (bucket)}
                {@const r = a.by_conviction.find((x) => x.conviction === bucket)}
                <div class="rounded-md border border-border bg-surface-2 px-2 py-1.5">
                  <div class="text-[10px] uppercase tracking-wider text-faint">{bucket}/5</div>
                  {#if r}
                    <div class={['mt-0.5 text-[13px] font-semibold', (r.ret_sum_pct ?? 0) >= 0 ? 'text-good' : 'text-bad'].join(' ')}>
                      {(r.ret_sum_pct ?? 0) >= 0 ? '+' : ''}{r.ret_sum_pct.toFixed(0)}%
                    </div>
                    <div class="text-[10px] text-faint">{r.n}</div>
                  {:else}
                    <div class="mt-0.5 text-[13px] font-semibold text-faint">—</div>
                    <div class="text-[10px] text-faint">0</div>
                  {/if}
                </div>
              {/each}
            </div>
          {/if}
        </div>

        <div>
          <div class="mb-1 text-[10px] font-semibold uppercase tracking-wider text-faint">
            By direction
          </div>
          <div class="grid grid-cols-2 gap-1.5 text-[11.5px] tabular">
            {#each ['long', 'short'] as side (side)}
              {@const r = a.by_direction.find((x) => x.direction === side)}
              <div class={['rounded-md border px-2.5 py-1.5', side === 'long' ? 'border-good/30 bg-good-soft' : 'border-bad/30 bg-bad-soft'].join(' ')}>
                <div class="text-[10px] uppercase tracking-wider text-faint">{side}</div>
                {#if r}
                  <div class={['mt-0.5 text-[13px] font-semibold', (r.ret_sum_pct ?? 0) >= 0 ? 'text-good' : 'text-bad'].join(' ')}>
                    {(r.ret_sum_pct ?? 0) >= 0 ? '+' : ''}{r.ret_sum_pct.toFixed(1)}% · {r.wins}/{r.n}
                  </div>
                {:else}
                  <div class="mt-0.5 text-[13px] text-faint">no calls</div>
                {/if}
              </div>
            {/each}
          </div>
        </div>

        <!-- top + bottom tickers -->
        <div class="grid grid-cols-2 gap-2">
          <div>
            <div class="mb-1 text-[10px] font-semibold uppercase tracking-wider text-faint">
              Top tickers
            </div>
            {#if !a.top_tickers.length}
              <div class="text-[11px] text-faint">No data.</div>
            {:else}
              {#each a.top_tickers as t (t.ticker)}
                <div class="flex items-center justify-between text-[11.5px] tabular">
                  <TickerLink ticker={t.ticker!} class="text-[11.5px]" />
                  <span class="text-good">+{t.ret_sum_pct.toFixed(1)}%</span>
                </div>
              {/each}
            {/if}
          </div>
          <div>
            <div class="mb-1 text-[10px] font-semibold uppercase tracking-wider text-faint">
              Bottom tickers
            </div>
            {#if !a.bottom_tickers.length}
              <div class="text-[11px] text-faint">No losers in window 🎉</div>
            {:else}
              {#each a.bottom_tickers as t (t.ticker)}
                <div class="flex items-center justify-between text-[11.5px] tabular">
                  <TickerLink ticker={t.ticker!} class="text-[11.5px]" />
                  <span class="text-bad">{t.ret_sum_pct.toFixed(1)}%</span>
                </div>
              {/each}
            {/if}
          </div>
        </div>
      </div>
    </div>
  {/if}
</Card>
