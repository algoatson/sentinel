<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { health, systemMetrics } from '$api';
  import Card from '$components/Card.svelte';
  import Pill from '$components/Pill.svelte';
  import StatTile from '$components/StatTile.svelte';
  import Spinner from '$components/Spinner.svelte';
  import LogPanel from '$components/LogPanel.svelte';
  import ToolCallsPanel from '$components/ToolCallsPanel.svelte';
  import PromptEditor from '$components/PromptEditor.svelte';
  import { compact } from '$lib/format';
  import { Cog, AlertTriangle, AlertOctagon, CheckCircle2 } from 'lucide-svelte';

  const healthQ = createQuery({
    queryKey: ['health'],
    queryFn: health,
    refetchInterval: 30_000
  });
  const sysQ = createQuery({
    queryKey: ['system-metrics'],
    queryFn: systemMetrics,
    refetchInterval: 15_000
  });

  function fmtUptime(s: number | null | undefined): string {
    if (!s || s < 0) return '—';
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d) return `${d}d ${h}h ${m}m`;
    if (h) return `${h}h ${m}m`;
    return `${m}m`;
  }

  function verdictColour(v: string | undefined): 'pos' | 'neg' | 'warn' | 'neutral' {
    if (v === 'ok') return 'pos';
    if (v === 'crit') return 'neg';
    if (v === 'warn') return 'warn';
    return 'neutral';
  }
</script>

<svelte:head><title>System · Sentinel</title></svelte:head>

<div class="mb-4 flex items-end justify-between border-b border-border pb-3">
  <h1 class="flex items-center gap-2 text-lg font-semibold tracking-tight">
    <Cog class="h-5 w-5 text-muted" /><span>System</span>
  </h1>
</div>

{#if $healthQ.data}
  {@const h = $healthQ.data}
  <Card class={[
    'border-l-4 px-4 py-3',
    h.verdict === 'ok'
      ? 'border-l-good'
      : h.verdict === 'crit'
        ? 'border-l-bad'
        : h.verdict === 'warn'
          ? 'border-l-warn'
          : 'border-l-border-strong'
  ].join(' ')}>
    <div class="flex items-center gap-3">
      <div class="text-[28px] leading-none">{h.marker}</div>
      <div class="min-w-0 flex-1">
        <div class="flex items-baseline gap-2">
          <span class={[
            'text-base font-semibold',
            h.verdict === 'ok' ? 'text-good' :
            h.verdict === 'crit' ? 'text-bad' :
            h.verdict === 'warn' ? 'text-warn' : 'text-muted'
          ].join(' ')}>
            {h.headline}
          </span>
          <Pill variant={verdictColour(h.verdict)}>{h.verdict.toUpperCase()}</Pill>
        </div>
        <div class="mt-1 text-[11px] text-faint">
          {h.jobs_runs}/{h.jobs_n} jobs ran cleanly in 24h
          {#if h.jobs_fail > 0} · <span class="text-bad">{h.jobs_fail} failures</span>{/if}
          · {h.watchlist} watchlist symbols · {h.open_calls} open calls
        </div>
      </div>
    </div>

    {#if h.critical.length || h.warnings.length}
      <div class="mt-3 space-y-1.5 border-t border-border-soft pt-3">
        {#each h.critical as msg (msg)}
          <div class="flex items-start gap-2 text-[12px] text-bad">
            <AlertOctagon class="mt-0.5 h-4 w-4 shrink-0" />
            <span>{msg}</span>
          </div>
        {/each}
        {#each h.warnings as msg (msg)}
          <div class="flex items-start gap-2 text-[12px] text-warn">
            <AlertTriangle class="mt-0.5 h-4 w-4 shrink-0" />
            <span>{msg}</span>
          </div>
        {/each}
      </div>
    {:else}
      <div class="mt-2 flex items-center gap-2 text-[11.5px] text-good">
        <CheckCircle2 class="h-4 w-4" />
        No active alerts.
      </div>
    {/if}
  </Card>
{:else if $healthQ.isLoading}
  <div class="flex justify-center py-12"><Spinner /></div>
{/if}

<!-- ── process gauges ──────────────────────────────────────── -->
{#if $sysQ.data}
  {@const s = $sysQ.data}
  <div class="mt-4 grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
    <StatTile label="Uptime" value={fmtUptime(s.uptime_s)} sub="since restart" />
    <StatTile
      label="CPU"
      value={s.cpu_pct !== null ? `${s.cpu_pct.toFixed(1)}%` : '—'}
      accent={s.cpu_pct !== null && s.cpu_pct > 60 ? 'warn' : 'none'}
      sub="this process"
    />
    <StatTile
      label="RSS"
      value={s.rss_mb !== null ? `${s.rss_mb.toFixed(0)} MB` : '—'}
      accent={s.rss_mb !== null && s.rss_mb > 600 ? 'warn' : 'none'}
      sub="resident"
    />
    <StatTile label="Threads" value={s.threads !== null ? String(s.threads) : '—'} sub="active" />
    <StatTile label="FDs" value={s.fds !== null ? String(s.fds) : '—'} sub="open file descriptors" />
    <StatTile label="DB size" value={s.db_human} sub="incl. WAL+SHM" />
  </div>
{/if}

<!-- ── jobs grid ───────────────────────────────────────── -->
{#if $healthQ.data}
  {@const jobs = $healthQ.data.jobs ?? []}
  <Card class="mt-4 overflow-hidden">
    <div class="flex items-baseline gap-3 border-b border-border px-4 py-2.5">
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Scheduler jobs (24h)
      </div>
      <div class="text-[11px] text-faint">{jobs.length} jobs</div>
    </div>
    {#if !jobs.length}
      <div class="px-4 py-6 text-center text-[12px] text-faint">
        No job runs in the last 24h.
      </div>
    {:else}
      <div class="grid grid-cols-1 gap-x-4 px-4 py-2 md:grid-cols-2">
        {#each jobs as j (j.id)}
          <div class="flex items-center gap-2 border-b border-border-soft py-1.5 text-[12px] tabular last:border-b-0">
            <span class={[
              'inline-block h-1.5 w-1.5 shrink-0 rounded-full',
              j.fail === 0 ? 'bg-good' : j.fail < j.runs / 2 ? 'bg-warn' : 'bg-bad'
            ].join(' ')}></span>
            <span class="flex-1 truncate font-mono text-muted" title={j.id}>{j.id}</span>
            <span class={['tabular text-[11.5px]', j.fail > 0 ? 'text-bad' : 'text-faint'].join(' ')}>
              {j.runs - j.fail}/{j.runs}
            </span>
          </div>
        {/each}
      </div>
    {/if}
  </Card>

  <!-- ── streams + LLM ────────────────────────────── -->
  <div class="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2">
    <Card class="px-4 py-3">
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        Ingestion streams (24h)
      </div>
      {#if !Object.keys($healthQ.data.streams).length}
        <div class="mt-2 text-[11.5px] text-faint">No streams reporting.</div>
      {:else}
        <div class="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-[12px] tabular md:grid-cols-3">
          {#each Object.entries($healthQ.data.streams) as [stream, n] (stream)}
            <div class="flex items-center justify-between">
              <span class="truncate text-muted" title={stream}>{stream}</span>
              <span class={n === 0 ? 'text-bad' : n < 5 ? 'text-warn' : 'text-good'}>
                {n}
              </span>
            </div>
          {/each}
        </div>
      {/if}
    </Card>

    <Card class="px-4 py-3">
      <div class="text-[10px] font-semibold uppercase tracking-wider text-faint">
        LLM
      </div>
      {@const llm = $healthQ.data.llm}
      <div class="mt-2 grid grid-cols-3 gap-2 text-center text-[12px] tabular">
        <div class="rounded-md border border-border bg-surface-2 px-2 py-2">
          <div class="text-[10px] uppercase tracking-wider text-faint">Calls</div>
          <div class="mt-0.5 text-[15px] font-semibold">{compact(llm.calls)}</div>
        </div>
        <div class="rounded-md border border-border bg-surface-2 px-2 py-2">
          <div class="text-[10px] uppercase tracking-wider text-faint">Errors</div>
          <div class={['mt-0.5 text-[15px] font-semibold', llm.errors > 0 ? 'text-bad' : 'text-muted'].join(' ')}>
            {llm.errors}
          </div>
        </div>
        <div class="rounded-md border border-border bg-surface-2 px-2 py-2">
          <div class="text-[10px] uppercase tracking-wider text-faint">Err rate</div>
          <div class={[
            'mt-0.5 text-[15px] font-semibold',
            llm.rate >= 0.1 ? 'text-bad' : llm.rate >= 0.03 ? 'text-warn' : 'text-good'
          ].join(' ')}>
            {(llm.rate * 100).toFixed(1)}%
          </div>
        </div>
      </div>

      {#if $healthQ.data.faded.length}
        <div class="mt-3 border-t border-border-soft pt-2">
          <div class="text-[10px] font-semibold uppercase tracking-wider text-warn">
            Faded sources
          </div>
          <div class="mt-1 text-[11px] text-muted">
            {$healthQ.data.faded.join(', ')}
          </div>
          <div class="mt-0.5 text-[10.5px] text-faint">
            Sources below 35% calibrated hit-rate (50+ scored calls).
          </div>
        </div>
      {/if}
    </Card>
  </div>

  <div class="mt-4">
    <ToolCallsPanel />
  </div>

  <div class="mt-4">
    <PromptEditor />
  </div>

  <div class="mt-4">
    <LogPanel n={220} intervalMs={4_000} />
  </div>
{/if}
