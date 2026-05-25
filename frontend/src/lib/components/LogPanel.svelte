<script lang="ts">
  import { tick } from 'svelte';
  import { createQuery } from '@tanstack/svelte-query';
  import { systemLogs } from '$api';
  import Spinner from './Spinner.svelte';
  import { Pause, Play, Trash2 } from 'lucide-svelte';

  interface Props {
    /** Default tail size. */
    n?: number;
    /** Poll interval (ms). */
    intervalMs?: number;
  }

  let { n = 220, intervalMs = 4_000 }: Props = $props();

  let paused = $state(false);
  let container: HTMLDivElement;

  const logsQ = createQuery(() => ({
    queryKey: ['system-logs', n],
    queryFn: () => systemLogs(n),
    refetchInterval: paused ? false : intervalMs
  }));

  // Re-pin to bottom whenever the data changes, but only if the user is
  // already near the bottom — so a scrolled-up reader doesn't get yanked.
  $effect(() => {
    $logsQ.data;
    if (!container) return;
    const nearBottom =
      container.scrollHeight -
        container.scrollTop -
        container.clientHeight <
      120;
    if (nearBottom) {
      tick().then(() => {
        if (container) container.scrollTop = container.scrollHeight;
      });
    }
  });

  function levelClass(line: string): string {
    // Format: HH:MM:SS | LEVEL   | module:line - message
    const m = line.match(/\|\s*(\w+)\s*\|/);
    const lvl = m?.[1]?.toUpperCase() ?? '';
    if (lvl === 'ERROR' || lvl === 'CRITICAL') return 'text-bad';
    if (lvl === 'WARNING') return 'text-warn';
    if (lvl === 'DEBUG' || lvl === 'TRACE') return 'text-faint';
    if (lvl === 'SUCCESS') return 'text-good';
    return 'text-muted';
  }
</script>

<div class="flex flex-col overflow-hidden rounded-xl border border-border bg-surface">
  <div class="flex items-center gap-3 border-b border-border px-3 py-2">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Live log
    </div>
    <span class="text-[10.5px] text-faint">
      {$logsQ.data?.lines.length ?? 0} lines
    </span>
    <span class="flex items-center gap-1.5 text-[10.5px] text-faint">
      <span class={[
        'inline-block h-1.5 w-1.5 rounded-full',
        paused ? 'bg-faint' : 'animate-pulse bg-good'
      ].join(' ')}></span>
      {paused ? 'paused' : `auto · every ${(intervalMs / 1000).toFixed(0)}s`}
    </span>
    <button
      type="button"
      onclick={() => (paused = !paused)}
      class="ml-auto flex items-center gap-1 rounded-md border border-border bg-surface-2 px-2 py-0.5 text-[10.5px] text-muted transition-colors hover:text-text"
    >
      {#if paused}
        <Play class="h-3 w-3" /> Resume
      {:else}
        <Pause class="h-3 w-3" /> Pause
      {/if}
    </button>
    <button
      type="button"
      onclick={() => {
        if (container) container.scrollTop = container.scrollHeight;
      }}
      class="rounded-md border border-border bg-surface-2 px-2 py-0.5 text-[10.5px] text-muted transition-colors hover:text-text"
    >↓ tail</button>
  </div>

  <div
    bind:this={container}
    class="h-[28rem] overflow-y-auto bg-bg/60 px-3 py-2 font-mono text-[10.5px] leading-[1.45] tabular"
  >
    {#if $logsQ.isLoading}
      <div class="flex h-full items-center justify-center text-muted">
        <Spinner size={14} />
      </div>
    {:else if !$logsQ.data?.lines.length}
      <div class="flex h-full items-center justify-center text-faint">
        log buffer empty — has the bot logged anything yet?
      </div>
    {:else}
      {#each $logsQ.data.lines as line, i (i)}
        <div class={['whitespace-pre-wrap break-all', levelClass(line)].join(' ')}>
          {line}
        </div>
      {/each}
    {/if}
  </div>
</div>
