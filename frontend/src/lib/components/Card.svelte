<script lang="ts">
  /**
   * Card primitive — the atom of the new dashboard.
   * Surface-1 background, subtle border, optional hover-lift.
   *
   * When interactive=true, renders as a <button> so click+keyboard+aria
   * are handled by the platform. Otherwise renders as a plain <div>.
   */
  interface Props {
    interactive?: boolean;
    class?: string;
    onclick?: (e: MouseEvent) => void;
    children?: import('svelte').Snippet;
  }

  let {
    interactive = false,
    class: klass = '',
    onclick,
    children
  }: Props = $props();

  const base = 'rounded-xl border border-border bg-surface';
</script>

{#if interactive}
  <button
    type="button"
    class={[base, 'card-lift block w-full text-left', klass].filter(Boolean).join(' ')}
    {onclick}
  >
    {@render children?.()}
  </button>
{:else}
  <div class={[base, klass].filter(Boolean).join(' ')}>
    {@render children?.()}
  </div>
{/if}
