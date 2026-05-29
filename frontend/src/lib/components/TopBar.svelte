<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { page } from '$app/state';
  import { base } from '$app/paths';
  import { kpi, health } from '$api';
  import { usd, pct, tone } from '../format';
  import { Menu, Search } from 'lucide-svelte';
  import NotificationBell from './NotificationBell.svelte';
  import MarketStatusPill from './MarketStatusPill.svelte';

  interface Props {
    onOpenMobileNav?: () => void;
    onOpenPalette?: () => void;
  }

  let { onOpenMobileNav, onOpenPalette }: Props = $props();

  // Detect the user's modifier key so the hint chip shows ⌘ on mac,
  // Ctrl elsewhere. SSR-safe — falls back to ⌘ until hydration.
  let modKey = $state('⌘');
  $effect(() => {
    const isMac =
      typeof navigator !== 'undefined' &&
      /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent);
    modKey = isMac ? '⌘' : 'Ctrl';
  });

  // Equity readout: stays in the TopBar so it's always visible across
  // every page (not just Overview where the hero shows it big). A
  // slimmer treatment than before — equity number + signed return —
  // and we hide it on Overview specifically, since the hero there
  // makes it redundant.
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

  // Are we on Overview? If so, the hero already shows the equity 2.6rem
  // tall, so the TopBar readout is redundant there. Cheap reactive
  // check; no extra wire calls.
  const onOverview = $derived(
    page.url.pathname.replace(new RegExp(`^${base}`), '') === '/overview'
  );

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

  <!-- Equity readout — slim, always visible (so the user keeps the
       book context on every page), hidden specifically on /overview
       where the hero would duplicate it. -->
  <div class="flex min-w-0 flex-1 items-baseline gap-3 text-sm">
    {#if !onOverview && $kpiQ.data}
      {@const eq = $kpiQ.data.equity}
      {@const ret = $kpiQ.data.return_pct}
      <a
        href={`${base}/overview`}
        class="flex items-baseline gap-1.5 rounded-md px-1 hover:bg-surface-2"
        title="Go to Overview"
      >
        <span class="hidden text-[10px] uppercase tracking-wider text-faint sm:inline">Equity</span>
        <span class="font-semibold tabular text-text">{usd(eq)}</span>
        {#if ret !== null}
          <span class={['tabular text-xs', tone(ret) === 'pos' ? 'text-good' : tone(ret) === 'neg' ? 'text-bad' : 'text-muted'].join(' ')}>
            {pct(ret, 1)}
          </span>
        {/if}
      </a>
    {/if}
  </div>

  <div class="flex shrink-0 items-center gap-3 text-xs">
    <button
      type="button"
      onclick={onOpenPalette}
      title="Open command palette"
      class="hidden items-center gap-2 rounded-md border border-border bg-surface-2 px-2.5 py-1 text-[11.5px] text-faint transition-colors hover:border-primary/40 hover:text-text sm:flex"
    >
      <Search class="h-3 w-3" />
      <span>Jump…</span>
      <kbd class="rounded border border-border bg-bg px-1 py-px text-[9.5px] font-mono text-faint">
        {modKey}K
      </kbd>
    </button>
    <button
      type="button"
      aria-label="Open palette"
      onclick={onOpenPalette}
      class="rounded-md p-1.5 text-muted transition-colors hover:bg-surface-2 hover:text-text sm:hidden"
    >
      <Search class="h-4 w-4" />
    </button>
    <span class="hidden tabular font-mono text-muted lg:inline">{clock}</span>
    <span class="tabular font-mono text-muted lg:hidden">{clockShort}</span>
    <MarketStatusPill />
    <NotificationBell />
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
