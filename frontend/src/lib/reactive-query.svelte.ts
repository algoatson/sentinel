/**
 * Bridge Svelte 5 runes → Svelte stores for @tanstack/svelte-query 5.x.
 *
 * The svelte-query 5 createQuery API accepts `StoreOrVal<options>` —
 * either a static options object or a Svelte `Readable<options>`. It
 * does NOT understand the `() => options` function form (that gets
 * wrapped via `readable()` whose value IS the function, so the inner
 * options never update when reactive state changes).
 *
 * This helper takes a runes-aware getter and returns a Readable that
 * tracks it. The internal $effect re-runs whenever any reactive
 * dependency inside the getter changes, pushing fresh options through
 * the store and triggering svelte-query to re-evaluate.
 *
 * Usage:
 *   const q = createQuery(reactiveQueryOptions(() => ({
 *     queryKey: ['thing', someId],
 *     queryFn: () => fetchThing(someId!),
 *     enabled: someId !== null
 *   })));
 *
 * Must be called during component init (top-level of `<script>`).
 */

import { writable, type Readable } from 'svelte/store';

export function reactiveQueryOptions<T>(getter: () => T): Readable<T> {
  const store = writable(getter());
  $effect(() => {
    store.set(getter());
  });
  return store;
}
