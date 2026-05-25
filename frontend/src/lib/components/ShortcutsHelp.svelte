<script lang="ts">
  /**
   * Keyboard shortcuts help overlay — surfaced via "?" or from a
   * "Keyboard shortcuts" footer link. Listing every binding here so
   * the user can rediscover them without grep'ing the source.
   */
  interface Props {
    open: boolean;
    onClose: () => void;
  }
  let { open, onClose }: Props = $props();

  type Row = { keys: string[]; label: string };
  type Group = { title: string; rows: Row[] };

  const groups: Group[] = [
    {
      title: 'Navigation',
      rows: [
        { keys: ['⌘', 'K'], label: 'Open command palette' },
        { keys: ['/'], label: 'Open palette (when not in a field)' },
        { keys: ['?'], label: 'Show this help' },
        { keys: ['Esc'], label: 'Close any modal / drawer' }
      ]
    },
    {
      title: 'Jump to page (press g, then…)',
      rows: [
        { keys: ['g', 'o'], label: 'Overview' },
        { keys: ['g', 'p'], label: 'Portfolio' },
        { keys: ['g', 'm'], label: 'Markets' },
        { keys: ['g', 'r'], label: 'Research' },
        { keys: ['g', 't'], label: 'Theses' },
        { keys: ['g', 'i'], label: 'Intel' },
        { keys: ['g', 'c'], label: 'Calls' },
        { keys: ['g', 'w'], label: 'Watches' },
        { keys: ['g', 'l'], label: 'Lookup' },
        { keys: ['g', 'a'], label: 'Copilot (Ask)' },
        { keys: ['g', 's'], label: 'System' },
        { keys: ['g', 'f'], label: 'Live feed' }
      ]
    },
    {
      title: 'In a drawer / dialog',
      rows: [
        { keys: ['Esc'], label: 'Close' }
      ]
    },
    {
      title: 'Lookup tab',
      rows: [
        { keys: ['Enter'], label: 'Re-run last category' }
      ]
    },
    {
      title: 'Copilot',
      rows: [
        { keys: ['Enter'], label: 'Send' },
        { keys: ['⇧', 'Enter'], label: 'Newline' }
      ]
    }
  ];
</script>

<svelte:window
  onkeydown={(e) => {
    if (e.key === 'Escape' && open) onClose();
  }}
/>

{#if open}
  <div class="fixed inset-0 z-[55] flex items-start justify-center px-4 pt-[10vh]">
    <button
      type="button"
      aria-label="Close shortcuts"
      class="absolute inset-0 cursor-default bg-black/55 backdrop-blur-sm animate-[fadeIn_0.12s_ease-out]"
      onclick={onClose}
    ></button>
    <div
      class="relative w-full max-w-xl overflow-hidden rounded-xl border border-border bg-surface shadow-2xl animate-[popIn_0.16s_ease-out]"
      role="dialog"
      aria-modal="true"
    >
      <div class="flex items-center justify-between border-b border-border px-4 py-2.5">
        <div class="flex items-center gap-2 text-sm font-semibold text-text">
          <span>⌨</span>
          <span>Keyboard shortcuts</span>
        </div>
        <button
          onclick={onClose}
          class="rounded p-1 text-faint transition-colors hover:bg-surface-2 hover:text-text"
        >✕</button>
      </div>
      <div class="max-h-[70vh] overflow-y-auto px-4 py-3">
        {#each groups as g (g.title)}
          <div class="mb-3">
            <div class="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-faint">
              {g.title}
            </div>
            <ul class="divide-soft">
              {#each g.rows as r (r.label)}
                <li class="flex items-center justify-between py-1.5 text-[12.5px]">
                  <span class="text-muted">{r.label}</span>
                  <span class="flex items-center gap-1">
                    {#each r.keys as k, i (i)}
                      {#if i > 0}<span class="text-faint">+</span>{/if}
                      <kbd
                        class="min-w-[1.4rem] rounded border border-border bg-surface-2 px-1.5 py-px text-center font-mono text-[10.5px] text-text"
                      >{k}</kbd>
                    {/each}
                  </span>
                </li>
              {/each}
            </ul>
          </div>
        {/each}
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
