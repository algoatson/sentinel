<script lang="ts">
  /**
   * Today's plan scratchpad. Autosave on blur + 1.5s debounce so the
   * trader writes once, walks away, and the body is in the DB.
   * Empty body deletes the row (so a missed day shows "no plan" rather
   * than an orphaned blank).
   *
   * Persistence is per UTC date — a fresh day silently starts a blank,
   * yesterday's plan stays in the table for retrospectives.
   */
  import { createQuery, createMutation, useQueryClient } from '@tanstack/svelte-query';
  import { getDailyPlan, putDailyPlan } from '$api';
  import Card from './Card.svelte';
  import { ClipboardList, CheckCircle2, Loader2 } from 'lucide-svelte';
  import { timeAgo } from '$lib/format';

  const q = createQuery({
    queryKey: ['plan-today'],
    queryFn: getDailyPlan,
    refetchInterval: 5 * 60_000
  });
  const qc = useQueryClient();
  const saveM = createMutation({
    mutationFn: (body: string) => putDailyPlan(body),
    onSuccess: () => {
      // Don't invalidate — would trigger a refetch that clobbers
      // the user's in-flight edits. Just stamp savedAt locally.
      savedAt = new Date();
      // Refresh cache silently for the case where another tab edited.
      qc.setQueryData(['plan-today'], (prev: any) =>
        prev ? { ...prev, body: draft.trim(), updated_at: new Date().toISOString() } : prev
      );
    },
    onError: (e) => {
      saveError = e instanceof Error ? e.message : String(e);
    }
  });

  let draft = $state('');
  let initialised = false;
  let dirty = $state(false);
  let savedAt = $state<Date | null>(null);
  let saveError = $state<string | null>(null);
  let debounceTimer: ReturnType<typeof setTimeout> | null = null;

  // Hydrate draft from query once data arrives.
  $effect(() => {
    const d = $q.data;
    if (d && !initialised) {
      draft = d.body;
      initialised = true;
    }
  });

  function scheduleSave() {
    dirty = true;
    saveError = null;
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      $saveM.mutate(draft);
      dirty = false;
    }, 1500);
  }

  function saveNow() {
    if (debounceTimer) {
      clearTimeout(debounceTimer);
      debounceTimer = null;
    }
    if (dirty) {
      $saveM.mutate(draft);
      dirty = false;
    }
  }

  const wordCount = $derived(
    draft.trim() ? draft.trim().split(/\s+/).length : 0
  );
</script>

<Card class="px-4 py-3">
  <div class="mb-2 flex items-center gap-2">
    <ClipboardList class="h-3.5 w-3.5 text-primary" />
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Today's plan
      <span class="ml-1 text-[9.5px] normal-case text-muted">
        ({$q.data?.plan_date ?? ''})
      </span>
    </div>
    <span class="ml-auto inline-flex items-center gap-1 text-[10.5px] text-faint">
      {#if $saveM.isPending || dirty}
        <Loader2 class="h-3 w-3 animate-spin" /> saving…
      {:else if saveError}
        <span class="text-bad">{saveError}</span>
      {:else if savedAt}
        <CheckCircle2 class="h-3 w-3 text-good" />
        saved {timeAgo(savedAt.toISOString())}
      {:else if $q.data?.updated_at}
        <CheckCircle2 class="h-3 w-3 text-good" />
        saved {timeAgo($q.data.updated_at)}
      {:else}
        <span class="text-faint">{wordCount} words</span>
      {/if}
    </span>
  </div>
  <textarea
    bind:value={draft}
    oninput={scheduleSave}
    onblur={saveNow}
    rows="3"
    placeholder="Watching, intent, risk caps for today…"
    class="w-full resize-y rounded border border-border bg-surface-2 px-3 py-2 text-[12.5px] leading-relaxed text-text placeholder:text-faint/60 focus:border-primary/60 focus:outline-none"
  ></textarea>
</Card>
