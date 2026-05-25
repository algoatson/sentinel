<script lang="ts">
  /**
   * Settings — user preferences that live in localStorage. Server-side
   * config (refresh intervals, channel ids, LLM models) is configured
   * in .env on the Pi; this page is for client-side display tweaks.
   */
  import { onMount } from 'svelte';
  import { browser } from '$app/environment';
  import Card from '$components/Card.svelte';
  import { toast } from '$lib/toast.svelte';
  import { Cog, RotateCcw, Bell, MessageCircle, ExternalLink } from 'lucide-svelte';

  type Prefs = {
    streamToasts: boolean;       // surface toasts for live events
    autoOpenDossier: boolean;    // open dossier when clicking a news/call card
    compactDensity: boolean;     // tighter spacing across the app
  };

  const DEFAULTS: Prefs = {
    streamToasts: true,
    autoOpenDossier: true,
    compactDensity: false
  };

  const KEY = 'sentinel.prefs.v1';
  let prefs: Prefs = $state({ ...DEFAULTS });

  onMount(() => {
    if (!browser) return;
    try {
      const raw = localStorage.getItem(KEY);
      if (raw) prefs = { ...DEFAULTS, ...(JSON.parse(raw) as Partial<Prefs>) };
    } catch (_) {
      /* corrupted → keep defaults */
    }
  });

  $effect(() => {
    if (!browser) return;
    try {
      localStorage.setItem(KEY, JSON.stringify(prefs));
    } catch (_) {
      /* quota → silently drop */
    }
  });

  function reset() {
    prefs = { ...DEFAULTS };
    toast.info('Settings reset to defaults');
  }

  function clearCopilot() {
    try {
      localStorage.removeItem('sentinel.copilot.turns.v1');
      toast.success('Cleared Copilot chat history');
    } catch (_) {
      toast.error('Could not clear');
    }
  }
</script>

<svelte:head><title>Settings · Sentinel</title></svelte:head>

<div class="mb-4 flex items-end justify-between border-b border-border pb-3">
  <div>
    <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
      <Cog class="h-5 w-5 text-muted" /><span>Settings</span>
    </h1>
    <div class="mt-0.5 text-[11.5px] text-faint">
      Browser-local preferences. Server config (intervals, channel ids,
      LLM models) lives in <code class="rounded bg-surface-2 px-1 text-[10px]">.env</code> on the Pi.
    </div>
  </div>

  <button
    type="button"
    onclick={reset}
    class="flex items-center gap-1.5 rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-[11.5px] text-muted hover:text-text"
  >
    <RotateCcw class="h-3.5 w-3.5" />
    Reset to defaults
  </button>
</div>

<div class="grid grid-cols-1 gap-3 lg:grid-cols-2">
  <Card class="px-4 py-3">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Notifications
    </div>

    <label class="mt-3 flex items-start gap-3 cursor-pointer">
      <input
        type="checkbox"
        bind:checked={prefs.streamToasts}
        class="mt-0.5 h-4 w-4 cursor-pointer accent-primary"
      />
      <div>
        <div class="text-[13px] text-text">
          <Bell class="mr-1 inline h-3.5 w-3.5 align-middle text-muted" />
          Toast on live events
        </div>
        <div class="text-[11px] text-faint">
          Show a transient toast (bottom-right) for new calls and watch
          trips. Disable if you find them noisy — the bell still
          counts.
        </div>
      </div>
    </label>

    <label class="mt-3 flex items-start gap-3 cursor-pointer">
      <input
        type="checkbox"
        bind:checked={prefs.autoOpenDossier}
        class="mt-0.5 h-4 w-4 cursor-pointer accent-primary"
      />
      <div>
        <div class="text-[13px] text-text">
          Auto-load dossier on open
        </div>
        <div class="text-[11px] text-faint">
          When opening a news or call drawer, fetch the cached
          dossier immediately. Off = wait for explicit ↻.
        </div>
      </div>
    </label>
  </Card>

  <Card class="px-4 py-3">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Density
    </div>

    <label class="mt-3 flex items-start gap-3 cursor-pointer">
      <input
        type="checkbox"
        bind:checked={prefs.compactDensity}
        class="mt-0.5 h-4 w-4 cursor-pointer accent-primary"
      />
      <div>
        <div class="text-[13px] text-text">
          Compact layout
        </div>
        <div class="text-[11px] text-faint">
          Tighter padding and smaller fonts across cards. Helps on
          smaller laptops; off on big monitors. (Future versions will
          gate more spacing on this.)
        </div>
      </div>
    </label>
  </Card>

  <Card class="px-4 py-3 lg:col-span-2">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Maintenance
    </div>
    <div class="mt-3 flex flex-wrap items-center gap-2">
      <button
        type="button"
        onclick={clearCopilot}
        class="flex items-center gap-1.5 rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-[11.5px] text-muted hover:text-text"
      >
        <MessageCircle class="h-3.5 w-3.5" />
        Clear Copilot chat history
      </button>
      <span class="text-[11px] text-faint">
        Chat is stored in browser localStorage, capped at 60 turns.
      </span>
    </div>
  </Card>

  <Card class="px-4 py-3 lg:col-span-2">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      About
    </div>
    <div class="mt-2 grid grid-cols-1 gap-2 text-[12.5px] sm:grid-cols-2">
      <div>
        <div class="text-faint">Frontend</div>
        <div>SvelteKit 2 · Svelte 5 · Tailwind v4 · TanStack Query</div>
      </div>
      <div>
        <div class="text-faint">Backend</div>
        <div>FastAPI · SQLModel · SQLite (WAL) · APScheduler</div>
      </div>
      <div>
        <div class="text-faint">Charting</div>
        <div>TradingView Lightweight Charts</div>
      </div>
      <div>
        <div class="text-faint">Repo</div>
        <div>
          <a
            href="https://github.com/algoatson/sentinel"
            target="_blank"
            rel="noopener"
            class="text-primary underline hover:text-primary/80"
          >
            github.com/algoatson/sentinel
            <ExternalLink class="ml-0.5 inline h-3 w-3 align-baseline opacity-70" />
          </a>
        </div>
      </div>
    </div>
  </Card>
</div>
