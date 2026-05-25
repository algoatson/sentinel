<script lang="ts">
  import Markdown from './Markdown.svelte';
  import Spinner from './Spinner.svelte';
  import Pill from './Pill.svelte';
  import { timeAgo } from '../format';

  interface Props {
    body: string | undefined;
    meta: { created_at: string; model: string } | null | undefined;
    isLoading?: boolean;
    onRefresh?: () => void;
    refreshing?: boolean;
  }

  let {
    body,
    meta,
    isLoading = false,
    onRefresh,
    refreshing = false
  }: Props = $props();
</script>

<div>
  <div class="mb-2 flex items-center gap-2">
    <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Dossier
    </div>
    {#if meta}
      <Pill variant="pos">
        ✓ cached · {meta.created_at.slice(0, 16).replace('T', ' ')}Z · {meta.model}
      </Pill>
    {:else if !isLoading}
      <Pill variant="info">fresh</Pill>
    {/if}
    {#if onRefresh}
      <button
        type="button"
        onclick={onRefresh}
        disabled={refreshing}
        class="ml-auto rounded-md border border-border bg-surface-2 px-2 py-0.5 text-[10.5px] text-muted transition-colors hover:border-primary/40 hover:text-text disabled:opacity-50"
      >{refreshing ? 'refreshing…' : '↻ regenerate'}</button>
    {/if}
  </div>

  {#if isLoading}
    <div class="flex items-center gap-2 py-6 text-[12px] text-muted">
      <Spinner size={14} />
      <span>Composing analysis (light LLM)…</span>
    </div>
  {:else if body}
    <div class="rounded-lg border border-border bg-surface-2 px-4 py-3">
      <Markdown source={body} />
    </div>
  {:else}
    <div class="text-[12px] text-faint">No dossier available.</div>
  {/if}
</div>
