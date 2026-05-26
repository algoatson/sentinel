"""News + filings mentioning currently-held tickers.

A position-aware feed: the trader holds $NVDA in `swing` and $AMD in
`degen` — show them every story in the last 24h that mentions either,
with the holding wallet badged on the row. Surfaces "something
happened to my book" without grepping the global news list.

Read-only. Reuses funds.open_positions_all() (same enriched payload
already cached for /book + Risk Monitor + Earnings Exposure) and one
news / filings query per call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlmodel import select

from .. import funds as _funds
from ..db import session_scope
from ..models import Filing, NewsItem


def _aware_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (
        dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    ).isoformat()


def holdings_news(hours: int = 24, limit: int = 30) -> dict:
    """News + filings from the last `hours` hours touching any
    currently-held ticker.

    Returns:
      tickers: list[str] — the held tickers we looked up
      holdings_by_ticker: {ticker: [funds]}
      news: list[{id, ticker, title, url, source, ts, sentiment,
                  funds, tickers, impact_1d_pct}]
      filings: list[{id, ticker, form_type, filed_at, url,
                     materiality_score, funds}]
    """
    rows = _funds.open_positions_all()
    if not rows:
        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "window_hours": hours,
            "tickers": [],
            "holdings_by_ticker": {},
            "news": [],
            "filings": [],
        }

    holdings_by_ticker: dict[str, list[str]] = {}
    for r in rows:
        holdings_by_ticker.setdefault(r["ticker"].upper(), []).append(r["fund"])
    # Sort fund names per ticker for deterministic output.
    holdings_by_ticker = {
        t: sorted(set(fs)) for t, fs in holdings_by_ticker.items()
    }
    tickers = sorted(holdings_by_ticker.keys())

    cutoff_naive = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).replace(tzinfo=None)

    news_out: list[dict] = []
    filings_out: list[dict] = []

    with session_scope() as s:
        # NewsItem: primary ticker match OR substring match in
        # tickers_csv (which stores ",NVDA,AMD," for multi-ticker
        # stories — using leading+trailing commas means LIKE
        # '%,X,%' is safe for single-name lookups).
        news_clauses = [NewsItem.ticker.in_(tickers)] + [
            NewsItem.tickers_csv.contains(f",{t},") for t in tickers
        ]
        news_q = (
            select(NewsItem)
            .where(NewsItem.published_at >= cutoff_naive)
            .where(or_(*news_clauses))
            .order_by(NewsItem.published_at.desc())
            .limit(max(1, min(limit, 200)))
        )
        for n in s.exec(news_q).all():
            # Resolve which held tickers this article touches (so we
            # can render multiple fund badges per row if needed).
            cs = (n.tickers_csv or "").strip(",")
            full = [t.strip() for t in cs.split(",") if t.strip()]
            if n.ticker and n.ticker not in full:
                full.append(n.ticker)
            mine = [t for t in full if t in tickers] or (
                [n.ticker] if n.ticker in tickers else []
            )
            funds: list[str] = []
            for t in mine:
                funds.extend(holdings_by_ticker.get(t, []))
            funds = sorted(set(funds))
            news_out.append({
                "id": n.id,
                "ticker": n.ticker,
                "title": n.title,
                "url": n.url,
                "source": n.source,
                "ts": _aware_iso(n.published_at),
                "sentiment": n.sentiment,
                "impact_1d_pct": n.impact_1d_pct,
                "tickers": full,
                "held_tickers": mine,
                "funds": funds,
            })

        filings_q = (
            select(Filing)
            .where(Filing.filed_at >= cutoff_naive)
            .where(Filing.ticker.in_(tickers))
            .order_by(Filing.filed_at.desc())
            .limit(max(1, min(limit, 200)))
        )
        for f in s.exec(filings_q).all():
            tk = (f.ticker or "").upper()
            filings_out.append({
                "id": f.id,
                "ticker": tk,
                "form_type": f.form_type,
                "filed_at": _aware_iso(f.filed_at),
                "url": f.primary_doc_url,
                "materiality_score": getattr(f, "materiality_score", None),
                "funds": holdings_by_ticker.get(tk, []),
            })

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "window_hours": hours,
        "tickers": tickers,
        "holdings_by_ticker": holdings_by_ticker,
        "news": news_out,
        "filings": filings_out,
    }
