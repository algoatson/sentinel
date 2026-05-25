<script lang="ts">
  /**
   * Multi-line equity curve, one line per fund, normalised to
   * starting=100% so different fund sizes are visually comparable.
   *
   * Pure SVG so the bundle stays light — lightweight-charts is already
   * paying its weight on Markets, no need to ship a 2nd chart engine
   * for this small visualization.
   */
  import type { EquityCurve } from '$lib/types';

  interface Props {
    series: EquityCurve[];
    height?: number;
  }

  let { series, height = 200 }: Props = $props();

  const PALETTE = [
    'var(--color-primary)', // blue
    'var(--color-good)',    // mint
    'var(--color-violet)',  // violet
    'var(--color-warn)',    // amber
    'var(--color-bad)',     // red
    '#8ed1fc',              // sky
    '#f2c0ff',              // lavender
    '#a3e635'               // lime
  ];

  type Pt = { x: number; y: number; ts: string; pct: number };
  type Line = {
    fund: string;
    mandate: string;
    color: string;
    pts: Pt[];
    last: number; // last % vs starting
  };

  const W = 800; // viewBox width — SVG scales, this is just the coord space
  const H = $derived(height);
  const PAD = { l: 4, r: 56, t: 8, b: 22 };

  const lines = $derived.by<Line[]>(() => {
    const out: Line[] = [];
    if (!series?.length) return out;

    // Common time domain — all funds plotted on the same x-axis even
    // if some have fewer points (e.g. newly seeded `research` wallet).
    const allTs: number[] = [];
    for (const s of series) {
      for (const p of s.points) allTs.push(new Date(p.ts).getTime());
    }
    if (!allTs.length) return out;
    const tMin = Math.min(...allTs);
    const tMax = Math.max(...allTs);
    const tSpan = tMax - tMin || 1;

    // Y domain — % vs each fund's starting cash, take global min/max
    let yMin = Infinity;
    let yMax = -Infinity;
    const normalised = series.map((s) =>
      s.points.map((p) => {
        const pct = s.starting ? ((p.equity - s.starting) / s.starting) * 100 : 0;
        yMin = Math.min(yMin, pct);
        yMax = Math.max(yMax, pct);
        return { ts: p.ts, pct };
      })
    );
    if (!isFinite(yMin)) {
      yMin = -5;
      yMax = 5;
    }
    // Always include 0% in the visible range so the baseline is visible.
    yMin = Math.min(yMin, 0);
    yMax = Math.max(yMax, 0);
    // Add headroom so the lines aren't glued to the edges.
    const pad = Math.max(1, (yMax - yMin) * 0.08);
    yMin -= pad;
    yMax += pad;

    const innerW = W - PAD.l - PAD.r;
    const innerH = H - PAD.t - PAD.b;

    series.forEach((s, i) => {
      const color = PALETTE[i % PALETTE.length];
      const pts = normalised[i].map((p) => {
        const t = new Date(p.ts).getTime();
        const x = PAD.l + ((t - tMin) / tSpan) * innerW;
        const y = PAD.t + ((yMax - p.pct) / (yMax - yMin)) * innerH;
        return { x, y, ts: p.ts, pct: p.pct };
      });
      out.push({
        fund: s.fund,
        mandate: s.mandate,
        color,
        pts,
        last: pts.length ? pts[pts.length - 1].pct : 0
      });
    });
    return out;
  });

  const baselineY = $derived.by(() => {
    if (!lines.length || !lines[0].pts.length) return 0;
    // Find where 0% maps in pixel space — same formula as the line plotter
    // but with pct=0. Use the first line's y-domain since they all share.
    const all = lines.flatMap((l) => l.pts.map((p) => p.pct));
    const yMin = Math.min(0, ...all) - Math.max(1, (Math.max(0, ...all) - Math.min(0, ...all)) * 0.08);
    const yMax = Math.max(0, ...all) + Math.max(1, (Math.max(0, ...all) - Math.min(0, ...all)) * 0.08);
    const innerH = H - PAD.t - PAD.b;
    return PAD.t + ((yMax - 0) / (yMax - yMin)) * innerH;
  });

  function path(pts: Pt[]): string {
    return pts.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ');
  }
</script>

<div>
  {#if !lines.length}
    <div class="flex h-full items-center justify-center py-12 text-[12px] text-faint">
      No equity history yet — funds need at least one daily mark.
    </div>
  {:else}
    <svg viewBox="0 0 {W} {H}" class="w-full" preserveAspectRatio="none" style:height="{H}px">
      <!-- baseline at 0% -->
      <line
        x1={PAD.l}
        x2={W - PAD.r}
        y1={baselineY}
        y2={baselineY}
        stroke="var(--color-border)"
        stroke-dasharray="2 4"
      />
      <text
        x={W - PAD.r + 4}
        y={baselineY + 3}
        font-size="9"
        fill="var(--color-faint)"
        font-family="var(--font-mono)"
      >0%</text>

      {#each lines as l, i (l.fund)}
        <path
          d={path(l.pts)}
          fill="none"
          stroke={l.color}
          stroke-width="1.6"
          stroke-linejoin="round"
          stroke-linecap="round"
          opacity="0.95"
        />
        <!-- end marker + label -->
        {#if l.pts.length}
          {@const lp = l.pts[l.pts.length - 1]}
          <circle cx={lp.x} cy={lp.y} r="2" fill={l.color} />
          <text
            x={W - PAD.r + 4}
            y={lp.y + 3}
            font-size="10"
            fill={l.color}
            font-family="var(--font-mono)"
          >
            {l.fund} {l.last >= 0 ? '+' : ''}{l.last.toFixed(1)}%
          </text>
        {/if}
      {/each}
    </svg>

    <!-- legend -->
    <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] tabular text-faint">
      {#each lines as l (l.fund)}
        <div class="flex items-center gap-1.5">
          <span
            class="inline-block h-2 w-2 rounded-full"
            style:background-color={l.color}
          ></span>
          <span class="capitalize text-muted">{l.fund}</span>
          <span class={l.last >= 0 ? 'text-good' : 'text-bad'}>
            {l.last >= 0 ? '+' : ''}{l.last.toFixed(2)}%
          </span>
          <span class="text-faint">·</span>
          <span class="text-faint">{l.mandate}</span>
        </div>
      {/each}
    </div>
  {/if}
</div>
