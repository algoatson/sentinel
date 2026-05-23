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
