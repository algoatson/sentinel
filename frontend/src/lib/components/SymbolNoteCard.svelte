<script lang="ts">
  /**
   * Per-ticker notebook card. Separate from FundTrade.notes (the
   * per-trade reflection) — this is the trader's persistent book of
   * what they've learned about a *ticker*, surviving every trade
   * lifecycle. Shows on the Symbol page.
   *
   * Two states: read-only when the body is set, or edit textarea
   * when the user clicks "Edit" / "Add". Saving PUTs the upsert
   * endpoint; clearing + saving deletes the row.
   */
  import { createQuery, createMutation, useQueryClient } from '@tanstack/svelte-query';
  import { getSymbolNote, putSymbolNote } from '$api';
  import Card from './Card.svelte';
  import { BookText, Save, RotateCcw, Trash2 } from 'lucide-svelte';
  import { toast } from '$lib/toast.svelte';
  import { timeAgo } from '$lib/format';

  interface Props {
    ticker: string;
  }
  let { ticker }: Props = $props();

  const q = createQuery({
    queryKey: ['symbol-note', ticker],
    queryFn: () => getSymbolNote(ticker),
    refetchInterval: false,
    staleTime: 5 * 60_000
  });

  let editing = $state(false);
  let draft = $state('');
  const qc = useQueryClient();
  const saveM = createMutation({
    mutationFn: ({ body }: { body: string }) => putSymbolNote(ticker, body),
    onSuccess: (n) => {
      toast.success(
        n.body ? `Note saved on $${ticker}` : `Note cleared on $${ticker}`
      );
      editing = false;
      qc.invalidateQueries({ queryKey: ['symbol-note', ticker] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : String(e))
  });

  function startEdit() {
    draft = $q.data?.body ?? '';
    editing = true;
  }
  function save() {
    $saveM.mutate({ body: draft });
  }
  function clearNote() {
    $saveM.mutate({ body: '' });
  }
</script>

<Card class="px-4 py-3">
  <div class="mb-2 flex items-center gap-2">
    <BookText class="h-3.5 w-3.5 text-primary" />
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Notebook · ${ticker}
    </div>
    {#if $q.data?.updated_at}
      <span class="text-[10.5px] text-faint">· edited {timeAgo($q.data.updated_at)}</span>
    {/if}
    {#if !editing}
      <button
        type="button"
        onclick={startEdit}
        class="ml-auto text-[10.5px] text-primary hover:underline"
      >{$q.data?.body ? 'Edit' : 'Add'}</button>
    {/if}
  </div>

  {#if editing}
    <textarea
      bind:value={draft}
      rows="5"
      placeholder="What do you know about this ticker? Patterns, prior trades, things to watch…"
      class="w-full resize-y rounded border border-border bg-surface-2 px-2.5 py-1.5 text-[12px] text-text placeholder:text-faint/60 focus:border-primary/60 focus:outline-none"
    ></textarea>
    <div class="mt-2 flex items-center gap-2">
      <button
        type="button"
        onclick={save}
        disabled={$saveM.isPending}
        class="inline-flex items-center gap-1 rounded border border-primary/40 bg-primary-soft px-2.5 py-1 text-[11.5px] text-primary hover:bg-primary/15 disabled:opacity-50"
      ><Save class="h-3 w-3" /> Save</button>
      <button
        type="button"
        onclick={() => (editing = false)}
        class="inline-flex items-center gap-1 rounded border border-border bg-bg px-2 py-1 text-[11.5px] text-muted hover:text-text"
      ><RotateCcw class="h-3 w-3" /> Cancel</button>
      {#if $q.data?.body}
        <button
          type="button"
          onclick={clearNote}
          disabled={$saveM.isPending}
          class="ml-auto inline-flex items-center gap-1 rounded border border-bad/40 bg-bad-soft px-2 py-1 text-[11.5px] text-bad hover:bg-bad/15 disabled:opacity-50"
          title="Delete the note"
        ><Trash2 class="h-3 w-3" /> Clear</button>
      {/if}
    </div>
  {:else if $q.data?.body}
    <div class="whitespace-pre-wrap rounded border border-border-soft bg-surface-2/40 px-3 py-2 text-[12px] leading-relaxed text-text">
      {$q.data.body}
    </div>
  {:else}
    <div class="text-[11.5px] italic text-faint">
      No note yet — click "Add" to start a notebook for this ticker.
    </div>
  {/if}
</Card>
