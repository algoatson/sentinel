<script lang="ts">
  /**
   * Today's pulse — one-row "what fired in the last 24h" strip.
   * Designed for the Overview as a single dense informational
   * card just under the hero.
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { todayPulse } from '$api';
  import TickerLink from './TickerLink.svelte';
  import { usd } from '$lib/format';
  import {
    Newspaper, FileText, Target as TargetIcon, MessageCircle,
    TrendingUp, TrendingDown, Zap, Sparkles
  } from 'lucide-svelte';

  const q = createQuery({
    queryKey: ['today-pulse'],
    queryFn: todayPulse,
    refetchInterval: 60_000
  });
</script>

{#if $q.data}
  {@const t = $q.data}
  <div class="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-xl border border-border bg-surface px-4 py-3">
    <span class="text-[10px] font-semibold uppercase tracking-[0.13em] text-faint">
      Last 24h
    </span>

    <span class="inline-flex items-center gap-1.5 text-[12px] tabular text-muted">
      <Newspaper class="h-3 w-3 text-primary" />
      <span class="font-semibold text-text">{t.news_count}</span>
      <span>news</span>
    </span>
    <span class="inline-flex items-center gap-1.5 text-[12px] tabular text-muted">
      <FileText class="h-3 w-3 text-violet" />
      <span class="font-semibold text-text">{t.filings_count}</span>
      <span>filings</span>
    </span>
    <span class="inline-flex items-center gap-1.5 text-[12px] tabular text-muted">
      <TargetIcon class="h-3 w-3 text-good" />
      <span class="font-semibold text-text">{t.calls_count}</span>
      <span>calls</span>
    </span>
    <span class="inline-flex items-center gap-1.5 text-[12px] tabular text-muted">
      <MessageCircle class="h-3 w-3 text-warn" />
      <span class="font-semibold text-text">{t.reddit_count}</span>
      <span>reddit</span>
    </span>

    <span class="h-3 w-px bg-border"></span>

    <span class="inline-flex items-center gap-1.5 text-[12px] tabular">
      <Zap class="h-3 w-3 text-warn" />
      <span class={[
        'font-semibold tabular',
        t.realized_today > 0 ? 'text-good' :
        t.realized_today < 0 ? 'text-bad' : 'text-muted'
      ].join(' ')}>
        {usd(t.realized_today, true)}
      </span>
      <span class="text-faint">realised · {t.trades_closed} closed</span>
    </span>

    {#if t.trades_opened > 0}
      <span class="inline-flex items-center gap-1.5 text-[12px] tabular text-muted">
        <Sparkles class="h-3 w-3 text-primary" />
        <span class="font-semibold text-text">{t.trades_opened}</span>
        <span>opened</span>
      </span>
    {/if}

    {#if t.best_close}
      <span class="inline-flex items-center gap-1.5 text-[12px] tabular text-good">
        <TrendingUp class="h-3 w-3" />
        <span class="text-faint">best</span>
        <TickerLink ticker={t.best_close.ticker} class="text-[12px]" />
        <span>+{t.best_close.pnl.toFixed(0)}</span>
      </span>
    {/if}
    {#if t.worst_close}
      <span class="inline-flex items-center gap-1.5 text-[12px] tabular text-bad">
        <TrendingDown class="h-3 w-3" />
        <span class="text-faint">worst</span>
        <TickerLink ticker={t.worst_close.ticker} class="text-[12px]" />
        <span>{t.worst_close.pnl.toFixed(0)}</span>
      </span>
    {/if}

    {#if t.highest_conviction_call}
      <span class="ml-auto inline-flex items-center gap-1.5 text-[11.5px] tabular text-muted">
        <span class="text-faint">top call</span>
        <TickerLink ticker={t.highest_conviction_call.ticker} class="text-[12px]" />
        <span class="text-good">{t.highest_conviction_call.conviction}/5</span>
        <span class="text-faint">via {t.highest_conviction_call.source}</span>
      </span>
    {/if}
  </div>
{/if}
