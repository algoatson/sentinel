"""All SQLModel table definitions per SPEC §5. Schema-first: every table is
defined now even if Phase 1 only writes to a subset."""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class Watchlist(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    cik: str = Field(index=True, max_length=10)
    ticker: Optional[str] = Field(default=None, index=True)
    source: str  # "index" | "tracked_entity" | "activity" | "crypto" | "macro"
    # "equity" | "crypto" | "future" | "rate". Drives price-poll scheduling
    # (crypto/future are 24/7; equity is NYSE-hours) and filing applicability
    # (only equity rows ever match EDGAR).
    asset_class: str = Field(default="equity", index=True)
    added_at: datetime
    expires_at: Optional[datetime] = Field(default=None)


class TrackedEntity(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    cik: str = Field(index=True, max_length=10)
    type: str  # "fund" | "insider"
    notes: Optional[str] = Field(default=None)


class Filing(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    cik: str = Field(max_length=10)
    ticker: Optional[str] = Field(default=None)
    form_type: str
    accession_number: str = Field(unique=True, index=True)
    filed_at: datetime
    primary_doc_url: str
    summary: Optional[str] = Field(default=None)
    materiality_score: Optional[int] = Field(default=None)
    materiality_reason: Optional[str] = Field(default=None)
    posted_at: Optional[datetime] = Field(default=None)
    message_id: Optional[str] = Field(default=None, index=True)
    channel: Optional[str] = Field(default=None)


class SeenFiling(SQLModel, table=True):
    accession_number: str = Field(primary_key=True)
    seen_at: datetime


class RedditMention(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    subreddit: str
    post_id: str = Field(index=True)
    comment_id: Optional[str] = Field(default=None)
    ticker: str = Field(index=True)
    author: str
    score: int
    num_comments: int
    created_at: datetime = Field(index=True)
    title: str
    body_excerpt: str = Field(max_length=500)
    permalink: str
    sentiment: Optional[int] = Field(default=None)
    is_thesis: Optional[bool] = Field(default=None)
    # Set when the reddit_feed pipeline surfaces this post to the dedicated
    # Reddit channel — the de-dupe marker so a thread is posted at most once
    # even though it may have several rows (one per matched ticker).
    posted_at: Optional[datetime] = Field(default=None)


class HnMention(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ticker: str = Field(index=True)
    hn_id: str = Field(unique=True)
    title: str
    url: str
    points: int
    num_comments: int
    author: str
    created_at: datetime = Field(index=True)


class NewsItem(SQLModel, table=True):
    """Macro + per-ticker news. Ticker is nullable: macro/geopolitical items
    have ticker=None, per-name items have a ticker. is_macro distinguishes
    them explicitly so the macro_themes pipeline can filter cleanly.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    source: str  # "yfinance", "rss:cnbc-markets", etc.
    external_id: str = Field(unique=True, index=True)
    title: str
    url: str
    summary: Optional[str] = Field(default=None)
    ticker: Optional[str] = Field(default=None, index=True)
    # Comma-joined list of EVERY ticker the extractor found (uppercase).
    # `ticker` (singular) above is the "primary" — best one for back-compat
    # filtering and downstream pipelines that expect one. `tickers_csv`
    # carries the full set so a "$NVDA and $AMD both reported beats" story
    # tags both. Format: ",NVDA,AMD," (leading/trailing commas to make
    # LIKE '%,X,%' substring search safe).
    tickers_csv: Optional[str] = Field(default=None, index=True)
    is_macro: bool = Field(default=False)
    published_at: datetime = Field(index=True)
    fetched_at: datetime
    sentiment: Optional[int] = Field(default=None)
    # News→price correlation measurement. Filled by the news_impact pipeline.
    price_at_publish: Optional[float] = Field(default=None)
    impact_1h_pct: Optional[float] = Field(default=None)
    impact_1d_pct: Optional[float] = Field(default=None)
    impact_tagged_at: Optional[datetime] = Field(default=None)
    # Breaking-news alert dedupe — set when news_alerts posts about this item.
    alerted_at: Optional[datetime] = Field(default=None)


class PriceBar(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("ticker", "ts", name="uix_pricebar_ticker_ts"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    ticker: str = Field(index=True)
    ts: datetime = Field(index=True)
    open: float
    high: float
    low: float
    close: float
    volume: int


class PriceContext(SQLModel, table=True):
    ticker: str = Field(primary_key=True)
    last_price: float
    change_1d_pct: float
    change_5d_pct: float
    volume_vs_20d_avg: float
    last_updated: datetime


class SocialPulse(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ticker: str
    mention_count: int
    baseline: float
    ratio: float
    summary: str
    created_at: datetime
    message_id: Optional[str] = Field(default=None)


class Feedback(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    message_id: str = Field(index=True)
    emoji: str
    user_id: str
    created_at: datetime


class PaperTrade(SQLModel, table=True):
    """A paper position. No broker — entry/size/marks tracked locally so the
    bot can reason about the user's actual book and P&L."""
    id: Optional[int] = Field(default=None, primary_key=True)
    ticker: str = Field(index=True)
    side: str  # "long" | "short"
    qty: float
    entry_price: float
    entry_at: datetime
    status: str = Field(default="open", index=True)  # "open" | "closed"
    exit_price: Optional[float] = Field(default=None)
    exit_at: Optional[datetime] = Field(default=None)
    realized_pnl: Optional[float] = Field(default=None)
    note: Optional[str] = Field(default=None)
    opened_by: str = Field(default="manual")  # "manual" | "bot"


class Fund(SQLModel, table=True):
    """An autonomous paper-trading account. Three of these run different
    deterministic policies over the shared TradingCall stream — same signals,
    different mandates, so their P&L is a clean comparison."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)  # "degen" | "catalyst" | "macro"
    mandate: str  # human-readable description
    starting_cash: float
    cash: float
    last_call_id: int = Field(default=0)  # cursor into TradingCall
    created_at: datetime
    active: bool = Field(default=True)


class FundTrade(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    fund_id: int = Field(index=True)
    ticker: str = Field(index=True)
    side: str  # "long" | "short"
    qty: float
    entry_price: float
    entry_at: datetime
    status: str = Field(default="open", index=True)  # "open" | "closed"
    exit_price: Optional[float] = Field(default=None)
    exit_at: Optional[datetime] = Field(default=None)
    realized_pnl: Optional[float] = Field(default=None)
    call_id: Optional[int] = Field(default=None)
    open_reason: Optional[str] = Field(default=None)
    close_reason: Optional[str] = Field(default=None)
    # User-set risk management. The auto_exits pipeline scans every
    # cycle and force-closes any open trade whose mark crosses the
    # stop or target. Trailing stops ratchet via watermark_price.
    stop_price: Optional[float] = Field(default=None)
    target_price: Optional[float] = Field(default=None)
    trailing_stop_pct: Optional[float] = Field(default=None)
    watermark_price: Optional[float] = Field(default=None)
    # Free-form journal slot — write decisions, regrets, what-ifs.
    # Surfaced verbatim in the /book drawer and exported with CSV.
    notes: Optional[str] = Field(default=None)


class FundEquity(SQLModel, table=True):
    """Periodic mark of a fund's total equity — the equity curve."""
    id: Optional[int] = Field(default=None, primary_key=True)
    fund_id: int = Field(index=True)
    ts: datetime = Field(index=True)
    equity: float


class TradingCall(SQLModel, table=True):
    """A directional call the bot made (synthesis/convergence/why_moved),
    logged so it can be marked-to-market and scored. This is the
    accountability layer for the bot's opinions."""
    id: Optional[int] = Field(default=None, primary_key=True)
    ticker: str = Field(index=True)
    direction: str  # "long" | "short"
    conviction: int = Field(default=3)  # 1-5
    source: str = Field(index=True)  # pipeline name
    thesis: str = Field(max_length=400)
    price_at_call: Optional[float] = Field(default=None)
    created_at: datetime = Field(index=True)
    ret_1d_pct: Optional[float] = Field(default=None)
    ret_5d_pct: Optional[float] = Field(default=None)
    ret_20d_pct: Optional[float] = Field(default=None)
    marked_at: Optional[datetime] = Field(default=None)
    settled: bool = Field(default=False, index=True)  # 20d done
    # Set when call_review has posted (or finalized) this call's verdict — the
    # de-dupe marker for the visible-accountability post. Orthogonal to
    # `settled`/scoring: a call can be verdict-posted at 5d while its 20d
    # calibration mark is still maturing.
    resolved_posted_at: Optional[datetime] = Field(default=None, index=True)


class ArticleBody(SQLModel, table=True):
    """Cached extracted body text for a news article URL.

    Without this, the news dossier only sees the RSS `title` + (often
    near-empty) `summary` — the LLM ends up reasoning about a headline
    in isolation and confabulating. `fetch_article_text` populates this
    on demand; once we have a body, it's effectively immutable so the
    cache is forever-keyed-by-URL.

    `source` records HOW we got the body so a later "all articles via
    Jina" or "all articles via direct only" introspection is one query.
    A "stub" row means we tried, got nothing useful, and we don't want
    to re-try every dossier open — re-attempted with `force=True`.
    """
    url: str = Field(primary_key=True, max_length=1024)
    body: str
    source: str = Field(max_length=16)  # "direct" | "jina" | "stub"
    fetched_at: datetime
    char_count: int = Field(default=0)


class CallSummary(SQLModel, table=True):
    """Cached LLM dossier for a TradingCall. Generated on first dashboard
    click; never regenerated automatically (the underlying call data is
    immutable — thesis, price_at_call). Stale-but-cheap > regen-on-every-
    click; the user can force a refresh from the modal if desired.

    One row per call; `call_id` is the PK so an upsert just overwrites."""
    call_id: int = Field(primary_key=True, foreign_key="tradingcall.id")
    summary: str  # markdown
    created_at: datetime
    model: str = Field(default="", max_length=120)


class NewsAnalysis(SQLModel, table=True):
    """Cached LLM dossier for a NewsItem — same philosophy as CallSummary.
    Per-item read on what it means for the bot's book / watchlist names."""
    news_id: int = Field(primary_key=True, foreign_key="newsitem.id")
    summary: str
    created_at: datetime
    model: str = Field(default="", max_length=120)


class ResearchTask(SQLModel, table=True):
    """One Research Desk request — full audit trail from prompt to trade.

    The user types a free-form research prompt; the bot runs the heavy
    LLM with current data and returns a verdict (TRADE / WATCHLIST /
    PASS) + a markdown dossier. If TRADE and the user clicks Execute,
    the trade lands on the dedicated `research` Fund (separate from
    autonomous wallets so its P&L doesn't pollute their experiment).

    One row per request. The dossier is cached after generation — clicks
    that re-open the modal don't re-bill the LLM."""
    id: Optional[int] = Field(default=None, primary_key=True)
    prompt: str = Field(max_length=2000)
    created_at: datetime = Field(index=True)
    # Generated dossier (markdown body that the modal renders).
    dossier: Optional[str] = Field(default=None)
    dossier_at: Optional[datetime] = Field(default=None)
    # Parsed recommendation
    verdict: Optional[str] = Field(default=None, index=True)  # TRADE/WATCHLIST/PASS
    rec_ticker: Optional[str] = Field(default=None)
    rec_direction: Optional[str] = Field(default=None)  # long/short
    rec_conviction: Optional[int] = Field(default=None)
    rec_size_pct: Optional[float] = Field(default=None)
    rec_thesis: Optional[str] = Field(default=None, max_length=1000)
    rec_risks: Optional[str] = Field(default=None, max_length=1000)
    # Execution
    executed_at: Optional[datetime] = Field(default=None, index=True)
    executed_trade_id: Optional[int] = Field(default=None)
    execution_note: Optional[str] = Field(default=None, max_length=400)
    model: str = Field(default="", max_length=120)


class Thesis(SQLModel, table=True):
    """A running hypothesis the bot maintains across time.

    Generated by `thesis.generate_cycle()` from open positions, recent
    high-conviction calls, and material events. Each thesis is the
    bot's mental model for ONE ticker (or "MACRO" for cross-asset
    themes) — what it thinks is true now, what would prove it wrong,
    and the price/horizon it would close on.

    As new news/filings arrive, the linker tags them against active
    theses with an `impact` judgement (supports / challenges /
    neutral). The supporting and challenging counts inform the next
    review cycle — if challenges pile up, the bot revisits and may
    invalidate the thesis. That's the cross-pollination loop: data
    arrives → linked to live theses → theses re-evaluated → mental
    model updated. The user can read the trail at any time.

    State machine:
        active → validated (target hit / thesis confirmed)
        active → invalidated (price contradicts thesis / news kills it)
        active → matured (hit horizon_days without conviction shift)
        active → closed (user-closed manually)
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    ticker: str = Field(index=True)
    direction: str  # "long" | "short" | "neutral"
    title: str = Field(max_length=200)
    body: str
    invalidation_criteria: str = Field(max_length=500)

    conviction: int = Field(default=3)  # 1-5
    target_price: Optional[float] = Field(default=None)
    horizon_days: Optional[int] = Field(default=None)

    state: str = Field(default="active", index=True)
    source_event: Optional[str] = Field(default=None, max_length=120)
    model: str = Field(default="", max_length=120)

    created_at: datetime = Field(index=True)
    updated_at: datetime
    closed_at: Optional[datetime] = Field(default=None)
    close_reason: Optional[str] = Field(default=None, max_length=400)

    # Cached aggregates — updated by the linker so the UI doesn't have
    # to recount ThesisEvent rows on every card render.
    supporting_events: int = Field(default=0)
    challenging_events: int = Field(default=0)
    last_event_at: Optional[datetime] = Field(default=None)


class ThesisEvent(SQLModel, table=True):
    """One piece of new data the bot linked to an active Thesis.

    `kind`+`ref_table`+`ref_id` triangulate back to the source row
    (NewsItem, Filing, TradingCall, etc.). `impact` is the bot's
    judgement on how this data point bears on the thesis; `rationale`
    is a one-line explanation the user can read at-a-glance.

    The thesis modal shows these as a chronological timeline — what
    the bot has noticed and how it read each piece. Audit-grade
    visibility into a thesis evolving (or being abandoned)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    thesis_id: int = Field(index=True, foreign_key="thesis.id")
    kind: str = Field(max_length=20)
    ref_table: Optional[str] = Field(default=None, max_length=40)
    ref_id: Optional[int] = Field(default=None)
    description: str = Field(max_length=500)
    impact: str = Field(max_length=20)  # supports | challenges | neutral
    rationale: str = Field(default="", max_length=400)
    created_at: datetime = Field(index=True)


class JobRun(SQLModel, table=True):
    """One scheduler job execution. Powers the !health heartbeat."""
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: str = Field(index=True)
    ran_at: datetime = Field(index=True)
    ok: bool
    duration_ms: Optional[int] = Field(default=None)
    error: Optional[str] = Field(default=None, max_length=500)


class NarrativeEvent(SQLModel, table=True):
    """Per-ticker dated story log. Pipelines append here when they post
    something material so the bot has a memory: synthesis and thread Q&A
    read it back ("how has this evolved"), and it's the de-dupe backbone
    for story coalescing.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    ticker: str = Field(index=True)
    ts: datetime = Field(index=True)
    kind: str  # filing | why_moved | convergence | news_alert | synthesis
    tier: int = Field(default=1)  # 3=priority/filing, 2=convergence, 1=rest
    headline: str = Field(max_length=300)
    detail: Optional[str] = Field(default=None, max_length=1200)
    channel_id: Optional[int] = Field(default=None)
    message_id: Optional[str] = Field(default=None)


class CryptoMicro(SQLModel, table=True):
    """Latest crypto microstructure snapshot per ticker (one row per ticker,
    upserted). Funding/OI/orderbook context for why_moved + synthesis."""
    ticker: str = Field(primary_key=True)  # canonical BASE-USD
    venue: str
    funding_rate: Optional[float] = Field(default=None)
    open_interest: Optional[float] = Field(default=None)
    oi_change_24h_pct: Optional[float] = Field(default=None)
    orderbook_imbalance: Optional[float] = Field(default=None)
    updated_at: datetime


class Holding(SQLModel, table=True):
    """Paper portfolio — a plain list of what the user owns. No broker, no
    P&L advice; used purely to tag/prioritize content that touches the book.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    ticker: str = Field(unique=True, index=True)
    quantity: Optional[float] = Field(default=None)
    note: Optional[str] = Field(default=None)
    added_at: datetime


class Watch(SQLModel, table=True):
    """A user-defined natural-language alert, compiled by the LLM into a
    constrained condition spec (condition_json) evaluated each cycle.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    raw_text: str
    condition_json: str
    created_at: datetime
    active: bool = Field(default=True)
    last_triggered_at: Optional[datetime] = Field(default=None)
    trigger_count: int = Field(default=0)


class PromptVersion(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    prompt_name: str
    content: str
    created_at: datetime
    active: bool = Field(default=True)


class EarningsDate(SQLModel, table=True):
    """Next scheduled earnings report per ticker, one row per ticker
    (upserted). Populated by the catalyst pipeline; read by funds (entry
    blackout) and synthesis (binary-risk awareness) so nothing trades or
    reasons blind into a print. `fetched_at` lets readers reject stale rows."""
    ticker: str = Field(primary_key=True)
    report_date: date = Field(index=True)
    fetched_at: datetime


class PendingTuning(SQLModel, table=True):
    """A posted-but-undecided monthly tuning proposal. Persisted so a restart
    between the #meta post and the user's ✅/❌ reaction doesn't silently drop
    it (the proposal cadence is monthly — losing one costs a month)."""
    message_id: str = Field(primary_key=True)
    delta: str
    created_at: datetime


class SymbolNote(SQLModel, table=True):
    """Free-form per-ticker notebook. Distinct from FundTrade.notes, which
    is per-trade — a SymbolNote persists across trades so observations
    like "tends to gap up after upgrades" stay attached to the ticker
    forever. One row per ticker, body upserted, updated_at bumps on save."""
    ticker: str = Field(primary_key=True)
    body: str = Field(default="", max_length=4000)
    updated_at: datetime


class DailyPlan(SQLModel, table=True):
    """One free-form plan row per UTC date — the trader's morning
    intent ("watching $NVDA into earnings, no new shorts this week").
    A new day silently starts a fresh plan; yesterday's stays in the
    table for retrospectives."""
    plan_date: date = Field(primary_key=True)
    body: str = Field(default="", max_length=4000)
    updated_at: datetime
