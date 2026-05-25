<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import '../app.css';
  import { QueryClient, QueryClientProvider } from '@tanstack/svelte-query';
  import { browser } from '$app/environment';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { liveEvents } from '$lib/events.svelte';
  import { toast } from '$lib/toast.svelte';
  import Sidebar from '$components/Sidebar.svelte';
  import TopBar from '$components/TopBar.svelte';
  import ToastHost from '$components/ToastHost.svelte';
  import CommandPalette from '$components/CommandPalette.svelte';
  import ShortcutsHelp from '$components/ShortcutsHelp.svelte';

  let { children } = $props();

  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: browser,
        retry: 1
      }
    }
  });

  /* ─── live event stream ───────────────────────────────────
   * Single EventSource for the whole app. Each event:
   *  - invalidates the matching TanStack cache keys
   *  - emits a toast for high-signal kinds (calls, watches)
   *  - feeds the notification bell
   */
  onMount(() => {
    if (!browser) return;
    liveEvents.start();
    const off = liveEvents.onEvent((kind, ev) => {
      const p = ev.payload || {};
      if (kind === 'news') {
        queryClient.invalidateQueries({ queryKey: ['news'] });
        queryClient.invalidateQueries({ queryKey: ['activity'] });
      } else if (kind === 'filing') {
        queryClient.invalidateQueries({ queryKey: ['filings'] });
        queryClient.invalidateQueries({ queryKey: ['activity'] });
      } else if (kind === 'call') {
        queryClient.invalidateQueries({ queryKey: ['calls'] });
        queryClient.invalidateQueries({ queryKey: ['scorecard'] });
        queryClient.invalidateQueries({ queryKey: ['activity'] });
        queryClient.invalidateQueries({ queryKey: ['kpi'] });
        const dir = (p.direction || '').toUpperCase();
        const ticker = p.ticker ?? '';
        toast.info(`📣 New ${dir} call · $${ticker} (conv ${p.conviction}/5)`);
      } else if (kind === 'watch') {
        toast.warn(`🔔 Watch #${p.id} tripped — ${p.raw_text || ''}`);
        queryClient.invalidateQueries({ queryKey: ['watches'] });
      } else if (kind === 'trade') {
        queryClient.invalidateQueries({ queryKey: ['wallets'] });
        queryClient.invalidateQueries({ queryKey: ['wallet-history'] });
        queryClient.invalidateQueries({ queryKey: ['kpi'] });
      }
    });
    return off;
  });
  onDestroy(() => {
    if (browser) liveEvents.stop();
  });

  let mobileNavOpen = $state(false);
  let paletteOpen = $state(false);
  let helpOpen = $state(false);

  $effect(() => {
    page.url.pathname;
    mobileNavOpen = false;
    paletteOpen = false;
    helpOpen = false;
  });

  // Vim-style `g + letter` jump table. `gPending` flips on for 1.2s
  // after the user presses g; if a registered letter follows we
  // navigate, otherwise we silently drop the prefix.
  const GO_MAP: Record<string, string> = {
    o: '/overview',
    p: '/portfolio',
    b: '/book',
    m: '/markets',
    r: '/research',
    t: '/theses',
    i: '/intel',
    c: '/calls',
    w: '/watches',
    l: '/lookup',
    a: '/copilot', // a for "ask"
    s: '/system',
    f: '/feed'
  };

  let gPending = $state(false);
  let gTimer: ReturnType<typeof setTimeout> | null = null;
  function startGPending() {
    gPending = true;
    if (gTimer) clearTimeout(gTimer);
    gTimer = setTimeout(() => (gPending = false), 1200);
  }

  function inField(e: KeyboardEvent): boolean {
    const t = e.target as HTMLElement | null;
    return (
      !!t &&
      (t.tagName === 'INPUT' ||
        t.tagName === 'TEXTAREA' ||
        t.isContentEditable)
    );
  }

  function onGlobalKey(e: KeyboardEvent) {
    // Cmd/Ctrl + K — palette toggle (works inside fields too, by design)
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      paletteOpen = !paletteOpen;
      return;
    }

    // The rest are bare-key shortcuts — never hijack when typing.
    if (inField(e)) return;

    if (e.key === '/' && !paletteOpen) {
      e.preventDefault();
      paletteOpen = true;
      return;
    }
    if (e.key === '?' && !helpOpen) {
      e.preventDefault();
      helpOpen = true;
      return;
    }

    // g-prefix sequence: g, then a registered letter.
    if (gPending) {
      const k = e.key.toLowerCase();
      const target = GO_MAP[k];
      if (target) {
        e.preventDefault();
        gPending = false;
        if (gTimer) clearTimeout(gTimer);
        goto(`${base}${target}`);
      } else if (e.key !== 'g') {
        // Any other key cancels.
        gPending = false;
      }
      return;
    }
    if (e.key === 'g' && !e.metaKey && !e.ctrlKey && !e.altKey) {
      e.preventDefault();
      startGPending();
    }
  }
</script>

<svelte:window onkeydown={onGlobalKey} />

<QueryClientProvider client={queryClient}>
  <div class="flex min-h-screen">
    <Sidebar mobileOpen={mobileNavOpen} onClose={() => (mobileNavOpen = false)} />
    <div class="min-w-0 flex-1">
      <TopBar
        onOpenMobileNav={() => (mobileNavOpen = true)}
        onOpenPalette={() => (paletteOpen = true)}
      />
      <main class="px-4 py-4 md:px-6 md:py-6">
        {@render children?.()}
      </main>
    </div>
  </div>

  <CommandPalette open={paletteOpen} onClose={() => (paletteOpen = false)} />
  <ShortcutsHelp open={helpOpen} onClose={() => (helpOpen = false)} />
  <ToastHost />

  <!-- g-prefix indicator — small flicker so the user knows g registered -->
  {#if gPending}
    <div
      class="pointer-events-none fixed bottom-4 left-4 z-[60] flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary-soft px-2.5 py-1 text-[11px] font-medium text-primary shadow-lg"
    >
      <kbd class="rounded border border-primary/40 bg-bg px-1 py-px font-mono text-[10px]">g</kbd>
      <span>then…</span>
    </div>
  {/if}
</QueryClientProvider>
