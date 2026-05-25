<script lang="ts">
  /**
   * Generic pager — compact controls + (optionally) a page-size
   * picker. Designed for client-side pagination of already-fetched
   * lists; if the backend ever needs server-side paging we'll add
   * an offset prop. For now everything fits in memory.
   *
   * Usage:
   *   <Pager bind:page bind:pageSize total={items.length} />
   *
   * Convention: `page` is 1-indexed.
   */
  interface Props {
    page: number;
    pageSize: number;
    total: number;
    sizes?: number[];
    /** Hide the page-size picker (e.g. when the parent fixes the size). */
    showSizes?: boolean;
    class?: string;
  }

  let {
    page = $bindable(),
    pageSize = $bindable(),
    total,
    sizes = [10, 25, 50, 100],
    showSizes = true,
    class: klass = ''
  }: Props = $props();

  const pageCount = $derived(Math.max(1, Math.ceil(total / pageSize)));
  const safePage = $derived(Math.min(Math.max(1, page), pageCount));
  $effect(() => {
    if (page !== safePage) page = safePage;
  });
  const start = $derived((safePage - 1) * pageSize + 1);
  const end = $derived(Math.min(total, safePage * pageSize));

  function go(p: number) {
    page = Math.min(Math.max(1, p), pageCount);
  }
</script>

{#if total > 0}
  <div class={['flex flex-wrap items-center gap-2 text-[11px] tabular text-faint', klass].filter(Boolean).join(' ')}>
    {#if showSizes}
      <span>Per page</span>
      <select
        bind:value={pageSize}
        onchange={() => (page = 1)}
        class="rounded-md border border-border bg-surface-2 px-2 py-0.5 text-[11px] text-text focus:border-primary/60 focus:outline-none"
      >
        {#each sizes as s (s)}<option value={s}>{s}</option>{/each}
      </select>
    {/if}

    <span>{start}–{end} of {total}</span>

    <div class="ml-auto flex items-center gap-1">
      <button
        type="button"
        onclick={() => go(1)}
        disabled={safePage === 1}
        class="rounded-md border border-border bg-surface-2 px-1.5 py-0.5 transition-colors hover:border-primary/40 hover:text-text disabled:opacity-40"
      >«</button>
      <button
        type="button"
        onclick={() => go(safePage - 1)}
        disabled={safePage === 1}
        class="rounded-md border border-border bg-surface-2 px-1.5 py-0.5 transition-colors hover:border-primary/40 hover:text-text disabled:opacity-40"
      >‹</button>
      <span class="tabular text-text">
        {safePage} / {pageCount}
      </span>
      <button
        type="button"
        onclick={() => go(safePage + 1)}
        disabled={safePage === pageCount}
        class="rounded-md border border-border bg-surface-2 px-1.5 py-0.5 transition-colors hover:border-primary/40 hover:text-text disabled:opacity-40"
      >›</button>
      <button
        type="button"
        onclick={() => go(pageCount)}
        disabled={safePage === pageCount}
        class="rounded-md border border-border bg-surface-2 px-1.5 py-0.5 transition-colors hover:border-primary/40 hover:text-text disabled:opacity-40"
      >»</button>
    </div>
  </div>
{/if}
