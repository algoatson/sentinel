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
  AttributionSummary,
  CalibrationSummary,
  CallDossier,
  CallItem,
  CatalystEvent,
  EquityCurve,
  FilingItem,
  HealthReport,
  HotTicker,
  KpiSnapshot,
  LiveEvent,
  LookupResult,
  NewsDossier,
  NewsItem,
  RealizedCurvePoint,
  RedditMention,
  ResearchExecuteResult,
  ResearchTask,
  ResearchTaskDetail,
  Scorecard,
  SocialTicker,
  SymbolProfile,
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

export interface AtrInfo {
  ticker: string;
  period: number;
  last_close: number | null;
  atr: number | null;
  atr_pct: number | null;
  suggested_long_stop: number | null;
  suggested_short_stop: number | null;
  suggested_long_stop_tight: number | null;
  suggested_short_stop_tight: number | null;
  bars_used: number;
}
export const tickerAtr = (ticker: string, period = 14) =>
  get<AtrInfo>(`/markets/${encodeURIComponent(ticker)}/atr?period=${period}`);

export interface MoverRow {
  ticker: string;
  asset_class: string;
  last_price: number | null;
  change_1d_pct: number;
  volume_vs_20d_avg: number | null;
}
export const topMovers = (limit = 8) =>
  get<{ gainers: MoverRow[]; losers: MoverRow[] }>(
    `/markets/top-movers?limit=${limit}`
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
export const news = (
  hours = 24,
  ticker?: string,
  opts: { dedupe?: boolean } = {}
) => {
  const params = new URLSearchParams({ hours: String(hours) });
  if (ticker) params.set('ticker', ticker);
  if (opts.dedupe) params.set('dedupe', 'true');
  return get<NewsItem[]>(`/news?${params}`);
};
export const newsDossier = (id: number, refresh = false) =>
  get<NewsDossier>(
    `/news/${id}/dossier${refresh ? '?refresh=true' : ''}`
  );
export const askNews = (id: number, question: string) =>
  post<{ answer: string }>(`/news/${id}/ask`, { question });
export const newsArticle = (id: number) =>
  get<{
    news_id: number;
    url: string;
    body: string | null;
    source: string | null;
    char_count: number;
    fetched_at: string | null;
  }>(`/news/${id}/article`);

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

/* ─── market status ─── */
export interface MarketStatus {
  state: 'open' | 'pre' | 'after' | 'closed' | 'holiday';
  label: string;
  emoji: string;
  session_open: boolean;
  regular_open: boolean;
  next_event: string | null;
  as_of: string;
  et_clock: string;
  today: string;
  holiday: string | null;
  half_day: string | null;
}
export const marketStatus = () => get<MarketStatus>('/market-status');

/* ─── watches ─── */
export const listWatches = () => get<Watch[]>('/watches');
export const getWatch = (wid: number) =>
  get<Watch & { spec: Record<string, unknown> }>(`/watches/${wid}`);
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

/* ─── symbol detail ─── */
export const symbolProfile = (ticker: string, days = 90) =>
  get<SymbolProfile>(
    `/symbol/${encodeURIComponent(ticker)}?days=${days}`
  );

/* ─── catalysts ─── */
export const catalysts = () =>
  get<{ text: string; events: CatalystEvent[] }>('/catalysts');

/* ─── social ─── */
export const socialRecent = (hours = 48, ticker?: string) => {
  const p = new URLSearchParams({ hours: String(hours) });
  if (ticker) p.set('ticker', ticker);
  return get<RedditMention[]>(`/social?${p}`);
};
export const socialTopTickers = (hours = 48, n = 10) =>
  get<SocialTicker[]>(`/social/top-tickers?hours=${hours}&n=${n}`);

/* ─── events (SSE history fallback) ─── */
export const recentEvents = (sinceId?: number) =>
  get<{ events: LiveEvent[] }>(
    `/events/recent${sinceId ? `?since_id=${sinceId}` : ''}`
  );

/* ─── analytics ─── */
export const hotTickers = (hours = 24, limit = 12) =>
  get<HotTicker[]>(`/analytics/hot?hours=${hours}&limit=${limit}`);
export const calibration = (days = 90) =>
  get<CalibrationSummary>(`/analytics/calibration?days=${days}`);
export const attribution = (days = 90) =>
  get<AttributionSummary>(`/analytics/attribution?days=${days}`);
export const newsClusters = (hours = 24) =>
  get<{ clusters: Record<string, number[]> }>(
    `/analytics/news-clusters?hours=${hours}`
  );
export interface SentimentQuality {
  window_days: number;
  overall: {
    n: number; right: number; wrong: number; muted: number; neutral: number;
    directional_accuracy: number | null;
  };
  by_source: Array<{
    source: string; n: number; right: number; wrong: number; muted: number;
    neutral: number; directional_accuracy: number | null;
  }>;
}
export const sentimentQuality = (days = 60) =>
  get<SentimentQuality>(`/analytics/sentiment-quality?days=${days}`);

export interface MonthlyCell {
  month: string;
  realized_pnl: number;
  closed: number;
  wins: number;
}
export interface MonthlyWallet {
  wallet: string;
  cells: MonthlyCell[];
  total_pnl: number;
  total_closed: number;
  total_wins: number;
}
export const monthlyPnl = (months = 12) =>
  get<{ months: string[]; wallets: MonthlyWallet[] }>(
    `/analytics/monthly?months=${months}`
  );

export interface ConcentrationGroup {
  asset_class: string;
  notional: number;
  pct: number;
  tickers: string[];
  count: number;
}
export interface ConcentrationWallet {
  mandate: string | null;
  total_notional: number;
  groups: ConcentrationGroup[];
}
export const concentration = () =>
  get<{ wallets: Record<string, ConcentrationWallet> }>(
    '/analytics/concentration'
  );

/* ─── positions (unified book + close action) ─── */
export interface OpenPositionRow {
  id: number;
  fund: string;
  fund_mandate: string;
  ticker: string;
  side: 'long' | 'short';
  qty: number;
  entry: number;
  entry_at: string;
  age_h: number;
  mark: number;
  mark_live: boolean;
  upnl: number;
  upnl_pct: number;
  open_reason: string | null;
  call_id: number | null;
  stop_price: number | null;
  target_price: number | null;
  trailing_stop_pct: number | null;
  watermark_price: number | null;
  notes: string | null;
  dist_to_stop_pct: number | null;
  dist_to_target_pct: number | null;
  r_multiple: number | null;
  pct_of_equity: number;
  notional: number;
}
export const openPositions = () =>
  get<OpenPositionRow[]>('/positions/open');
export const closePosition = (id: number, reason?: string) =>
  post<{ ok: boolean; message: string; trade_id: number; realized_pnl: number | null }>(
    `/positions/${id}/close`,
    reason ? { reason } : {}
  );

export interface RiskPatch {
  stop_price?: number | null;
  target_price?: number | null;
  trailing_stop_pct?: number | null;
  notes?: string | null;
  /** Names of fields to explicitly null out. */
  clear?: Array<'stop_price' | 'target_price' | 'trailing_stop_pct' | 'notes'>;
}
export const updateRisk = (id: number, body: RiskPatch) =>
  fetch(`/api/positions/${id}/risk`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(async (r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json() as Promise<{ ok: boolean; message: string }>;
  });

export const bulkClose = (ids: number[], reason?: string) =>
  post<{
    ok: boolean;
    closed: number;
    attempted: number;
    total_realized_pnl: number;
  }>('/positions/bulk-close', { trade_ids: ids, reason });

export const csvExportUrl = '/api/positions/export.csv';

export interface OpenRequest {
  fund_name: string;
  ticker: string;
  side: 'long' | 'short';
  /** Sizing — provide exactly one of qty / notional / (risk_pct + stop_price). */
  qty?: number;
  notional?: number;
  risk_pct?: number;
  stop_price?: number;
  note?: string;
}
export const openPosition = (body: OpenRequest) =>
  post<{
    ok: boolean; message: string;
    trade_id: number | null; fill_price: number | null; qty: number | null;
  }>('/positions/open', body);

/* ─── analytics: daily + drawdown ─── */
export interface DailyCell {
  date: string;          // YYYY-MM-DD
  weekday: number;       // 1=Mon … 7=Sun
  realized_pnl: number;
  closed: number;
  wins: number;
  losses: number;
}
export interface DailyPnlPayload {
  days: number;
  from: string;
  to: string;
  cells: DailyCell[];
  max_abs: number;
  active_days: number;
  total_realized: number;
  best_day: DailyCell | null;
  worst_day: DailyCell | null;
}
export const dailyPnl = (days = 180) =>
  get<DailyPnlPayload>(`/analytics/daily?days=${days}`);

export interface DrawdownPoint {
  ts: string;
  equity: number;
  peak: number;
  drawdown_pct: number;
}
export interface DrawdownWallet {
  fund: string;
  starting: number;
  points: DrawdownPoint[];
  current_dd_pct: number;
  max_dd_pct: number;
}
export const drawdownCurves = (days = 90) =>
  get<{ window_days: number; wallets: DrawdownWallet[] }>(
    `/analytics/drawdown?days=${days}`
  );
