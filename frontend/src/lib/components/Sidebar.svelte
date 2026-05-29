<script lang="ts">
  import { page } from '$app/state';
  import { base } from '$app/paths';
  import { createQuery } from '@tanstack/svelte-query';
  import { openPositions, riskMonitor, closedPositions } from '$api';
  import {
    LayoutDashboard,
    Briefcase,
    Layers,
    LineChart,
    FlaskConical,
    Brain,
    Satellite,
    Target,
    Bell,
    Search,
    Sparkles,
    Cog,
    SlidersHorizontal,
    Activity as ActivityIcon,
    BarChart3,
    GitCompareArrows,
    BookText
  } from 'lucide-svelte';

  interface Props {
    /** Mobile-only: parent toggles to show as an overlay. */
    mobileOpen?: boolean;
    /** Mobile-only: called when the user clicks a link or backdrop. */
    onClose?: () => void;
  }

  let { mobileOpen = false, onClose }: Props = $props();

  type Item = { href: string; label: string; icon: typeof LayoutDashboard };
  type Section = { label: string; items: Item[] };

  // Resectioned for scanability. Old layout had "Portfolio" and "Book"
  // both using the same Briefcase icon (looked like a typo), 11 nav
  // items in a single section (a wall to scan), and "Live feed" sitting
  // in the workspace mix even though it's a passive stream. Now:
  //   • Trade  — the day-to-day money loop (4)
  //   • Intel  — the bot's research output (4)
  //   • Tools  — utilities (5)
  //   • Ops    — config + system (2)
  // Book gets the Layers icon so it's visually distinct from Portfolio.
  const sections: Section[] = [
    {
      label: 'Trade',
      items: [
        { href: '/overview', label: 'Overview', icon: LayoutDashboard },
        { href: '/portfolio', label: 'Portfolio', icon: Briefcase },
        { href: '/book', label: 'Book', icon: Layers },
        { href: '/journal', label: 'Journal', icon: BookText }
      ]
    },
    {
      label: 'Intel',
      items: [
        { href: '/markets', label: 'Markets', icon: LineChart },
        { href: '/research', label: 'Research', icon: FlaskConical },
        { href: '/theses', label: 'Theses', icon: Brain },
        { href: '/intel', label: 'Intel', icon: Satellite },
        { href: '/calls', label: 'Calls', icon: Target },
        { href: '/analytics', label: 'Analytics', icon: BarChart3 }
      ]
    },
    {
      label: 'Tools',
      items: [
        { href: '/copilot', label: 'Copilot', icon: Sparkles },
        { href: '/watches', label: 'Watches', icon: Bell },
        { href: '/lookup', label: 'Lookup', icon: Search },
        { href: '/compare', label: 'Compare', icon: GitCompareArrows },
        { href: '/feed', label: 'Live feed', icon: ActivityIcon }
      ]
    },
    {
      label: 'Ops',
      items: [
        { href: '/system', label: 'System', icon: Cog },
        { href: '/settings', label: 'Settings', icon: SlidersHorizontal }
      ]
    }
  ];

  const current = $derived(
    page.url.pathname.replace(new RegExp(`^${base}`), '') || '/'
  );

  // Live-pulses for nav rows. These reuse the same cache keys the
  // route pages already poll, so adding them to the sidebar costs
  // nothing on the wire — TanStack Query dedupes the in-flight
  // requests across mounts.
  const positionsQ = createQuery({
    queryKey: ['positions-open'],
    queryFn: openPositions,
    refetchInterval: 30_000
  });
  const riskQ = createQuery({
    queryKey: ['risk-monitor'],
    queryFn: riskMonitor,
    refetchInterval: 60_000
  });
  const closedQ = createQuery({
    queryKey: ['positions-closed-sidebar'],
    queryFn: () => closedPositions({ limit: 250 }),
    refetchInterval: 90_000
  });

  const openCount = $derived($positionsQ.data?.length ?? 0);
  const nearStopCount = $derived($riskQ.data?.n_near_stop ?? 0);
  const nakedCount = $derived($riskQ.data?.naked.length ?? 0);
  const unreflectedCount = $derived(
    ($closedQ.data ?? []).filter((t) => !(t.notes ?? '').trim()).length
  );

  type Badge = { label: string; tone: 'pos' | 'neg' | 'warn' | 'mute' };
  function badgesFor(href: string): Badge[] {
    if (href === '/book') {
      const out: Badge[] = [];
      if (openCount) out.push({ label: String(openCount), tone: 'mute' });
      if (nearStopCount) out.push({
        label: `${nearStopCount}!`,
        tone: 'neg'
      });
      if (nakedCount) out.push({
        label: `${nakedCount}∅`,
        tone: 'warn'
      });
      return out;
    }
    if (href === '/journal' && unreflectedCount > 0) {
      return [{ label: String(unreflectedCount), tone: 'warn' }];
    }
    return [];
  }

  const TONE_CLASS: Record<Badge['tone'], string> = {
    pos: 'border-good/40 bg-good-soft text-good',
    neg: 'border-bad/40 bg-bad-soft text-bad',
    warn: 'border-warn/40 bg-warn-soft text-warn',
    mute: 'border-border bg-surface-2 text-muted'
  };
</script>

{#snippet inner()}
  <a
    href={`${base}/overview`}
    onclick={() => onClose?.()}
    class="mb-3 flex items-center gap-2 px-2 py-1.5 text-base font-semibold tracking-tight"
  >
    <span class="text-lg">🛰</span>
    <span>Sentinel</span>
    <span
      class="ml-1 rounded border border-border bg-surface-2 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-widest text-faint"
    >
      v2
    </span>
  </a>

  {#each sections as section, i (section.label)}
    <div
      class={[
        'mt-2 px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-[0.13em] text-faint',
        i === 0 && 'mt-0'
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {section.label}
    </div>
    {#each section.items as item (item.href)}
      {@const active = current === item.href || current.startsWith(item.href + '/')}
      {@const badges = badgesFor(item.href)}
      <a
        href={`${base}${item.href}`}
        onclick={() => onClose?.()}
        class={[
          'group relative flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-[13px]',
          'transition-colors',
          active
            ? 'bg-white/[0.08] font-medium text-text'
            : 'text-muted hover:bg-white/[0.04] hover:text-text'
        ]
          .filter(Boolean)
          .join(' ')}
      >
        {#if active}
          <span class="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-r bg-primary"></span>
        {/if}
        <item.icon class={[
          'h-3.5 w-3.5 shrink-0',
          active ? 'text-primary opacity-100' : 'opacity-70'
        ].join(' ')} />
        <span>{item.label}</span>
        {#if badges.length}
          <span class="ml-auto inline-flex items-center gap-1">
            {#each badges as b (b.label)}
              <span
                class={[
                  'rounded border px-1 py-0 text-[9.5px] font-semibold tabular',
                  TONE_CLASS[b.tone]
                ].join(' ')}
                title={
                  item.href === '/book' && b.tone === 'neg'
                    ? `${nearStopCount} positions within 1.5% of stop`
                    : item.href === '/book' && b.tone === 'warn'
                      ? `${nakedCount} open positions with no stop set`
                      : item.href === '/journal' && b.tone === 'warn'
                        ? `${unreflectedCount} closed trades without a reflection`
                        : `${openCount} open positions`
                }
              >{b.label}</span>
            {/each}
          </span>
        {/if}
      </a>
    {/each}
  {/each}

  <div class="mt-auto px-2 pt-3 text-[10px] text-faint">
    <a
      href="/"
      class="underline hover:text-muted"
      data-sveltekit-reload
    >v1</a>
    <span> · </span>
    <button
      type="button"
      onclick={(e) => {
        e.preventDefault();
        // bubble a "?" key so the layout's global handler opens help
        window.dispatchEvent(new KeyboardEvent('keydown', { key: '?' }));
      }}
      class="underline hover:text-muted"
    >shortcuts (?)</button>
  </div>
{/snippet}

<!-- ── desktop: sticky left rail ─────────────────────────── -->
<aside
  class="sticky top-0 hidden h-screen w-56 shrink-0 flex-col gap-0.5 border-r border-border bg-white/[0.014] px-3 py-4 md:flex"
>
  {@render inner()}
</aside>

<!-- ── mobile: overlay drawer ────────────────────────────── -->
{#if mobileOpen}
  <div class="fixed inset-0 z-50 flex md:hidden">
    <button
      type="button"
      aria-label="Close menu"
      class="absolute inset-0 cursor-default bg-black/55 backdrop-blur-sm"
      onclick={() => onClose?.()}
    ></button>
    <aside
      class="relative flex h-full w-64 flex-col gap-0.5 border-r border-border bg-surface px-3 py-4 shadow-2xl animate-[slideInLeft_0.18s_ease-out]"
    >
      {@render inner()}
    </aside>
  </div>
{/if}

<style>
  @keyframes slideInLeft {
    from {
      transform: translateX(-100%);
      opacity: 0;
    }
    to {
      transform: translateX(0);
      opacity: 1;
    }
  }
</style>
