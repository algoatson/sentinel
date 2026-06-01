<script lang="ts">
  /**
   * Today's plan card — TWO PANES side by side:
   *   left  = the user's manual scratchpad (autosaved DailyPlan)
   *   right = the bot's pre-market briefing (read-only, persisted by
   *           pipelines.briefing at 08:30 ET)
   *
   * The pre-market briefing pipeline reads the user's plan as part of
   * its payload, so the right pane is the bot's read of the day given
   * the left pane's stance — they feed each other.
   *
   * No new LLM cost: the briefing already runs daily and posts to
   * Discord; we just surface the persisted body here.
   */
  import { createQuery, createMutation, useQueryClient } from '@tanstack/svelte-query';
  import { getDailyPlan, putDailyPlan, briefingToday } from '$api';
  import Card from './Card.svelte';
  import Markdown from './Markdown.svelte';
  import {
    ClipboardList, CheckCircle2, Loader2, Sparkles,
  } from 'lucide-svelte';
  import { timeAgo } from '$lib/format';

  // ── user plan (left pane) ────────────────────────────────────────
  const planQ = createQuery({
    queryKey: ['plan-today'],
    queryFn: getDailyPlan,
    refetchInterval: 5 * 60_000,
  });
  const qc = useQueryClient();
  const saveM = createMutation({
    mutationFn: (body: string) => putDailyPlan(body),
    onSuccess: () => {
      savedAt = new Date();
      qc.setQueryData(['plan-today'], (prev: any) =>
        prev ? { ...prev, body: draft.trim(), updated_at: new Date().toISOString() } : prev,
      );
    },
    onError: (e) => { saveError = e instanceof Error ? e.message : String(e); },
  });

  let draft = $state('');
  let initialised = false;
  let dirty = $state(false);
  let savedAt = $state<Date | null>(null);
  let saveError = $state<string | null>(null);
  let debounceTimer: ReturnType<typeof setTimeout> | null = null;

  $effect(() => {
    const d = $planQ.data;
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
    if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null; }
    if (dirty) { $saveM.mutate(draft); dirty = false; }
  }
  const wordCount = $derived(
    draft.trim() ? draft.trim().split(/\s+/).length : 0,
  );

  // ── bot briefing (right pane) ───────────────────────────────────
  const briefingQ = createQuery({
    queryKey: ['briefing-today'],
    queryFn: briefingToday,
    // Briefing fires once at 08:30 ET. Hourly refetch is plenty —
    // and the user can refresh the page.
    refetchInterval: 60 * 60_000,
  });

  const IMPORTANCE_TONE: Record<number, string> = {
    5: 'border-bad/40 bg-bad-soft text-bad',
    4: 'border-warn/40 bg-warn-soft text-warn',
    3: 'border-primary/40 bg-primary-soft text-primary',
    2: 'border-border bg-surface-2 text-muted',
    1: 'border-border bg-surface-2 text-faint',
  };
</script>

<Card class="px-0 py-0">
  <div class="grid grid-cols-1 lg:grid-cols-2">
    <!-- ── LEFT: user plan ────────────────────────────────────── -->
    <div class="border-b border-border lg:border-b-0 lg:border-r">
      <div class="mb-2 flex items-center gap-2 border-b border-border-soft px-4 py-2">
        <ClipboardList class="h-3.5 w-3.5 text-primary" />
        <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
          Your plan
          <span class="ml-1 text-[9.5px] normal-case text-muted">
            ({$planQ.data?.plan_date ?? ''})
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
          {:else if $planQ.data?.updated_at}
            <CheckCircle2 class="h-3 w-3 text-good" />
            saved {timeAgo($planQ.data.updated_at)}
          {:else}
            <span class="text-faint">{wordCount} words</span>
          {/if}
        </span>
      </div>
      <div class="px-4 pb-3">
        <textarea
          bind:value={draft}
          oninput={scheduleSave}
          onblur={saveNow}
          rows="6"
          placeholder="Watching, intent, risk caps for today… the bot reads this into the pre-market briefing"
          class="w-full resize-y rounded border border-border bg-surface-2 px-3 py-2 text-[12.5px] leading-relaxed text-text placeholder:text-faint/60 focus:border-primary/60 focus:outline-none"
        ></textarea>
      </div>
    </div>

    <!-- ── RIGHT: bot briefing ───────────────────────────────── -->
    <div>
      <div class="mb-2 flex items-center gap-2 border-b border-border-soft px-4 py-2">
        <Sparkles class="h-3.5 w-3.5 text-violet" />
        <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
          Bot briefing
          {#if $briefingQ.data?.brief_date}
            <span class="ml-1 text-[9.5px] normal-case text-muted">
              ({$briefingQ.data.brief_date})
            </span>
          {/if}
        </div>
        <span class="ml-auto inline-flex items-center gap-1 text-[10.5px] text-faint">
          {#if $briefingQ.data?.importance}
            {@const imp = $briefingQ.data.importance}
            <span class={[
              'rounded border px-1.5 py-0.5 text-[9.5px] font-semibold uppercase tracking-wider',
              IMPORTANCE_TONE[imp] ?? 'border-border bg-surface-2 text-muted',
            ].join(' ')}
              title={$briefingQ.data.importance_reason ?? ''}
            >
              {imp}/5
            </span>
          {/if}
          {#if $briefingQ.data?.generated_at}
            <span>· generated {timeAgo($briefingQ.data.generated_at)}</span>
          {/if}
        </span>
      </div>
      <!-- Cap the briefing height: the pre-market read can run many
           paragraphs, and ungated it shoved the equity chart + positions
           far down the page. Bounded + scroll keeps the hero compact while
           every word stays accessible. -->
      <div class="max-h-72 overflow-y-auto px-4 pb-3 text-[12.5px] leading-relaxed text-text">
        {#if $briefingQ.isLoading}
          <div class="py-2 text-center text-[12px] text-faint">Loading…</div>
        {:else if $briefingQ.data?.body}
          <Markdown source={$briefingQ.data.body} />
        {:else}
          <div class="py-2 text-[12px] italic text-faint">
            No briefing yet — the bot generates one at 08:30 ET on
            trading days. Tomorrow's read will appear here.
          </div>
        {/if}
      </div>
    </div>
  </div>
</Card>
