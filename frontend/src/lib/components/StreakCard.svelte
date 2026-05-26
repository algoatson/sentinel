<script lang="ts">
  /**
   * W/L streak + edge-quality stats over the last 200 closed trades.
   *
   * The big number on the left is the *current* run (e.g. "5W" or
   * "3L"). The scoreboard is a tiny row of coloured pips — one per
   * trade, newest→oldest, so a hot streak looks green and a cold
   * patch looks red at a glance. Right column carries the numbers a
   * trader actually wants to see: hit rate, expectancy, avg win,
   * avg loss, and the all-time max W / max L runs.
   *
   * Polls /api/analytics/streaks every 90s (closed-trades cadence).
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { streaks } from '$api';
  import { base } from '$app/paths';
  import { price } from '$lib/format';
  import Card from './Card.svelte';
  import { Flame, Award, Snowflake } from 'lucide-svelte';

  const q = createQuery({
    queryKey: ['streaks', 200],
    queryFn: () => streaks(200),
    refetchInterval: 90_000
  });

  const d = $derived($q.data);
</script>

<Card class="flex h-full flex-col px-4 py-3">
  <div class="mb-2 flex items-center gap-2">
    <Award class="h-3.5 w-3.5 text-primary" />
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Streaks · edge quality
    </div>
    <a
      href={`${base}/journal`}
      class="ml-auto text-[10.5px] text-muted hover:text-primary hover:underline"
    >Journal →</a>
  </div>

  {#if !d}
    <div class="flex flex-1 items-center justify-center text-[12px] text-faint">Loading…</div>
  {:else if d.n === 0}
    <div class="flex flex-1 items-center justify-center text-center text-[12px] text-faint">
      No closed trades yet.
    </div>
  {:else}
    <div class="flex items-stretch gap-4">
      <!-- Big current-streak number on the left -->
      <div class={[
        'flex flex-col items-center justify-center rounded-md border px-3 py-2 text-center',
        d.current.kind === 'win'
          ? 'border-good/40 bg-good-soft text-good'
          : d.current.kind === 'loss'
            ? 'border-bad/40 bg-bad-soft text-bad'
            : 'border-border bg-surface-2 text-muted'
      ].join(' ')}>
        <div class="flex items-center gap-1 text-[9.5px] uppercase tracking-wider opacity-80">
          {#if d.current.kind === 'win'}
            <Flame class="h-3 w-3" /> Win streak
          {:else if d.current.kind === 'loss'}
            <Snowflake class="h-3 w-3" /> Cold streak
          {:else}
            Last trade
          {/if}
        </div>
        <div class="mt-0.5 text-[26px] font-semibold tabular leading-none">
          {d.current.length}
          <span class="text-[12px] uppercase">
            {d.current.kind === 'win' ? 'W' : d.current.kind === 'loss' ? 'L' : '—'}
          </span>
        </div>
        <div class="mt-0.5 text-[9.5px] opacity-70">
          max {d.max_win}W · {d.max_loss}L
        </div>
      </div>

      <!-- Right: pip scoreboard + numbers grid -->
      <div class="flex min-w-0 flex-1 flex-col">
        <div class="mb-2 flex flex-wrap items-center gap-0.5">
          {#each d.last_pnls as p, i (i)}
            <span
              class={[
                'inline-block h-3 w-1.5 rounded-sm',
                p > 0 ? 'bg-good' : p < 0 ? 'bg-bad' : 'bg-faint/40'
              ].join(' ')}
              title={`${p >= 0 ? '+' : ''}${p.toFixed(2)} (${d.last_pnls.length - i} ago)`}
            ></span>
          {/each}
          <span class="ml-2 text-[9.5px] text-faint">last {d.last_pnls.length}</span>
        </div>
        <div class="grid flex-1 grid-cols-3 gap-1.5 text-[10.5px] tabular">
          <div class="rounded border border-border bg-surface-2/50 px-2 py-1">
            <div class="text-[9px] uppercase tracking-wider text-faint">Hit rate</div>
            <div class={[
              'text-[14px] font-semibold',
              (d.hit_rate ?? 0) >= 50 ? 'text-good' : 'text-bad'
            ].join(' ')}>
              {d.hit_rate !== null ? `${d.hit_rate.toFixed(0)}%` : '—'}
            </div>
            <div class="text-[9px] text-faint">{d.wins}W · {d.losses}L</div>
          </div>
          <div class="rounded border border-border bg-surface-2/50 px-2 py-1">
            <div class="text-[9px] uppercase tracking-wider text-faint">Expectancy</div>
            <div class={[
              'text-[14px] font-semibold',
              d.expectancy >= 0 ? 'text-good' : 'text-bad'
            ].join(' ')}>
              {d.expectancy >= 0 ? '+' : ''}{d.expectancy.toFixed(2)}
            </div>
            <div class="text-[9px] text-faint">per trade</div>
          </div>
          <div class="rounded border border-border bg-surface-2/50 px-2 py-1">
            <div class="text-[9px] uppercase tracking-wider text-faint">Avg W / L</div>
            <div class="flex items-baseline gap-1">
              <span class="text-[12px] font-semibold text-good">
                {d.avg_win !== null ? `+${d.avg_win.toFixed(0)}` : '—'}
              </span>
              <span class="text-[10px] text-faint">/</span>
              <span class="text-[12px] font-semibold text-bad">
                {d.avg_loss !== null ? `${d.avg_loss.toFixed(0)}` : '—'}
              </span>
            </div>
            <div class="text-[9px] text-faint">
              {#if d.avg_win !== null && d.avg_loss !== null && d.avg_loss !== 0}
                ratio {(d.avg_win / Math.abs(d.avg_loss)).toFixed(2)}×
              {/if}
            </div>
          </div>
        </div>
      </div>
    </div>
  {/if}
</Card>
