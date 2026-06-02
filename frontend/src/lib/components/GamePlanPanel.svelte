<script lang="ts">
  /**
   * Morning Game Plan — the day's ranked, book-centric action list.
   *
   * One decision surface fusing risk / maturing calls / catalysts / fresh
   * ideas. Numbers are grounded by construction (the backend assembler pulls
   * real figures; the LLM only ranks + phrases). Refreshes on the `game_plan`
   * SSE event (wired in +layout) + a slow poll.
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { gamePlanToday, type GamePlanSection } from '$api';
  import Card from '$components/Card.svelte';
  import TickerLink from '$components/TickerLink.svelte';
  import Skeleton from '$components/Skeleton.svelte';
  import { timeAgo } from '$lib/format';
  import {
    ClipboardList, Shield, Target as TargetIcon, CalendarDays, Sparkles
  } from 'lucide-svelte';

  const q = createQuery({
    queryKey: ['game-plan-today'],
    queryFn: gamePlanToday,
    refetchInterval: 300_000
  });

  type Meta = { label: string; icon: typeof Shield; accent: string };
  const SECTION: Record<string, Meta> = {
    book_risk:   { label: 'Book risk',    icon: Shield,      accent: 'text-bad' },
    maturing:    { label: 'Maturing calls', icon: TargetIcon, accent: 'text-violet' },
    catalysts:   { label: 'Catalysts',    icon: CalendarDays, accent: 'text-warn' },
    fresh_ideas: { label: 'Fresh ideas',  icon: Sparkles,    accent: 'text-primary' }
  };
  function meta(kind: string): Meta {
    return SECTION[kind] ?? { label: kind, icon: ClipboardList, accent: 'text-muted' };
  }

  // priority 1 = act first → strongest accent.
  const PRIO: Record<number, string> = {
    1: 'border-bad/60 bg-bad-soft text-bad',
    2: 'border-warn/50 bg-warn-soft text-warn',
    3: 'border-border bg-surface-2 text-faint'
  };
  function prio(p: number): string {
    return PRIO[p] ?? PRIO[3];
  }

  function nonEmpty(secs: GamePlanSection[]): GamePlanSection[] {
    return (secs ?? []).filter((s) => (s.items ?? []).length > 0);
  }
</script>

<Card class="p-0 overflow-hidden">
  <div class="flex items-center justify-between border-b border-border px-3.5 py-2.5">
    <div class="flex items-center gap-2">
      <ClipboardList class="h-4 w-4 text-primary" />
      <span class="text-[13px] font-semibold tracking-tight">Morning Game Plan</span>
    </div>
    {#if $q.data?.generated_at}
      <span class="text-[10px] tabular text-faint" title={$q.data.generated_at}>
        {timeAgo($q.data.generated_at)}
      </span>
    {/if}
  </div>

  {#if $q.isLoading}
    <div class="space-y-2 p-3.5"><Skeleton class="h-5 w-full rounded" lines={6} /></div>
  {:else if !$q.data || !$q.data.exists}
    <div class="px-3.5 py-6 text-center text-[12px] text-faint">
      No plan yet — it's generated automatically at <span class="tabular">08:45 ET</span> on
      trading days, fusing your book risk, maturing calls, catalysts and fresh ideas
      into one ranked list.
    </div>
  {:else}
    {@const sections = nonEmpty($q.data.sections)}
    {#if $q.data.the_read}
      <div class="border-b border-border-soft bg-surface-2/40 px-3.5 py-2.5 text-[12.5px] leading-relaxed text-muted">
        {$q.data.the_read}
      </div>
    {/if}

    {#if !sections.length}
      <div class="px-3.5 py-5 text-center text-[12px] text-faint">
        Nothing pressing this morning — book is quiet, no triggers or fresh ideas to action.
      </div>
    {:else}
      <div class="divide-y divide-border-soft">
        {#each sections as section (section.kind)}
          {@const m = meta(section.kind)}
          <div class="px-3.5 py-2.5">
            <div class="mb-1.5 flex items-center gap-1.5">
              <m.icon class={['h-3.5 w-3.5', m.accent].join(' ')} />
              <span class="text-[10px] font-semibold uppercase tracking-[0.13em] text-faint">
                {m.label}
              </span>
            </div>
            <div class="space-y-1.5">
              {#each section.items as item (item.headline)}
                <div class="flex items-start gap-2.5 rounded-md border border-border-soft bg-surface-2/30 px-2.5 py-1.5">
                  <span
                    class={[
                      'mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border text-[10px] font-bold tabular',
                      prio(item.priority)
                    ].join(' ')}
                    title={`priority ${item.priority} (1 = act first)`}
                  >{item.priority}</span>
                  <div class="min-w-0 flex-1">
                    <div class="flex items-baseline gap-1.5 text-[12.5px]">
                      {#if item.ticker}
                        <TickerLink ticker={item.ticker} class="font-semibold" />
                      {/if}
                      <span class="text-text">{item.headline}</span>
                    </div>
                    <div class="mt-0.5 flex items-center gap-2 text-[11px]">
                      <span class="text-primary">→ {item.action}</span>
                      {#if item.trigger}
                        <span class="rounded bg-surface-3 px-1 py-0 text-[9.5px] tabular text-faint">{item.trigger}</span>
                      {/if}
                    </div>
                  </div>
                </div>
              {/each}
            </div>
          </div>
        {/each}
      </div>
    {/if}

    {#if $q.data.model}
      <div class="border-t border-border-soft px-3.5 py-1.5 text-[9.5px] text-faint">
        ranked by {$q.data.model} · figures pulled from live book data
      </div>
    {/if}
  {/if}
</Card>
