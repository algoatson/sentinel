<script lang="ts">
  /**
   * GitHub-style P&L calendar heatmap.
   *
   * Columns = weeks (oldest left → newest right), rows = weekdays
   * (Mon top → Sun bottom). Each cell is a single trading day:
   *  - empty (no closed trades) → faint grid square
   *  - winning day → green, intensity ∝ |pnl| / max_abs
   *  - losing day  → red,   same intensity scale
   *
   * Hover shows the date + dollar P&L + W/L count. Click jumps to the
   * book filter for that day (future hook — for now click does nothing).
   */
  import type { DailyPnlPayload, DailyCell } from '$api';

  interface Props {
    data: DailyPnlPayload | undefined;
  }
  let { data }: Props = $props();

  /**
   * Group cells into week columns. The first column may be partial
   * (start in the middle of a week). Weekday 1=Mon … 7=Sun → row
   * index 0..6.
   */
  const weeks = $derived.by(() => {
    if (!data?.cells?.length) return [] as Array<(DailyCell | null)[]>;
    const cols: Array<(DailyCell | null)[]> = [];
    let col: (DailyCell | null)[] = new Array(7).fill(null);
    let started = false;
    for (const cell of data.cells) {
      const row = cell.weekday - 1; // 0..6
      if (!started) {
        // Pad rows above this weekday with null so the FIRST column
        // begins at the right weekday-row.
        col = new Array(7).fill(null);
        started = true;
      }
      col[row] = cell;
      if (row === 6) {
        cols.push(col);
        col = new Array(7).fill(null);
      }
    }
    // Push any remaining partial week
    if (col.some((c) => c !== null)) cols.push(col);
    return cols;
  });

  const maxAbs = $derived(data?.max_abs ?? 1);

  function colour(c: DailyCell | null): string {
    if (!c || c.closed === 0) return 'rgba(255, 255, 255, 0.04)';
    const intensity = Math.min(1, Math.abs(c.realized_pnl) / maxAbs);
    const alpha = 0.18 + intensity * 0.62;
    if (c.realized_pnl > 0) return `rgba(61, 220, 151, ${alpha.toFixed(2)})`;
    if (c.realized_pnl < 0) return `rgba(255, 107, 107, ${alpha.toFixed(2)})`;
    return 'rgba(255, 255, 255, 0.10)';
  }

  function tooltip(c: DailyCell | null): string {
    if (!c) return '';
    if (c.closed === 0) return `${c.date} · no trades`;
    const sign = c.realized_pnl >= 0 ? '+' : '';
    return (
      `${c.date} · ${sign}$${c.realized_pnl.toFixed(2)} ` +
      `(${c.wins}W / ${c.losses}L · ${c.closed} closed)`
    );
  }

  // Month labels above the columns — show the first column that
  // belongs to each month.
  const monthLabels = $derived.by(() => {
    const out: Array<{ col: number; label: string }> = [];
    let prev = '';
    weeks.forEach((wk, i) => {
      const firstCell = wk.find((c) => c !== null);
      if (!firstCell) return;
      const m = firstCell.date.slice(0, 7);
      if (m !== prev) {
        out.push({
          col: i,
          label: new Date(firstCell.date).toLocaleString('en-US', {
            month: 'short'
          })
        });
        prev = m;
      }
    });
    return out;
  });
</script>

<div>
  {#if data && data.cells.length}
    <div class="flex items-baseline gap-3 text-[10.5px] tabular text-faint">
      <span>{data.from} → {data.to}</span>
      <span class="text-good">
        +{data.cells.reduce((s, c) => s + Math.max(0, c.realized_pnl), 0).toFixed(0)}
      </span>
      <span class="text-bad">
        {data.cells.reduce((s, c) => s + Math.min(0, c.realized_pnl), 0).toFixed(0)}
      </span>
      <span class={[
        'ml-2 font-semibold',
        data.total_realized >= 0 ? 'text-good' : 'text-bad'
      ].join(' ')}>
        net {data.total_realized >= 0 ? '+' : ''}{data.total_realized.toFixed(0)}
      </span>
      <span class="ml-auto">{data.active_days} trading days</span>
    </div>

    <!-- chart body -->
    <div class="mt-3 overflow-x-auto">
      <div class="inline-grid gap-[2px]" style:grid-template-columns="auto repeat({weeks.length}, 12px)">
        <!-- top row: month labels -->
        <span></span>
        {#each weeks as _, i (i)}
          {@const lbl = monthLabels.find((m) => m.col === i)}
          <span class="text-[9px] text-faint">{lbl?.label ?? ''}</span>
        {/each}

        <!-- 7 rows -->
        {#each ['Mon', '', 'Wed', '', 'Fri', '', 'Sun'] as label, row (row)}
          <span class="pr-1 text-[9px] tabular text-faint" style:line-height="12px">{label}</span>
          {#each weeks as wk (wk)}
            {@const c = wk[row]}
            <div
              class="h-[12px] w-[12px] rounded-[2px] transition-colors hover:ring-1 hover:ring-primary/50"
              style:background-color={colour(c)}
              title={tooltip(c)}
            ></div>
          {/each}
        {/each}
      </div>
    </div>

    <!-- legend -->
    <div class="mt-3 flex items-center gap-2 text-[10px] tabular text-faint">
      <span>−</span>
      {#each [0.18, 0.32, 0.46, 0.6, 0.74] as a (a)}
        <div class="h-[10px] w-[10px] rounded-[2px]" style:background-color="rgba(255, 107, 107, {a})"></div>
      {/each}
      <div class="h-[10px] w-[10px] rounded-[2px]" style:background-color="rgba(255, 255, 255, 0.06)"></div>
      {#each [0.18, 0.32, 0.46, 0.6, 0.74] as a (a)}
        <div class="h-[10px] w-[10px] rounded-[2px]" style:background-color="rgba(61, 220, 151, {a})"></div>
      {/each}
      <span>+</span>

      {#if data.best_day || data.worst_day}
        <span class="ml-auto flex items-center gap-3">
          {#if data.best_day}
            <span class="text-good">
              best {data.best_day.date.slice(5)}: +{data.best_day.realized_pnl.toFixed(0)}
            </span>
          {/if}
          {#if data.worst_day}
            <span class="text-bad">
              worst {data.worst_day.date.slice(5)}: {data.worst_day.realized_pnl.toFixed(0)}
            </span>
          {/if}
        </span>
      {/if}
    </div>
  {:else}
    <div class="py-8 text-center text-[12px] text-faint">
      No closed trades in window — the heatmap fills in as positions close.
    </div>
  {/if}
</div>
