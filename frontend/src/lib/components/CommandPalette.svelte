<script lang="ts">
  /**
   * Cmd+K / Ctrl+K command palette. Global keybind comes from +layout;
   * this component renders the overlay + handles fuzzy search,
   * keyboard navigation, and dispatching the selection.
   *
   * Items are unified — pages and tickers compete on score, so typing
   * "mar" jumps to Markets and typing "nvd" jumps to /markets?ticker=NVDA.
   */
  import { onMount, tick } from 'svelte';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { createQuery } from '@tanstack/svelte-query';
  import { reactiveQueryOptions } from '$lib/reactive-query.svelte';
  import { watchlist } from '$api';
  import {
    LayoutDashboard, Briefcase, LineChart, FlaskConical, Brain,
    Satellite, Target, Bell, Search, Sparkles, Cog, ArrowRight
  } from 'lucide-svelte';

  interface Props {
    open: boolean;
    onClose: () => void;
  }
  let { open, onClose }: Props = $props();

  type Item = {
    kind: 'page' | 'ticker';
    label: string;
    sub?: string;
    href: string;
    icon: typeof Search;
  };

  const PAGES: Item[] = [
    { kind: 'page', label: 'Overview', href: '/overview', icon: LayoutDashboard },
    { kind: 'page', label: 'Portfolio', href: '/portfolio', icon: Briefcase },
    { kind: 'page', label: 'Markets', href: '/markets', icon: LineChart },
    { kind: 'page', label: 'Research', href: '/research', icon: FlaskConical },
    { kind: 'page', label: 'Theses', href: '/theses', icon: Brain },
    { kind: 'page', label: 'Intel', href: '/intel', icon: Satellite },
    { kind: 'page', label: 'Calls', href: '/calls', icon: Target },
    { kind: 'page', label: 'Watches', href: '/watches', icon: Bell },
    { kind: 'page', label: 'Lookup', href: '/lookup', icon: Search },
    { kind: 'page', label: 'Copilot', href: '/copilot', icon: Sparkles },
    { kind: 'page', label: 'System', href: '/system', icon: Cog },
    { kind: 'page', label: 'Settings', href: '/settings', icon: Cog }
  ];

  // Cache watchlist for tickers; only fetched when the palette opens.
  const wlQ = createQuery(reactiveQueryOptions(() => ({
    queryKey: ['watchlist'],
    queryFn: watchlist,
    enabled: open,
    staleTime: 5 * 60_000
  })));

  const tickerItems = $derived<Item[]>(
    ($wlQ.data ?? []).slice(0, 200).map((r) => ({
      kind: 'ticker' as const,
      label: r.ticker,
      sub: r.asset_class,
      href: `/markets?ticker=${encodeURIComponent(r.ticker)}`,
      icon: LineChart
    }))
  );

  let query = $state('');
  let cursor = $state(0);
  let input: HTMLInputElement;

  // Simple substring + start-of-word scoring. No fuse.js — saves a
  // dep, and for ~210 items the search is instant.
  function score(item: Item, q: string): number {
    if (!q) return item.kind === 'page' ? 5 : 1; // pages first when empty
    const lo = q.toLowerCase();
    const label = item.label.toLowerCase();
    if (label === lo) return 1000;
    if (label.startsWith(lo)) return 600;
    if (label.includes(lo)) return 300;
    if (item.sub?.toLowerCase().includes(lo)) return 100;
    return 0;
  }

  const items = $derived<Item[]>(
    [...PAGES, ...tickerItems]
      .map((item) => ({ item, s: score(item, query) }))
      .filter((x) => x.s > 0)
      .sort((a, b) => b.s - a.s)
      .slice(0, 30)
      .map((x) => x.item)
  );

  $effect(() => {
    items;
    cursor = 0;
  });

  $effect(() => {
    if (open) {
      query = '';
      cursor = 0;
      tick().then(() => input?.focus());
    }
  });

  function select(i: Item) {
    onClose();
    goto(`${base}${i.href}`);
  }

  function onKey(e: KeyboardEvent) {
    if (!open) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      cursor = Math.min(cursor + 1, items.length - 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      cursor = Math.max(cursor - 1, 0);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (items[cursor]) select(items[cursor]);
    }
  }
</script>

<svelte:window onkeydown={onKey} />

{#if open}
  <div class="fixed inset-0 z-[55] flex items-start justify-center px-4 pt-[12vh]">
    <button
      type="button"
      aria-label="Close palette"
      class="absolute inset-0 cursor-default bg-black/55 backdrop-blur-sm animate-[fadeIn_0.12s_ease-out]"
      onclick={onClose}
    ></button>
    <div
      class="relative w-full max-w-xl overflow-hidden rounded-xl border border-border bg-surface shadow-2xl animate-[popIn_0.16s_ease-out]"
      role="dialog"
      aria-modal="true"
    >
      <div class="flex items-center gap-2 border-b border-border px-3 py-2.5">
        <Search class="h-4 w-4 shrink-0 text-faint" />
        <input
          bind:this={input}
          bind:value={query}
          type="text"
          placeholder="Jump to page or ticker…"
          class="flex-1 bg-transparent text-[14px] text-text placeholder:text-faint/60 focus:outline-none"
        />
        <kbd class="rounded border border-border bg-surface-2 px-1.5 py-0.5 text-[10px] text-faint">esc</kbd>
      </div>

      <div class="max-h-[55vh] overflow-y-auto py-1">
        {#if !items.length}
          <div class="px-3 py-6 text-center text-[12px] text-faint">
            No matches.
          </div>
        {:else}
          {#each items as it, i (it.href)}
            <button
              type="button"
              onmouseenter={() => (cursor = i)}
              onclick={() => select(it)}
              class={[
                'flex w-full items-center gap-2.5 px-3 py-2 text-left text-[13px] transition-colors',
                cursor === i ? 'bg-white/[0.06] text-text' : 'text-muted'
              ].join(' ')}
            >
              <it.icon class={[
                'h-4 w-4 shrink-0',
                it.kind === 'ticker' ? 'text-primary' : 'text-faint'
              ].join(' ')} />
              <span class={it.kind === 'ticker' ? 'font-mono font-semibold' : ''}>
                {it.kind === 'ticker' ? '$' : ''}{it.label}
              </span>
              {#if it.sub}
                <span class="ml-1 text-[10.5px] uppercase tracking-wider text-faint">
                  {it.sub}
                </span>
              {/if}
              <span class="ml-auto text-[10px] uppercase tracking-wider text-faint">
                {it.kind}
              </span>
              {#if cursor === i}
                <ArrowRight class="h-3.5 w-3.5 text-primary" />
              {/if}
            </button>
          {/each}
        {/if}
      </div>

      <div class="flex items-center justify-between border-t border-border bg-surface-2/40 px-3 py-1.5 text-[10.5px] text-faint">
        <div class="flex items-center gap-2">
          <kbd class="rounded border border-border bg-surface-2 px-1 py-px">↑↓</kbd> nav
          <kbd class="rounded border border-border bg-surface-2 px-1 py-px">⏎</kbd> open
          <kbd class="rounded border border-border bg-surface-2 px-1 py-px">?</kbd> all shortcuts
        </div>
        <div>{items.length} match{items.length === 1 ? '' : 'es'}</div>
      </div>
    </div>
  </div>
{/if}

<style>
  @keyframes popIn {
    from {
      opacity: 0;
      transform: translateY(-8px) scale(0.985);
    }
    to {
      opacity: 1;
      transform: translateY(0) scale(1);
    }
  }
  @keyframes fadeIn {
    from {
      opacity: 0;
    }
    to {
      opacity: 1;
    }
  }
</style>
