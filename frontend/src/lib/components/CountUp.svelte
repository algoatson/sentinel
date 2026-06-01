<script lang="ts">
  /**
   * Tweens a number to its target and renders it through `format` each
   * frame — a subtle premium touch on focal metrics (hero equity). Counts
   * from 0 on first paint, then animates between values on update. Instant
   * (no tween) under prefers-reduced-motion.
   */
  import { tweened } from 'svelte/motion';
  import { cubicOut } from 'svelte/easing';

  interface Props {
    value: number;
    duration?: number;
    format?: (n: number) => string;
    class?: string;
  }
  let {
    value,
    duration = 700,
    format = (n: number) => String(Math.round(n)),
    class: cls = ''
  }: Props = $props();

  const reduce =
    typeof window !== 'undefined' &&
    window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

  const n = tweened(reduce ? value : 0, {
    duration: reduce ? 0 : duration,
    easing: cubicOut
  });
  $effect(() => {
    n.set(value);
  });
</script>

<span class={cls}>{format($n)}</span>
