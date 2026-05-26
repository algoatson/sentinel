<script lang="ts">
  /**
   * Recent LLM tool-call audit. Shows which pipeline asked the model
   * to call which tool, on which ticker, with what arguments and
   * what came back. Polled every 15s; backed by an in-memory ring
   * (bounded ~500 entries) so it's cheap.
   *
   * Helps debug "why did the bot reach this conclusion?" — if the
   * model spent its budget calling `get_atr` and `peer_movers` and
   * still wrote nonsense, you can see that here.
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { recentToolCalls } from '$api';
  import { base } from '$app/paths';
  import Card from './Card.svelte';
  import EmptyState from './EmptyState.svelte';
  import { Wrench, AlertTriangle, ArrowRight } from 'lucide-svelte';
  import { timeAgo } from '$lib/format';

  const q = createQuery({
    queryKey: ['tool-calls', 100],
    queryFn: () => recentToolCalls(100),
    refetchInterval: 15_000
  });
  const data = $derived($q.data);
  const items = $derived(data?.items ?? []);
  const stats = $derived(data?.stats);
</script>

<Card class="px-4 py-3">
  <div class="mb-2 flex items-baseline gap-3">
    <Wrench class="h-3.5 w-3.5 text-primary" />
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      LLM tool calls
    </div>
    {#if stats}
      <span class="text-[10.5px] text-faint">
        {stats.count} in window
        {#if stats.errors > 0}
          · <span class="text-bad">{stats.errors} errors</span>
        {/if}
      </span>
    {/if}
  </div>

  {#if stats && Object.keys(stats.by_tool).length > 0}
    <div class="mb-2 flex flex-wrap items-center gap-1.5 text-[10.5px] tabular">
      {#each Object.entries(stats.by_tool).sort((a, b) => b[1] - a[1]) as [name, count] (name)}
        <span class="inline-flex items-center gap-1 rounded border border-border bg-surface-2 px-1.5 py-0.5">
          <span class="font-mono text-text">{name}</span>
          <span class="text-faint">×{count}</span>
        </span>
      {/each}
    </div>
  {/if}

  {#if items.length === 0}
    <EmptyState
      title="No tool calls yet"
      description="The bot's pipelines drive the LLM through the tool registry only when a dossier needs more context. They'll show up here as soon as one fires."
    />
  {:else}
    <ul class="divide-y divide-border-soft text-[11.5px]">
      {#each items as e (e.id)}
        <li class="py-1.5">
          <div class="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            {#if !e.ok}
              <AlertTriangle class="h-3 w-3 text-bad" />
            {/if}
            <span class="rounded border border-border bg-surface-2 px-1 py-0 text-[10px] font-mono uppercase text-muted">
              {e.pipeline}
            </span>
            {#if e.iteration !== null}
              <span class="text-[9.5px] text-faint">it{e.iteration}</span>
            {/if}
            <span class="font-mono font-semibold text-text">{e.tool}</span>
            {#if e.ticker}
              <ArrowRight class="h-3 w-3 text-faint" />
              <a
                href={`${base}/symbol/${encodeURIComponent(e.ticker)}`}
                class="font-mono text-primary hover:underline"
              >${e.ticker}</a>
            {/if}
            {#if e.took_ms !== null}
              <span class="ml-auto text-[10px] tabular text-faint">{e.took_ms.toFixed(0)} ms</span>
            {:else}
              <span class="ml-auto"></span>
            {/if}
            <span class="text-[10px] text-faint">{timeAgo(e.ts)}</span>
          </div>
          {#if e.arguments && Object.keys(e.arguments).length > 0}
            <div class="ml-1 mt-0.5 truncate text-[10.5px] tabular text-faint">
              args: <span class="font-mono text-muted">{JSON.stringify(e.arguments)}</span>
            </div>
          {/if}
          <div class="ml-1 mt-0.5 truncate text-[10.5px] tabular {e.ok ? 'text-muted' : 'text-bad'}">
            → <span class="font-mono">{e.result_summary}</span>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</Card>
