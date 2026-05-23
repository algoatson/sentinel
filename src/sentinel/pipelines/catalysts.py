"""Catalyst radar — forward-looking scheduled events.

Surfaces *known* dates, not forecasts: monthly/quarterly options expiration
(computed), the jobs-report Friday (computed), fixed macro events from
config/macro_calendar.yaml (FOMC/CPI/etc.), and upcoming earnings dates for
the names that actually matter (held + recently-active, via yfinance).

Pairs with the pre-market briefing ("what happened" ← → "what's coming").
The dates are computed, never guessed — but flagging the setup and risk
around one (earnings into a held name, OPEX pin) is in scope, not withheld.
"""

from __future__ import annotations

import asyncio
import calendar
from datetime import date, datetime, timedelta, timezone

import discord
import yaml
import yfinance as yf
from loguru import logger
from sqlmodel import select

from .. import discord_client
from ..config import CONFIG_DIR, settings
from ..db import session_scope
from ..models import Filing, Holding, RedditMention, Watchlist


_HORIZON_DAYS = 10
_MAX_EARNINGS_TICKERS = 40
_MACRO_PATH = CONFIG_DIR / "macro_calendar.yaml"

# Filled by the daily run so the !catalysts command can answer instantly
# without doing network I/O on the event loop.
_LAST_EARNINGS: list[dict] = []
_LAST_RUN_DATE: date | None = None


def _third_friday(year: int, month: int) -> date:
    fridays = [
        d
        for d in range(1, calendar.monthrange(year, month)[1] + 1)
        if date(year, month, d).weekday() == 4
    ]
    return date(year, month, fridays[2])


def _first_friday(year: int, month: int) -> date:
    for d in range(1, 8):
        if date(year, month, d).weekday() == 4:
            return date(year, month, d)
    return date(year, month, 1)


def _computed_events(today: date, horizon: date) -> list[tuple[date, str]]:
    """OPEX, quad-witching and NFP within [today, horizon]."""
    out: list[tuple[date, str]] = []
    y, m = today.year, today.month
    for _ in range(2):  # this month + next (horizon ≤ ~2 weeks)
        opex = _third_friday(y, m)
        if today <= opex <= horizon:
            quad = m in (3, 6, 9, 12)
            out.append((opex, "Quad-witching OPEX" if quad else "Monthly OPEX"))
        nfp = _first_friday(y, m)
        if today <= nfp <= horizon:
            out.append((nfp, "Jobs report (NFP), 08:30 ET"))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _macro_events(today: date, horizon: date) -> list[tuple[date, str]]:
    if not _MACRO_PATH.exists():
        return []
    try:
        cfg = yaml.safe_load(_MACRO_PATH.read_text()) or {}
    except Exception as e:
        logger.warning("macro_calendar.yaml parse failed: {}", e)
        return []
    out: list[tuple[date, str]] = []
    for ev in cfg.get("events") or []:
        try:
            d = date.fromisoformat(str(ev["date"]))
        except (ValueError, KeyError, TypeError):
            continue
        if today <= d <= horizon:
            out.append((d, str(ev.get("label", "macro event"))))
    return out


def _earnings_universe() -> list[str]:
    """Bounded ticker set worth checking earnings for: held + recently-active.
    yfinance is one network call per ticker, so we cap hard."""
    now = datetime.now(timezone.utc)
    cut_7d = now - timedelta(days=7)
    with session_scope() as s:
        held = {h.ticker for h in s.exec(select(Holding)).all() if h.ticker}
        active_filings = {
            f.ticker
            for f in s.exec(
                select(Filing)
                .where(Filing.filed_at >= cut_7d)
                .where(Filing.ticker.is_not(None))
            ).all()
            if f.ticker
        }
        active_social = {
            t
            for t in s.exec(
                select(RedditMention.ticker)
                .where(RedditMention.created_at >= cut_7d)
                .distinct()
            ).all()
            if t
        }
        equities = {
            w.ticker
            for w in s.exec(
                select(Watchlist)
                .where(Watchlist.asset_class == "equity")
                .where(Watchlist.ticker.is_not(None))
            ).all()
            if w.ticker
        }
    # Only equities have earnings; rank held first.
    ranked = list(held & equities) + sorted(
        (active_filings | active_social) & equities - held
    )
    return ranked[:_MAX_EARNINGS_TICKERS]


def _next_earnings(ticker: str) -> date | None:
    try:
        cal = yf.Ticker(ticker.replace(".", "-")).calendar
    except Exception:
        return None
    if not cal:
        return None
    val = cal.get("Earnings Date") if isinstance(cal, dict) else None
    if not val:
        return None
    dates = val if isinstance(val, (list, tuple)) else [val]
    out: list[date] = []
    for d in dates:
        try:
            out.append(d if isinstance(d, date) else d.date())
        except Exception:
            continue
    today = date.today()
    upcoming = sorted(d for d in out if d >= today)
    return upcoming[0] if upcoming else None


def _fetch_earnings(today: date, horizon: date) -> list[dict]:
    out: list[dict] = []
    for t in _earnings_universe():
        d = _next_earnings(t)
        if d is not None and today <= d <= horizon:
            out.append({"ticker": t, "date": d.isoformat()})
    out.sort(key=lambda r: r["date"])
    return out


def _build_text(today: date, horizon: date, earnings: list[dict]) -> str:
    events = _computed_events(today, horizon) + _macro_events(today, horizon)
    events.sort(key=lambda e: e[0])

    lines = [f"**📅 Catalyst radar — next {_HORIZON_DAYS} days**"]
    if events:
        lines.append("\n**Macro / market structure**")
        for d, label in events:
            dow = d.strftime("%a %m-%d")
            lines.append(f"`{dow}` {label}")
    if earnings:
        lines.append("\n**Earnings (held + active names)**")
        for e in earnings[:25]:
            dd = date.fromisoformat(e["date"]).strftime("%a %m-%d")
            lines.append(f"`{dd}` ${e['ticker']}")
    if not events and not earnings:
        lines.append("\nNothing scheduled in the window.")
    return "\n".join(lines)[:4000]


def catalysts_text() -> str:
    """Instant answer for the !catalysts command — computed events live,
    earnings from the last daily run (network-free)."""
    today = date.today()
    horizon = today + timedelta(days=_HORIZON_DAYS)
    txt = _build_text(today, horizon, _LAST_EARNINGS)
    if _LAST_RUN_DATE != today:
        txt += "\n\n_(earnings list refreshes in the daily radar run)_"
    return txt


def _run_sync() -> tuple[str, list[dict]]:
    today = date.today()
    horizon = today + timedelta(days=_HORIZON_DAYS)
    earnings = _fetch_earnings(today, horizon)
    # Persist so funds/synthesis can read "when does X report" cheaply,
    # off-network and restart-safe (this runs in a worker thread already).
    from ..earnings import upsert_earnings

    upsert_earnings(earnings)
    return _build_text(today, horizon, earnings), earnings


async def run_catalyst_radar() -> None:
    global _LAST_EARNINGS, _LAST_RUN_DATE
    try:
        text, earnings = await asyncio.to_thread(_run_sync)
        _LAST_EARNINGS = earnings
        _LAST_RUN_DATE = date.today()

        embed = discord.Embed(
            title="📅 Catalyst radar",
            description=text,
            color=0x3498DB,
        )
        # Prefer the dedicated #catalysts channel — a forward calendar wants
        # a persistent home users can scroll, not a single daily blip in
        # #digest. Falls through #digest → #news → #pulse if not configured.
        channel = (
            settings.DISCORD_CATALYSTS_CHANNEL_ID
            or settings.DISCORD_DIGEST_CHANNEL_ID
            or settings.DISCORD_NEWS_CHANNEL_ID
            or settings.DISCORD_PULSE_CHANNEL_ID
        )
        if await discord_client.post_embed(channel, embed, importance=2):
            logger.info(
                "catalysts: posted ({} earnings in window)", len(earnings)
            )
    except Exception as e:
        logger.exception("run_catalyst_radar top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ catalyst radar error: {e}")
        except Exception:
            pass
