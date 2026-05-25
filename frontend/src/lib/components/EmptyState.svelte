<script lang="ts">
  interface Props {
    title: string;
    description?: string;
    children?: import('svelte').Snippet;
  }

  let { title, description, children }: Props = $props();
</script>

<div class="flex flex-col items-center justify-center gap-2 py-12 text-center">
  <div class="text-sm font-medium text-muted">{title}</div>
  {#if description}
    <div class="max-w-md text-[12px] text-faint">{description}</div>
  {/if}
  {#if children}
    <div class="mt-2">
      {@render children?.()}
    </div>
  {/if}
</div>
