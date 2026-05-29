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

export interface EdgeMultiplierDiag {
  n: number;
  avg_r_pct: number | null;
  raw_mult?: number;
  confidence?: number;
  mult: number;
  shrunk: boolean;
  reason?: string;
}
export interface EdgeMultipliers {
  as_of: string;
  min_sample: number;
  full_conf_n: number;
  floor: number;
  ceiling: number;
  by_source: Record<string, EdgeMultiplierDiag>;
}
export interface WalletMeta {
  as_of: string;
  funds: Record<string, unknown>[];
  by_source: Record<string, Record<string, unknown>>;
  by_conviction: Record<string, Record<string, unknown>>;
  by_asset: Record<string, Record<string, unknown>>;
  experiments: Record<string, Record<string, unknown>>;
  edge_multipliers: EdgeMultipliers;
}
export const walletMeta = () => get<WalletMeta>('/wallets/meta');

export interface WalletKnob {
  value: number | null;
  default: number | null;
  overridden: boolean;
}
export interface WalletPolicy {
  name: string;
  mandate: string;
  active: boolean;
  starting_cash: number;
  cash: number;
  knobs: {
    size_pct: WalletKnob;
    max_positions: WalletKnob;
    stop_pct: WalletKnob;
    take_pct: WalletKnob;
    max_hold_days: WalletKnob;
    min_conviction: WalletKnob;
    max_opens_per_day: WalletKnob;
  };
  sources: string[];
  asset_classes: string[] | null;
}
export type WalletKnobKey = keyof WalletPolicy['knobs'];

export const walletPolicy = (name: string) =>
  get<WalletPolicy>(`/wallets/${encodeURIComponent(name)}/policy`);

/* ─── prompt editor ─── */
export interface PromptListItem {
  name: string;
  active_id: number | null;
  created_at: string | null;
  overridden: boolean;
  seed_len: number;
  active_len: number;
}
export interface PromptHistoryItem {
  id: number;
  created_at: string;
  active: boolean;
  len: number;
}
export interface PromptDetail {
  name: string;
  seed: string;
  active: {
    id: number;
    content: string;
    created_at: string;
  } | null;
  active_content: string;
  overridden: boolean;
  history: PromptHistoryItem[];
}
export const listPrompts = () => get<PromptListItem[]>('/prompts');
export const getPrompt = (name: string) =>
  get<PromptDetail>(`/prompts/${encodeURIComponent(name)}`);
export const savePrompt = (name: string, content: string) =>
  fetch(`/api/prompts/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  }).then(async (r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  });
export const resetPrompt = (name: string) =>
  fetch(`/api/prompts/${encodeURIComponent(name)}/reset`, { method: 'POST' })
    .then(async (r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      return r.json();
    });
export const restorePrompt = (name: string, versionId: number) =>
  fetch(
    `/api/prompts/${encodeURIComponent(name)}/restore/${versionId}`,
    { method: 'POST' },
  ).then(async (r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  });

export const updateWalletPolicy = (
  name: string,
  body: Partial<{
    active: boolean;
    mandate: string;
    size_pct: number;
    max_positions: number;
    stop_pct: number;
    take_pct: number;
    max_hold_days: number;
    min_conviction: number;
    max_opens_per_day: number;
    clear: WalletKnobKey[];
  }>,
) =>
  fetch(`/api/wallets/${encodeURIComponent(name)}/policy`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(async (r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json() as Promise<WalletPolicy>;
  });
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
export interface CopilotToolCall {
  name: string;
  arguments: Record<string, unknown>;
  iteration: number | null;
}
export interface CopilotAnswer {
  answer: string;
  tool_calls: CopilotToolCall[];
}
export const askCopilot = (question: string, opts: { deep?: boolean } = {}) =>
  post<CopilotAnswer>('/copilot/ask', {
    question,
    deep: opts.deep ?? true,
  });

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

export interface CorrelationMatrix {
  tickers: string[];
  matrix: Array<Array<number | null>>;
  days: number;
  n: number;
  bars_used?: Record<string, number>;
}
export const correlationMatrix = (tickers?: string[], days = 30) => {
  const p = new URLSearchParams({ days: String(days) });
  if (tickers && tickers.length) p.set('tickers', tickers.join(','));
  return get<CorrelationMatrix>(`/analytics/correlation?${p}`);
};

export interface TodayPulse {
  as_of: string;
  window_hours: number;
  news_count: number;
  calls_count: number;
  filings_count: number;
  reddit_count: number;
  trades_opened: number;
  trades_closed: number;
  realized_today: number;
  best_close: { ticker: string; side: string; pnl: number } | null;
  worst_close: { ticker: string; side: string; pnl: number } | null;
  highest_conviction_call: {
    ticker: string; direction: string; conviction: number; source: string;
  } | null;
  top_material_filing: {
    ticker: string | null; form_type: string; materiality_score: number | null;
  } | null;
}
export const todayPulse = () => get<TodayPulse>('/analytics/today');

export interface RiskRowSlim {
  id: number;
  fund: string;
  ticker: string;
  side: string;
  upnl: number | null;
  upnl_pct: number | null;
  mark: number | null;
  stop_price: number | null;
  target_price: number | null;
  dist_to_stop_pct: number | null;
  dist_to_target_pct: number | null;
  r_multiple: number | null;
}
export interface RiskSnapshot {
  n_open: number;
  n_with_stop: number;
  n_with_target: number;
  n_near_stop: number;
  n_near_target: number;
  n_underwater: number;
  n_in_profit: number;
  dollar_at_risk: number;
  pct_book_at_risk: number;
  avg_r_multiple: number | null;
  median_dist_to_stop_pct: number | null;
  biggest_winner: RiskRowSlim | null;
  biggest_loser: RiskRowSlim | null;
  near_stop: RiskRowSlim[];
  near_target: RiskRowSlim[];
  naked: RiskRowSlim[];
  near_pct: number;
}
export const riskMonitor = () => get<RiskSnapshot>('/analytics/risk-monitor');

export interface ClosedTradeRow {
  id: number;
  fund: string;
  ticker: string;
  side: 'long' | 'short' | string;
  qty: number;
  entry: number;
  entry_at: string;
  exit: number | null;
  exit_at: string | null;
  hold_h: number | null;
  realized_pnl: number | null;
  realized_pct: number;
  open_reason: string | null;
  close_reason: string | null;
  call_id: number | null;
  stop_price: number | null;
  target_price: number | null;
  r_multiple: number | null;
  notes: string | null;
  notional: number;
}
export const closedPositions = (opts: { limit?: number; fund?: string } = {}) => {
  const p = new URLSearchParams();
  if (opts.limit) p.set('limit', String(opts.limit));
  if (opts.fund) p.set('fund', opts.fund);
  const q = p.toString();
  return get<ClosedTradeRow[]>(`/positions/closed${q ? `?${q}` : ''}`);
};
export interface EarningsExposureRow {
  ticker: string;
  report_date: string;
  days_until: number;
  funds: string;
  notional: number;
  upnl: number;
  n_positions: number;
  fetched_at: string | null;
}
export interface EarningsExposureUnknown {
  ticker: string;
  funds: string;
  notional: number;
  upnl: number;
  n_positions: number;
}
export interface EarningsExposure {
  window_days: number;
  as_of: string;
  upcoming: EarningsExposureRow[];
  this_week: number;
  this_month: number;
  unknown: EarningsExposureUnknown[];
}
export const earningsExposure = (windowDays = 30) =>
  get<EarningsExposure>(`/analytics/earnings-exposure?window_days=${windowDays}`);

export interface HoldingsNewsItem {
  id: number;
  ticker: string | null;
  title: string;
  url: string;
  source: string;
  ts: string;
  sentiment: number | null;
  impact_1d_pct: number | null;
  tickers: string[];
  held_tickers: string[];
  funds: string[];
}
export interface HoldingsNewsFiling {
  id: number;
  ticker: string;
  form_type: string;
  filed_at: string;
  url: string | null;
  materiality_score: number | null;
  funds: string[];
}
export interface HoldingsNews {
  as_of: string;
  window_hours: number;
  tickers: string[];
  holdings_by_ticker: Record<string, string[]>;
  news: HoldingsNewsItem[];
  filings: HoldingsNewsFiling[];
}
export const holdingsNews = (hours = 24, limit = 30) =>
  get<HoldingsNews>(`/analytics/holdings-news?hours=${hours}&limit=${limit}`);

export interface StreakSnapshot {
  n: number;
  current: { kind: 'win' | 'loss' | 'none'; length: number; started_at: string | null };
  max_win: number;
  max_loss: number;
  last_pnls: number[];
  hit_rate: number | null;
  wins: number;
  losses: number;
  scratches: number;
  expectancy: number;
  avg_win: number | null;
  avg_loss: number | null;
}
export const streaks = (limit = 200) =>
  get<StreakSnapshot>(`/analytics/streaks?limit=${limit}`);

export interface PerfGroup {
  source: string;
  n: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  total_pnl: number;
  expectancy: number;
  avg_win: number | null;
  avg_loss: number | null;
  avg_r: number | null;
  avg_hold_h: number | null;
  recent_pnls: number[];
}
export interface PerfBySource {
  n: number;
  groups: PerfGroup[];
}
export const perfBySource = (limit = 500) =>
  get<PerfBySource>(`/analytics/perf-by-source?limit=${limit}`);

export interface PnlBin {
  lo: number;
  hi: number;
  count: number;
}
export interface PnlDistribution {
  n: number;
  bin_width_pct: number;
  range_max?: number;
  bins: PnlBin[];
  mean_pct: number | null;
  median_pct: number | null;
  stdev_pct: number | null;
  skew: number | null;
  p10: number | null;
  p90: number | null;
  best?: number;
  worst?: number;
}
export const pnlDistribution = (limit = 500) =>
  get<PnlDistribution>(`/analytics/pnl-distribution?limit=${limit}`);

export interface ConvergingRow {
  ticker: string;
  sources: string[];
  source_count: number;
  filings: number;
  news: number;
  social: number;
  calls: number;
  last_ts: string | null;
}
export interface ConvergingPayload {
  window_hours: number;
  as_of: string;
  rows: ConvergingRow[];
}
export const convergingNow = (hours = 6, limit = 8) =>
  get<ConvergingPayload>(`/analytics/converging?hours=${hours}&limit=${limit}`);

export interface SymbolNote {
  ticker: string;
  body: string;
  updated_at: string | null;
}
export const getSymbolNote = (ticker: string) =>
  get<SymbolNote>(`/symbol/${encodeURIComponent(ticker)}/note`);
export const putSymbolNote = (ticker: string, body: string) =>
  fetch(`/api/symbol/${encodeURIComponent(ticker)}/note`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ body })
  }).then(async (r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json() as Promise<SymbolNote>;
  });

export interface DailyPlan {
  plan_date: string;
  body: string;
  updated_at: string | null;
}
export const getDailyPlan = () => get<DailyPlan>('/plan/today');
export interface ToolCallEvent {
  id: number;
  ts: string;
  pipeline: string;
  tool: string;
  ticker: string | null;
  iteration: number | null;
  arguments: Record<string, unknown>;
  result_summary: string;
  ok: boolean;
  took_ms: number | null;
}
export interface ToolCallSnapshot {
  items: ToolCallEvent[];
  stats: {
    count: number;
    errors: number;
    by_tool: Record<string, number>;
    by_pipeline: Record<string, number>;
  };
}
export const recentToolCalls = (limit = 60) =>
  get<ToolCallSnapshot>(`/health/tool-calls?limit=${limit}`);

export const putDailyPlan = (body: string) =>
  fetch('/api/plan/today', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ body })
  }).then(async (r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json() as Promise<DailyPlan>;
  });

export interface BriefingToday {
  brief_date: string | null;
  body: string;
  importance: number | null;
  importance_reason: string | null;
  generated_at: string | null;
}
export const briefingToday = () => get<BriefingToday>('/briefing/today');

export interface TradeLifecycle {
  trade: {
    id: number;
    ticker: string;
    side: string;
    fund: string | null;
    entry_at: string;
    exit_at: string | null;
    status: string;
  };
  news: Array<{
    id: number; title: string; url: string; source: string;
    ts: string; sentiment: number | null; impact_1d_pct: number | null;
  }>;
  filings: Array<{
    id: number; form_type: string; filed_at: string;
    url: string | null; materiality_score: number | null;
  }>;
  calls: Array<{
    id: number; source: string; direction: string; conviction: number;
    thesis: string; created_at: string;
    ret_1d_pct: number | null; ret_5d_pct: number | null; ret_20d_pct: number | null;
  }>;
}
export const tradeLifecycle = (tradeId: number) =>
  get<TradeLifecycle>(`/positions/${tradeId}/lifecycle`);

export const updateJournal = (tradeId: number, notes: string | null) =>
  fetch(`/api/positions/${tradeId}/journal`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ notes })
  }).then(async (r) => {
    if (!r.ok) throw new Error(await r.text());
    return r.json() as Promise<{ ok: boolean; trade_id: number; message: string }>;
  });
