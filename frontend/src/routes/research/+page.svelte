<script lang="ts">
  import { createQuery, createMutation, useQueryClient } from '@tanstack/svelte-query';
  import { reactiveQueryOptions } from '$lib/reactive-query.svelte';
  import {
    researchTasks,
    researchTask,
    runResearch,
    executeResearch,
    researchRemaining
  } from '$api';
  import Card from '$components/Card.svelte';
  import Pill from '$components/Pill.svelte';
  import StatTile from '$components/StatTile.svelte';
  import Drawer from '$components/Drawer.svelte';
  import EmptyState from '$components/EmptyState.svelte';
  import Spinner from '$components/Spinner.svelte';
  import SkeletonGrid from '$components/SkeletonGrid.svelte';
  import Markdown from '$components/Markdown.svelte';
  import Pager from '$components/Pager.svelte';
  import { timeAgo } from '$lib/format';
  import { FlaskConical, PlayCircle, Send, CheckCircle2, XCircle, AlertCircle } from 'lucide-svelte';

  let prompt = $state('');
  let selected = $state<number | null>(null);
  let confirmExecute = $state(false);
  let page = $state(1);
  let pageSize = $state(10);

  // Refetch faster when there's an in-flight task (verdict still null
  // because the heavy LLM is still composing). Idle: 30s. In-flight: 4s.
  let activeInflight = $state(false);
  const tasksQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['research-tasks'],
    queryFn: () => researchTasks(40),
    refetchInterval: activeInflight ? 4_000 : 30_000
  })));
  $effect(() => {
    activeInflight = ($tasksQ.data ?? []).some(
      (t) => t.verdict === null
    );
  });
  const remainingQ = createQuery({
    queryKey: ['research-remaining'],
    queryFn: researchRemaining,
    refetchInterval: 60_000
  });
  const detailQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['research-task', selected],
    queryFn: () => researchTask(selected!),
    enabled: selected !== null
  })));

  const qc = useQueryClient();
  const runM = createMutation({
    mutationFn: (p: string) => runResearch(p),
    onSuccess: (r) => {
      prompt = '';
      qc.invalidateQueries({ queryKey: ['research-tasks'] });
      // Poll the new task — opens detail when ready, fallback to selecting it
      selected = r.task_id;
    }
  });
  const executeM = createMutation({
    mutationFn: (id: number) => executeResearch(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['research-tasks'] });
      qc.invalidateQueries({ queryKey: ['research-task'] });
      qc.invalidateQueries({ queryKey: ['research-remaining'] });
      qc.invalidateQueries({ queryKey: ['wallets'] });
      confirmExecute = false;
    }
  });

  const lastExecuteResult = $derived($executeM.data);

  function verdictVariant(v: string | null): 'pos' | 'neg' | 'warn' | 'neutral' {
    if (v === 'TRADE') return 'pos';
    if (v === 'WATCHLIST') return 'warn';
    if (v === 'PASS') return 'neg';
    return 'neutral';
  }

  // ── stats ──
  const stats = $derived.by(() => {
    const ts = $tasksQ.data ?? [];
    return {
      total: ts.length,
      trade: ts.filter((t) => t.verdict === 'TRADE').length,
      watchlist: ts.filter((t) => t.verdict === 'WATCHLIST').length,
      executed: ts.filter((t) => t.executed_at !== null).length
    };
  });
</script>

<svelte:head><title>Research · Sentinel</title></svelte:head>

<div class="mb-4 flex items-end justify-between border-b border-border pb-3">
  <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
    <FlaskConical class="h-5 w-5 text-violet" /><span>Research Desk</span>
  </h1>
  <div class="flex items-center gap-3 text-[11px] tabular">
    <span class="text-faint">Executions left today:</span>
    <span class="text-[14px] font-semibold {($remainingQ.data?.remaining ?? 0) > 0 ? 'text-good' : 'text-bad'}">
      {$remainingQ.data?.remaining ?? '—'} / 3
    </span>
  </div>
</div>

<!-- ── prompt box ────────────────────────────────────────────── -->
<Card class="px-4 py-3">
  <form
    onsubmit={(e) => {
      e.preventDefault();
      if (prompt.trim() && !$runM.isPending) {
        $runM.mutate(prompt.trim());
      }
    }}
  >
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      New research task
    </div>
    <textarea
      bind:value={prompt}
      rows="3"
      placeholder="e.g. 'look at IONQ — the new quantum benchmark seems significant, is this a buy?'"
      class="mt-1.5 w-full resize-y rounded-md border border-border bg-surface-2 px-3 py-2 text-[13px] text-text placeholder:text-faint/60 focus:border-primary/60 focus:outline-none"
      disabled={$runM.isPending}
    ></textarea>
    <div class="mt-2 flex items-center gap-3">
      <button
        type="submit"
        disabled={$runM.isPending || !prompt.trim()}
        class="flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary-soft px-3 py-1.5 text-[12px] font-medium text-primary transition-colors hover:bg-primary/15 disabled:opacity-40"
      >
        {#if $runM.isPending}
          <Spinner size={12} />
          Researching (heavy LLM)…
        {:else}
          <Send class="h-3.5 w-3.5" />
          Run research
        {/if}
      </button>
      <div class="text-[11px] text-faint">
        Heavy-LLM cost; not rate-limited. Execution is rate-limited (3/day, conv ≥ 3/5, ≤10% sizing).
      </div>
      {#if $runM.isError}
        <span class="ml-auto text-[11px] text-bad">
          {$runM.error instanceof Error ? $runM.error.message : 'Failed'}
        </span>
      {/if}
    </div>
  </form>
</Card>

<!-- ── stats ──────────────────────────────────────────── -->
<div class="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4">
  <StatTile label="Total tasks" value={String(stats.total)} />
  <StatTile label="TRADE verdicts" value={String(stats.trade)} accent="pos" sub="actionable" />
  <StatTile label="WATCHLIST" value={String(stats.watchlist)} accent="warn" sub="track first" />
  <StatTile label="Executed" value={String(stats.executed)} sub="opened in wallet" />
</div>

<!-- ── tasks list ──────────────────────────────────── -->
<div class="mt-4">
  <div class="mb-2 text-[10px] font-semibold uppercase tracking-wider text-faint">
    Recent tasks
  </div>
  {#if $tasksQ.isLoading}
    <SkeletonGrid count={4} class="grid grid-cols-1 gap-2.5 md:grid-cols-2" />
  {:else if !$tasksQ.data?.length}
    <EmptyState
      icon={FlaskConical}
      title="No research tasks yet"
      description="Use the box above to ask the bot to look into something. The task list is your audit trail."
    />
  {:else}
    <Pager bind:page bind:pageSize total={$tasksQ.data.length} class="mb-2" />
    <div class="grid grid-cols-1 gap-2.5 md:grid-cols-2">
      {#each $tasksQ.data.slice((page - 1) * pageSize, page * pageSize) as t (t.id)}
        <Card interactive onclick={() => (selected = t.id)} class="px-4 py-3">
          <div class="flex items-center gap-1.5">
            {#if t.verdict}
              <Pill variant={verdictVariant(t.verdict)}>{t.verdict}</Pill>
            {:else}
              <Pill variant="info">
                <Spinner size={9} />
                processing
              </Pill>
            {/if}
            {#if t.rec_ticker}
              <Pill variant={t.rec_direction === 'short' ? 'neg' : 'pos'}>
                {(t.rec_direction || '').toUpperCase()} ${t.rec_ticker}
              </Pill>
            {/if}
            {#if t.rec_conviction !== null}
              <Pill variant={t.rec_conviction >= 4 ? 'pos' : 'neutral'}>
                conv {t.rec_conviction}/5
              </Pill>
            {/if}
            {#if t.executed_at}
              <Pill variant="violet"><CheckCircle2 class="h-2.5 w-2.5" /> EXECUTED</Pill>
            {/if}
            <span class="ml-auto text-[10px] tabular text-faint">
              {timeAgo(t.created_at)}
            </span>
          </div>
          <div class="mt-2 line-clamp-3 text-[12.5px] leading-snug text-muted">
            {t.prompt}
          </div>
          {#if t.execution_note}
            <div class="mt-2 rounded bg-surface-2 px-2 py-1 text-[10.5px] text-faint">
              <span class="font-medium">exec:</span> {t.execution_note}
            </div>
          {/if}
        </Card>
      {/each}
    </div>
    <Pager bind:page bind:pageSize total={$tasksQ.data.length} class="mt-3" />
  {/if}
</div>

<!-- ── detail drawer ──────────────────────────────────── -->
<Drawer
  open={selected !== null}
  onClose={() => {
    selected = null;
    confirmExecute = false;
  }}
  class="max-w-3xl"
>
  {#snippet header()}
    {#if $detailQ.data}
      {@const t = $detailQ.data}
      <div class="flex flex-1 flex-wrap items-baseline gap-1.5">
        {#if t.verdict}
          <Pill variant={verdictVariant(t.verdict)}>{t.verdict}</Pill>
        {/if}
        {#if t.rec_ticker}
          <Pill variant={t.rec_direction === 'short' ? 'neg' : 'pos'}>
            {(t.rec_direction || '').toUpperCase()} ${t.rec_ticker}
          </Pill>
        {/if}
        {#if t.rec_conviction !== null}
          <Pill variant={t.rec_conviction >= 4 ? 'pos' : 'neutral'}>
            conv {t.rec_conviction}/5
          </Pill>
        {/if}
        {#if t.rec_size_pct !== null}
          <Pill variant="neutral">{t.rec_size_pct.toFixed(1)}% sizing</Pill>
        {/if}
        <span class="text-[11px] text-faint">·</span>
        <span class="text-[11px] text-muted">#{t.id} · {t.model}</span>
      </div>
    {/if}
  {/snippet}

  {#snippet footer()}
    {#if $detailQ.data}
      {@const t = $detailQ.data}
      {#if t.executed_at}
        <div class="flex items-center gap-2 text-[11.5px] text-good">
          <CheckCircle2 class="h-4 w-4" />
          Executed {timeAgo(t.executed_at)} ago
          {#if t.executed_trade_id}· trade #{t.executed_trade_id}{/if}
        </div>
      {:else if t.verdict === 'TRADE' && t.rec_conviction !== null && t.rec_conviction >= 3}
        {#if confirmExecute}
          <div class="space-y-2">
            <div class="flex items-start gap-2 rounded-md border border-warn/30 bg-warn-soft px-3 py-2 text-[12px] text-warn">
              <AlertCircle class="mt-0.5 h-4 w-4 shrink-0" />
              <div>
                This opens a <strong>{t.rec_direction?.toUpperCase()}</strong> on
                <strong>${t.rec_ticker}</strong> at {t.rec_size_pct?.toFixed(1)}% of
                the <code class="rounded bg-surface-3 px-1">research</code> wallet.
                Counts against your daily cap.
              </div>
            </div>
            <div class="flex justify-end gap-2">
              <button
                onclick={() => (confirmExecute = false)}
                disabled={$executeM.isPending}
                class="rounded-md border border-border bg-surface-2 px-3 py-1.5 text-[12px] text-muted hover:text-text"
              >Cancel</button>
              <button
                onclick={() => $executeM.mutate(t.id)}
                disabled={$executeM.isPending}
                class="flex items-center gap-1.5 rounded-md border border-good/40 bg-good-soft px-3 py-1.5 text-[12px] font-medium text-good hover:bg-good/15 disabled:opacity-40"
              >
                {#if $executeM.isPending}
                  <Spinner size={12} />
                  Executing…
                {:else}
                  <PlayCircle class="h-3.5 w-3.5" />
                  Confirm — open the trade
                {/if}
              </button>
            </div>
          </div>
        {:else if lastExecuteResult && !lastExecuteResult.ok}
          <div class="flex items-center gap-2 text-[12px] text-bad">
            <XCircle class="h-4 w-4" />
            {lastExecuteResult.message}
          </div>
        {:else}
          <div class="flex items-center justify-between">
            <div class="text-[11px] text-faint">
              Verdict allows execution. Click to confirm.
            </div>
            <button
              onclick={() => (confirmExecute = true)}
              disabled={($remainingQ.data?.remaining ?? 0) === 0}
              class="flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary-soft px-3 py-1.5 text-[12px] font-medium text-primary transition-colors hover:bg-primary/15 disabled:opacity-40"
              title={($remainingQ.data?.remaining ?? 0) === 0 ? 'Daily execution cap reached.' : ''}
            >
              <PlayCircle class="h-3.5 w-3.5" />
              Execute trade
            </button>
          </div>
        {/if}
      {:else}
        <div class="text-[11.5px] text-faint">
          {#if t.verdict === 'PASS'}
            Verdict was PASS — no executable trade.
          {:else if t.verdict === 'WATCHLIST'}
            Verdict was WATCHLIST — bot recommends tracking, not opening.
          {:else if !t.verdict}
            Still processing…
          {:else}
            Conviction below the floor (≥3 required) — no executable trade.
          {/if}
        </div>
      {/if}
    {/if}
  {/snippet}

  {#if $detailQ.isLoading || !$detailQ.data}
    <div class="flex justify-center py-12"><Spinner /></div>
  {:else}
    {@const t = $detailQ.data}
    <div class="rounded-lg border border-border bg-surface-2 px-3 py-2">
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">Prompt</div>
      <div class="mt-1 text-[12.5px] leading-snug text-text">{t.prompt}</div>
      <div class="mt-2 text-[10px] tabular text-faint">
        {t.created_at.slice(0, 19).replace('T', ' ')}Z
      </div>
    </div>

    {#if t.rec_thesis}
      <div class="mt-4 rounded-lg border border-good/30 bg-good-soft px-3 py-2">
        <div class="text-[10px] font-semibold uppercase tracking-wider text-good">Thesis</div>
        <div class="mt-1 text-[12.5px] leading-snug text-text">{t.rec_thesis}</div>
      </div>
    {/if}

    {#if t.rec_risks}
      <div class="mt-3 rounded-lg border border-warn/30 bg-warn-soft px-3 py-2">
        <div class="text-[10px] font-semibold uppercase tracking-wider text-warn">Risks</div>
        <div class="mt-1 text-[12.5px] leading-snug text-text">{t.rec_risks}</div>
      </div>
    {/if}

    {#if t.dossier}
      <div class="mt-4">
        <div class="mb-2 text-[10px] font-semibold uppercase tracking-wider text-faint">
          Full dossier
        </div>
        <div class="rounded-lg border border-border bg-surface-2 px-4 py-3">
          <Markdown source={t.dossier} />
        </div>
      </div>
    {/if}
  {/if}
</Drawer>
