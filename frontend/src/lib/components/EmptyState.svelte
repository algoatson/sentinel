<script lang="ts">
  interface Props {
    title: string;
    description?: string;
    /** Optional lucide icon component, shown in a soft badge above the title. */
    icon?: any;
    children?: import('svelte').Snippet;
  }

  let { title, description, icon, children }: Props = $props();
</script>

<div class="flex flex-col items-center justify-center gap-2 py-12 text-center">
  {#if icon}
    {@const Icon = icon}
    <div class="mb-1 flex h-11 w-11 items-center justify-center rounded-full border border-border bg-surface-2 text-faint">
      <Icon class="h-5 w-5" />
    </div>
  {/if}
  <div class="text-sm font-medium text-muted">{title}</div>
  {#if description}
    <div class="max-w-md text-[12px] leading-relaxed text-faint">{description}</div>
  {/if}
  {#if children}
    <div class="mt-2">
      {@render children?.()}
    </div>
  {/if}
</div>
