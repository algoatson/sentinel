<script lang="ts">
  import '../app.css';
  import { QueryClient, QueryClientProvider } from '@tanstack/svelte-query';
  import { browser } from '$app/environment';
  import { page } from '$app/state';
  import Sidebar from '$components/Sidebar.svelte';
  import TopBar from '$components/TopBar.svelte';
  import ToastHost from '$components/ToastHost.svelte';

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

  // Close the mobile drawer on route change — a Svelte 5 effect on the
  // pathname keeps state in sync without needing afterNavigate hooks.
  $effect(() => {
    page.url.pathname;
    mobileNavOpen = false;
  });
</script>

<QueryClientProvider client={queryClient}>
  <div class="flex min-h-screen">
    <Sidebar mobileOpen={mobileNavOpen} onClose={() => (mobileNavOpen = false)} />
    <div class="min-w-0 flex-1">
      <TopBar onOpenMobileNav={() => (mobileNavOpen = true)} />
      <main class="px-4 py-4 md:px-6 md:py-6">
        {@render children?.()}
      </main>
    </div>
  </div>
  <ToastHost />
</QueryClientProvider>
