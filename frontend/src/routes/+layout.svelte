<script lang="ts">
  import '../app.css';
  import { QueryClient, QueryClientProvider } from '@tanstack/svelte-query';
  import { browser } from '$app/environment';
  import Sidebar from '$components/Sidebar.svelte';
  import TopBar from '$components/TopBar.svelte';

  let { children } = $props();

  // Single QueryClient per app instance. Cache defaults are tuned for a
  // dashboard: data is mostly server-side truth that the user wants
  // reasonably-fresh but never stale-blocking.
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,            // 15s "fresh enough"
        gcTime: 5 * 60_000,           // keep cached results 5min after unmount
        refetchOnWindowFocus: browser, // refresh when user returns to the tab
        retry: 1
      }
    }
  });
</script>

<QueryClientProvider client={queryClient}>
  <div class="flex min-h-screen">
    <Sidebar />
    <div class="min-w-0 flex-1">
      <TopBar />
      <main class="px-4 py-4 md:px-6 md:py-6">
        {@render children?.()}
      </main>
    </div>
  </div>
</QueryClientProvider>
