<script lang="ts">
  import { pct, tone } from '../format';

  interface Props {
    /** The raw percentage (already in 0–100 scale, e.g. 4.21 for 4.21%). */
    value: number | null | undefined;
    /** Suffix label, e.g. "1d", "5d". */
    label?: string;
    digits?: number;
    signed?: boolean;
    class?: string;
  }

  let {
    value,
    label,
    digits = 2,
    signed = true,
    class: klass = ''
  }: Props = $props();

  const t = $derived(tone(value));
  const colour = $derived(
    t === 'pos' ? 'text-good' : t === 'neg' ? 'text-bad' : 'text-muted'
  );
</script>

<span class={['tabular font-medium', colour, klass].filter(Boolean).join(' ')}>
  {pct(value, digits, signed)}{label ? ` ${label}` : ''}
</span>
