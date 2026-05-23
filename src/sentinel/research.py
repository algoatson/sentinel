"""Company research lookup.

When the user asks "what does $ARX do?", the post + thread rarely contain the
answer — but the bot can look it up from sources it already has access to:

- EDGAR company facts (issuer name, SIC industry, exchange) via the ticker→CIK
  map + submissions JSON.
- yfinance profile (long business summary, sector, industry, website).
- The bot's own DB (recent filings / news for the name).

This is *retrieved fact* (not the model guessing), so the thread/ask prompts
can safely ground on it — and then build their actual read on top. The facts
themselves don't editorialize; the prompts that consume them are free to.

Profiles are cached in-process (company facts barely move) to keep
interactive replies fast.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import yfinance as yf
from loguru import logger
from sqlmodel import select

from .db import session_scope
from .edgar.client import EdgarClient
from .models import Filing, NewsItem, Watchlist

# symbol -> (profile_dict, fetched_ts)
_CACHE: dict[str, tuple[dict, float]] = {}
_TTL = 24 * 3600


def _yf_symbol(symbol: str) -> str:
    """Best-effort yfinance symbol. Crypto/futures keep their suffix;
    equities translate class-share dots."""
    if symbol.endswith(("-USD", "=F")) or symbol.startswith("^"):
        return symbol
    return symbol.replace(".", "-")


def _from_edgar(symbol: str) -> dict:
    out: dict = {}
    try:
        client = EdgarClient()
        cik = client.get_ticker_to_cik_map().get(symbol.upper())
        if not cik:
            return out
        data = client.get_company_submissions(cik)
        out["name"] = data.get("name")
        out["sic_industry"] = data.get("sicDescription")
        ex = data.get("exchanges") or []
        out["exchange"] = ", ".join(ex) if ex else None
        out["edgar_cik"] = cik
    except Exception as e:
        logger.debug("research EDGAR lookup failed for {}: {}", symbol, e)
    return out


def _from_yfinance(symbol: str) -> dict:
    out: dict = {}
    try:
        info = yf.Ticker(_yf_symbol(symbol)).info or {}
    except Exception as e:
        logger.debug("research yfinance lookup failed for {}: {}", symbol, e)
        return out
    summary = info.get("longBusinessSummary") or info.get("description")
    if summary:
        out["business_summary"] = summary[:1500]
    for k_src, k_dst in (
        ("longName", "name"),
        ("sector", "sector"),
        ("industry", "industry"),
        ("website", "website"),
        ("country", "country"),
        ("quoteType", "quote_type"),
    ):
        v = info.get(k_src)
        if v:
            out[k_dst] = v
    mc = info.get("marketCap")
    if mc:
        out["market_cap"] = mc
    return out


def _from_db(symbol: str) -> dict:
    now = datetime.now(timezone.utc)
    cut = now - timedelta(days=30)
    with session_scope() as s:
        filings = s.exec(
            select(Filing)
            .where(Filing.ticker == symbol)
            .where(Filing.filed_at >= cut)
            .order_by(Filing.filed_at.desc())
            .limit(3)
        ).all()
        news = s.exec(
            select(NewsItem)
            .where(NewsItem.ticker == symbol)
            .where(NewsItem.published_at >= cut)
            .order_by(NewsItem.published_at.desc())
            .limit(3)
        ).all()
        wl = s.exec(
            select(Watchlist).where(Watchlist.ticker == symbol)
        ).first()
    out: dict = {}
    if wl is not None:
        out["asset_class"] = wl.asset_class
    if filings:
        out["recent_filings"] = [
            f"{f.form_type}: {(f.summary or '')[:160]}" for f in filings
        ]
    if news:
        out["recent_news"] = [n.title for n in news]
    return out


def company_profile(symbol: str) -> dict | None:
    """Fused profile for one symbol, or None if nothing was found.

    Network-bound (EDGAR + yfinance); call inside asyncio.to_thread.
    """
    sym = symbol.strip().upper().lstrip("$")
    if not sym:
        return None
    cached = _CACHE.get(sym)
    if cached is not None and (time.time() - cached[1]) < _TTL:
        return cached[0]

    profile: dict = {"symbol": sym}
    is_crypto = sym.endswith("-USD")
    if not is_crypto and not sym.endswith("=F") and not sym.startswith("^"):
        profile.update(_from_edgar(sym))
    profile.update({k: v for k, v in _from_yfinance(sym).items() if v})
    profile.update(_from_db(sym))

    # Need at least an identity or a description to be useful.
    if not any(k in profile for k in ("name", "business_summary", "sic_industry")):
        _CACHE[sym] = ({}, time.time())
        return None
    _CACHE[sym] = (profile, time.time())
    return profile


def profiles_for(symbols: list[str], *, limit: int = 3) -> list[dict]:
    out: list[dict] = []
    for sym in symbols[:limit]:
        p = company_profile(sym)
        if p:
            out.append(p)
    return out
