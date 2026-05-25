<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { kpi, health } from '$api';
  import { usd, pct, tone } from '../format';

  const kpiQ = createQuery({
    queryKey: ['kpi'],
    queryFn: kpi,
    refetchInterval: 30_000
  });

  const healthQ = createQuery({
    queryKey: ['health'],
    queryFn: health,
    refetchInterval: 60_000
  });

  let now = $state(new Date());
  setInterval(() => (now = new Date()), 1000);

  const clock = $derived(
    now.toISOString().slice(0, 19).replace('T', '  ') + 'Z'
  );
</script>

<header
  class="sticky top-0 z-30 flex h-14 items-center justify-between border-b border-border bg-bg/95 px-4 backdrop-blur"
>
  <div class="flex items-baseline gap-3 text-sm">
    {#if $kpiQ.data}
      {@const eq = $kpiQ.data.equity}
      {@const ret = $kpiQ.data.return_pct}
      <div class="flex items-baseline gap-1.5">
        <span class="text-[10px] uppercase tracking-wider text-faint">Equity</span>
        <span class="font-semibold tabular text-text">{usd(eq)}</span>
        {#if ret !== null}
          <span class={['tabular text-xs', tone(ret) === 'pos' ? 'text-good' : tone(ret) === 'neg' ? 'text-bad' : 'text-muted'].join(' ')}>
            {pct(ret, 1)}
          </span>
        {/if}
      </div>
    {/if}
  </div>

  <div class="flex items-center gap-3 text-xs">
    <span class="tabular font-mono text-muted">{clock}</span>
    {#if $healthQ.data}
      {@const v = $healthQ.data.verdict}
      <span
        class={[
          'rounded-full border px-2 py-0.5 text-[11px] font-semibold',
          v === 'ok'
            ? 'border-good/30 bg-good-soft text-good'
            : v === 'warn'
              ? 'border-warn/30 bg-warn-soft text-warn'
              : v === 'crit'
                ? 'border-bad/30 bg-bad-soft text-bad'
                : 'border-border bg-surface-2 text-muted'
        ].join(' ')}
      >
        {$healthQ.data.marker} {$healthQ.data.headline}
      </span>
    {/if}
  </div>
</header>
