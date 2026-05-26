<script lang="ts">
  /**
   * Editor for the LLM prompts that drive the autonomous pipelines.
   * Backed by the existing PromptVersion table — saving inserts a new
   * active row, the previous active row is kept inactive for rollback.
   *
   * Left column: prompt picker with an "overridden" tag for prompts
   * that have a DB row (custom) vs. the seed (code default).
   * Right column: textarea + actions (Save / Reset to default /
   * Diff against seed / Rollback to an older version).
   */
  import { createQuery, createMutation, useQueryClient } from '@tanstack/svelte-query';
  import {
    listPrompts, getPrompt, savePrompt, resetPrompt, restorePrompt,
    type PromptListItem,
  } from '$api';
  import Card from './Card.svelte';
  import Spinner from './Spinner.svelte';
  import { toast } from '$lib/toast.svelte';
  import {
    Wand2, Save, RotateCcw, History as HistoryIcon, AlertTriangle,
  } from 'lucide-svelte';
  import { timeAgo } from '$lib/format';

  let selected = $state<string | null>(null);

  const listQ = createQuery({
    queryKey: ['prompts'],
    queryFn: listPrompts,
    refetchInterval: 60_000,
  });
  const detailQ = createQuery({
    queryKey: ['prompt', selected],
    queryFn: () => getPrompt(selected!),
    enabled: !!selected,
    staleTime: 30_000,
    refetchInterval: false,
  });

  let draft = $state('');
  let initialised = $state(false);
  let showDiff = $state(false);

  $effect(() => {
    const d = $detailQ.data;
    if (d && !initialised) {
      draft = d.active_content;
      initialised = true;
    }
  });

  const qc = useQueryClient();
  const saveM = createMutation({
    mutationFn: ({ name, content }: { name: string; content: string }) =>
      savePrompt(name, content),
    onSuccess: () => {
      toast.success(`Saved · ${selected}`);
      qc.invalidateQueries({ queryKey: ['prompts'] });
      qc.invalidateQueries({ queryKey: ['prompt', selected] });
      initialised = false;
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
  });
  const resetM = createMutation({
    mutationFn: (name: string) => resetPrompt(name),
    onSuccess: () => {
      toast.success(`Reset to default · ${selected}`);
      qc.invalidateQueries({ queryKey: ['prompts'] });
      qc.invalidateQueries({ queryKey: ['prompt', selected] });
      initialised = false;
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
  });
  const restoreM = createMutation({
    mutationFn: ({ name, id }: { name: string; id: number }) =>
      restorePrompt(name, id),
    onSuccess: () => {
      toast.success(`Restored · ${selected}`);
      qc.invalidateQueries({ queryKey: ['prompts'] });
      qc.invalidateQueries({ queryKey: ['prompt', selected] });
      initialised = false;
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e)),
  });

  function select(name: string) {
    selected = name;
    initialised = false;
    showDiff = false;
  }

  // Cheap line-level diff — show only lines that differ between the
  // active draft and the seed. For a precise editor the user can
  // copy/paste either; this is a glance-check.
  const diffLines = $derived.by(() => {
    if (!$detailQ.data) return [];
    const seedLines = $detailQ.data.seed.split('\n');
    const draftLines = draft.split('\n');
    const out: { sign: '+' | '-' | ' '; text: string }[] = [];
    const max = Math.max(seedLines.length, draftLines.length);
    for (let i = 0; i < max; i++) {
      const s = seedLines[i];
      const d = draftLines[i];
      if (s === d) {
        out.push({ sign: ' ', text: s ?? '' });
      } else {
        if (s !== undefined) out.push({ sign: '-', text: s });
        if (d !== undefined) out.push({ sign: '+', text: d });
      }
    }
    return out;
  });
</script>

<Card class="px-0 py-0">
  <div class="flex items-baseline gap-2 border-b border-border px-4 py-3">
    <Wand2 class="h-3.5 w-3.5 text-violet" />
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Prompt editor
    </div>
    <span class="text-[10.5px] text-faint">
      DB row > code constant · save creates a new active version
    </span>
  </div>

  <div class="grid grid-cols-1 lg:grid-cols-[14rem_1fr]">
    <!-- Left: prompt picker -->
    <div class="max-h-[28rem] overflow-y-auto border-r border-border bg-surface-2/30">
      {#if $listQ.isLoading}
        <div class="flex h-24 items-center justify-center"><Spinner /></div>
      {:else}
        <ul>
          {#each $listQ.data ?? [] as p (p.name)}
            <li>
              <button
                type="button"
                onclick={() => select(p.name)}
                class={[
                  'flex w-full items-center gap-1.5 border-b border-border-soft px-3 py-1.5 text-left text-[11.5px] transition-colors',
                  selected === p.name
                    ? 'bg-violet-soft text-violet'
                    : 'text-muted hover:bg-surface-2/60 hover:text-text'
                ].join(' ')}
              >
                <span class="flex-1 font-mono">{p.name}</span>
                {#if p.overridden}
                  <span
                    class="rounded border border-violet/40 bg-violet-soft px-1 text-[9px] uppercase tracking-wider text-violet"
                    title="DB-overridden — saved version differs from the code default"
                  >db</span>
                {/if}
              </button>
            </li>
          {/each}
        </ul>
      {/if}
    </div>

    <!-- Right: editor -->
    <div class="p-3">
      {#if !selected}
        <div class="flex h-[24rem] items-center justify-center text-center text-[12px] text-faint">
          Pick a prompt on the left to edit.
        </div>
      {:else if $detailQ.isLoading}
        <div class="flex h-[24rem] items-center justify-center"><Spinner /></div>
      {:else if !$detailQ.data}
        <div class="py-6 text-center text-[12px] text-faint">No data.</div>
      {:else}
        {@const d = $detailQ.data}
        <div class="mb-2 flex items-baseline gap-2">
          <span class="font-mono text-[12px] text-text">{d.name}</span>
          {#if d.overridden}
            <span class="rounded border border-violet/40 bg-violet-soft px-1 py-0 text-[9.5px] uppercase tracking-wider text-violet">
              custom
            </span>
          {:else}
            <span class="rounded border border-border bg-surface-2 px-1 py-0 text-[9.5px] uppercase tracking-wider text-muted">
              default
            </span>
          {/if}
          {#if d.active}
            <span class="text-[10px] text-faint">edited {timeAgo(d.active.created_at)}</span>
          {/if}
          <button
            type="button"
            onclick={() => (showDiff = !showDiff)}
            class="ml-auto text-[10.5px] text-primary hover:underline"
          >{showDiff ? 'Hide diff' : 'Show diff vs default'}</button>
        </div>

        {#if showDiff}
          <pre class="mb-3 max-h-[18rem] overflow-y-auto whitespace-pre rounded border border-border bg-surface-2/40 p-2 text-[10.5px] leading-snug">{#each diffLines as l (l)}<span class={[
            l.sign === '+' ? 'block text-good' :
            l.sign === '-' ? 'block text-bad' : 'block text-muted'
          ].join(' ')}>{l.sign} {l.text}</span>{/each}</pre>
        {:else}
          <textarea
            bind:value={draft}
            rows="16"
            class="w-full resize-y rounded-md border border-border bg-surface-2 px-3 py-2 font-mono text-[11.5px] leading-snug text-text placeholder:text-faint/50 focus:border-primary/60 focus:outline-none"
          ></textarea>
        {/if}

        <div class="mt-2 flex items-center gap-2">
          <button
            type="button"
            onclick={() => $saveM.mutate({ name: d.name, content: draft })}
            disabled={$saveM.isPending || draft === d.active_content}
            class="inline-flex items-center gap-1 rounded-md border border-primary/40 bg-primary-soft px-2.5 py-1 text-[11.5px] font-medium text-primary transition-colors hover:bg-primary/15 disabled:opacity-50"
          >
            {#if $saveM.isPending}<Spinner size={12} />{:else}<Save class="h-3 w-3" />{/if}
            Save as new active
          </button>
          {#if d.overridden}
            <button
              type="button"
              onclick={() => $resetM.mutate(d.name)}
              disabled={$resetM.isPending}
              class="inline-flex items-center gap-1 rounded-md border border-warn/40 bg-warn-soft px-2.5 py-1 text-[11.5px] font-medium text-warn transition-colors hover:bg-warn/15"
              title="Drop DB row → engine falls back to the code default"
            >
              <RotateCcw class="h-3 w-3" /> Reset to default
            </button>
          {/if}
        </div>

        {#if d.history.length}
          <div class="mt-3 border-t border-border-soft pt-2">
            <div class="mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wider text-faint">
              <HistoryIcon class="h-2.5 w-2.5" /> Version history
            </div>
            <ul class="space-y-0.5">
              {#each d.history as h (h.id)}
                <li class="flex items-center gap-2 text-[11px] tabular">
                  <span class="text-faint">#{h.id}</span>
                  <span class="text-muted">{timeAgo(h.created_at)}</span>
                  <span class="text-[9.5px] text-faint">{h.len} chars</span>
                  <button
                    type="button"
                    onclick={() => $restoreM.mutate({ name: d.name, id: h.id })}
                    disabled={$restoreM.isPending}
                    class="ml-auto text-[10.5px] text-primary hover:underline"
                  >Restore</button>
                </li>
              {/each}
            </ul>
          </div>
        {/if}

        <div class="mt-2 inline-flex items-center gap-1 text-[10px] text-faint">
          <AlertTriangle class="h-2.5 w-2.5 text-warn" />
          Changes go live on the next pipeline call — no restart needed.
        </div>
      {/if}
    </div>
  </div>
</Card>
