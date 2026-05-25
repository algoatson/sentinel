<script lang="ts">
  import '../app.css';
  import { QueryClient, QueryClientProvider } from '@tanstack/svelte-query';
  import { browser } from '$app/environment';
  import { page } from '$app/state';
  import Sidebar from '$components/Sidebar.svelte';
  import TopBar from '$components/TopBar.svelte';
  import ToastHost from '$components/ToastHost.svelte';
  import CommandPalette from '$components/CommandPalette.svelte';

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

  let mobileNavOpen = $state(false);
  let paletteOpen = $state(false);

  // Close the mobile drawer on route change — a Svelte 5 effect on the
  // pathname keeps state in sync without needing afterNavigate hooks.
  $effect(() => {
    page.url.pathname;
    mobileNavOpen = false;
    paletteOpen = false;
  });

  function onGlobalKey(e: KeyboardEvent) {
    // Cmd+K (mac) / Ctrl+K (win/linux). Toggle so a second press
    // closes — matches the convention every editor uses.
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      paletteOpen = !paletteOpen;
      return;
    }
    // "/" focuses the palette unless the user is already typing
    // somewhere — avoids hijacking textareas + inputs.
    if (e.key === '/' && !paletteOpen) {
      const t = e.target as HTMLElement | null;
      const inField =
        !!t &&
        (t.tagName === 'INPUT' ||
          t.tagName === 'TEXTAREA' ||
          t.isContentEditable);
      if (!inField) {
        e.preventDefault();
        paletteOpen = true;
      }
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
  <ToastHost />
</QueryClientProvider>
