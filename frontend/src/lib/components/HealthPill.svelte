<script lang="ts">
  /**
   * At-a-glance bot health indicator for the Overview hero. Reads
   * /api/health (same data the /system page renders) and surfaces it
   * as a colour-coded dot + headline. Hover/tooltip lists the actual
   * critical + warning lines so the user doesn't need to jump to
   * /system to see what's wrong.
   *
   * Polled every 60s. Failure to fetch lands as "unknown" so the pill
   * doesn't disappear silently.
   */
  import { createQuery } from '@tanstack/svelte-query';
  import { health } from '$api';
  import { base } from '$app/paths';
  import { AlertTriangle, CheckCircle2, AlertOctagon, HelpCircle } from 'lucide-svelte';

  const q = createQuery({
    queryKey: ['health'],
    queryFn: health,
    refetchInterval: 60_000,
  });

  const verdict = $derived($q.data?.verdict ?? 'unknown');
  const headline = $derived($q.data?.headline ?? 'health unavailable');
  const issues = $derived([
    ...($q.data?.critical ?? []),
    ...($q.data?.warnings ?? []),
  ]);

  const TONE: Record<string, { bg: string; dot: string; Icon: typeof CheckCircle2 }> = {
    ok:      { bg: 'border-good/40 bg-good-soft text-good', dot: 'bg-good',  Icon: CheckCircle2 },
    warn:    { bg: 'border-warn/40 bg-warn-soft text-warn', dot: 'bg-warn',  Icon: AlertTriangle },
    crit:    { bg: 'border-bad/40 bg-bad-soft text-bad',    dot: 'bg-bad',   Icon: AlertOctagon },
    unknown: { bg: 'border-border bg-surface-2 text-muted', dot: 'bg-faint/50', Icon: HelpCircle },
  };
  const tone = $derived(TONE[verdict] ?? TONE.unknown);
</script>

<a
  href={`${base}/system`}
  class={[
    'inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] tabular transition-opacity hover:opacity-80',
    tone.bg,
  ].join(' ')}
  title={
    issues.length
      ? `${headline}\n\n${issues.join('\n')}\n\nClick to open /system`
      : `${headline} — click to open /system`
  }
>
  <span class={['inline-block h-1.5 w-1.5 rounded-full animate-pulse', tone.dot].join(' ')}></span>
  <tone.Icon class="h-3 w-3" />
  <span class="font-semibold uppercase tracking-wider text-[9.5px]">
    {verdict === 'ok' ? 'healthy' : verdict === 'warn' ? 'warning' : verdict === 'crit' ? 'critical' : '—'}
  </span>
  {#if issues.length}
    <span class="opacity-80">· {issues.length} issue{issues.length === 1 ? '' : 's'}</span>
  {/if}
</a>
