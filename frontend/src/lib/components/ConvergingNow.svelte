<script lang="ts">
  /**
   * Tickers with multi-source signal stacking right now — same view
   * the convergence pipeline uses internally. Surfaces what the bot
   * is *about* to act on, not just what it has already acted on.
   *
   * Each row shows the source set as small badges (filing / news /
   * social / call) coloured by type. Click → /symbol/$X.
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { convergingNow } from '$api';
  import { base } from '$app/paths';
  import Card from './Card.svelte';
  import EmptyState from './EmptyState.svelte';
  import { Zap, FileText, Newspaper, MessageCircle, Megaphone } from 'lucide-svelte';
  import { timeAgo } from '$lib/format';

  const q = createQuery({
    queryKey: ['converging-now', 6],
    queryFn: () => convergingNow(6, 8),
    refetchInterval: 90_000,
  });

  const SOURCE_META: Record<string, { Icon: typeof FileText; tone: string }> = {
    filing: { Icon: FileText,       tone: 'border-primary/40 bg-primary-soft text-primary' },
    news:   { Icon: Newspaper,      tone: 'border-warn/40 bg-warn-soft text-warn' },
    social: { Icon: MessageCircle,  tone: 'border-violet/40 bg-violet-soft text-violet' },
    call:   { Icon: Megaphone,      tone: 'border-good/40 bg-good-soft text-good' },
  };
</script>

<Card class="px-4 py-3">
  <div class="mb-2 flex items-baseline gap-2">
    <Zap class="h-3.5 w-3.5 text-warn" />
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Converging now · last 6h
    </div>
    {#if $q.data}
      <span class="text-[10.5px] text-faint">
        {$q.data.rows.length} ticker{$q.data.rows.length === 1 ? '' : 's'} stacking ≥ 2 sources
      </span>
    {/if}
  </div>

  {#if !$q.data}
    <div class="py-4 text-center text-[12px] text-faint">Loading…</div>
  {:else if $q.data.rows.length === 0}
    <EmptyState
      title="No convergence right now"
      description="When a ticker stacks 2+ source types (filing + price + social + news) in the same window, it shows up here — that's what the convergence pipeline acts on."
    />
  {:else}
    <ul class="space-y-1">
      {#each $q.data.rows as r (r.ticker)}
        <li>
          <a
            href={`${base}/symbol/${encodeURIComponent(r.ticker)}`}
            class="flex items-center gap-2 rounded border border-border-soft bg-surface-2/40 px-2 py-1.5 hover:border-primary/40"
          >
            <span class="w-16 flex-none font-mono text-[12.5px] font-semibold text-text">
              ${r.ticker}
            </span>
            <span class="flex flex-wrap items-center gap-1">
              {#each r.sources as src (src)}
                {@const meta = SOURCE_META[src]}
                {#if meta}
                  <span class={['inline-flex items-center gap-0.5 rounded border px-1.5 py-0 text-[9.5px] uppercase tracking-wider', meta.tone].join(' ')}>
                    <meta.Icon class="h-2.5 w-2.5" />
                    {src}
                    {#if (src === 'filing' && r.filings > 1) || (src === 'news' && r.news > 1) || (src === 'social' && r.social > 1) || (src === 'call' && r.calls > 1)}
                      <span class="ml-0.5 opacity-80">×{r[src + 's' as keyof typeof r] ?? r[src as keyof typeof r]}</span>
                    {/if}
                  </span>
                {/if}
              {/each}
            </span>
            <span class="ml-auto flex items-center gap-1.5 text-[10.5px] tabular text-faint">
              {#if r.last_ts}
                <span>{timeAgo(r.last_ts)}</span>
              {/if}
              <span class="rounded border border-border bg-surface-2 px-1 text-[9.5px] font-semibold text-muted">
                {r.source_count}
              </span>
            </span>
          </a>
        </li>
      {/each}
    </ul>
  {/if}
</Card>
