<script lang="ts">
  /**
   * Pairwise daily-return correlation heatmap — concentration risk
   * view. Symmetric grid; cells go red as |corr| rises toward 1 (or
   * -1), neutral at zero.
   *
   * Diagonal = 1 (a name is perfectly correlated with itself).
   * Hidden by default in the colour-scale legend so the eye isn't
   * yanked to the constant ones.
   */
  import type { CorrelationMatrix } from '$api';

  interface Props {
    data: CorrelationMatrix | undefined;
    /** Threshold above which a cell counts as "high correlation" for
     * the summary line. Default 0.6 — strong positive linkage. */
    highThreshold?: number;
  }
  let { data, highThreshold = 0.6 }: Props = $props();

  function bg(v: number | null, isDiagonal: boolean): string {
    if (isDiagonal) return 'rgba(255, 255, 255, 0.10)';
    if (v === null) return 'rgba(255, 255, 255, 0.04)';
    const intensity = Math.min(1, Math.abs(v));
    const alpha = 0.15 + intensity * 0.6;
    if (v > 0) return `rgba(255, 107, 107, ${alpha.toFixed(2)})`;
    return `rgba(102, 153, 255, ${alpha.toFixed(2)})`;
  }

  function txt(v: number | null): string {
    if (v === null) return '—';
    return v >= 0 ? `+${v.toFixed(2)}` : v.toFixed(2);
  }

  // High-correlation pairs called out below the matrix.
  const highPairs = $derived.by(() => {
    if (!data?.matrix?.length) return [] as { a: string; b: string; v: number }[];
    const out: { a: string; b: string; v: number }[] = [];
    for (let i = 0; i < data.tickers.length; i++) {
      for (let j = i + 1; j < data.tickers.length; j++) {
        const v = data.matrix[i][j];
        if (v !== null && v >= highThreshold) {
          out.push({ a: data.tickers[i], b: data.tickers[j], v });
        }
      }
    }
    return out.sort((x, y) => y.v - x.v);
  });
</script>

{#if !data || !data.tickers.length}
  <div class="py-6 text-center text-[12px] text-faint">
    No open positions to correlate — open at least 2 trades.
  </div>
{:else if data.tickers.length === 1}
  <div class="py-6 text-center text-[12px] text-faint">
    Need ≥2 positions for a correlation matrix.
  </div>
{:else}
  <div class="overflow-x-auto">
    <table class="text-[11px] tabular">
      <thead>
        <tr>
          <th class="px-1.5 py-1"></th>
          {#each data.tickers as t (t)}
            <th class="px-1.5 py-1 font-mono text-faint" style="writing-mode: vertical-rl; transform: rotate(180deg);">
              {t}
            </th>
          {/each}
        </tr>
      </thead>
      <tbody>
        {#each data.matrix as row, i (data.tickers[i])}
          <tr>
            <td class="pr-2 text-right font-mono text-muted">{data.tickers[i]}</td>
            {#each row as v, j (data.tickers[j])}
              <td
                class="text-center"
                style:background-color={bg(v, i === j)}
                style:padding="0"
                title={`${data.tickers[i]} vs ${data.tickers[j]}: ${txt(v)} (${data.days}d log returns, n=${(data.bars_used?.[data.tickers[i]] ?? 0)}/${(data.bars_used?.[data.tickers[j]] ?? 0)})`}
              >
                <div class="flex h-6 w-12 items-center justify-center tabular text-[10.5px] text-text/90">
                  {i === j ? '1.00' : txt(v)}
                </div>
              </td>
            {/each}
          </tr>
        {/each}
      </tbody>
    </table>
  </div>

  <div class="mt-2 flex items-center gap-2 text-[10px] tabular text-faint">
    <span>-1</span>
    {#each [-0.7, -0.4, 0, 0.4, 0.7] as v (v)}
      <div class="h-3 w-8 rounded-sm" style:background-color={bg(v, false)}></div>
    {/each}
    <span>+1</span>
    <span class="ml-3">{data.days}d log-return correlation · {data.n} positions</span>
  </div>

  {#if highPairs.length}
    <div class="mt-3 rounded-md border border-warn/30 bg-warn-soft/30 px-3 py-2 text-[11px]">
      <div class="mb-1 flex items-center gap-1.5 text-warn">
        <span class="font-semibold">⚠ Concentration risk</span>
        <span class="text-faint">— {highPairs.length} pair{highPairs.length === 1 ? '' : 's'} ≥ {highThreshold}</span>
      </div>
      <div class="flex flex-wrap gap-x-3 gap-y-1 text-[10.5px] tabular text-muted">
        {#each highPairs.slice(0, 8) as p (p.a + '-' + p.b)}
          <span>
            <span class="font-mono text-text">{p.a}</span>↔<span class="font-mono text-text">{p.b}</span>
            <span class="ml-1 text-warn font-medium">{p.v.toFixed(2)}</span>
          </span>
        {/each}
        {#if highPairs.length > 8}
          <span class="text-faint">+{highPairs.length - 8} more</span>
        {/if}
      </div>
    </div>
  {/if}
{/if}
