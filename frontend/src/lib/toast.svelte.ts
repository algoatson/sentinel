/**
 * App-wide toast notifications via a Svelte 5 rune.
 *
 * Usage from any component:
 *   import { toast } from '$lib/toast.svelte';
 *   toast.success('Watch added');
 *   toast.error('Compile failed: ...');
 *
 * The <ToastHost /> in +layout.svelte subscribes to `toasts` and
 * renders the queue in the bottom-right.
 */

type ToastKind = 'success' | 'error' | 'warn' | 'info';

export interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
  /** ms to auto-dismiss; 0 = manual */
  ttl: number;
}

let nextId = 1;

class ToastBus {
  items = $state<ToastItem[]>([]);

  push(kind: ToastKind, message: string, ttl = 4500) {
    const id = nextId++;
    this.items = [...this.items, { id, kind, message, ttl }];
    if (ttl > 0) {
      setTimeout(() => this.dismiss(id), ttl);
    }
    return id;
  }
  success(msg: string, ttl?: number) {
    return this.push('success', msg, ttl);
  }
  error(msg: string, ttl?: number) {
    return this.push('error', msg, ttl ?? 6500);
  }
  warn(msg: string, ttl?: number) {
    return this.push('warn', msg, ttl);
  }
  info(msg: string, ttl?: number) {
    return this.push('info', msg, ttl);
  }
  dismiss(id: number) {
    this.items = this.items.filter((t) => t.id !== id);
  }
}

export const toast = new ToastBus();
