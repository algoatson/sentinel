<script lang="ts">
  /**
   * Shimmer loading placeholder. Size it with Tailwind classes via `class`
   * (e.g. `h-4 w-32`). The shimmer + base colour come from `.skel` in
   * app.css (static under prefers-reduced-motion). For multi-line blocks
   * pass `lines` to stack N bars; the last is shortened for a natural look.
   */
  interface Props {
    class?: string;
    lines?: number;
  }
  let { class: cls = 'h-4 w-full', lines = 1 }: Props = $props();
</script>

{#if lines > 1}
  <div class="space-y-2">
    {#each Array(lines) as _, i (i)}
      <div class={['skel', cls, i === lines - 1 ? 'w-2/3' : ''].join(' ')}></div>
    {/each}
  </div>
{:else}
  <div class={['skel', cls].join(' ')}></div>
{/if}
