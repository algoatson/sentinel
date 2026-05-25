<script lang="ts">
  import Card from './Card.svelte';
  import { tone } from '../format';

  interface Props {
    label: string;
    /** Big primary value, pre-formatted. */
    value: string;
    /** Optional sub-line. */
    sub?: string;
    /** If set, colour-tone the value by sign. */
    toneValue?: number | null;
    accent?: 'primary' | 'pos' | 'neg' | 'warn' | 'violet' | 'none';
  }

  let {
    label,
    value,
    sub,
    toneValue,
    accent = 'none'
  }: Props = $props();

  const t = $derived(toneValue !== undefined ? tone(toneValue) : null);
  const colour = $derived(
    accent === 'pos' || t === 'pos'
      ? 'text-good'
      : accent === 'neg' || t === 'neg'
        ? 'text-bad'
        : accent === 'warn'
          ? 'text-warn'
          : accent === 'primary'
            ? 'text-primary'
            : accent === 'violet'
              ? 'text-violet'
              : 'text-text'
  );
</script>

<Card class="px-3.5 py-2.5">
  <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
    {label}
  </div>
  <div class={['mt-0.5 text-[1.5rem] font-semibold leading-tight tabular', colour].join(' ')}>
    {value}
  </div>
  {#if sub}
    <div class="text-[11px] text-muted tabular">{sub}</div>
  {/if}
</Card>
