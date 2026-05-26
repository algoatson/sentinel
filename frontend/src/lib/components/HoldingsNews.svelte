<script lang="ts">
  /**
   * "Touches my book" — news + filings from the last 24h that mention
   * any ticker the bot currently holds. Each row is badged with the
   * wallet(s) holding the name so the trader sees at a glance which
   * piece of the book is affected.
   *
   * Polls /api/analytics/holdings-news every 90s.
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { holdingsNews } from '$api';
  import { base } from '$app/paths';
  import Card from './Card.svelte';
  import EmptyState from './EmptyState.svelte';
  import { Newspaper, FileText, Radio } from 'lucide-svelte';
  import { stripMd, timeAgo, pct } from '$lib/format';

  let tab: 'news' | 'filings' = $state('news');
  const q = createQuery({
    queryKey: ['holdings-news', 24],
    queryFn: () => holdingsNews(24, 30),
    refetchInterval: 90_000
  });

  const data = $derived($q.data);
  const newsCount = $derived(data?.news.length ?? 0);
  const filingsCount = $derived(data?.filings.length ?? 0);
</script>

<Card class="px-4 py-3">
  <div class="mb-2 flex items-baseline gap-3">
    <Radio class="h-3.5 w-3.5 text-primary" />
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Touches my book · last 24h
    </div>
    <div class="ml-auto flex items-center gap-1">
      <button
        type="button"
        onclick={() => (tab = 'news')}
        class={[
          'inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[10.5px] transition-colors',
          tab === 'news'
            ? 'border-primary/50 bg-primary-soft text-primary'
            : 'border-border bg-surface-2 text-muted hover:text-text'
        ].join(' ')}
      >
        <Newspaper class="h-3 w-3" />
        News
        {#if data}<span class="text-[9.5px] text-faint">{newsCount}</span>{/if}
      </button>
      <button
        type="button"
        onclick={() => (tab = 'filings')}
        class={[
          'inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[10.5px] transition-colors',
          tab === 'filings'
            ? 'border-primary/50 bg-primary-soft text-primary'
            : 'border-border bg-surface-2 text-muted hover:text-text'
        ].join(' ')}
      >
        <FileText class="h-3 w-3" />
        Filings
        {#if data}<span class="text-[9.5px] text-faint">{filingsCount}</span>{/if}
      </button>
    </div>
  </div>

  {#if !data}
    <div class="py-4 text-center text-[12px] text-faint">Loading…</div>
  {:else if !data.tickers.length}
    <div class="py-4 text-center text-[12px] text-faint">No open positions — nothing to filter for.</div>
  {:else if tab === 'news' && newsCount === 0}
    <EmptyState
      title="No news touching the book in 24h"
      description="As soon as a story mentions a held ticker, it'll show up here."
    />
  {:else if tab === 'filings' && filingsCount === 0}
    <EmptyState
      title="No new filings on held tickers"
      description="SEC drops appear here as soon as the catalyst pipeline ingests them."
    />
  {:else if tab === 'news'}
    <ul class="divide-y divide-border-soft">
      {#each data.news as n (n.id)}
        <li class="py-1.5">
          <a
            href={n.url}
            target="_blank"
            rel="noopener"
            class="block hover:bg-surface-2/40"
          >
            <div class="flex items-start gap-2">
              <div class="mt-0.5 flex flex-none gap-1">
                {#each n.held_tickers as t (t)}
                  <span class="rounded border border-primary/40 bg-primary-soft px-1 py-0.5 text-[9.5px] font-mono font-semibold text-primary">
                    ${t}
                  </span>
                {/each}
              </div>
              <div class="min-w-0 flex-1">
                <div class="line-clamp-1 text-[12px] text-text">{stripMd(n.title)}</div>
                <div class="mt-0.5 flex flex-wrap items-baseline gap-x-2 text-[10px] text-faint">
                  <span>{n.source}</span>
                  <span>·</span>
                  <span>{timeAgo(n.ts)}</span>
                  {#each n.funds as f (f)}
                    <span class="rounded border border-border bg-surface-2 px-1 capitalize">
                      {f}
                    </span>
                  {/each}
                  {#if n.impact_1d_pct !== null && n.impact_1d_pct !== undefined}
                    <span class={n.impact_1d_pct >= 0 ? 'text-good' : 'text-bad'}>
                      1d {pct(n.impact_1d_pct, 2)}
                    </span>
                  {/if}
                  {#if n.sentiment !== null && n.sentiment !== undefined}
                    <span class={[
                      'rounded px-1',
                      n.sentiment > 0 ? 'bg-good-soft text-good' :
                      n.sentiment < 0 ? 'bg-bad-soft text-bad' :
                      'bg-surface-2 text-muted'
                    ].join(' ')}>
                      {n.sentiment > 0 ? '+' : ''}{n.sentiment}
                    </span>
                  {/if}
                </div>
              </div>
            </div>
          </a>
        </li>
      {/each}
    </ul>
  {:else}
    <ul class="divide-y divide-border-soft">
      {#each data.filings as f (f.id)}
        <li class="py-1.5">
          <a
            href={f.url}
            target="_blank"
            rel="noopener"
            class="block hover:bg-surface-2/40"
          >
            <div class="flex items-start gap-2">
              <span class="mt-0.5 rounded border border-primary/40 bg-primary-soft px-1 py-0.5 text-[9.5px] font-mono font-semibold text-primary">
                ${f.ticker}
              </span>
              <span class="mt-0.5 rounded border border-border bg-surface-2 px-1 py-0.5 text-[9.5px] font-mono text-muted">
                {f.form_type}
              </span>
              <div class="min-w-0 flex-1">
                <div class="text-[12px] text-text">{f.form_type} · {f.ticker}</div>
                <div class="mt-0.5 flex flex-wrap items-baseline gap-x-2 text-[10px] text-faint">
                  <span>{timeAgo(f.filed_at)}</span>
                  {#each f.funds as fund (fund)}
                    <span class="rounded border border-border bg-surface-2 px-1 capitalize">
                      {fund}
                    </span>
                  {/each}
                  {#if f.materiality_score !== null && f.materiality_score !== undefined}
                    <span class={[
                      'rounded px-1',
                      f.materiality_score >= 7 ? 'bg-bad-soft text-bad' :
                      f.materiality_score >= 4 ? 'bg-warn-soft text-warn' :
                      'bg-surface-2 text-muted'
                    ].join(' ')}>
                      ×{f.materiality_score}
                    </span>
                  {/if}
                </div>
              </div>
            </div>
          </a>
        </li>
      {/each}
    </ul>
  {/if}
</Card>
