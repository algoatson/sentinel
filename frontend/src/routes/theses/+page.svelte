<script lang="ts">
  import {
    createQuery,
    createMutation,
    useQueryClient
  } from '@tanstack/svelte-query';
  import { reactiveQueryOptions } from '$lib/reactive-query.svelte';
  import {
    thesesActive,
    thesesClosed,
    thesisDetail,
    closeThesis,
    runThesisGenerate
  } from '$api';
  import Card from '$components/Card.svelte';
  import Drawer from '$components/Drawer.svelte';
  import Pill from '$components/Pill.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import Spinner from '$components/Spinner.svelte';
  import SkeletonGrid from '$components/SkeletonGrid.svelte';
  import StatTile from '$components/StatTile.svelte';
  import TickerLink from '$components/TickerLink.svelte';
  import Markdown from '$components/Markdown.svelte';
  import { timeAgo } from '$lib/format';
  import { Brain } from 'lucide-svelte';

  type SortKey = 'recent' | 'conv' | 'support' | 'challenge' | 'last_event';
  let sortKey: SortKey = $state('recent');

  const activeQ = createQuery({
    queryKey: ['theses', 'active'],
    queryFn: thesesActive,
    refetchInterval: 60_000
  });

  function field(t: any, key: SortKey): number {
    switch (key) {
      case 'recent':     return new Date(t.created_at).getTime();
      case 'conv':       return t.conviction ?? 0;
      case 'support':    return t.supporting_events ?? 0;
      case 'challenge':  return t.challenging_events ?? 0;
      case 'last_event': return t.last_event_at ? new Date(t.last_event_at).getTime() : 0;
    }
  }
  const sortedActive = $derived(
    [...($activeQ.data ?? [])].sort((a, b) => field(b, sortKey) - field(a, sortKey))
  );
  const closedQ = createQuery({
    queryKey: ['theses', 'closed', 30],
    queryFn: () => thesesClosed(30),
    refetchInterval: 90_000
  });

  let selectedId = $state<number | null>(null);
  const detailQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['thesis', selectedId],
    queryFn: () =>
      selectedId !== null ? thesisDetail(selectedId) : Promise.reject('no id'),
    enabled: selectedId !== null
  })));

  const qc = useQueryClient();
  const closeM = createMutation({
    mutationFn: ({ id, state, reason }: { id: number; state: any; reason: string }) =>
      closeThesis(id, state, reason),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['theses'] });
      selectedId = null;
    }
  });
  const generateM = createMutation({
    mutationFn: runThesisGenerate,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['theses'] })
  });

  function variantForState(state: string): 'pos' | 'neg' | 'warn' | 'neutral' {
    if (state === 'validated') return 'pos';
    if (state === 'invalidated') return 'neg';
    if (state === 'matured') return 'warn';
    return 'neutral';
  }

  const validated30d = $derived(($closedQ.data ?? []).filter((t) => t.state === 'validated').length);
  const invalidated30d = $derived(($closedQ.data ?? []).filter((t) => t.state === 'invalidated').length);
  const matured30d = $derived(($closedQ.data ?? []).filter((t) => t.state === 'matured').length);
</script>

<svelte:head><title>Theses · Sentinel</title></svelte:head>

<div class="mb-4 flex items-end justify-between border-b border-border pb-3">
  <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
    <Brain class="h-5 w-5 text-violet" /><span>Running theses</span>
  </h1>
  <button
    onclick={() => $generateM.mutate()}
    disabled={$generateM.isPending}
    class="rounded-md border border-primary/40 bg-primary-soft px-3 py-1.5 text-[11.5px] font-medium text-primary transition-colors hover:bg-primary/15 disabled:opacity-50"
  >
    {#if $generateM.isPending}
      <Spinner size={12} />
    {:else}
      Generate now
    {/if}
  </button>
</div>

<!-- ── headline tiles ────────────────────────────────────────────── -->
<div class="grid grid-cols-2 gap-3 md:grid-cols-4">
  <StatTile label="Active" value={String($activeQ.data?.length ?? 0)} />
  <StatTile label="Validated 30d" value={String(validated30d)} accent="pos" />
  <StatTile label="Invalidated 30d" value={String(invalidated30d)} accent="neg" />
  <StatTile label="Matured 30d" value={String(matured30d)} accent="warn" />
</div>

<!-- ── active theses ─────────────────────────────────────────────── -->
<div class="mt-5">
  <div class="mb-2 flex items-center gap-3">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Active
    </div>
    <div class="ml-auto flex items-center gap-1">
      <span class="mr-1 text-[10px] font-semibold uppercase tracking-wider text-faint">Sort</span>
      {#each [
        ['recent', 'Newest'],
        ['conv', 'Conv'],
        ['support', '+events'],
        ['challenge', '-events'],
        ['last_event', 'Last event']
      ] as [k, label] (k)}
        <button
          onclick={() => (sortKey = k as SortKey)}
          class={[
            'rounded-md border px-2 py-0.5 text-[10.5px] transition-colors',
            sortKey === k
              ? 'border-primary/50 bg-primary-soft text-primary'
              : 'border-border bg-surface-2 text-muted hover:text-text'
          ].join(' ')}
        >{label}</button>
      {/each}
    </div>
  </div>
  {#if $activeQ.isLoading}
    <SkeletonGrid count={6} />
  {:else if !$activeQ.data?.length}
    <EmptyState
      icon={Brain}
      title="No active theses yet"
      description="The generator runs daily at 08:15 ET — or hit “Generate now” above to trigger it."
    />
  {:else}
    <div class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
      {#each sortedActive as t (t.id)}
        <Card interactive onclick={() => (selectedId = t.id)} class="px-4 py-3">
          <div class="flex items-center gap-1.5">
            <Pill variant={t.direction === 'long' ? 'pos' : t.direction === 'short' ? 'neg' : 'neutral'}>
              {t.direction.toUpperCase()}
            </Pill>
            <Pill variant="neutral">conv {t.conviction}/5</Pill>
            <span class="ml-auto text-[10px] text-faint tabular">
              {t.created_at.slice(0, 10)}
            </span>
          </div>
          <div class="mt-2 text-[13.5px] leading-snug">
            <TickerLink ticker={t.ticker} />
            <span class="ml-1">{t.title}</span>
          </div>
          <div class="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10.5px] tabular text-faint">
            {#if t.target_price !== null}<span>target {t.target_price.toFixed(2)}</span>{/if}
            {#if t.horizon_days !== null}<span>{t.horizon_days}d horizon</span>{/if}
            {#if t.supporting_events + t.challenging_events > 0}
              {@const sup = t.supporting_events}
              {@const ch = t.challenging_events}
              <span
                class={[
                  'tabular font-medium',
                  sup >= ch * 2 && sup > 0
                    ? 'text-good'
                    : ch >= sup * 2 && ch > 0
                      ? 'text-bad'
                      : 'text-muted'
                ].join(' ')}
              >
                +{sup} / -{ch} events
              </span>
            {/if}
          </div>
          {#if t.invalidation_criteria}
            <div class="mt-2 line-clamp-2 text-[11.5px] text-muted">
              <span class="font-medium text-warn">Kills it:</span>
              {t.invalidation_criteria}
            </div>
          {/if}
        </Card>
      {/each}
    </div>
  {/if}
</div>

<!-- ── closed 30d ───────────────────────────────────────────────── -->
{#if $closedQ.data?.length}
  <div class="mt-6">
    <div class="mb-2 text-[10px] font-semibold uppercase tracking-wider text-faint">
      Closed (30d)
    </div>
    <div class="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
      {#each $closedQ.data as t (t.id)}
        <Card interactive onclick={() => (selectedId = t.id)} class="px-3 py-2">
          <div class="flex items-center gap-1.5">
            <Pill variant={variantForState(t.state)}>{t.state.toUpperCase()}</Pill>
            <span class="ml-auto text-[10px] text-faint tabular">
              {(t.closed_at ?? '').slice(0, 10)}
            </span>
          </div>
          <div class="mt-1.5 text-[12.5px]">
            <TickerLink ticker={t.ticker} />
            <span class="ml-1 text-muted">{t.title}</span>
          </div>
          {#if t.close_reason}
            <div class="mt-1 line-clamp-1 text-[11px] text-faint">{t.close_reason}</div>
          {/if}
        </Card>
      {/each}
    </div>
  </div>
{/if}

<Drawer
  open={selectedId !== null}
  onClose={() => (selectedId = null)}
  class="max-w-3xl"
>
  {#snippet header()}
    {#if $detailQ.data}
      {@const t = $detailQ.data}
      <div class="flex flex-1 flex-wrap items-baseline gap-1.5">
        <Pill variant={t.direction === 'long' ? 'pos' : t.direction === 'short' ? 'neg' : 'neutral'}>
          {t.direction.toUpperCase()}
        </Pill>
        <TickerLink ticker={t.ticker} class="text-base font-bold" />
        <Pill variant={variantForState(t.state)}>{t.state.toUpperCase()}</Pill>
        <span class="text-[11px] text-faint">·</span>
        <span class="text-[11px] text-muted">#{t.id}</span>
      </div>
    {/if}
  {/snippet}

  {#if $detailQ.isLoading || !$detailQ.data}
    <div class="flex justify-center py-12"><Spinner /></div>
  {:else}
    {@const t = $detailQ.data}
    <div class="rounded-lg border border-border bg-surface-2 px-4 py-3">
      <div class="text-[13.5px] font-medium text-text">{t.title}</div>
      <Markdown source={t.body} class="mt-2" />
      {#if t.invalidation_criteria}
        <div class="mt-3 text-[12px]">
          <span class="font-semibold text-warn">Kills it:</span>
          <span class="ml-1 text-muted">{t.invalidation_criteria}</span>
        </div>
      {/if}
      <div class="mt-2 flex flex-wrap gap-x-3 text-[10.5px] tabular text-faint">
        <span>conv {t.conviction}/5</span>
        {#if t.target_price !== null}<span>target {t.target_price.toFixed(2)}</span>{/if}
        {#if t.horizon_days !== null}<span>{t.horizon_days}d horizon</span>{/if}
        <span>created {t.created_at.slice(0, 10)}</span>
      </div>
    </div>

    {#if t.state === 'active'}
      <div class="mt-4 flex flex-wrap gap-2">
        <button
          onclick={() => selectedId !== null && $closeM.mutate({ id: selectedId, state: 'validated', reason: 'manual: validated' })}
          disabled={$closeM.isPending}
          class="rounded-md border border-good/40 bg-good-soft px-3 py-1.5 text-[11.5px] font-medium text-good hover:bg-good/15"
        >✅ Validated</button>
        <button
          onclick={() => selectedId !== null && $closeM.mutate({ id: selectedId, state: 'invalidated', reason: 'manual: invalidated' })}
          disabled={$closeM.isPending}
          class="rounded-md border border-bad/40 bg-bad-soft px-3 py-1.5 text-[11.5px] font-medium text-bad hover:bg-bad/15"
        >❌ Invalidated</button>
        <button
          onclick={() => selectedId !== null && $closeM.mutate({ id: selectedId, state: 'matured', reason: 'manual: matured' })}
          disabled={$closeM.isPending}
          class="rounded-md border border-warn/40 bg-warn-soft px-3 py-1.5 text-[11.5px] font-medium text-warn hover:bg-warn/15"
        >⏳ Matured</button>
        <button
          onclick={() => selectedId !== null && $closeM.mutate({ id: selectedId, state: 'closed', reason: 'manual: closed' })}
          disabled={$closeM.isPending}
          class="rounded-md border border-border bg-surface-2 px-3 py-1.5 text-[11.5px] text-muted hover:text-text"
        >✕ Close</button>
      </div>
    {:else if t.close_reason}
      <div class="mt-4 rounded-md border border-border bg-surface-2 px-3 py-2 text-[12px] text-muted">
        Closed as <strong>{t.state}</strong>: {t.close_reason}
      </div>
    {/if}

    <div class="mt-5">
      <div class="mb-2 text-[10px] font-semibold uppercase tracking-wider text-faint">
        Linked events (timeline)
      </div>
      {#if !t.events.length}
        <div class="rounded-md border border-border-soft bg-surface-2 px-3 py-2 text-[11.5px] text-faint">
          No events linked yet. New news/filings on this ticker will appear here.
        </div>
      {:else}
        <ul class="divide-soft">
          {#each t.events as e (e.id)}
            <li class="grid grid-cols-[auto_1fr_auto] items-start gap-3 py-2 text-[12.5px]">
              <Pill
                variant={e.impact === 'supports' ? 'pos' : e.impact === 'challenges' ? 'neg' : 'info'}
              >{e.impact.toUpperCase()}</Pill>
              <div class="min-w-0">
                <div class="text-text">{e.description}</div>
                <div class="mt-0.5 flex flex-wrap items-center gap-x-2 text-[10.5px] text-faint">
                  <span>{e.kind.toUpperCase()}</span>
                  {#if e.rationale}<span class="text-muted">{e.rationale}</span>{/if}
                </div>
              </div>
              <span class="tabular text-[10px] text-faint">{timeAgo(e.created_at)}</span>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</Drawer>
