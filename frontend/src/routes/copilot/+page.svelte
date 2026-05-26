<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { browser } from '$app/environment';
  import { askCopilot, type CopilotToolCall } from '$api';
  import Markdown from '$components/Markdown.svelte';
  import Spinner from '$components/Spinner.svelte';
  import { Sparkles, Send, User, Wrench } from 'lucide-svelte';

  interface Turn {
    role: 'user' | 'bot';
    content: string;
    error?: boolean;
    tool_calls?: CopilotToolCall[];
  }

  const EXAMPLES = [
    "What's the read on $NVDA into earnings?",
    "Which wallet is leading and why?",
    "Anything notable in filings today?",
    "How calibrated are my high-conviction calls?",
    "Summarize the active theses in one paragraph.",
    "What's the biggest risk in the book right now?"
  ];

  // localStorage persistence — chat survives navigation + reload. The
  // bot's context is server-side (calls live DB queries), so it's fine
  // for the user to scroll back through last week's questions.
  const STORAGE_KEY = 'sentinel.copilot.turns.v1';
  const STORAGE_LIMIT = 60; // cap stored turns so storage never balloons

  let input = $state('');
  let deep = $state(true);
  let turns = $state<Turn[]>([]);
  let pending = $state(false);
  let feed: HTMLDivElement;

  onMount(() => {
    if (!browser) return;
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) turns = JSON.parse(raw) as Turn[];
    } catch (_) {
      /* corrupted storage → start fresh */
    }
    scrollToBottom();
  });

  $effect(() => {
    if (!browser) return;
    try {
      // Persist the most-recent slice to keep storage bounded.
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify(turns.slice(-STORAGE_LIMIT))
      );
    } catch (_) {
      /* quota / privacy mode → silently drop */
    }
  });

  async function scrollToBottom() {
    await tick();
    if (feed) feed.scrollTop = feed.scrollHeight;
  }

  async function submit() {
    const q = input.trim();
    if (!q || pending) return;
    input = '';
    pending = true;
    turns = [...turns, { role: 'user', content: q }];
    await scrollToBottom();
    try {
      const r = await askCopilot(q, { deep });
      turns = [
        ...turns,
        { role: 'bot', content: r.answer, tool_calls: r.tool_calls }
      ];
    } catch (e) {
      turns = [
        ...turns,
        {
          role: 'bot',
          content: e instanceof Error ? e.message : String(e),
          error: true
        }
      ];
    } finally {
      pending = false;
      await scrollToBottom();
    }
  }

  function pick(q: string) {
    input = q;
  }

  function clearChat() {
    turns = [];
  }
</script>

<svelte:head><title>Copilot · Sentinel</title></svelte:head>

<div class="mb-4 flex items-end justify-between border-b border-border pb-3">
  <div>
    <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
      <Sparkles class="h-5 w-5 text-violet" /><span>Copilot</span>
    </h1>
    <div class="mt-0.5 text-[11.5px] text-faint">
      Same context as Discord <code class="rounded bg-surface-2 px-1 text-[10px]">!ask</code> /
      @-mention — the bot sees the live book, wallets, recent filings &amp; theses.
    </div>
  </div>
  {#if turns.length > 0}
    <button
      onclick={clearChat}
      class="rounded-md border border-border bg-surface-2 px-2.5 py-1 text-[11px] text-muted hover:text-text"
    >Clear</button>
  {/if}
</div>

<div class="flex h-[calc(100vh-12rem)] flex-col rounded-xl border border-border bg-surface">
  <div bind:this={feed} class="flex-1 overflow-y-auto px-4 py-4">
    {#if turns.length === 0}
      <div class="flex h-full flex-col items-center justify-center gap-3 text-center">
        <Sparkles class="h-7 w-7 text-violet" />
        <div class="max-w-md text-[13px] text-muted">
          Ask the copilot anything about the book, wallets, filings, sentiment,
          or a ticker. Same context and voice as Discord — just here.
        </div>
        <div class="mt-2 flex max-w-xl flex-wrap justify-center gap-1.5">
          {#each EXAMPLES as ex (ex)}
            <button
              type="button"
              onclick={() => pick(ex)}
              class="rounded-md border border-border bg-surface-2 px-2.5 py-1 text-[11.5px] text-muted transition-colors hover:border-violet/40 hover:text-text"
            >{ex}</button>
          {/each}
        </div>
      </div>
    {:else}
      <div class="mx-auto max-w-3xl space-y-4">
        {#each turns as t, i (i)}
          {#if t.role === 'user'}
            <div class="flex items-start gap-2.5">
              <div class="grid h-7 w-7 shrink-0 place-items-center rounded-full border border-border bg-surface-2 text-primary">
                <User class="h-3.5 w-3.5" />
              </div>
              <div class="flex-1 rounded-lg border border-border bg-surface-2 px-3 py-2 text-[13px] text-text">
                {t.content}
              </div>
            </div>
          {:else}
            <div class="flex items-start gap-2.5">
              <div class="grid h-7 w-7 shrink-0 place-items-center rounded-full border border-violet/40 bg-violet-soft text-violet">
                <Sparkles class="h-3.5 w-3.5" />
              </div>
              <div class={[
                'flex-1 rounded-lg border px-3 py-2',
                t.error ? 'border-bad/40 bg-bad-soft' : 'border-border bg-surface-2'
              ].join(' ')}>
                {#if t.error}
                  <div class="text-[12px] text-bad">{t.content}</div>
                {:else}
                  <Markdown source={t.content} />
                  {#if t.tool_calls && t.tool_calls.length}
                    <div class="mt-2 flex flex-wrap items-center gap-1 border-t border-border-soft pt-1.5 text-[10px] text-faint">
                      <Wrench class="h-2.5 w-2.5" />
                      <span class="uppercase tracking-wider">tools used</span>
                      {#each t.tool_calls as c (`${c.iteration}-${c.name}`)}
                        {@const args = Object.entries(c.arguments ?? {})
                          .filter(([k]) => k !== 'limit')
                          .map(([_, v]) => String(v))
                          .join(', ')}
                        <span
                          class="rounded border border-border bg-surface px-1 py-0.5 font-mono text-muted"
                          title={JSON.stringify(c.arguments)}
                        >{c.name}{args ? `(${args})` : ''}</span>
                      {/each}
                    </div>
                  {/if}
                {/if}
              </div>
            </div>
          {/if}
        {/each}
        {#if pending}
          <div class="flex items-start gap-2.5">
            <div class="grid h-7 w-7 shrink-0 place-items-center rounded-full border border-violet/40 bg-violet-soft text-violet">
              <Sparkles class="h-3.5 w-3.5" />
            </div>
            <div class="flex items-center gap-2 rounded-lg border border-border bg-surface-2 px-3 py-2 text-[12.5px] text-muted">
              <Spinner size={12} />
              Thinking…
            </div>
          </div>
        {/if}
      </div>
    {/if}
  </div>

  <form
    onsubmit={(e) => {
      e.preventDefault();
      submit();
    }}
    class="border-t border-border bg-surface-2/30 p-3"
  >
    <div class="mx-auto flex max-w-3xl items-end gap-2">
      <label
        class="flex flex-none cursor-pointer items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1.5 text-[11px] transition-colors hover:border-violet/40"
        title="Deep mode lets the LLM call market tools (chart, ATR, peer movers, news, filings, correlation). Off = fast one-shot answer."
      >
        <input
          type="checkbox"
          bind:checked={deep}
          class="h-3 w-3 cursor-pointer accent-violet"
        />
        <span class={deep ? 'text-violet' : 'text-muted'}>Deep</span>
      </label>
      <textarea
        bind:value={input}
        rows="2"
        placeholder="Ask the copilot — Enter to send, Shift+Enter for newline"
        disabled={pending}
        onkeydown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
        class="flex-1 resize-none rounded-md border border-border bg-surface px-3 py-2 text-[13px] text-text placeholder:text-faint/60 focus:border-violet/60 focus:outline-none"
      ></textarea>
      <button
        type="submit"
        disabled={pending || !input.trim()}
        class="flex h-9 items-center gap-1.5 rounded-md border border-violet/40 bg-violet-soft px-3 text-[12px] font-medium text-violet transition-colors hover:bg-violet/15 disabled:opacity-40"
      >
        {#if pending}
          <Spinner size={12} />
        {:else}
          <Send class="h-3.5 w-3.5" />
        {/if}
        Send
      </button>
    </div>
  </form>
</div>
