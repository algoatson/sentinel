"""Enrichment pipeline per SPEC §7.

Pure DB query — no LLM call, no network. Produces an EnrichmentContext that
flows into both the materiality scorer (as JSON) and the Discord embed footer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlmodel import func, select

from ..db import session_scope
from ..models import Filing, HnMention, NewsItem, PriceContext, RedditMention


@dataclass
class EnrichmentContext:
    reddit_mentions_24h: int = 0
    reddit_mentions_baseline: float = 0.0
    reddit_top_titles: list[str] = field(default_factory=list)
    reddit_avg_sentiment: Optional[float] = None
    hn_mentions_24h: int = 0
    hn_top_title: Optional[str] = None
    news_24h: int = 0
    news_top_titles: list[str] = field(default_factory=list)
    news_avg_impact_1h: Optional[float] = None
    news_avg_impact_1d: Optional[float] = None
    price_change_1d_pct: Optional[float] = None
    volume_ratio: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


def enrich(filing: Filing) -> EnrichmentContext:
    """Build an EnrichmentContext for the given filing's ticker.

    Returns a defaults-filled context (zero counts, None prices) if the filing
    has no ticker or no related ingester data — never raises.
    """
    try:
        return _enrich(filing)
    except Exception as e:
        logger.warning("enrich({}) failed: {}", filing.accession_number, e)
        return EnrichmentContext()


def _enrich(filing: Filing) -> EnrichmentContext:
    ctx = EnrichmentContext()
    ticker = filing.ticker
    if not ticker:
        return ctx

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    with session_scope() as session:
        # Reddit: 24h count + 7d baseline (per day) + top titles + avg sentiment.
        reddit_24h = session.exec(
            select(RedditMention).where(
                RedditMention.ticker == ticker,
                RedditMention.created_at >= cutoff_24h,
            )
        ).all()
        ctx.reddit_mentions_24h = len(reddit_24h)

        baseline_count = session.exec(
            select(func.count()).select_from(RedditMention).where(
                RedditMention.ticker == ticker,
                RedditMention.created_at >= cutoff_7d,
                RedditMention.created_at < cutoff_24h,
            )
        ).one()
        # 6-day window before the last 24h, normalize to mentions/day.
        ctx.reddit_mentions_baseline = float(baseline_count or 0) / 6.0

        top = sorted(reddit_24h, key=lambda r: (r.score or 0), reverse=True)[:5]
        ctx.reddit_top_titles = [t.title for t in top if t.title]

        sents = [r.sentiment for r in reddit_24h if r.sentiment is not None]
        if sents:
            ctx.reddit_avg_sentiment = sum(sents) / len(sents)

        # HN: 24h count + top title.
        hn_rows = session.exec(
            select(HnMention).where(
                HnMention.ticker == ticker,
                HnMention.created_at >= cutoff_24h,
            )
        ).all()
        ctx.hn_mentions_24h = len(hn_rows)
        if hn_rows:
            top_hn = max(hn_rows, key=lambda h: (h.points or 0))
            ctx.hn_top_title = top_hn.title

        # News (ticker-tagged): per-ticker yfinance + RSS stories that
        # extracted to this ticker.
        news_rows = session.exec(
            select(NewsItem)
            .where(NewsItem.ticker == ticker)
            .where(NewsItem.published_at >= cutoff_24h)
            .order_by(NewsItem.published_at.desc())
            .limit(10)
        ).all()
        ctx.news_24h = len(news_rows)
        ctx.news_top_titles = [n.title for n in news_rows[:3] if n.title]

        # Average measured news impact across last 14d (broader window so we
        # have signal even if today's news hasn't been tagged yet).
        impact_rows = session.exec(
            select(NewsItem)
            .where(NewsItem.ticker == ticker)
            .where(NewsItem.published_at >= now - timedelta(days=14))
            .where(NewsItem.impact_1h_pct.is_not(None))
        ).all()
        if impact_rows:
            i1h = [r.impact_1h_pct for r in impact_rows if r.impact_1h_pct is not None]
            i1d = [r.impact_1d_pct for r in impact_rows if r.impact_1d_pct is not None]
            if i1h:
                ctx.news_avg_impact_1h = sum(i1h) / len(i1h)
            if i1d:
                ctx.news_avg_impact_1d = sum(i1d) / len(i1d)

        # Price context — already aggregated by the prices ingester.
        pc = session.get(PriceContext, ticker)
        if pc is not None:
            ctx.price_change_1d_pct = pc.change_1d_pct
            ctx.volume_ratio = pc.volume_vs_20d_avg

    return ctx


def render_footer(ctx: EnrichmentContext) -> str:
    """Compact one-line footer for the Discord embed."""
    parts: list[str] = []
    if ctx.price_change_1d_pct is not None:
        pct = ctx.price_change_1d_pct * 100
        vol = ctx.volume_ratio or 0.0
        parts.append(f"📊 Price: {pct:+.1f}% on {vol:.1f}x volume")
    if ctx.reddit_mentions_24h or ctx.reddit_mentions_baseline:
        baseline = ctx.reddit_mentions_baseline
        if baseline > 0:
            parts.append(
                f"💬 Reddit: {ctx.reddit_mentions_24h} (baseline {baseline:.1f}/d)"
            )
        else:
            parts.append(f"💬 Reddit: {ctx.reddit_mentions_24h} mentions")
    if ctx.hn_mentions_24h:
        parts.append(f"HN: {ctx.hn_mentions_24h} story" + ("s" if ctx.hn_mentions_24h != 1 else ""))
    if ctx.news_24h:
        news_part = f"📰 News: {ctx.news_24h}"
        if ctx.news_avg_impact_1d is not None:
            news_part += f" (avg 1d {ctx.news_avg_impact_1d * 100:+.1f}%)"
        parts.append(news_part)
    return " · ".join(parts)
