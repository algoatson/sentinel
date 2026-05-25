/**
 * API client — thin fetch wrappers around the FastAPI router at /api.
 *
 * Dev: vite proxies /api to localhost:8730 (vite.config.ts).
 * Prod: same-origin; no proxy needed.
 *
 * Every wrapper returns a typed response. Errors are thrown so
 * TanStack Query handles them. No retry logic here — TanStack Query
 * is configured with sensible defaults in +layout.svelte.
 */

import type {
  Activity,
  CallDossier,
  CallItem,
  EquityCurve,
  FilingItem,
  HealthReport,
  KpiSnapshot,
  LookupResult,
  NewsDossier,
  NewsItem,
  RealizedCurvePoint,
  ResearchExecuteResult,
  ResearchTask,
  ResearchTaskDetail,
  Scorecard,
  SystemMetrics,
  Thesis,
  ThesisDetail,
  TickerChart,
  TickerStats,
  Wallet,
  WalletHistory,
  Watch,
  WatchlistRow,
} from './types';

const BASE = '/api';

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${path}`);
  return (await res.json()) as T;
}

async function post<T, Body = unknown>(
  path: string,
  body?: Body
): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${path}`);
  return (await res.json()) as T;
}

/* ─── overview ─── */
export const kpi = () => get<KpiSnapshot>('/overview/kpi');
export const equityCurve = (days = 30) =>
  get<EquityCurve[]>(`/overview/equity-curve?days=${days}`);
export const realizedCurve = () =>
  get<RealizedCurvePoint[]>('/overview/realized-curve');
export const activity = (hours = 48) =>
  get<Activity[]>(`/overview/activity?hours=${hours}`);

/* ─── markets ─── */
export const watchlist = () => get<WatchlistRow[]>('/markets/watchlist');
export const tickerChart = (ticker: string, days?: number | null) => {
  const q = days === null ? '?days=0' : days !== undefined ? `?days=${days}` : '';
  return get<TickerChart>(`/markets/${encodeURIComponent(ticker)}/chart${q}`);
};
export const tickerStats = (ticker: string, days = 365) =>
  get<TickerStats | null>(
    `/markets/${encodeURIComponent(ticker)}/stats?days=${days}`
  );

/* ─── theses ─── */
export const thesesActive = () => get<Thesis[]>('/theses/active');
export const thesesClosed = (days = 30) =>
  get<Thesis[]>(`/theses/closed?days=${days}`);
export const thesisDetail = (id: number) =>
  get<ThesisDetail>(`/theses/${id}`);
export const closeThesis = (
  id: number,
  state: 'validated' | 'invalidated' | 'matured' | 'closed',
  reason: string
) => post<{ ok: boolean }>(`/theses/${id}/close`, { state, reason });
export const runThesisGenerate = () =>
  post<{ ok: boolean }>('/theses/run-generate');
export const runThesisReview = () =>
  post<{ ok: boolean }>('/theses/run-review');

/* ─── wallets ─── */
export const wallets = () => get<Wallet[]>('/wallets');
export const walletDetail = (name: string) =>
  get<WalletHistory>(`/wallets/${encodeURIComponent(name)}`);
export const walletHistory = (name: string, days = 90) =>
  get<WalletHistory>(`/wallets/${encodeURIComponent(name)}/history?days=${days}`);

/* ─── calls ─── */
export const calls = (days = 7) => get<CallItem[]>(`/calls?days=${days}`);
export const callDossier = (id: number, refresh = false) =>
  get<CallDossier>(
    `/calls/${id}/dossier${refresh ? '?refresh=true' : ''}`
  );
export const askCall = (id: number, question: string) =>
  post<{ answer: string }>(`/calls/${id}/ask`, { question });
export const scorecard = () => get<Scorecard>('/scorecard');

/* ─── news ─── */
export const news = (hours = 24, ticker?: string) => {
  const params = new URLSearchParams({ hours: String(hours) });
  if (ticker) params.set('ticker', ticker);
  return get<NewsItem[]>(`/news?${params}`);
};
export const newsDossier = (id: number, refresh = false) =>
  get<NewsDossier>(
    `/news/${id}/dossier${refresh ? '?refresh=true' : ''}`
  );
export const askNews = (id: number, question: string) =>
  post<{ answer: string }>(`/news/${id}/ask`, { question });

/* ─── filings ─── */
export const filings = (
  opts: { hours?: number; ticker?: string; form?: string; min_materiality?: number } = {}
) => {
  const params = new URLSearchParams({
    hours: String(opts.hours ?? 48),
    min_materiality: String(opts.min_materiality ?? 0)
  });
  if (opts.ticker) params.set('ticker', opts.ticker);
  if (opts.form) params.set('form', opts.form);
  return get<FilingItem[]>(`/filings?${params}`);
};

/* ─── research ─── */
export const researchTasks = (n = 30) =>
  get<ResearchTask[]>(`/research?n=${n}`);
export const researchTask = (id: number) =>
  get<ResearchTaskDetail>(`/research/${id}`);
export const runResearch = (prompt: string) =>
  post<{ task_id: number }>('/research/run', { prompt });
export const executeResearch = (id: number) =>
  post<ResearchExecuteResult>(`/research/${id}/execute`);
export const researchRemaining = () =>
  get<{ remaining: number }>('/research/meta/executions-remaining');

/* ─── health ─── */
export const health = () => get<HealthReport>('/health');
export const systemMetrics = () => get<SystemMetrics>('/health/system');
export const systemLogs = (n = 220) =>
  get<{ lines: string[] }>(`/health/logs?n=${n}`);

/* ─── watches ─── */
export const listWatches = () => get<Watch[]>('/watches');
export const addWatch = (text: string) =>
  post<{ ok: boolean; message: string }>('/watches', { text });
export const removeWatch = (wid: number) =>
  fetch(`/api/watches/${wid}`, { method: 'DELETE' }).then(async (r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json() as Promise<{ ok: boolean; message: string }>;
  });

/* ─── lookup ─── */
export const lookup = (kind: string, arg: string = '') => {
  const params = new URLSearchParams();
  if (arg) params.set('arg', arg);
  const q = params.toString() ? `?${params}` : '';
  return get<LookupResult>(`/lookup/${encodeURIComponent(kind)}${q}`);
};

/* ─── copilot ─── */
export const askCopilot = (question: string) =>
  post<{ answer: string }>('/copilot/ask', { question });
