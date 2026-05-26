<script lang="ts">
  /**
   * Compact wallet-allocation strip. Single stacked bar that shows
   * each wallet's share of the aggregate book equity. The colour
   * tracks each wallet's return tone (green up, red down) so the
   * bar simultaneously shows allocation AND who's working — useful
   * when a fund is huge but flat (large grey segment) vs. small but
   * outperforming (thin green segment).
   *
   * Reuses the cached `wallets` query.
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { wallets } from '$api';
  import { base } from '$app/paths';
  import Card from './Card.svelte';
  import { Briefcase } from 'lucide-svelte';
  import { usd, pct } from '$lib/format';

  const q = createQuery({
    queryKey: ['wallets'],
    queryFn: wallets,
    refetchInterval: 30_000
  });

  const PALETTE = [
    '#6699ff', '#3ddc97', '#a78bfa', '#fbbf24',
    '#ff6b6b', '#8ed1fc', '#f2c0ff', '#a3e635'
  ];

  const ranked = $derived.by(() => {
    const ws = $q.data ?? [];
    if (!ws.length) return null;
    const total = ws.reduce((s, w) => s + (w.equity ?? 0), 0);
    if (total <= 0) return null;
    const ordered = [...ws].sort((a, b) => (b.equity ?? 0) - (a.equity ?? 0));
    return ordered.map((w, i) => ({
      ...w,
      colour: PALETTE[i % PALETTE.length],
      share: ((w.equity ?? 0) / total) * 100,
    }));
  });
  const totalEquity = $derived(
    ($q.data ?? []).reduce((s, w) => s + (w.equity ?? 0), 0)
  );
</script>

{#if ranked && ranked.length}
  <Card class="px-4 py-3">
    <div class="mb-2 flex items-baseline gap-2">
      <Briefcase class="h-3.5 w-3.5 text-primary" />
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Allocation · {ranked.length} wallets · {usd(totalEquity)}
      </div>
      <a
        href={`${base}/portfolio`}
        class="ml-auto text-[10.5px] text-muted hover:text-primary hover:underline"
      >Portfolio →</a>
    </div>
    <!-- The stacked bar -->
    <div class="flex h-3 w-full overflow-hidden rounded-sm border border-border">
      {#each ranked as w (w.name)}
        <a
          href={`${base}/portfolio`}
          class="block transition-opacity hover:opacity-80"
          style:width="{w.share}%"
          style:background-color={w.colour}
          title={`${w.name}: ${usd(w.equity)} (${w.share.toFixed(1)}% of book) · ${pct(w.ret_pct ?? 0, 2)}`}
        ></a>
      {/each}
    </div>
    <!-- Legend -->
    <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10.5px] tabular">
      {#each ranked as w (w.name)}
        <a
          href={`${base}/portfolio`}
          class="inline-flex items-center gap-1 hover:underline"
        >
          <span class="inline-block h-2 w-2 rounded-sm" style:background-color={w.colour}></span>
          <span class="capitalize text-muted">{w.name}</span>
          <span class="text-faint">{w.share.toFixed(1)}%</span>
          {#if (w.ret_pct ?? 0) !== 0}
            <span class={(w.ret_pct ?? 0) >= 0 ? 'text-good' : 'text-bad'}>
              {pct(w.ret_pct ?? 0, 1)}
            </span>
          {/if}
        </a>
      {/each}
    </div>
  </Card>
{/if}
