<script lang="ts">
  /**
   * "What happened while I was in this trade" panel.
   *
   * Lazy-loads /positions/{id}/lifecycle the first time it's mounted
   * (so journal rows that stay collapsed never pay the cost). Renders
   * three small lists: news, filings, trading-calls — each between
   * entry_at and exit_at (or now, for an open position).
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { tradeLifecycle } from '$api';
  import { Newspaper, FileText, Megaphone } from 'lucide-svelte';
  import { stripMd, timeAgo, pct } from '$lib/format';

  interface Props {
    tradeId: number;
  }
  let { tradeId }: Props = $props();

  const q = createQuery({
    queryKey: ['trade-lifecycle', tradeId],
    queryFn: () => tradeLifecycle(tradeId),
    // Stable forever — the news/filings inside this window aren't
    // changing once the trade closes. Even on open trades, refreshing
    // when the panel is shown is fine (one minute cadence).
    staleTime: 5 * 60_000,
    refetchInterval: 60_000
  });

  const d = $derived($q.data);
  const total = $derived(
    d ? d.news.length + d.filings.length + d.calls.length : 0
  );
</script>

<div class="rounded-md border border-border bg-surface-2/40 p-2 text-[11px]">
  <div class="mb-1 flex items-center gap-2">
    <span class="text-[9.5px] font-semibold uppercase tracking-wider text-faint">
      During the trade
    </span>
    {#if d}
      <span class="text-[10px] text-faint">
        · {total} event{total === 1 ? '' : 's'}
      </span>
    {/if}
  </div>

  {#if $q.isLoading}
    <div class="py-2 text-center text-[11px] text-faint">Loading…</div>
  {:else if $q.isError}
    <div class="py-2 text-center text-[11px] text-bad">Failed to load lifecycle.</div>
  {:else if !d}
    <div class="py-2 text-center text-[11px] text-faint">No data.</div>
  {:else if total === 0}
    <div class="py-2 text-center text-[11px] italic text-faint">
      Nothing the bot ingested touched this ticker during the trade window.
    </div>
  {:else}
    <div class="space-y-2">
      {#if d.news.length}
        <div>
          <div class="mb-0.5 flex items-center gap-1 text-[10px] uppercase tracking-wider text-faint">
            <Newspaper class="h-2.5 w-2.5" /> News ({d.news.length})
          </div>
          <ul class="space-y-0.5 text-[11px]">
            {#each d.news.slice(0, 8) as n (n.id)}
              <li>
                <a
                  href={n.url}
                  target="_blank"
                  rel="noopener"
                  class="block hover:bg-surface-2/60"
                >
                  <div class="line-clamp-1 text-text">{stripMd(n.title)}</div>
                  <div class="flex flex-wrap items-baseline gap-x-2 text-[9.5px] text-faint">
                    <span>{n.source}</span>
                    <span>·</span>
                    <span>{timeAgo(n.ts)}</span>
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
                    {#if n.impact_1d_pct !== null && n.impact_1d_pct !== undefined}
                      <span class={n.impact_1d_pct >= 0 ? 'text-good' : 'text-bad'}>
                        1d {pct(n.impact_1d_pct, 2)}
                      </span>
                    {/if}
                  </div>
                </a>
              </li>
            {/each}
          </ul>
          {#if d.news.length > 8}
            <div class="mt-0.5 text-[9.5px] text-faint">… +{d.news.length - 8} more</div>
          {/if}
        </div>
      {/if}

      {#if d.filings.length}
        <div>
          <div class="mb-0.5 flex items-center gap-1 text-[10px] uppercase tracking-wider text-faint">
            <FileText class="h-2.5 w-2.5" /> Filings ({d.filings.length})
          </div>
          <ul class="space-y-0.5 text-[11px]">
            {#each d.filings as f (f.id)}
              <li>
                <a
                  href={f.url ?? '#'}
                  target="_blank"
                  rel="noopener"
                  class="flex items-baseline gap-2 hover:bg-surface-2/60"
                >
                  <span class="rounded border border-border bg-surface-2 px-1 py-0.5 text-[9.5px] font-mono text-muted">
                    {f.form_type}
                  </span>
                  <span class="text-[10.5px] text-faint">{timeAgo(f.filed_at)}</span>
                  {#if f.materiality_score !== null && f.materiality_score !== undefined}
                    <span class={[
                      'ml-auto rounded px-1 text-[9.5px]',
                      f.materiality_score >= 7 ? 'bg-bad-soft text-bad' :
                      f.materiality_score >= 4 ? 'bg-warn-soft text-warn' :
                      'bg-surface-2 text-muted'
                    ].join(' ')}>×{f.materiality_score}</span>
                  {/if}
                </a>
              </li>
            {/each}
          </ul>
        </div>
      {/if}

      {#if d.calls.length}
        <div>
          <div class="mb-0.5 flex items-center gap-1 text-[10px] uppercase tracking-wider text-faint">
            <Megaphone class="h-2.5 w-2.5" /> Bot calls ({d.calls.length})
          </div>
          <ul class="space-y-1 text-[11px]">
            {#each d.calls as c (c.id)}
              <li class="rounded border border-border-soft bg-surface-2/50 px-1.5 py-1">
                <div class="flex flex-wrap items-baseline gap-x-2 text-[10px] text-faint">
                  <span class="text-muted">{c.source}</span>
                  <span class={[
                    'rounded px-1 text-[9.5px] uppercase tracking-wider',
                    c.direction === 'long' ? 'bg-good-soft text-good' : 'bg-bad-soft text-bad'
                  ].join(' ')}>{c.direction}</span>
                  <span>conviction {c.conviction}/5</span>
                  <span>·</span>
                  <span>{timeAgo(c.created_at)}</span>
                  {#if c.ret_5d_pct !== null && c.ret_5d_pct !== undefined}
                    <span class={c.ret_5d_pct >= 0 ? 'text-good' : 'text-bad'}>
                      5d {pct(c.ret_5d_pct, 2)}
                    </span>
                  {/if}
                </div>
                {#if c.thesis}
                  <div class="mt-0.5 line-clamp-2 text-[11px] text-text">{c.thesis}</div>
                {/if}
              </li>
            {/each}
          </ul>
        </div>
      {/if}
    </div>
  {/if}
</div>
