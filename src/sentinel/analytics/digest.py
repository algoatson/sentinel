"""Today's pulse — a quick "what happened" rollup.

Aggregated across every wallet + ingester, designed for the
Overview's "Today" strip. One call returns everything for the
current UTC day; the panel is dense info on a single horizontal
row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from ..db import session_scope
from ..models import Filing, FundTrade, NewsItem, RedditMention, TradingCall


def today_pulse() -> dict:
    """One-row summary of activity in the rolling last 24h.

    Returns:
      news_count, calls_count, filings_count, reddit_count,
      trades_opened, trades_closed, realized_today (sum of pnl),
      best_close {ticker, pnl}, worst_close {ticker, pnl},
      highest_conviction_call {ticker, conv, source},
      top_material_filing {ticker, form_type, score}
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    cutoff_naive = cutoff.replace(tzinfo=None)

    with session_scope() as s:
        news = s.exec(
            select(NewsItem).where(NewsItem.published_at >= cutoff_naive)
        ).all()
        calls = s.exec(
            select(TradingCall).where(TradingCall.created_at >= cutoff_naive)
        ).all()
        filings = s.exec(
            select(Filing).where(Filing.filed_at >= cutoff_naive)
        ).all()
        reddit = s.exec(
            select(RedditMention).where(RedditMention.created_at >= cutoff_naive)
        ).all()
        opens = s.exec(
            select(FundTrade)
            .where(FundTrade.entry_at >= cutoff_naive)
        ).all()
        closes = s.exec(
            select(FundTrade)
            .where(FundTrade.status == "closed")
            .where(FundTrade.exit_at >= cutoff_naive)
        ).all()

    realized = sum((c.realized_pnl or 0.0) for c in closes)
    best = max(closes, key=lambda t: t.realized_pnl or 0.0, default=None)
    worst = min(closes, key=lambda t: t.realized_pnl or 0.0, default=None)
    top_call = max(calls, key=lambda c: c.conviction, default=None)
    top_filing = max(
        (f for f in filings if f.materiality_score is not None),
        key=lambda f: f.materiality_score or 0,
        default=None,
    )

    return {
        "as_of": now.isoformat(),
        "window_hours": 24,
        "news_count": len(news),
        "calls_count": len(calls),
        "filings_count": len(filings),
        "reddit_count": len(reddit),
        "trades_opened": len(opens),
        "trades_closed": len(closes),
        "realized_today": round(realized, 2),
        "best_close": (
            {
                "ticker": best.ticker,
                "side": best.side,
                "pnl": round(best.realized_pnl or 0.0, 2),
            } if best else None
        ),
        "worst_close": (
            {
                "ticker": worst.ticker,
                "side": worst.side,
                "pnl": round(worst.realized_pnl or 0.0, 2),
            } if worst else None
        ),
        "highest_conviction_call": (
            {
                "ticker": top_call.ticker,
                "direction": top_call.direction,
                "conviction": top_call.conviction,
                "source": top_call.source,
            } if top_call else None
        ),
        "top_material_filing": (
            {
                "ticker": top_filing.ticker,
                "form_type": top_filing.form_type,
                "materiality_score": top_filing.materiality_score,
            } if top_filing else None
        ),
    }
