<script lang="ts">
  /**
   * Pill — small coloured tag chip.
   * Variants ride on token-aligned background + border + text colours.
   */
  type Variant = 'neutral' | 'pos' | 'neg' | 'warn' | 'info' | 'violet';

  interface Props {
    variant?: Variant;
    class?: string;
    children?: import('svelte').Snippet;
  }

  let { variant = 'neutral', class: klass = '', children }: Props = $props();

  const styles: Record<Variant, string> = {
    neutral: 'bg-surface-2 border-border text-muted',
    pos: 'bg-good-soft border-good/30 text-good',
    neg: 'bg-bad-soft border-bad/30 text-bad',
    warn: 'bg-warn-soft border-warn/30 text-warn',
    info: 'bg-primary-soft border-primary/30 text-primary',
    violet: 'bg-violet-soft border-violet/30 text-violet'
  };
</script>

<span
  class={[
    'inline-flex items-center gap-1 rounded border px-1.5 py-0.5',
    'text-[10px] font-semibold uppercase tracking-wider tabular',
    styles[variant],
    klass
  ]
    .filter(Boolean)
    .join(' ')}
>
  {@render children?.()}
</span>
