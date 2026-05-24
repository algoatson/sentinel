"""CoinGecko trending discovery.

Free, keyless endpoint: the top search-trending coins over the last ~24h.
This is the autonomous-discovery arm for crypto — the bot promotes whatever
the market is actually looking at into the watchlist for a short window
(14 days), so price/news coverage follows attention instead of a static list.

Promoted rows are `source="crypto_trending"`, `asset_class="crypto"`, and
expire — same lifecycle as equity activity-promotion. yfinance silently
returns nothing for coins it doesn't carry; that's fine, they just age out.

This arm only decides what gets *tracked* (attention → coverage); the trade
read on those names comes from synthesis / why_moved downstream.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import discord
import httpx
from loguru import logger
from sqlmodel import select

from .. import discord_client
from ..db import session_scope
from ..edgar.watchlist_builder import _synthetic_cik
from ..models import Watchlist
from . import prices


_URL = "https://api.coingecko.com/api/v3/search/trending"
_PROMOTE_DAYS = 14
_SOURCE = "crypto_trending"


async def poll_crypto_trending() -> None:
    try:
        new_syms = await asyncio.to_thread(_run)
    except Exception as e:
        logger.exception("poll_crypto_trending top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ crypto trending error: {e}")
        except Exception:
            pass
        return

    if new_syms:
        await _announce(new_syms)


def _run() -> list[str]:
    """Fetch trending coins, upsert them as expiring watchlist rows. Returns
    the symbols newly added this cycle (for the Discord announcement)."""
    try:
        r = httpx.get(_URL, timeout=20.0, headers={"User-Agent": "sentinel/0.1"})
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        logger.warning("coingecko trending fetch failed: {}", e)
        return []

    coins = payload.get("coins") or []
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=_PROMOTE_DAYS)

    newly: list[str] = []
    rejected_no_yf: list[str] = []
    with session_scope() as session:
        for entry in coins:
            item = entry.get("item") or {}
            sym = str(item.get("symbol") or "").upper().strip()
            if not sym or not sym.isalnum():
                continue
            ticker = f"{sym}-USD"
            cik = _synthetic_cik(ticker)
            existing = session.exec(
                select(Watchlist)
                .where(Watchlist.cik == cik)
                .where(Watchlist.source == _SOURCE)
            ).first()
            if existing is not None:
                existing.expires_at = expires  # refresh the window
                session.add(existing)
                continue
            # Skip if already covered by the curated crypto list.
            curated = session.exec(
                select(Watchlist)
                .where(Watchlist.ticker == ticker)
                .where(Watchlist.source == "crypto")
            ).first()
            if curated is not None:
                continue
            # Verify-before-promote: yfinance has thin long-tail crypto
            # coverage (PENGU, VVV, HYPE etc. don't price), and the
            # downstream strike-prune only kicks in after 3 empty cycles
            # → the channel spams the user with names the bot can't
            # actually price. `prices.can_price` does a single cached
            # probe (1s on cache-miss, free on hit; negative results
            # cached 7d so the same dead token doesn't get re-probed
            # on every trending poll).
            if not prices.can_price(ticker, "crypto"):
                rejected_no_yf.append(sym)
                continue
            session.add(
                Watchlist(
                    cik=cik,
                    ticker=ticker,
                    source=_SOURCE,
                    asset_class="crypto",
                    added_at=now,
                    expires_at=expires,
                )
            )
            newly.append(sym)
        if rejected_no_yf:
            logger.info(
                "crypto trending: {} rejected (no yfinance data): {}",
                len(rejected_no_yf), ", ".join(rejected_no_yf[:12]),
            )

        # Expire stale trending promotions.
        for row in session.exec(
            select(Watchlist)
            .where(Watchlist.source == _SOURCE)
            .where(Watchlist.expires_at.is_not(None))
            .where(Watchlist.expires_at < now)
        ).all():
            session.delete(row)

    logger.info(
        "crypto trending: {} coins seen, {} newly promoted", len(coins), len(newly)
    )
    return newly


async def _announce(symbols: list[str]) -> None:
    from ..routing import crypto_channel

    channel = crypto_channel()
    listed = ", ".join(f"${s}" for s in symbols[:15])
    embed = discord.Embed(
        title="📈 Trending on CoinGecko — now tracked (14d)",
        description=(
            f"Newly surfaced by search interest: {listed}\n\n"
            "Price + news coverage starts now and ages out automatically. "
            "Now in the analysis loop like any other name."
        )[:4000],
        color=0xF1C40F,
    )
    await discord_client.post_embed(channel, embed, importance=2)
