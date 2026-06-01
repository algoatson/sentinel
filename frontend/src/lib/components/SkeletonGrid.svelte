<script lang="ts">
  /**
   * A grid of card-shaped shimmer placeholders — drop-in replacement for a
   * bare centered <Spinner> on card-grid pages (Intel, Book…). Mirrors the
   * real card silhouette (eyebrow row → title → subtitle → meta) so the
   * load→content swap is a smooth fill rather than a layout jump. Only shows
   * on the first load; TanStack keeps data across refetches.
   */
  import Card from './Card.svelte';
  import Skeleton from './Skeleton.svelte';

  interface Props {
    count?: number;
    /** Grid classes — defaults to the standard 1/2/3-col card grid. */
    class?: string;
  }
  let {
    count = 6,
    class: cls = 'grid grid-cols-1 gap-2.5 md:grid-cols-2 xl:grid-cols-3'
  }: Props = $props();
</script>

<div class={cls} aria-hidden="true">
  {#each Array(count) as _, i (i)}
    <Card class="px-4 py-3">
      <div class="flex items-center gap-1.5">
        <Skeleton class="h-4 w-16 rounded" />
        <Skeleton class="h-4 w-10 rounded" />
        <Skeleton class="ml-auto h-3 w-8 rounded" />
      </div>
      <Skeleton class="mt-2.5 h-3.5 w-full rounded" />
      <Skeleton class="mt-1.5 h-3 w-3/5 rounded" />
      <Skeleton class="mt-3 h-2.5 w-24 rounded" />
    </Card>
  {/each}
</div>
