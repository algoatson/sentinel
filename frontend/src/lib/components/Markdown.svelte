<script lang="ts">
  import { marked } from 'marked';

  // Configure once at module load. The LLM is prompted to return markdown
  // (no inline HTML), so we don't bother with DOMPurify — the worst case is
  // a stray tag rendering as text.
  marked.setOptions({
    breaks: true,        // single newline → <br>; matches what the LLMs produce
    gfm: true            // tables, fenced code, etc.
  });

  interface Props {
    source: string;
    /** Extra classes (e.g. "max-h-96 overflow-y-auto"). */
    class?: string;
  }

  let { source, class: klass = '' }: Props = $props();

  const html = $derived(marked.parse(source ?? '') as string);
</script>

<div class={['prose-bot', klass].filter(Boolean).join(' ')}>{@html html}</div>
