<script lang="ts">
  import { createMutation } from '@tanstack/svelte-query';
  import { lookup } from '$api';
  import Card from '$components/Card.svelte';
  import Markdown from '$components/Markdown.svelte';
  import Spinner from '$components/Spinner.svelte';
  import { Search, Ticket, Newspaper, FileText, Clock4, History, Calendar, Activity as ActivityIcon } from 'lucide-svelte';

  type Kind = {
    key: string;
    label: string;
    icon: typeof Search;
    needs_arg: boolean;
    hint: string;
  };

  const KINDS: Kind[] = [
    { key: 'ticker', label: 'Ticker', icon: Ticket, needs_arg: true, hint: 'Symbol, e.g. NVDA' },
    { key: 'news', label: 'News', icon: Newspaper, needs_arg: false, hint: 'Optional ticker filter' },
    { key: 'filing', label: 'Filing', icon: FileText, needs_arg: true, hint: 'Accession number' },
    { key: 'timeline', label: 'Timeline', icon: Clock4, needs_arg: true, hint: 'Symbol' },
    { key: 'recent', label: 'Recent', icon: History, needs_arg: false, hint: 'Optional count (default 12)' },
    { key: 'catalysts', label: 'Catalysts', icon: Calendar, needs_arg: false, hint: 'Optional window in days' },
    { key: 'status', label: 'Status', icon: ActivityIcon, needs_arg: false, hint: 'Pipeline / scheduler health' }
  ];

  let arg = $state('');
  let activeKind = $state<Kind | null>(null);

  const lookupM = createMutation({
    mutationFn: ({ kind, arg }: { kind: string; arg: string }) =>
      lookup(kind, arg),
    onSuccess: (res, vars) => {
      activeKind = KINDS.find((k) => k.key === vars.kind) ?? null;
    }
  });

  function run(k: Kind) {
    if (k.needs_arg && !arg.trim()) {
      activeKind = k;
      $lookupM.reset();
      return;
    }
    $lookupM.mutate({ kind: k.key, arg: arg.trim() });
  }
</script>

<svelte:head><title>Lookup · Sentinel</title></svelte:head>

<div class="mb-4 flex items-end justify-between border-b border-border pb-3">
  <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
    <Search class="h-5 w-5 text-primary" /><span>Lookup</span>
  </h1>
</div>

<Card class="px-4 py-3">
  <div class="flex flex-wrap items-center gap-2">
    <label class="text-[10px] font-semibold uppercase tracking-wider text-faint">
      Argument
    </label>
    <input
      type="text"
      bind:value={arg}
      placeholder={activeKind?.hint ?? 'Ticker / accession / count …'}
      onkeydown={(e) => {
        if (e.key === 'Enter' && activeKind) {
          e.preventDefault();
          run(activeKind);
        }
      }}
      class="flex-1 min-w-[16rem] rounded-md border border-border bg-surface-2 px-3 py-1.5 font-mono text-[13px] text-text placeholder:text-faint/60 focus:border-primary/60 focus:outline-none"
    />
  </div>

  <div class="mt-3 flex flex-wrap gap-2">
    {#each KINDS as k (k.key)}
      <button
        type="button"
        onclick={() => run(k)}
        disabled={$lookupM.isPending}
        class={[
          'flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-[12px] transition-colors',
          activeKind?.key === k.key
            ? 'border-primary/50 bg-primary-soft text-primary'
            : 'border-border bg-surface-2 text-muted hover:border-primary/40 hover:text-text',
          'disabled:opacity-40'
        ].join(' ')}
      >
        <k.icon class="h-3.5 w-3.5" />
        {k.label}
        {#if k.needs_arg}
          <span class="text-[9px] font-semibold uppercase tracking-wider text-faint">
            arg
          </span>
        {/if}
      </button>
    {/each}
  </div>
</Card>

<div class="mt-4">
  {#if $lookupM.isPending}
    <Card class="flex items-center gap-2 px-4 py-3 text-[12.5px] text-muted">
      <Spinner size={14} />
      Looking up {activeKind?.label.toLowerCase() ?? '…'}{arg ? ` "${arg}"` : ''}…
    </Card>
  {:else if $lookupM.isError}
    <Card class="px-4 py-3 text-[12.5px] text-bad">
      Lookup failed: {$lookupM.error instanceof Error ? $lookupM.error.message : 'unknown error'}
    </Card>
  {:else if $lookupM.data}
    {@const r = $lookupM.data}
    <Card class="px-4 py-3">
      <div class="mb-2 flex items-center gap-2">
        <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
          {r.kind}{r.arg ? ` · ${r.arg}` : ''}
        </div>
      </div>
      <Markdown source={r.body} />
    </Card>
  {:else}
    <Card class="px-6 py-12 text-center">
      <div class="text-sm font-medium text-muted">Pick a category</div>
      <div class="mt-2 max-w-md mx-auto text-[12px] text-faint">
        Categories marked <span class="font-mono text-faint">arg</span> need a
        ticker or identifier in the input above. Hit Enter inside the input to
        re-run the last category.
      </div>
    </Card>
  {/if}
</div>
