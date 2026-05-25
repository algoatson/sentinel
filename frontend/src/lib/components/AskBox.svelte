<script lang="ts">
  import Markdown from './Markdown.svelte';
  import Spinner from './Spinner.svelte';
  import { Send } from 'lucide-svelte';

  interface QA {
    question: string;
    answer: string;
  }

  interface Props {
    placeholder?: string;
    /** Caller passes an async function that submits the question and resolves
     * with the bot's answer (markdown). */
    onAsk: (q: string) => Promise<string>;
  }

  let {
    placeholder = 'Ask a follow-up…',
    onAsk
  }: Props = $props();

  let input = $state('');
  let pending = $state(false);
  let history = $state<QA[]>([]);
  let error = $state<string | null>(null);

  async function submit() {
    const q = input.trim();
    if (!q || pending) return;
    input = '';
    pending = true;
    error = null;
    try {
      const a = await onAsk(q);
      history = [...history, { question: q, answer: a }];
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
      input = q;
    } finally {
      pending = false;
    }
  }
</script>

<div>
  {#if history.length > 0}
    <div class="mb-2 text-[10px] font-semibold uppercase tracking-wider text-faint">
      Follow-up Q&A
    </div>
    <div class="mb-3 space-y-3">
      {#each history as qa, i (i)}
        <div class="rounded-lg border border-border bg-surface-2 px-3 py-2">
          <div class="text-[11px] font-semibold uppercase tracking-wider text-primary">
            You
          </div>
          <div class="mt-0.5 text-[12.5px] text-text">{qa.question}</div>
          <div class="mt-2.5 border-t border-border-soft pt-2">
            <div class="text-[11px] font-semibold uppercase tracking-wider text-violet">
              Sentinel
            </div>
            <Markdown source={qa.answer} class="mt-0.5" />
          </div>
        </div>
      {/each}
    </div>
  {/if}

  <form
    onsubmit={(e) => {
      e.preventDefault();
      submit();
    }}
    class="flex items-end gap-2"
  >
    <textarea
      bind:value={input}
      rows="2"
      {placeholder}
      class="flex-1 resize-none rounded-md border border-border bg-surface-2 px-3 py-2 text-[12.5px] text-text placeholder:text-faint/60 focus:border-primary/60 focus:outline-none"
      disabled={pending}
      onkeydown={(e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          submit();
        }
      }}
    ></textarea>
    <button
      type="submit"
      disabled={pending || !input.trim()}
      class="flex h-9 items-center gap-1 rounded-md border border-primary/40 bg-primary-soft px-3 text-[12px] font-medium text-primary transition-colors hover:bg-primary/15 disabled:opacity-40"
    >
      {#if pending}
        <Spinner size={12} />
      {:else}
        <Send class="h-3.5 w-3.5" />
      {/if}
      Ask
    </button>
  </form>
  {#if error}
    <div class="mt-1 text-[11px] text-bad">{error}</div>
  {/if}
</div>
