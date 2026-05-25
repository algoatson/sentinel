<script lang="ts">
  import { base } from '$app/paths';

  /**
   * Clickable ticker pill that deep-links to /markets?ticker=…
   * Drop-in replacement for the static `${ticker}` spans that were
   * sprinkled across pages — same look, now navigable.
   *
   * Stops click propagation so it doesn't trigger a parent card's
   * onclick (e.g. opening a drawer behind it).
   */
  interface Props {
    ticker: string | null | undefined;
    /** Extra classes (e.g. text size variants). */
    class?: string;
    /** Show the leading $. Defaults to true. */
    dollar?: boolean;
  }

  let { ticker, class: klass = '', dollar = true }: Props = $props();

  const safe = $derived((ticker ?? '').toUpperCase().replace(/^\$/, ''));
</script>

{#if safe}
  <a
    href={`${base}/symbol/${encodeURIComponent(safe)}`}
    onclick={(e) => e.stopPropagation()}
    class={[
      'font-mono font-semibold text-primary transition-colors hover:text-primary/80 hover:underline',
      klass
    ]
      .filter(Boolean)
      .join(' ')}
    title={`Symbol ${safe} — chart, news, calls, theses, reddit`}
  >{dollar ? '$' : ''}{safe}</a>
{/if}
