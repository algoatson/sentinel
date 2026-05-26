<script lang="ts">
  import { page } from '$app/state';
  import { base } from '$app/paths';
  import {
    LayoutDashboard,
    Briefcase,
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
    GitCompareArrows
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

  const sections: Section[] = [
    {
      label: 'Workspace',
      items: [
        { href: '/overview', label: 'Overview', icon: LayoutDashboard },
        { href: '/portfolio', label: 'Portfolio', icon: Briefcase },
        { href: '/book', label: 'Book', icon: Briefcase },
        { href: '/markets', label: 'Markets', icon: LineChart },
        { href: '/research', label: 'Research', icon: FlaskConical },
        { href: '/theses', label: 'Theses', icon: Brain },
        { href: '/intel', label: 'Intel', icon: Satellite },
        { href: '/calls', label: 'Calls', icon: Target },
        { href: '/analytics', label: 'Analytics', icon: BarChart3 },
        { href: '/feed', label: 'Live feed', icon: ActivityIcon }
      ]
    },
    {
      label: 'Tools',
      items: [
        { href: '/watches', label: 'Watches', icon: Bell },
        { href: '/lookup', label: 'Lookup', icon: Search },
        { href: '/compare', label: 'Compare', icon: GitCompareArrows },
        { href: '/copilot', label: 'Copilot', icon: Sparkles }
      ]
    },
    {
      label: 'Operations',
      items: [
        { href: '/system', label: 'System', icon: Cog },
        { href: '/settings', label: 'Settings', icon: SlidersHorizontal }
      ]
    }
  ];

  const current = $derived(
    page.url.pathname.replace(new RegExp(`^${base}`), '') || '/'
  );
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
