<script lang="ts">
  /**
   * Bar histogram of trade return %. Bins are server-side and
   * symmetric around 0%, so the chart is trivially renderable as
   * raw CSS-flex bars — no charting library needed.
   *
   * Above the bars: a stat strip with mean / median / stdev / skew
   * plus p10 / p90 cuts. The skew sign + magnitude tells you the
   * shape of the edge at a glance:
   *  - positive skew  → big rare winners, cluster of small losses (good)
   *  - negative skew  → big rare losers,  cluster of small wins   (bad)
   */
  import type { PnlDistribution } from '$api';

  interface Props {
    data: PnlDistribution | undefined;
    height?: number;
  }
  let { data, height = 180 }: Props = $props();

  const maxCount = $derived(
    data ? Math.max(1, ...data.bins.map((b) => b.count)) : 1
  );
</script>

{#if !data}
  <div class="py-6 text-center text-[12px] text-faint">Loading…</div>
{:else if data.n === 0}
  <div class="py-6 text-center text-[12px] text-faint">No closed trades yet.</div>
{:else}
  <!-- Moment stats strip -->
  <div class="mb-2 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-[10.5px] tabular text-faint">
    <span><span class="text-text">{data.n}</span> trades</span>
    {#if data.mean_pct !== null}
      <span>·</span>
      <span>mean
        <span class={data.mean_pct >= 0 ? 'text-good' : 'text-bad'}>
          {data.mean_pct >= 0 ? '+' : ''}{data.mean_pct.toFixed(2)}%
        </span>
      </span>
    {/if}
    {#if data.median_pct !== null}
      <span>median
        <span class={data.median_pct >= 0 ? 'text-good' : 'text-bad'}>
          {data.median_pct >= 0 ? '+' : ''}{data.median_pct.toFixed(2)}%
        </span>
      </span>
    {/if}
    {#if data.stdev_pct !== null}
      <span>σ <span class="text-text">{data.stdev_pct.toFixed(2)}%</span></span>
    {/if}
    {#if data.skew !== null}
      <span>
        skew
        <span class={data.skew >= 0 ? 'text-good' : 'text-bad'}>
          {data.skew >= 0 ? '+' : ''}{data.skew.toFixed(2)}
        </span>
      </span>
    {/if}
    {#if data.p10 !== null && data.p90 !== null}
      <span>·</span>
      <span>p10 <span class={data.p10 >= 0 ? 'text-good' : 'text-bad'}>{data.p10.toFixed(2)}%</span></span>
      <span>p90 <span class={data.p90 >= 0 ? 'text-good' : 'text-bad'}>{data.p90.toFixed(2)}%</span></span>
    {/if}
    {#if data.best !== undefined && data.worst !== undefined}
      <span>·</span>
      <span>best <span class="text-good">+{data.best.toFixed(2)}%</span></span>
      <span>worst <span class="text-bad">{data.worst.toFixed(2)}%</span></span>
    {/if}
  </div>

  <div class="flex items-end gap-[1px]" style:height="{height}px">
    {#each data.bins as b (b.lo)}
      {@const h = (b.count / maxCount) * 100}
      <div class="relative flex flex-1 items-end" title={`${b.lo}%..${b.hi}% : ${b.count}`}>
        <div
          class={[
            'w-full rounded-t',
            b.lo >= 0 ? 'bg-good/70' : 'bg-bad/70',
            b.count === 0 ? 'opacity-30' : ''
          ].join(' ')}
          style:height="{Math.max(b.count ? 2 : 1, h)}%"
        ></div>
        <!-- Centerline at 0% — the bin straddling zero is the
             pivot. The very first non-negative bin marks the
             boundary; we draw a thin vertical at its left edge. -->
        {#if b.lo === 0}
          <div class="pointer-events-none absolute -top-1 bottom-0 -left-px w-px bg-text/30"></div>
        {/if}
      </div>
    {/each}
  </div>
  <div class="mt-1 flex justify-between text-[9.5px] tabular text-faint">
    <span>{data.bins[0]?.lo}%</span>
    <span>0%</span>
    <span>{data.bins.at(-1)?.hi}%</span>
  </div>
{/if}
