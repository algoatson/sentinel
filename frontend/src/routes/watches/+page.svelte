<script lang="ts">
  import { createQuery, createMutation, useQueryClient } from '@tanstack/svelte-query';
  import { reactiveQueryOptions } from '$lib/reactive-query.svelte';
  import { listWatches, getWatch, addWatch, removeWatch } from '$api';
  import { toast } from '$lib/toast.svelte';
  import Card from '$components/Card.svelte';
  import Pill from '$components/Pill.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import Spinner from '$components/Spinner.svelte';
  import Drawer from '$components/Drawer.svelte';
  import { timeAgo } from '$lib/format';
  import { Bell, Send, X } from 'lucide-svelte';

  let prompt = $state('');
  let selectedId = $state<number | null>(null);

  const watchesQ = createQuery({
    queryKey: ['watches'],
    queryFn: listWatches,
    refetchInterval: 30_000
  });
  const detailQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['watch', selectedId],
    queryFn: () => getWatch(selectedId!),
    enabled: selectedId !== null
  })));
  const qc = useQueryClient();

  const addM = createMutation({
    mutationFn: (text: string) => addWatch(text),
    onSuccess: (res) => {
      (res.ok ? toast.success : toast.warn).call(toast, res.message);
      if (res.ok) prompt = '';
      qc.invalidateQueries({ queryKey: ['watches'] });
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : String(err))
  });

  const removeM = createMutation({
    mutationFn: (wid: number) => removeWatch(wid),
    onSuccess: (res) => {
      toast.success(res.message);
      qc.invalidateQueries({ queryKey: ['watches'] });
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : String(err))
  });

  const EXAMPLES = [
    "tell me if NVDA moves >5% on >2x volume",
    "ping me when any 13D drops on AAPL",
    "alert if 'recession' is in 2+ macro headlines today",
    "any 8-K from energy names mentioning 'halt'"
  ];
</script>

<svelte:head><title>Watches · Sentinel</title></svelte:head>

<div class="mb-4 flex items-end justify-between border-b border-border pb-3">
  <div>
    <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
      <Bell class="h-5 w-5 text-warn" /><span>Watches</span>
    </h1>
    <div class="mt-0.5 text-[11.5px] text-faint">
      Describe what to alert on in plain English. The bot compiles it to a
      machine-checkable spec and posts to Discord when the condition trips.
    </div>
  </div>
</div>

<Card class="px-4 py-3">
  <form
    onsubmit={(e) => {
      e.preventDefault();
      if (prompt.trim() && !$addM.isPending) {
        $addM.mutate(prompt.trim());
      }
    }}
  >
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      New watch
    </div>
    <textarea
      bind:value={prompt}
      rows="2"
      placeholder="e.g. 'tell me if NVDA moves >5% on >2x volume'"
      class="mt-1.5 w-full resize-y rounded-md border border-border bg-surface-2 px-3 py-2 text-[13px] text-text placeholder:text-faint/60 focus:border-primary/60 focus:outline-none"
      disabled={$addM.isPending}
    ></textarea>
    <div class="mt-2 flex items-center gap-3">
      <button
        type="submit"
        disabled={$addM.isPending || !prompt.trim()}
        class="flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary-soft px-3 py-1.5 text-[12px] font-medium text-primary transition-colors hover:bg-primary/15 disabled:opacity-40"
      >
        {#if $addM.isPending}
          <Spinner size={12} />
          Compiling…
        {:else}
          <Send class="h-3.5 w-3.5" />
          Add watch
        {/if}
      </button>
      <div class="text-[11px] text-faint">
        Compiled by light LLM into a spec — same path Discord <code class="rounded bg-surface-2 px-1 text-[10px]">!watch</code> uses.
      </div>
    </div>

    <div class="mt-3 flex flex-wrap gap-1.5">
      <span class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Examples
      </span>
      {#each EXAMPLES as ex (ex)}
        <button
          type="button"
          onclick={() => (prompt = ex)}
          class="rounded-md border border-border bg-surface-2 px-2 py-0.5 text-[10.5px] text-muted transition-colors hover:border-primary/40 hover:text-text"
        >{ex}</button>
      {/each}
    </div>
  </form>
</Card>

<div class="mt-4">
  <div class="mb-2 flex items-baseline justify-between">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Active watches
    </div>
    <div class="text-[11px] text-faint">
      {$watchesQ.data?.length ?? 0} total
    </div>
  </div>

  {#if $watchesQ.isLoading}
    <div class="flex justify-center py-8"><Spinner /></div>
  {:else if !$watchesQ.data?.length}
    <EmptyState
      title="No watches set"
      description="Use the box above to describe what you want to know about. The bot will compile it into a checkable rule."
    />
  {:else}
    <div class="grid grid-cols-1 gap-2.5 md:grid-cols-2">
      {#each $watchesQ.data as w (w.id)}
        <Card interactive onclick={() => (selectedId = w.id)} class="px-3.5 py-3">
          <div class="flex items-start gap-2">
            <Pill variant={w.active ? 'pos' : 'neutral'}>
              #{w.id}{w.active ? '' : ' · paused'}
            </Pill>
            <div class="min-w-0 flex-1 text-[12.5px] leading-snug text-text">
              {w.raw_text}
            </div>
            <button
              type="button"
              aria-label="Remove watch"
              onclick={(e) => {
                e.stopPropagation();
                $removeM.mutate(w.id);
              }}
              disabled={$removeM.isPending}
              class="-mt-1 -mr-1 shrink-0 rounded p-1 text-faint transition-colors hover:bg-surface-2 hover:text-bad"
            >
              <X class="h-3.5 w-3.5" />
            </button>
          </div>
          <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border-soft pt-2 text-[10.5px] tabular text-faint">
            <span><span class="text-muted font-semibold">{w.trigger_count}</span> trips</span>
            {#if w.last_triggered_at}
              <span>last fired {timeAgo(w.last_triggered_at)} ago</span>
            {/if}
            {#if w.created_at}
              <span class="ml-auto">created {timeAgo(w.created_at)} ago</span>
            {/if}
          </div>
        </Card>
      {/each}
    </div>
  {/if}
</div>

<!-- ── watch detail drawer (shows the compiled spec) ───────────────── -->
<Drawer
  open={selectedId !== null}
  onClose={() => (selectedId = null)}
  class="max-w-xl"
>
  {#snippet header()}
    {#if $detailQ.data}
      {@const w = $detailQ.data}
      <div class="flex flex-1 items-baseline gap-2">
        <Pill variant={w.active ? 'pos' : 'neutral'}>
          #{w.id}{w.active ? '' : ' · paused'}
        </Pill>
        <span class="text-[11.5px] text-muted">
          {w.trigger_count} trip{w.trigger_count === 1 ? '' : 's'}
        </span>
      </div>
    {/if}
  {/snippet}

  {#if $detailQ.isLoading}
    <div class="flex justify-center py-12"><Spinner /></div>
  {:else if $detailQ.data}
    {@const w = $detailQ.data}
    <div class="rounded-lg border border-border bg-surface-2 px-3 py-2">
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Your request
      </div>
      <div class="mt-1 text-[13px] leading-snug text-text">{w.raw_text}</div>
    </div>

    <div class="mt-4">
      <div class="mb-1 text-[10px] font-semibold uppercase tracking-wider text-faint">
        Compiled spec
      </div>
      <pre class="overflow-x-auto rounded-lg border border-border bg-bg/60 px-3 py-2 font-mono text-[11px] leading-relaxed text-text"><code>{JSON.stringify(w.spec, null, 2)}</code></pre>
      <div class="mt-1 text-[10.5px] text-faint">
        This is what the light LLM turned your plain-English request into.
        The watcher checks this rule each cycle (5min) and posts to Discord
        when it trips, with a 6h cooldown to prevent spam.
      </div>
    </div>

    <div class="mt-4 grid grid-cols-2 gap-2 text-[11.5px] tabular">
      <div class="rounded-md border border-border bg-surface-2 px-3 py-1.5">
        <div class="text-[10px] uppercase tracking-wider text-faint">Last triggered</div>
        <div class="mt-0.5 text-text">
          {w.last_triggered_at ? `${timeAgo(w.last_triggered_at)} ago` : '—'}
        </div>
      </div>
      <div class="rounded-md border border-border bg-surface-2 px-3 py-1.5">
        <div class="text-[10px] uppercase tracking-wider text-faint">Created</div>
        <div class="mt-0.5 text-text">
          {w.created_at ? `${timeAgo(w.created_at)} ago` : '—'}
        </div>
      </div>
    </div>
  {/if}
</Drawer>
