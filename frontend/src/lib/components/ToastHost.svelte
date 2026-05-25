<script lang="ts">
  import { toast } from '$lib/toast.svelte';
  import { CheckCircle2, AlertCircle, AlertTriangle, Info, X } from 'lucide-svelte';

  const styleFor = {
    success: { cls: 'border-good/40 bg-good-soft text-good', icon: CheckCircle2 },
    error: { cls: 'border-bad/40 bg-bad-soft text-bad', icon: AlertCircle },
    warn: { cls: 'border-warn/40 bg-warn-soft text-warn', icon: AlertTriangle },
    info: { cls: 'border-primary/40 bg-primary-soft text-primary', icon: Info }
  };
</script>

<div class="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-full max-w-sm flex-col gap-2">
  {#each toast.items as t (t.id)}
    {@const s = styleFor[t.kind]}
    <div
      class={[
        'pointer-events-auto flex items-start gap-2 rounded-lg border px-3 py-2 shadow-lg backdrop-blur',
        'animate-[toastIn_0.18s_ease-out]',
        s.cls
      ].join(' ')}
      role="status"
    >
      <s.icon class="mt-0.5 h-4 w-4 shrink-0" />
      <div class="min-w-0 flex-1 text-[12.5px] leading-snug">{t.message}</div>
      <button
        type="button"
        aria-label="Dismiss"
        onclick={() => toast.dismiss(t.id)}
        class="-mr-1 shrink-0 rounded p-0.5 opacity-60 transition-opacity hover:opacity-100"
      ><X class="h-3 w-3" /></button>
    </div>
  {/each}
</div>

<style>
  @keyframes toastIn {
    from {
      opacity: 0;
      transform: translateY(8px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
</style>
