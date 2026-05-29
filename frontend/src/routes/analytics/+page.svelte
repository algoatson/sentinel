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
  import { hotTickers, calibration, attribution, sentimentQuality, monthlyPnl, concentration, dailyPnl, drawdownCurves, correlationMatrix, perfBySource, pnlDistribution } from '$api';
  import PnlCalendar from '$components/PnlCalendar.svelte';
  import PnlHistogram from '$components/PnlHistogram.svelte';
  import CorrelationMatrix from '$components/CorrelationMatrix.svelte';
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
  const sqQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['sentiment-quality', days],
    queryFn: () => sentimentQuality(days),
    refetchInterval: 10 * 60_000
  })));
  const monthlyQ = createQuery({
    queryKey: ['monthly', 12],
    queryFn: () => monthlyPnl(12),
    refetchInterval: 10 * 60_000
  });
  const concQ = createQuery({
    queryKey: ['concentration'],
    queryFn: concentration,
    refetchInterval: 60_000
  });
  const dailyQ = createQuery({
    queryKey: ['daily-pnl', 180],
    queryFn: () => dailyPnl(180),
    refetchInterval: 5 * 60_000
  });
  const ddQ = createQuery({
    queryKey: ['drawdown-curves', 90],
    queryFn: () => drawdownCurves(90),
    refetchInterval: 5 * 60_000
  });
  const corrQ = createQuery({
    queryKey: ['correlation', 30],
    queryFn: () => correlationMatrix(undefined, 30),
    refetchInterval: 5 * 60_000
  });
  const pbsQ = createQuery({
    queryKey: ['perf-by-source'],
    queryFn: () => perfBySource(500),
    refetchInterval: 5 * 60_000
  });
  const pnldQ = createQuery({
    queryKey: ['pnl-distribution'],
    queryFn: () => pnlDistribution(500),
    refetchInterval: 5 * 60_000
  });

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
  <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
    <BarChart3 class="h-5 w-5 text-primary" /><span>Analytics</span>
  </h1>

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

<!-- ── SENTIMENT QUALITY ─────────────────────────────── -->
<Card class="mt-4 px-4 py-3">
  <div class="mb-2 flex items-baseline gap-3">
    <div class="flex items-center gap-1.5">
      <Newspaper class="h-3.5 w-3.5 text-primary" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Sentiment quality
      </div>
    </div>
    <span class="text-[10.5px] text-faint">
      did the bot's news-sentiment direction predict next-day price?
    </span>
  </div>

  {#if !$sqQ.data || !$sqQ.data.overall.n}
    <EmptyState
      title="No graded news in window"
      description="Needs items with both a sentiment score AND a populated 24h impact."
    />
  {:else}
    {@const o = $sqQ.data.overall}
    {@const dir = (o.right + o.wrong) || 1}
    <div class="grid grid-cols-2 gap-3 md:grid-cols-4">
      <div class="rounded-lg border border-border bg-surface-2 px-3 py-2">
        <div class="text-[10px] uppercase tracking-wider text-faint">Sample</div>
        <div class="mt-0.5 text-[18px] font-semibold tabular text-text">{o.n}</div>
        <div class="text-[10.5px] text-faint">graded · {$sqQ.data.window_days}d</div>
      </div>
      <div class="rounded-lg border border-border bg-surface-2 px-3 py-2">
        <div class="text-[10px] uppercase tracking-wider text-faint">Directional accuracy</div>
        <div class={[
          'mt-0.5 text-[18px] font-semibold tabular',
          (o.directional_accuracy ?? 0) >= 0.55 ? 'text-good' :
          (o.directional_accuracy ?? 0) < 0.45 ? 'text-bad' : 'text-text'
        ].join(' ')}>
          {o.directional_accuracy !== null ? `${(o.directional_accuracy * 100).toFixed(0)}%` : '—'}
        </div>
        <div class="text-[10.5px] text-faint">{o.right}/{o.right + o.wrong} dir. right</div>
      </div>
      <div class="rounded-lg border border-border bg-surface-2 px-3 py-2">
        <div class="text-[10px] uppercase tracking-wider text-faint">Muted moves</div>
        <div class="mt-0.5 text-[18px] font-semibold tabular text-faint">{o.muted}</div>
        <div class="text-[10.5px] text-faint">|impact| ≤ 0.5%</div>
      </div>
      <div class="rounded-lg border border-border bg-surface-2 px-3 py-2">
        <div class="text-[10px] uppercase tracking-wider text-faint">Neutral scores</div>
        <div class="mt-0.5 text-[18px] font-semibold tabular text-faint">{o.neutral}</div>
        <div class="text-[10.5px] text-faint">|sentiment| ≤ 0.15</div>
      </div>
    </div>

    {#if $sqQ.data.by_source.length}
      <div class="mt-3 grid grid-cols-1 gap-1.5 md:grid-cols-2 lg:grid-cols-3">
        {#each $sqQ.data.by_source as src (src.source)}
          {@const directional = src.right + src.wrong}
          <div class="flex items-center justify-between rounded-md border border-border-soft bg-surface-2/40 px-2.5 py-1.5 text-[11.5px] tabular">
            <span class="truncate text-muted" title={src.source}>{src.source}</span>
            <span class={[
              'ml-2 font-medium',
              src.directional_accuracy === null ? 'text-faint'
                : src.directional_accuracy >= 0.55 ? 'text-good'
                : src.directional_accuracy < 0.45 ? 'text-bad' : 'text-text'
            ].join(' ')}>
              {src.directional_accuracy !== null
                ? `${(src.directional_accuracy * 100).toFixed(0)}%`
                : '—'}
              <span class="ml-1 text-[10px] text-faint">({src.right}/{directional || src.n})</span>
            </span>
          </div>
        {/each}
      </div>
    {/if}
  {/if}
</Card>

<!-- ── MONTHLY P&L MATRIX ───────────────────────────── -->
<Card class="mt-4 px-4 py-3">
  <div class="mb-2 flex items-baseline gap-3">
    <div class="flex items-center gap-1.5">
      <BarChart3 class="h-3.5 w-3.5 text-good" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Month-over-month P&L
      </div>
    </div>
    <span class="text-[10.5px] text-faint">
      realised PnL per wallet × month (12 months)
    </span>
  </div>

  {#if !$monthlyQ.data || !$monthlyQ.data.wallets.length}
    <EmptyState title="No closed trades to attribute" />
  {:else}
    {@const data = $monthlyQ.data}
    {@const maxAbs = Math.max(
      1,
      ...data.wallets.flatMap((w) => w.cells.map((c) => Math.abs(c.realized_pnl)))
    )}
    <div class="overflow-x-auto">
      <table class="w-full text-[11.5px] tabular">
        <thead>
          <tr class="border-b border-border text-[10px] uppercase tracking-wider text-faint">
            <th class="px-2 py-1.5 text-left">Wallet</th>
            {#each data.months as m (m)}
              <th class="px-2 py-1.5 text-center font-mono">{m.slice(5)}/{m.slice(2, 4)}</th>
            {/each}
            <th class="px-2 py-1.5 text-right">Total</th>
          </tr>
        </thead>
        <tbody>
          {#each data.wallets as w (w.wallet)}
            <tr class="border-b border-border-soft">
              <td class="px-2 py-1.5 text-left capitalize text-muted">{w.wallet}</td>
              {#each w.cells as c (c.month)}
                {@const intensity = Math.min(1, Math.abs(c.realized_pnl) / maxAbs)}
                {@const bg = c.realized_pnl > 0
                  ? `rgba(61, 220, 151, ${(intensity * 0.5).toFixed(2)})`
                  : c.realized_pnl < 0
                    ? `rgba(255, 107, 107, ${(intensity * 0.5).toFixed(2)})`
                    : 'transparent'}
                <td
                  class={[
                    'px-2 py-1.5 text-center',
                    c.realized_pnl > 0 ? 'text-good' : c.realized_pnl < 0 ? 'text-bad' : 'text-faint'
                  ].join(' ')}
                  style:background-color={bg}
                  title={`${c.month} · ${c.wins}/${c.closed} won`}
                >
                  {c.closed > 0 ? (c.realized_pnl >= 0 ? '+' : '') + c.realized_pnl.toFixed(0) : '—'}
                </td>
              {/each}
              <td class={[
                'px-2 py-1.5 text-right font-semibold',
                w.total_pnl > 0 ? 'text-good' : w.total_pnl < 0 ? 'text-bad' : 'text-text'
              ].join(' ')}>
                {w.total_pnl >= 0 ? '+' : ''}{w.total_pnl.toFixed(0)}
                <span class="ml-1 text-[10px] text-faint">{w.total_wins}/{w.total_closed}</span>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</Card>

<!-- ── CONCENTRATION ─────────────────────────────── -->
<Card class="mt-4 px-4 py-3">
  <div class="mb-2 flex items-baseline gap-3">
    <div class="flex items-center gap-1.5">
      <FileText class="h-3.5 w-3.5 text-warn" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Sector concentration
      </div>
    </div>
    <span class="text-[10.5px] text-faint">
      open-position exposure by asset class per wallet
    </span>
  </div>

  {#if !$concQ.data || !Object.keys($concQ.data.wallets).length}
    <EmptyState title="No open positions" />
  {:else}
    <div class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
      {#each Object.entries($concQ.data.wallets) as [name, w] (name)}
        <div class="rounded-lg border border-border bg-surface-2/40 px-3 py-2.5">
          <div class="flex items-baseline justify-between">
            <div class="text-[12px] font-semibold capitalize text-text">{name}</div>
            <div class="text-[10.5px] tabular text-faint">
              ${w.total_notional.toFixed(0)} notional
            </div>
          </div>
          <div class="mt-2 space-y-1">
            {#each w.groups as g (g.asset_class)}
              <div>
                <div class="flex items-center justify-between text-[10.5px] tabular">
                  <span class={[
                    'truncate',
                    g.pct >= 50 ? 'text-bad font-medium' :
                    g.pct >= 35 ? 'text-warn' : 'text-muted'
                  ].join(' ')}>{g.asset_class}</span>
                  <span class={[
                    g.pct >= 50 ? 'text-bad' :
                    g.pct >= 35 ? 'text-warn' : 'text-muted'
                  ].join(' ')}>{g.pct.toFixed(0)}%</span>
                </div>
                <div class="mt-0.5 flex h-1 overflow-hidden rounded-full bg-bg/40">
                  <div
                    style:width="{g.pct}%"
                    style:background-color={
                      g.pct >= 50 ? 'var(--color-bad)' :
                      g.pct >= 35 ? 'var(--color-warn)' : 'var(--color-primary)'
                    }
                  ></div>
                </div>
                <div class="mt-0.5 text-[9.5px] text-faint tabular truncate" title={g.tickers.join(', ')}>
                  {g.tickers.slice(0, 6).join(' · ')}{g.tickers.length > 6 ? ' …' : ''}
                </div>
              </div>
            {/each}
          </div>
        </div>
      {/each}
    </div>
  {/if}
</Card>

<!-- ── PNL CALENDAR HEATMAP ─────────────────────────── -->
<Card class="mt-4 px-4 py-3">
  <div class="mb-2 flex items-baseline gap-3">
    <div class="flex items-center gap-1.5">
      <BarChart3 class="h-3.5 w-3.5 text-good" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        P&amp;L calendar (180d)
      </div>
    </div>
    <span class="text-[10.5px] text-faint">
      one cell per day · green = winning day · red = losing day · intensity ∝ |pnl|
    </span>
  </div>
  <PnlCalendar data={$dailyQ.data} />
</Card>

<!-- ── PER-WALLET DRAWDOWN ─────────────────────────── -->
<Card class="mt-4 px-4 py-3">
  <div class="mb-2 flex items-baseline gap-3">
    <div class="flex items-center gap-1.5">
      <TrendingUp class="h-3.5 w-3.5 text-warn" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Drawdown by wallet (90d)
      </div>
    </div>
    <span class="text-[10.5px] text-faint">
      peak-to-current % · risk_circuit pauses a wallet at -15%
    </span>
  </div>

  {#if !$ddQ.data || !$ddQ.data.wallets.length}
    <EmptyState title="No equity history yet" />
  {:else}
    <div class="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
      {#each $ddQ.data.wallets as w (w.fund)}
        <div class="rounded-lg border border-border bg-surface-2/40 px-3 py-2.5">
          <div class="flex items-baseline justify-between">
            <span class="text-[12.5px] font-semibold capitalize text-text">{w.fund}</span>
            <span class={[
              'tabular text-[12px] font-semibold',
              w.current_dd_pct >= -2 ? 'text-good' :
              w.current_dd_pct >= -7 ? 'text-warn' : 'text-bad'
            ].join(' ')}>
              {w.current_dd_pct.toFixed(2)}%
            </span>
          </div>
          <div class="mt-1 flex items-baseline justify-between text-[10.5px] tabular text-faint">
            <span>worst {w.max_dd_pct.toFixed(2)}%</span>
            <span>{w.points.length} marks</span>
          </div>

          <!-- drawdown sparkline (0% at top, deeper down = more red) -->
          {#if w.points.length > 1}
            {@const minDd = Math.min(...w.points.map((p) => p.drawdown_pct), 0)}
            {@const ddRange = Math.max(1, -minDd)}
            {@const ptStr = w.points.map((p, i) => {
              const x = (i / (w.points.length - 1)) * 100;
              const y = (-p.drawdown_pct / ddRange) * 36;
              return `${x.toFixed(2)},${y.toFixed(2)}`;
            }).join(' ')}
            <svg viewBox="0 0 100 36" preserveAspectRatio="none" class="mt-2 h-9 w-full">
              <line x1="0" x2="100" y1="0" y2="0" stroke="var(--color-border)" stroke-dasharray="1 2" />
              <polyline
                points={ptStr}
                fill="none"
                stroke={w.max_dd_pct <= -10 ? 'var(--color-bad)' : 'var(--color-warn)'}
                stroke-width="1.4"
                vector-effect="non-scaling-stroke"
              />
            </svg>
            <div class="mt-0.5 flex justify-between text-[9px] tabular text-faint">
              <span>0%</span>
              <span>{minDd.toFixed(1)}%</span>
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</Card>

<!-- ── POSITION CORRELATION ─────────────────────────── -->
<Card class="mt-4 px-4 py-3">
  <div class="mb-2 flex items-baseline gap-3">
    <div class="flex items-center gap-1.5">
      <BarChart3 class="h-3.5 w-3.5 text-bad" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Position correlation (30d log returns)
      </div>
    </div>
    <span class="text-[10.5px] text-faint">
      red = positive · blue = negative · diagonal hidden from intensity
    </span>
  </div>
  <CorrelationMatrix data={$corrQ.data} />
</Card>

<!-- ── PERFORMANCE BY SOURCE ──────────────────────── -->
<Card class="mt-4 px-4 py-3">
  <div class="mb-2 flex items-baseline gap-3">
    <div class="flex items-center gap-1.5">
      <TrendingUp class="h-3.5 w-3.5 text-good" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Performance by source · which setup actually works
      </div>
    </div>
    {#if $pbsQ.data}
      <span class="text-[10.5px] text-faint">
        {$pbsQ.data.n} closed trades · {$pbsQ.data.groups.length} entry sources
      </span>
    {/if}
  </div>
  {#if !$pbsQ.data}
    <div class="py-6 text-center text-[12px] text-faint">Loading…</div>
  {:else if $pbsQ.data.n === 0}
    <div class="py-6 text-center text-[12px] text-faint">
      No closed trades yet — once trades close they'll cluster here by entry source.
    </div>
  {:else}
    <div class="overflow-x-auto">
      <table class="w-full text-[12px] tabular">
        <thead>
          <tr class="border-b border-border bg-surface-2/40 text-[10px] uppercase tracking-wider text-faint">
            <th class="px-2 py-2 text-left">Source</th>
            <th class="px-2 py-2 text-right">N</th>
            <th class="px-2 py-2 text-right">Win %</th>
            <th class="px-2 py-2 text-right">Total PnL</th>
            <th class="px-2 py-2 text-right">Expectancy</th>
            <th class="px-2 py-2 text-right">Avg W / L</th>
            <th class="px-2 py-2 text-right">Avg R</th>
            <th class="px-2 py-2 text-right">Hold</th>
            <th class="px-2 py-2 text-right">Recent</th>
          </tr>
        </thead>
        <tbody>
          {#each $pbsQ.data.groups as g (g.source)}
            <tr class="border-b border-border-soft">
              <td class="px-2 py-2 text-left font-mono text-text">{g.source}</td>
              <td class="px-2 py-2 text-right text-text">{g.n}</td>
              <td class={[
                'px-2 py-2 text-right',
                g.win_rate === null ? 'text-faint' :
                g.win_rate >= 55 ? 'text-good' : g.win_rate >= 40 ? 'text-muted' : 'text-bad'
              ].join(' ')}>
                {g.win_rate !== null ? `${g.win_rate.toFixed(0)}%` : '—'}
                <span class="ml-1 text-[9.5px] text-faint">
                  {g.wins}W/{g.losses}L
                </span>
              </td>
              <td class={[
                'px-2 py-2 text-right font-semibold',
                g.total_pnl >= 0 ? 'text-good' : 'text-bad'
              ].join(' ')}>
                {g.total_pnl >= 0 ? '+' : ''}{g.total_pnl.toFixed(2)}
              </td>
              <td class={[
                'px-2 py-2 text-right',
                g.expectancy >= 0 ? 'text-good' : 'text-bad'
              ].join(' ')}>
                {g.expectancy >= 0 ? '+' : ''}{g.expectancy.toFixed(2)}
              </td>
              <td class="px-2 py-2 text-right">
                <span class="text-good">{g.avg_win !== null ? `+${g.avg_win.toFixed(0)}` : '—'}</span>
                <span class="text-faint"> / </span>
                <span class="text-bad">{g.avg_loss !== null ? g.avg_loss.toFixed(0) : '—'}</span>
              </td>
              <td class={[
                'px-2 py-2 text-right',
                g.avg_r === null ? 'text-faint' :
                g.avg_r >= 0 ? 'text-good' : 'text-bad'
              ].join(' ')}>
                {g.avg_r !== null ? `${g.avg_r >= 0 ? '+' : ''}${g.avg_r.toFixed(2)}R` : '—'}
              </td>
              <td class="px-2 py-2 text-right text-muted">
                {#if g.avg_hold_h !== null}
                  {g.avg_hold_h < 24 ? `${g.avg_hold_h.toFixed(1)}h` : `${(g.avg_hold_h / 24).toFixed(1)}d`}
                {:else}
                  —
                {/if}
              </td>
              <td class="px-2 py-2 text-right">
                <div class="inline-flex items-center gap-0.5">
                  {#each g.recent_pnls as p, i (i)}
                    <span
                      class={[
                        'inline-block h-2.5 w-1.5 rounded-sm',
                        p > 0 ? 'bg-good' : p < 0 ? 'bg-bad' : 'bg-faint/40'
                      ].join(' ')}
                      title={`${p >= 0 ? '+' : ''}${p.toFixed(2)}`}
                    ></span>
                  {/each}
                </div>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</Card>

<!-- ── PNL DISTRIBUTION ───────────────────────────── -->
<Card class="mt-4 px-4 py-3">
  <div class="mb-2 flex items-baseline gap-3">
    <div class="flex items-center gap-1.5">
      <BarChart3 class="h-3.5 w-3.5 text-primary" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        PnL distribution · the shape of the edge
      </div>
    </div>
    {#if $pnldQ.data && $pnldQ.data.bin_width_pct}
      <span class="text-[10.5px] text-faint">{$pnldQ.data.bin_width_pct}% bins</span>
    {/if}
  </div>
  <PnlHistogram data={$pnldQ.data} />
</Card>
