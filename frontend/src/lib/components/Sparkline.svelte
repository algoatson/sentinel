<script lang="ts">
  /**
   * Tiny inline SVG sparkline. Colour-coded by whether the last value
   * is above (green) or below (red) the first — same convention as
   * the equity-curve chart on Overview.
   */
  interface Props {
    values: number[] | undefined;
    width?: number;
    height?: number;
    /** Stroke colour override; auto-tones by sign otherwise. */
    color?: string;
    /** Draw a 0% / no-change baseline line. */
    baseline?: boolean;
  }

  let {
    values,
    width = 80,
    height = 22,
    color,
    baseline = false
  }: Props = $props();

  const vs = $derived(values ?? []);
  const path = $derived.by(() => {
    if (vs.length < 2) return '';
    const min = Math.min(...vs);
    const max = Math.max(...vs);
    const range = max - min || 1;
    const step = width / (vs.length - 1);
    return vs
      .map((v, i) => {
        const x = i * step;
        const y = height - ((v - min) / range) * height;
        return `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`;
      })
      .join(' ');
  });

  const stroke = $derived(
    color ??
      (vs.length >= 2
        ? vs[vs.length - 1] >= vs[0]
          ? 'var(--color-good)'
          : 'var(--color-bad)'
        : 'var(--color-faint)')
  );

  const baselineY = $derived(height / 2);
</script>

{#if vs.length >= 2}
  <svg
    viewBox="0 0 {width} {height}"
    {width}
    {height}
    preserveAspectRatio="none"
    class="block"
  >
    {#if baseline}
      <line
        x1="0"
        x2={width}
        y1={baselineY}
        y2={baselineY}
        stroke="var(--color-border)"
        stroke-dasharray="1 3"
      />
    {/if}
    <path d={path} fill="none" stroke={stroke} stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round" />
  </svg>
{:else}
  <span class="inline-block text-faint">—</span>
{/if}
