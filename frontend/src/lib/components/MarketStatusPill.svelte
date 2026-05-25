<script lang="ts">
  /**
   * Tiny pill in the TopBar that shows the US-equity market state:
   * Open (mint, pulsing dot), Pre-market (amber sunrise),
   * After-hours (violet city), Closed (slate moon), Holiday (themed
   * emoji + warm tint).
   *
   * Polls every minute — the state only changes at 4 known minute
   * boundaries per day so once-a-minute is plenty.
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { marketStatus } from '$api';

  const q = createQuery({
    queryKey: ['market-status'],
    queryFn: marketStatus,
    refetchInterval: 60_000,
    staleTime: 30_000
  });

  const variant = $derived.by(() => {
    const s = $q.data?.state;
    if (s === 'open')
      return {
        bg: 'bg-good-soft', border: 'border-good/35', text: 'text-good',
        dot: 'bg-good animate-pulse'
      };
    if (s === 'pre')
      return {
        bg: 'bg-warn-soft', border: 'border-warn/35', text: 'text-warn',
        dot: 'bg-warn animate-pulse'
      };
    if (s === 'after')
      return {
        bg: 'bg-violet-soft', border: 'border-violet/35', text: 'text-violet',
        dot: 'bg-violet'
      };
    if (s === 'holiday')
      return {
        bg: 'bg-bad-soft', border: 'border-bad/35', text: 'text-bad',
        dot: 'bg-bad'
      };
    return {
      bg: 'bg-surface-2', border: 'border-border', text: 'text-faint',
      dot: 'bg-faint'
    };
  });
</script>

{#if $q.data}
  <span
    class={[
      'inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-semibold',
      variant.bg, variant.border, variant.text
    ].join(' ')}
    title={[
      $q.data.label,
      $q.data.next_event ?? '',
      $q.data.et_clock
    ].filter(Boolean).join(' · ')}
  >
    <span class={['inline-block h-1.5 w-1.5 rounded-full', variant.dot].join(' ')}></span>
    <span class="text-[12px] leading-none">{$q.data.emoji}</span>
    <span class="hidden md:inline">{$q.data.label}</span>
  </span>
{/if}
