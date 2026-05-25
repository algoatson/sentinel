<script lang="ts">
  /**
   * Right-side drawer with backdrop. Escape closes; backdrop-click closes;
   * content area scrolls independently. Width capped at max-w-2xl so the
   * underlying page stays partially visible for context.
   */
  interface Props {
    open: boolean;
    onClose: () => void;
    title?: string;
    /** Header content snippet — replaces the simple `title` if given. */
    header?: import('svelte').Snippet;
    /** Footer content (sticky bottom). */
    footer?: import('svelte').Snippet;
    children?: import('svelte').Snippet;
    /** Extra class on the drawer panel (e.g. 'max-w-3xl'). */
    class?: string;
  }

  let {
    open,
    onClose,
    title,
    header,
    footer,
    children,
    class: klass = 'max-w-2xl'
  }: Props = $props();
</script>

<svelte:window
  onkeydown={(e) => {
    if (e.key === 'Escape' && open) onClose();
  }}
/>

{#if open}
  <div class="fixed inset-0 z-50 flex">
    <button
      type="button"
      aria-label="Close drawer"
      class="absolute inset-0 cursor-default bg-black/55 backdrop-blur-sm animate-[fadeIn_0.15s_ease-out]"
      onclick={onClose}
    ></button>
    <aside
      class={[
        'relative ml-auto flex h-full w-full flex-col border-l border-border bg-surface shadow-2xl',
        'animate-[slideInRight_0.22s_cubic-bezier(0.16,1,0.3,1)]',
        klass
      ].join(' ')}
      role="dialog"
      aria-modal="true"
    >
      <div class="flex items-center gap-2 border-b border-border px-5 py-3">
        {#if header}
          {@render header()}
        {:else if title}
          <div class="text-sm font-semibold text-text">{title}</div>
        {/if}
        <button
          type="button"
          aria-label="Close"
          class="ml-auto -mr-1 rounded p-1 text-faint transition-colors hover:bg-surface-2 hover:text-text"
          onclick={onClose}
        >✕</button>
      </div>
      <div class="flex-1 overflow-y-auto px-5 py-4">
        {@render children?.()}
      </div>
      {#if footer}
        <div class="border-t border-border bg-surface-2/40 px-5 py-3">
          {@render footer()}
        </div>
      {/if}
    </aside>
  </div>
{/if}

<style>
  @keyframes slideInRight {
    from {
      transform: translateX(100%);
    }
    to {
      transform: translateX(0);
    }
  }
  @keyframes fadeIn {
    from {
      opacity: 0;
    }
    to {
      opacity: 1;
    }
  }
</style>
