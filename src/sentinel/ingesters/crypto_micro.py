"""Crypto microstructure — funding, open interest, orderbook imbalance.

The one place free data genuinely beats Yahoo: perpetual-swap funding rate,
open interest and its 24h drift, and spot orderbook imbalance. This is the
context that explains *why* a coin ripped — "funding flipped deeply negative
while price rose" is a squeeze tell. It feeds why_moved evidence, the
synthesis snapshot and the !ticker dossier.

This module only stores the raw feed; why_moved and synthesis are what turn
it into the call.

Sources: Binance public API primary; OKX public fallback when Binance is
geo-blocked (HTTP 451/403 from some hosts). Per-ticker failures are skipped,
never fatal. Bounded to a curated subset to stay polite.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
from loguru import logger
from sqlmodel import select

from .. import discord_client
from ..db import session_scope
from ..models import CryptoMicro, Holding, RedditMention, Watchlist


_UA = {"User-Agent": "sentinel/0.1"}
_MAX_TICKERS = 25
_BINANCE_FAPI = "https://fapi.binance.com"
_BINANCE_API = "https://api.binance.com"
_OKX = "https://www.okx.com"


async def poll_crypto_micro() -> None:
    try:
        await asyncio.to_thread(_run)
    except Exception as e:
        logger.exception("poll_crypto_micro top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ crypto micro error: {e}")
        except Exception:
            pass


def _targets() -> list[str]:
    """Curated subset: held + buzzed + first N crypto watchlist names."""
    with session_scope() as s:
        crypto = [
            w.ticker
            for w in s.exec(
                select(Watchlist)
                .where(Watchlist.asset_class == "crypto")
                .where(Watchlist.ticker.is_not(None))
            ).all()
            if w.ticker
        ]
        held = {h.ticker for h in s.exec(select(Holding)).all() if h.ticker}
        buzzed = set(
            s.exec(
                select(RedditMention.ticker)
                .where(RedditMention.ticker.in_(crypto))
                .distinct()
            ).all()
        )
    ranked = [t for t in crypto if t in held]
    ranked += [t for t in crypto if t in buzzed and t not in ranked]
    ranked += [t for t in crypto if t not in ranked]
    return ranked[:_MAX_TICKERS]


def _base(ticker: str) -> str:
    return ticker.split("-")[0].upper()


# ---- Binance ---------------------------------------------------------------


def _binance(base: str, client: httpx.Client) -> dict | None:
    sym = f"{base}USDT"
    try:
        pi = client.get(
            f"{_BINANCE_FAPI}/fapi/v1/premiumIndex", params={"symbol": sym}
        )
        if pi.status_code in (403, 451):
            raise _GeoBlocked()
        if pi.status_code != 200:
            return None
        funding = float(pi.json().get("lastFundingRate") or 0.0)

        oi_now = client.get(
            f"{_BINANCE_FAPI}/fapi/v1/openInterest", params={"symbol": sym}
        )
        open_interest = (
            float(oi_now.json().get("openInterest"))
            if oi_now.status_code == 200
            else None
        )

        oi_chg = None
        hist = client.get(
            f"{_BINANCE_FAPI}/futures/data/openInterestHist",
            params={"symbol": sym, "period": "1d", "limit": 2},
        )
        if hist.status_code == 200:
            rows = hist.json()
            if len(rows) == 2:
                old = float(rows[0].get("sumOpenInterest") or 0)
                new = float(rows[1].get("sumOpenInterest") or 0)
                if old:
                    oi_chg = (new - old) / old

        depth = client.get(
            f"{_BINANCE_API}/api/v3/depth", params={"symbol": sym, "limit": 100}
        )
        imbalance = _imbalance_from(depth.json()) if depth.status_code == 200 else None
    except _GeoBlocked:
        raise
    except Exception as e:
        logger.debug("binance micro failed for {}: {}", base, e)
        return None
    return {
        "venue": "binance",
        "funding_rate": funding,
        "open_interest": open_interest,
        "oi_change_24h_pct": oi_chg,
        "orderbook_imbalance": imbalance,
    }


# ---- OKX fallback ----------------------------------------------------------


def _okx(base: str, client: httpx.Client) -> dict | None:
    swap = f"{base}-USDT-SWAP"
    spot = f"{base}-USDT"
    try:
        fr = client.get(
            f"{_OKX}/api/v5/public/funding-rate", params={"instId": swap}
        )
        funding = None
        if fr.status_code == 200 and fr.json().get("data"):
            funding = float(fr.json()["data"][0].get("fundingRate") or 0.0)

        oi = client.get(
            f"{_OKX}/api/v5/public/open-interest", params={"instId": swap}
        )
        open_interest = None
        if oi.status_code == 200 and oi.json().get("data"):
            open_interest = float(oi.json()["data"][0].get("oi") or 0.0)

        bk = client.get(
            f"{_OKX}/api/v5/market/books", params={"instId": spot, "sz": 50}
        )
        imbalance = None
        if bk.status_code == 200 and bk.json().get("data"):
            d = bk.json()["data"][0]
            imbalance = _imbalance_from(
                {"bids": d.get("bids", []), "asks": d.get("asks", [])}
            )
    except Exception as e:
        logger.debug("okx micro failed for {}: {}", base, e)
        return None
    if funding is None and open_interest is None and imbalance is None:
        return None
    return {
        "venue": "okx",
        "funding_rate": funding,
        "open_interest": open_interest,
        "oi_change_24h_pct": None,
        "orderbook_imbalance": imbalance,
    }


class _GeoBlocked(Exception):
    pass


def _imbalance_from(book: dict) -> float | None:
    try:
        bid = sum(float(b[1]) for b in book.get("bids", [])[:100])
        ask = sum(float(a[1]) for a in book.get("asks", [])[:100])
    except (TypeError, ValueError, IndexError):
        return None
    tot = bid + ask
    if tot <= 0:
        return None
    return round((bid - ask) / tot, 4)  # +1 bid-heavy … -1 ask-heavy


def _run() -> None:
    targets = _targets()
    if not targets:
        logger.info("crypto micro: no crypto watchlist")
        return

    use_okx = False
    n = 0
    with httpx.Client(headers=_UA, timeout=12.0, follow_redirects=True) as client:
        for ticker in targets:
            base = _base(ticker)
            data = None
            if not use_okx:
                try:
                    data = _binance(base, client)
                except _GeoBlocked:
                    logger.warning(
                        "crypto micro: Binance geo-blocked, switching to OKX"
                    )
                    use_okx = True
            if data is None:
                data = _okx(base, client)
            if data is None:
                continue

            with session_scope() as s:
                row = s.get(CryptoMicro, ticker)
                if row is None:
                    row = CryptoMicro(ticker=ticker, venue=data["venue"],
                                      updated_at=datetime.now(timezone.utc))
                row.venue = data["venue"]
                row.funding_rate = data["funding_rate"]
                row.open_interest = data["open_interest"]
                row.oi_change_24h_pct = data["oi_change_24h_pct"]
                row.orderbook_imbalance = data["orderbook_imbalance"]
                row.updated_at = datetime.now(timezone.utc)
                s.add(row)
            n += 1

    logger.info("crypto micro: updated {}/{} tickers", n, len(targets))


def micro_for(ticker: str) -> dict | None:
    """Latest microstructure for a ticker as a compact dict, or None."""
    with session_scope() as s:
        row = s.get(CryptoMicro, ticker)
    if row is None:
        return None
    out: dict = {"venue": row.venue}
    if row.funding_rate is not None:
        out["funding_rate_pct"] = round(row.funding_rate * 100, 4)
    if row.open_interest is not None:
        out["open_interest"] = row.open_interest
    if row.oi_change_24h_pct is not None:
        out["oi_change_24h_pct"] = round(row.oi_change_24h_pct * 100, 2)
    if row.orderbook_imbalance is not None:
        out["orderbook_imbalance"] = row.orderbook_imbalance
    return out if len(out) > 1 else None
