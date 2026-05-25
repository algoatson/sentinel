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
    Cog
  } from 'lucide-svelte';

  type Item = { href: string; label: string; icon: typeof LayoutDashboard };
  type Section = { label: string; items: Item[] };

  const sections: Section[] = [
    {
      label: 'Workspace',
      items: [
        { href: '/overview', label: 'Overview', icon: LayoutDashboard },
        { href: '/portfolio', label: 'Portfolio', icon: Briefcase },
        { href: '/markets', label: 'Markets', icon: LineChart },
        { href: '/research', label: 'Research', icon: FlaskConical },
        { href: '/theses', label: 'Theses', icon: Brain },
        { href: '/intel', label: 'Intel', icon: Satellite },
        { href: '/calls', label: 'Calls', icon: Target }
      ]
    },
    {
      label: 'Tools',
      items: [
        { href: '/watches', label: 'Watches', icon: Bell },
        { href: '/lookup', label: 'Lookup', icon: Search },
        { href: '/copilot', label: 'Copilot', icon: Sparkles }
      ]
    },
    {
      label: 'Operations',
      items: [{ href: '/system', label: 'System', icon: Cog }]
    }
  ];

  // page.url.pathname includes the base; strip it so /app/markets → /markets
  const current = $derived(
    page.url.pathname.replace(new RegExp(`^${base}`), '') || '/'
  );
</script>

<aside
  class="sticky top-0 hidden h-screen w-56 shrink-0 flex-col gap-0.5 border-r border-border bg-white/[0.014] px-3 py-4 md:flex"
>
  <a
    href={`${base}/overview`}
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
        class={[
          'group flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-[13px]',
          'transition-colors',
          active
            ? 'bg-white/[0.11] font-medium text-text'
            : 'text-muted hover:bg-white/[0.07] hover:text-text'
        ]
          .filter(Boolean)
          .join(' ')}
      >
        <item.icon class="h-3.5 w-3.5 shrink-0 opacity-80" />
        <span>{item.label}</span>
      </a>
    {/each}
  {/each}

  <div class="mt-auto px-2 pt-3 text-[10px] text-faint">
    Built side-by-side with NiceGUI · <a
      href="/"
      class="underline hover:text-muted"
      data-sveltekit-reload>v1</a
    >
  </div>
</aside>
