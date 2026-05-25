/**
 * Live event stream — EventSource to /api/events with auto-reconnect
 * + an in-memory notification ring backed by a Svelte 5 rune.
 *
 * Mounted once from +layout.svelte; every page sees the same feed
 * via the exported `liveEvents` rune. New events:
 *   - prepend to liveEvents.items (cap 80)
 *   - increment liveEvents.unread for the bell badge
 *   - call any registered cache-invalidation hooks (TanStack)
 *   - emit a toast for high-signal kinds
 *
 * The EventSource handles reconnection automatically. We pass
 * Last-Event-ID via the standard SSE protocol so reconnects replay
 * what was missed (capped to the server's 200-event ring).
 */

import { browser } from '$app/environment';
import type { LiveEvent } from './types';

const MAX_ITEMS = 80;

type InvalidateFn = (kind: string, ev: LiveEvent) => void;

class LiveEventBus {
  items = $state<LiveEvent[]>([]);
  unread = $state(0);
  connected = $state(false);
  /** Monotonic id of the latest event we've processed (for visual diffing). */
  lastSeenId = $state(0);

  private source: EventSource | null = null;
  private invalidators: InvalidateFn[] = [];

  start() {
    if (!browser || this.source) return;
    this.source = new EventSource('/api/events');

    this.source.onopen = () => {
      this.connected = true;
    };
    this.source.onerror = () => {
      this.connected = false;
      // Browser handles reconnect; just flip the badge while down.
    };

    const handle = (kind: string) => (e: MessageEvent) => {
      try {
        const ev = JSON.parse(e.data) as LiveEvent;
        ev.kind = kind;
        this.items = [ev, ...this.items].slice(0, MAX_ITEMS);
        this.unread += 1;
        if (ev.id > this.lastSeenId) this.lastSeenId = ev.id;
        for (const fn of this.invalidators) fn(kind, ev);
      } catch (_) {
        /* malformed; ignore */
      }
    };

    // Subscribe to the kinds we publish from the backend.
    for (const kind of ['news', 'call', 'filing', 'watch', 'trade']) {
      this.source.addEventListener(kind, handle(kind));
    }
  }

  stop() {
    if (this.source) {
      this.source.close();
      this.source = null;
    }
    this.connected = false;
  }

  markRead() {
    this.unread = 0;
  }

  /** Register a callback for cache invalidation (TanStack qc). */
  onEvent(fn: InvalidateFn): () => void {
    this.invalidators.push(fn);
    return () => {
      this.invalidators = this.invalidators.filter((f) => f !== fn);
    };
  }
}

export const liveEvents = new LiveEventBus();
