/**
 * Type definitions matching the Python API responses.
 * Kept hand-curated rather than generated — the API surface is small
 * enough that the maintenance overhead is lower than wiring up
 * openapi-typescript on every backend tweak.
 */

export interface KpiSnapshot {
  equity: number | null;
  return_pct: number | null;
  wallets: number | null;
  open_positions: number | null;
  unrealized_pnl: number | null;
  realized_pnl: number | null;
  wins: number | null;
  closed: number | null;
  calls_scored: number | null;
  hit_rate_pct: number | null;
  hits: number | null;
  llm_calls: number | null;
  llm_errors: number | null;
  llm_reliability_pct: number | null;
}

export interface EquityCurvePoint {
  ts: string;
  equity: number;
}
export interface EquityCurve {
  fund: string;
  mandate: string;
  starting: number;
  points: EquityCurvePoint[];
}

export interface RealizedCurvePoint {
  ts: string;
  ticker: string;
  side: 'long' | 'short';
  pnl: number;
  cumulative: number;
}

export interface Activity {
  kind: 'call' | 'filing' | 'news';
  id: number;
  ticker: string | null;
  ts: string;
  title: string;
  side?: string;
  src?: string;
  conviction?: number;
  url?: string | null;
  form?: string;
  materiality_score?: number | null;
  sentiment?: number | null;
}

export interface WatchlistRow {
  ticker: string;
  asset_class: string;
  last_price: number | null;
  change_1d_pct: number | null;
  change_1w_pct: number | null;
  change_1m_pct: number | null;
  change_1y_pct: number | null;
  volume_vs_avg: number | null;
  day_low: number | null;
  day_high: number | null;
  high_52w: number | null;
  low_52w: number | null;
}

export interface OHLC {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface OpenPosition {
  side: 'long' | 'short';
  qty: number;
  entry: number;
  entry_at: string;
  mark: number | null;
  pnl: number | null;
  pnl_pct: number | null;
}

export interface ClosedTrade {
  side: 'long' | 'short';
  qty: number;
  entry: number;
  entry_at: string;
  exit: number | null;
  exit_at: string | null;
  pnl: number | null;
}

export interface PriceContextSnapshot {
  last_price: number;
  change_1d_pct: number;
  change_5d_pct: number;
  volume_vs_20d_avg: number;
  last_updated: string;
}

export interface TickerChart {
  ticker: string;
  bars: OHLC[];
  open_position: OpenPosition | null;
  closed: ClosedTrade[];
  context: PriceContextSnapshot | null;
}

export interface TickerStats {
  ticker: string;
  last_price: number | null;
  change_1d_pct: number | null;
  change_5d_pct: number | null;
  volume: number | null;
  avg_volume_20d: number | null;
  day_low: number | null;
  day_high: number | null;
  high_52w: number | null;
  low_52w: number | null;
  bars_count: number;
  earliest_bar: string | null;
}

export interface Thesis {
  id: number;
  ticker: string;
  direction: 'long' | 'short' | 'neutral';
  title: string;
  body: string;
  invalidation_criteria: string;
  conviction: number;
  target_price: number | null;
  horizon_days: number | null;
  state: 'active' | 'validated' | 'invalidated' | 'matured' | 'closed';
  source_event: string | null;
  model: string;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
  close_reason: string | null;
  supporting_events: number;
  challenging_events: number;
  last_event_at: string | null;
}

export interface ThesisEvent {
  id: number;
  thesis_id: number;
  kind: string;
  ref_table: string | null;
  ref_id: number | null;
  description: string;
  impact: 'supports' | 'challenges' | 'neutral';
  rationale: string;
  created_at: string;
}

export interface ThesisDetail extends Thesis {
  events: ThesisEvent[];
}

export interface Wallet {
  name: string;
  mandate: string;
  equity: number;
  start: number;
  ret_pct: number;
  open: number;
  upnl: number;
  closed: number;
  wins: number;
}

export interface WalletOpenRow {
  id: number;
  ticker: string;
  side: 'long' | 'short';
  qty: number;
  entry: number;
  entry_at: string;
  mark: number;
  mark_live: boolean;
  upnl: number;
  upnl_pct: number;
  open_reason: string | null;
  call_id: number | null;
}

export interface WalletClosedRow {
  id: number;
  ticker: string;
  side: 'long' | 'short';
  qty: number;
  entry: number;
  entry_at: string;
  exit: number | null;
  exit_at: string | null;
  realized_pnl: number | null;
  realized_pct: number;
  open_reason: string | null;
  close_reason: string | null;
  call_id: number | null;
}

export interface WalletHistory {
  name: string;
  mandate: string;
  cash: number;
  equity: number;
  starting: number;
  ret_pct: number;
  open: WalletOpenRow[];
  closed: WalletClosedRow[];
  as_of: string;
}

export interface ResearchTaskDetail extends ResearchTask {
  dossier: string;
  rec_thesis: string | null;
  rec_risks: string | null;
  executed_trade_id: number | null;
  model: string;
}

export interface CallItem {
  id: number;
  ticker: string;
  direction: 'long' | 'short';
  conviction: number;
  source: string;
  thesis: string;
  ts: string;
  ret_1d_pct: number | null;
  ret_5d_pct: number | null;
  ret_20d_pct: number | null;
  price_at_call: number | null;
  settled: boolean;
}

export interface CallDossier {
  call_id: number;
  body: string;
  meta: { created_at: string; model: string } | null;
}

export interface NewsItem {
  id: number;
  ticker: string | null;
  title: string;
  url: string;
  source: string;
  summary: string | null;
  ts: string;
  impact_1d_pct: number | null;
  sentiment: number | null;
  is_macro: boolean;
}

export interface NewsDossier {
  news_id: number;
  body: string;
  meta: { created_at: string; model: string } | null;
}

export interface ResearchTask {
  id: number;
  prompt: string;
  created_at: string;
  verdict: 'TRADE' | 'WATCHLIST' | 'PASS' | null;
  rec_ticker: string | null;
  rec_direction: string | null;
  rec_conviction: number | null;
  rec_size_pct: number | null;
  executed_at: string | null;
  execution_note: string | null;
  has_dossier: boolean;
}

export interface ResearchExecuteResult {
  task_id: number;
  ok: boolean;
  message: string;
  trade_id: number | null;
}

export interface Scorecard {
  overall: { n: number; hits: number };
  by_source: Record<string, { n: number; hits: number }>;
  by_conviction: Record<string, { n: number; hits: number }>;
}

export interface HealthReport {
  marker: string;
  verdict: string;
  headline: string;
  critical: string[];
  warnings: string[];
  jobs: { id: string; runs: number; fail: number; ok: boolean }[];
  jobs_runs: number;
  jobs_n: number;
  jobs_fail: number;
  streams: Record<string, number>;
  llm: { calls: number; errors: number; rate: number };
  watchlist: number;
  open_calls: number;
  faded: string[];
}

export interface SystemMetrics {
  cpu_pct: number | null;
  rss_mb: number | null;
  threads: number | null;
  fds: number | null;
  uptime_s: number | null;
  db_human: string;
  llm_calls: number;
  llm_errors: number;
}
