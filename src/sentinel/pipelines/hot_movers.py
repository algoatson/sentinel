"""Hot movers — watchlist names moving NOW on real volume.

Scans `PriceContext` for watchlist tickers whose **absolute 1d change**
clears a threshold AND whose **volume vs 20d-avg** clears another. Posts a
single curated embed to `#hot` listing the top N, with a per-ticker
cooldown so a sustained mover doesn't respam.

This is intentionally NOT the same as `movers.py`:
- `movers.py` posts LLM-narrated hypotheses for *unexplained* movers (no
  filing trigger in the last 6h) — slower cadence, narrative output.
- `hot_movers.py` is a fast, terse "what's moving on the watchlist right
  now" feed — no LLM call, just structured data, cheap.

Posts only when `DISCORD_HOT_CHANNEL_ID` is set. Skips quietly outside
market hours so the channel doesn't churn with stale overnight prints.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from loguru import logger
from sqlmodel import select

from .. import discord_client
from ..config import settings
from ..db import session_scope
from ..models import Filing, NewsItem, PriceContext, Watchlist


# Thresholds — tuned so the channel surfaces *meaningful* moves, not noise.
# An "either" pair (4% with any volume, or 2% with ≥1.8× vol) catches both
# big news jolts and grinding accumulation. Both are interesting.
_PCT_LARGE = 0.04        # 4% absolute 1d move (any volume)
_PCT_SMALL = 0.02        # 2% absolute 1d move (requires volume kicker)
_VOL_KICKER = 1.8        # 1.8× 20d average volume

_TOP_N = 6               # max movers in one embed (longer = wall-of-text)
_COOLDOWN_HOURS = 4      # per-ticker cooldown to prevent respam

# In-memory cooldown table. The bot restart resets this — acceptable: a
# restart re-posts each mover at most once, well under the spam threshold,
# and avoids a dedicated dedup table just for this lightweight surface.
_LAST_POSTED: dict[str, datetime] = {}

# US market hours (NYSE: 09:30–16:00 ET). Crypto-only tickers (asset_class
# crypto) bypass the gate so 24/7 names can still surface.
_ET = ZoneInfo("America/New_York")
_OPEN_ET = time(9, 30)
_CLOSE_ET = time(16, 0)


def _market_open_now(now_utc: datetime | None = None) -> bool:
    now = (now_utc or datetime.now(timezone.utc)).astimezone(_ET)
    if now.weekday() >= 5:  # sat/sun
        return False
    return _OPEN_ET <= now.time() <= _CLOSE_ET


def _is_hot(pc: PriceContext) -> bool:
    abs_pct = abs(pc.change_1d_pct or 0.0)
    vol = pc.volume_vs_20d_avg or 0.0
    if abs_pct >= _PCT_LARGE:
        return True
    if abs_pct >= _PCT_SMALL and vol >= _VOL_KICKER:
        return True
    return False


def _cooldown_ok(ticker: str, now: datetime) -> bool:
    last = _LAST_POSTED.get(ticker)
    return last is None or (now - last) >= timedelta(hours=_COOLDOWN_HOURS)


def _scan() -> list[dict]:
    """Pull the hot list — watchlist members whose PriceContext is moving
    AND whose ticker is past cooldown. Sorted by abs(1d) × volume score so
    "big move on big volume" sits at the top."""
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    with session_scope() as s:
        watchlist = {
            w.ticker: w.asset_class
            for w in s.exec(select(Watchlist)).all()
            if w.ticker
        }
        if not watchlist:
            return []

        for pc in s.exec(select(PriceContext)).all():
            if pc.ticker not in watchlist:
                continue
            if not _is_hot(pc):
                continue
            if not _cooldown_ok(pc.ticker, now):
                continue
            # Crypto bypasses the market-hours gate (24/7 names).
            asset = (watchlist.get(pc.ticker) or "").lower()
            if not (asset == "crypto" or _market_open_now(now)):
                continue

            # Light-weight context: is there a recent filing/news on this
            # ticker to flag in the embed? One row each, not full details.
            since = (now - timedelta(hours=12)).replace(tzinfo=None)
            had_filing = bool(s.exec(
                select(Filing)
                .where(Filing.ticker == pc.ticker)
                .where(Filing.filed_at >= since)
                .limit(1)
            ).first())
            had_news = bool(s.exec(
                select(NewsItem)
                .where(NewsItem.ticker == pc.ticker)
                .where(NewsItem.published_at >= since)
                .limit(1)
            ).first())

            change_pct = (pc.change_1d_pct or 0.0) * 100
            vol_ratio = pc.volume_vs_20d_avg or 0.0
            out.append({
                "ticker": pc.ticker,
                "asset_class": asset or "equity",
                "last_price": pc.last_price,
                "change_1d_pct": round(change_pct, 2),
                "change_5d_pct": round((pc.change_5d_pct or 0) * 100, 2),
                "vol_ratio": round(vol_ratio, 2),
                "had_filing": had_filing,
                "had_news": had_news,
                # Score: |1d move| weighted by volume. Stronger moves on
                # higher volume sit at the top of the embed.
                "_score": abs(change_pct) * (1.0 + vol_ratio / 2.0),
            })

    out.sort(key=lambda r: r["_score"], reverse=True)
    return out[:_TOP_N]


def _line(r: dict) -> str:
    """One row in the embed body — terse, fixed-width style."""
    arrow = "🟢" if r["change_1d_pct"] > 0 else "🔴"
    flags = []
    if r["had_filing"]:
        flags.append("📑")
    if r["had_news"]:
        flags.append("📰")
    flag_str = " " + "".join(flags) if flags else ""
    px = f"${r['last_price']:.4g}" if r["last_price"] else "—"
    return (
        f"{arrow} **${r['ticker']}** {px} · "
        f"{r['change_1d_pct']:+.2f}% (1d) · "
        f"×{r['vol_ratio']:.2f} vol{flag_str}"
    )


async def run_hot_movers() -> None:
    """Scheduler entry point. No-op if the dedicated channel isn't set."""
    chan = settings.DISCORD_HOT_CHANNEL_ID
    if not chan:
        logger.debug("hot_movers: DISCORD_HOT_CHANNEL_ID unset, skipping")
        return

    try:
        rows = _scan()
    except Exception as e:
        logger.exception("hot_movers scan failed: {}", e)
        return
    if not rows:
        return

    now = datetime.now(timezone.utc)
    up = sum(1 for r in rows if r["change_1d_pct"] > 0)
    down = len(rows) - up
    body = "\n".join(_line(r) for r in rows)

    embed = discord.Embed(
        title=f"🔥 Hot — {up}↑ · {down}↓",
        description=body[:3500],
        color=0xF59E0B,
    )
    embed.set_footer(
        text="📑 recent filing within 12h · 📰 recent news within 12h"
    )

    if await discord_client.post_embed(chan, embed, importance=2):
        for r in rows:
            _LAST_POSTED[r["ticker"]] = now
        logger.info(
            "hot_movers: posted {} entries ({}↑ {}↓)", len(rows), up, down
        )
