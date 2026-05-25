<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { kpi, health } from '$api';
  import { usd, pct, tone } from '../format';
  import { Menu } from 'lucide-svelte';

  interface Props {
    onOpenMobileNav?: () => void;
  }

  let { onOpenMobileNav }: Props = $props();

  const kpiQ = createQuery({
    queryKey: ['kpi'],
    queryFn: kpi,
    refetchInterval: 30_000
  });

  const healthQ = createQuery({
    queryKey: ['health'],
    queryFn: health,
    refetchInterval: 60_000
  });

  let now = $state(new Date());
  setInterval(() => (now = new Date()), 1000);

  // Day-name + time (drop the trailing :SS on narrow screens — see template)
  const clock = $derived(
    now.toLocaleString('en-US', {
      weekday: 'short',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
      timeZone: 'UTC'
    }) + ' UTC'
  );
  const clockShort = $derived(
    now.toLocaleString('en-US', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      timeZone: 'UTC'
    }) + 'Z'
  );
</script>

<header
  class="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-border bg-bg/90 px-3 backdrop-blur md:px-4"
>
  <!-- mobile hamburger -->
  <button
    type="button"
    aria-label="Open menu"
    onclick={onOpenMobileNav}
    class="-ml-1 rounded-md p-2 text-muted transition-colors hover:bg-surface-2 hover:text-text md:hidden"
  >
    <Menu class="h-5 w-5" />
  </button>

  <div class="flex min-w-0 flex-1 items-baseline gap-3 text-sm">
    {#if $kpiQ.data}
      {@const eq = $kpiQ.data.equity}
      {@const ret = $kpiQ.data.return_pct}
      <div class="flex items-baseline gap-1.5">
        <span class="hidden text-[10px] uppercase tracking-wider text-faint sm:inline">Equity</span>
        <span class="font-semibold tabular text-text">{usd(eq)}</span>
        {#if ret !== null}
          <span class={['tabular text-xs', tone(ret) === 'pos' ? 'text-good' : tone(ret) === 'neg' ? 'text-bad' : 'text-muted'].join(' ')}>
            {pct(ret, 1)}
          </span>
        {/if}
      </div>
    {/if}
  </div>

  <div class="flex shrink-0 items-center gap-3 text-xs">
    <span class="hidden tabular font-mono text-muted md:inline">{clock}</span>
    <span class="tabular font-mono text-muted md:hidden">{clockShort}</span>
    {#if $healthQ.data}
      {@const v = $healthQ.data.verdict}
      <span
        class={[
          'flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-semibold',
          v === 'ok'
            ? 'border-good/30 bg-good-soft text-good'
            : v === 'warn'
              ? 'border-warn/30 bg-warn-soft text-warn'
              : v === 'crit'
                ? 'border-bad/30 bg-bad-soft text-bad'
                : 'border-border bg-surface-2 text-muted'
        ].join(' ')}
        title={$healthQ.data.critical.concat($healthQ.data.warnings).join('\n') || 'all clear'}
      >
        <span class={[
          'inline-block h-1.5 w-1.5 rounded-full',
          v === 'ok' ? 'animate-pulse bg-good' :
          v === 'warn' ? 'bg-warn' :
          v === 'crit' ? 'bg-bad' : 'bg-faint'
        ].join(' ')}></span>
        <span class="hidden sm:inline">{$healthQ.data.marker} {$healthQ.data.headline}</span>
        <span class="sm:hidden">{v.toUpperCase()}</span>
      </span>
    {/if}
  </div>
</header>
