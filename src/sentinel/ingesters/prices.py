"""Prices ingester per SPEC §7.

Two jobs:

- `poll_prices()` — intraday 1-min bars during market hours. Inserts new bars
  into PriceBar and recomputes PriceContext per ticker.
- `poll_daily_bars()` — once daily at 17:00 ET. Pulls 30d of daily bars to
  keep the 5d/20d aggregates in PriceContext current.

Per-ticker failures are logged and skipped; the cycle never raises.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import pandas as pd
import pandas_market_calendars as mcal
import yfinance as yf
from loguru import logger
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import select

from .. import discord_client
from ..db import session_scope
from ..models import PriceBar, PriceContext, Watchlist


# yfinance prints "$X: possibly delisted" / "N Failed downloads" through its
# own logger straight to the console — pure noise for symbols we already
# handle (dead crypto auto-pruned below). Mute it; real failures still surface
# via our own logger.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


_NYSE = mcal.get_calendar("XNYS")
_BATCH_SIZE = 50


# Asset classes that trade around the clock — polled even when NYSE is shut.
_ALWAYS_ON = frozenset({"crypto", "future"})

# Yahoo carries some coins under disambiguated symbols (numeric id suffix, or
# post-rebrand name). We keep the clean canonical ticker everywhere (so
# Reddit "$PEPE" aliasing, display and news all work) and only translate at
# the fetch boundary. Verified against yfinance; revisit if Yahoo renumbers.
_CRYPTO_YF_OVERRIDES = {
    "PEPE-USD": "PEPE24478-USD",
    "UNI-USD": "UNI7083-USD",
    "TAO-USD": "TAO22974-USD",
    "GRT-USD": "GRT6719-USD",
    "POL-USD": "POL28321-USD",
}

# Auto-prune: crypto_trending promotes whatever CoinGecko is buzzing, much of
# which Yahoo doesn't price. Strike a ticker each empty cycle; drop the
# promotion after this many consecutive strikes so the noise self-heals.
_PRUNABLE_SOURCES = frozenset({"crypto_trending", "activity"})
_MAX_EMPTY_STRIKES = 3
_EMPTY_STRIKES: dict[str, int] = {}


# Probe cache for `can_price`. We avoid hammering yfinance for the same
# dead token over and over. Positive answers live for the process
# lifetime (a tradable name doesn't un-trade in our window); negative
# answers expire after _NEGATIVE_PROBE_TTL so a newly-listed coin gets
# a second chance after a week without us paying for the lookup daily.
_NEGATIVE_PROBE_TTL = timedelta(days=7)
_CAN_PRICE_CACHE: dict[str, tuple[bool, datetime]] = {}


def can_price(ticker: str, asset_class: str = "equity") -> bool:
    """Best-effort 'does yfinance actually return data for this ticker'.

    Used by promoters (crypto_trending especially) to avoid admitting
    tickers that the price ingester will silently strike for the next
    week — which spams the #crypto channel with names the bot can't
    actually price (PENGU, VVV, HYPE — those are real long-tail tokens
    yfinance doesn't carry).

    Cached so repeat calls within the same process don't re-probe; a
    negative result lives for `_NEGATIVE_PROBE_TTL` so a fresh listing
    eventually gets reprobed. ~1s per fresh probe, near-zero on hit.
    Errors (timeout, rate-limit, weird shape) read as 'False' — being
    wrong on the optimistic side leaves the bot in the broken state
    we're trying to fix.
    """
    key = ticker
    now = datetime.now(timezone.utc)
    cached = _CAN_PRICE_CACHE.get(key)
    if cached is not None:
        value, ts = cached
        if value or (now - ts) < _NEGATIVE_PROBE_TTL:
            return value
    yf_sym = _to_yfinance(ticker, asset_class)
    ok = False
    try:
        import yfinance as yf
        info = yf.Ticker(yf_sym).history(period="5d", interval="1d")
        ok = info is not None and not info.empty
    except Exception as e:
        logger.debug("can_price probe failed for {} ({}): {}",
                     ticker, yf_sym, e)
        ok = False
    _CAN_PRICE_CACHE[key] = (ok, now)
    return ok


def _to_yfinance(ticker: str, asset_class: str = "equity") -> str:
    """Map a watchlist ticker to its yfinance symbol.

    Equities: yfinance uses dashes for class shares (BRK-B) while the
    watchlist stores Wikipedia-style dots (BRK.B). Crypto may need a Yahoo
    disambiguation override. Futures (ES=F) and rate indices (^TNX) are
    already yfinance-native.
    """
    if asset_class == "equity":
        return ticker.replace(".", "-")
    if asset_class == "crypto":
        return _CRYPTO_YF_OVERRIDES.get(ticker, ticker)
    return ticker


def _is_market_open_now() -> bool:
    today = date.today()
    sched = _NYSE.schedule(start_date=today.isoformat(), end_date=today.isoformat())
    if sched.empty:
        return False
    now = pd.Timestamp.now(tz="UTC")
    open_ts = sched.iloc[0]["market_open"]
    close_ts = sched.iloc[0]["market_close"]
    return open_ts <= now <= close_ts


def _is_trading_day(d: date) -> bool:
    sched = _NYSE.schedule(start_date=d.isoformat(), end_date=d.isoformat())
    return not sched.empty


def _watchlist_assets() -> dict[str, str]:
    """ticker → asset_class for every priced watchlist row (first wins)."""
    with session_scope() as session:
        rows = session.exec(
            select(Watchlist).where(Watchlist.ticker.is_not(None))
        ).all()
    out: dict[str, str] = {}
    for r in rows:
        if r.ticker and r.ticker not in out:
            out[r.ticker] = r.asset_class or "equity"
    return out


_BACKFILL_MIN_BARS = 25  # below this a ticker can't compute 20d aggregates
# Daily-bar period yfinance is asked for on a fresh backfill. Was "60d"
# — enough for the 5d/20d aggregates but nothing more. Bumped to "5y"
# so the Symbol page chart can render a multi-year history (yfinance
# returns up to ~5y of free daily bars for most names). One-time cost
# per ticker; subsequent polls top up with intraday + recent daily.
_BACKFILL_DAILY_PERIOD = "5y"
# We also re-backfill tickers that have *some* bars but not enough for
# the long-history chart (e.g. legacy rows seeded with the old 60d
# default). 500 bars ≈ 2y of trading days, a sensible "deep history is
# present" threshold.
_BACKFILL_DEEP_BARS = 500


async def backfill_history() -> None:
    """One-shot-ish: pull history for tickers that don't yet have enough bars
    for the 5d/20d aggregates. Without this, why_moved / movers / PriceContext
    are silently wrong for ~3 weeks after a ticker is added.

    Idempotent and cheap on steady state — tickers with enough bars are
    skipped, so it can also run on a slow interval to catch newly-added
    (crypto_trending / activity) names.
    """
    try:
        await asyncio.to_thread(_backfill_sync)
    except Exception as e:
        logger.exception("backfill_history top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ price backfill error: {e}")
        except Exception:
            pass


def _purge_orphans() -> None:
    """Delete PriceContext/PriceBar for tickers no longer on the watchlist.

    A ticker can leave the watchlist via strike-prune, the weekly rebuild, or
    an activity/crypto_trending expiry — none of which clean up price rows.
    Left behind, a dead name keeps a stale PriceContext, and Yahoo prices
    dead coins at ~1e-15, so its change_1d reads as a fabricated +∞% and it
    resurfaces in movers / why_moved / synthesis. One cheap sweep per
    backfill pass closes the whole class regardless of how it was dropped.
    """
    with session_scope() as s:
        tracked = set(
            s.exec(
                select(Watchlist.ticker).where(Watchlist.ticker.is_not(None))
            ).all()
        )
        if not tracked:
            return
        orphans = [
            p for p in s.exec(select(PriceContext)).all()
            if p.ticker not in tracked
        ]
        names = [p.ticker for p in orphans]
        for p in orphans:
            s.delete(p)
        for t in names:
            for b in s.exec(
                select(PriceBar).where(PriceBar.ticker == t)
            ).all():
                s.delete(b)
        if names:
            logger.info(
                "prices: purged {} orphaned ticker(s): {}",
                len(names), names[:10],
            )


def _backfill_sync() -> None:
    from sqlmodel import func

    _purge_orphans()
    assets = _watchlist_assets()
    if not assets:
        return
    with session_scope() as s:
        counts = dict(
            s.exec(
                select(PriceBar.ticker, func.count(PriceBar.id)).group_by(
                    PriceBar.ticker
                )
            ).all()
        )
    # Tickers needing first-touch backfill (no bars at all / below the
    # aggregates threshold) get the full deep-history pull. Tickers
    # that already have *some* bars but less than the deep threshold
    # also get a deep pull — but only once. This bridges the gap for
    # rows seeded under the older 60d default so the long-history
    # chart works without manual re-seed.
    fresh = {t: c for t, c in assets.items() if counts.get(t, 0) < _BACKFILL_MIN_BARS}
    deep = {
        t: c for t, c in assets.items()
        if _BACKFILL_MIN_BARS <= counts.get(t, 0) < _BACKFILL_DEEP_BARS
    }
    need = {**fresh, **deep}
    if not need:
        logger.info("prices backfill: nothing to backfill")
        return

    logger.info(
        "prices backfill: {} fresh + {} deep-history",
        len(fresh), len(deep),
    )
    # 5y of daily bars covers the long-history chart on the Symbol
    # page. 5d of hourly adds recency so change_1d is sane before
    # the 1m poller catches up. yfinance dedupes by (ticker, ts) on
    # insert so re-pulling deep history is idempotent.
    daily = _download_into_bars(need, period=_BACKFILL_DAILY_PERIOD, interval="1d")
    hourly = _download_into_bars(fresh, period="5d", interval="60m")
    _recompute_contexts(list(need))
    logger.info(
        "prices backfill: inserted {} daily + {} hourly bars across {} tickers",
        daily,
        hourly,
        len(need),
    )


def _pollable_tickers(*, market_open: bool) -> dict[str, str]:
    """Subset of the watchlist to poll right now. Crypto + futures are always
    polled; equities and rate proxies only while the NYSE session is open.
    """
    assets = _watchlist_assets()
    if market_open:
        return assets
    return {t: c for t, c in assets.items() if c in _ALWAYS_ON}


def _chunked(xs: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


async def poll_prices() -> None:
    try:
        # yfinance.download() is sync and blocks. Run in a thread.
        await asyncio.to_thread(_poll_intraday)
    except Exception as e:
        logger.exception("poll_prices top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ prices poll error: {e}")
        except Exception:
            pass


async def poll_daily_bars() -> None:
    try:
        await asyncio.to_thread(_poll_daily)
    except Exception as e:
        logger.exception("poll_daily_bars top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ daily bars error: {e}")
        except Exception:
            pass


def _download_into_bars(assets: dict[str, str], *, period: str, interval: str) -> int:
    """Batch-download `assets` (ticker→asset_class) and persist new bars.
    Returns the number of inserted rows.
    """
    inserted = 0
    tickers = list(assets)
    had_data: set[str] = set()
    empty: set[str] = set()
    for batch in _chunked(tickers, _BATCH_SIZE):
        yf_to_wl = {_to_yfinance(t, assets[t]): t for t in batch}
        yf_tickers = list(yf_to_wl)
        try:
            df = yf.download(
                tickers=" ".join(yf_tickers),
                period=period,
                interval=interval,
                group_by="ticker",
                threads=True,
                progress=False,
                auto_adjust=False,
            )
        except Exception as e:
            logger.warning("yfinance batch failed ({} tickers): {}", len(batch), e)
            continue

        for yf_t, wl_t in yf_to_wl.items():
            try:
                sub = df[yf_t] if len(yf_tickers) > 1 else df
            except Exception:
                sub = None
            if sub is None or sub.empty or sub.dropna(how="all").empty:
                empty.add(wl_t)
                continue
            had_data.add(wl_t)
            try:
                inserted += _persist_bars(wl_t, sub)
            except Exception as e:
                logger.debug("price persist failed for {}: {}", wl_t, e)

    _reconcile_strikes(had_data, empty - had_data)
    return inserted


def _reconcile_strikes(had_data: set[str], empty: set[str]) -> None:
    """Clear strikes for tickers that returned data; strike the rest. When a
    ticker only exists via an auto-promoted source and keeps coming back
    empty, drop it so the watchlist (and the logs) self-heal.
    """
    for t in had_data:
        _EMPTY_STRIKES.pop(t, None)
    if not empty:
        return

    prune: list[str] = []
    for t in empty:
        _EMPTY_STRIKES[t] = _EMPTY_STRIKES.get(t, 0) + 1
        if _EMPTY_STRIKES[t] >= _MAX_EMPTY_STRIKES:
            prune.append(t)
    if not prune:
        return

    with session_scope() as session:
        for t in prune:
            rows = session.exec(
                select(Watchlist).where(Watchlist.ticker == t)
            ).all()
            # Only drop if every row for this ticker is from a prunable
            # source — never delete a curated/index holding.
            if rows and all(r.source in _PRUNABLE_SOURCES for r in rows):
                for r in rows:
                    session.delete(r)
                _EMPTY_STRIKES.pop(t, None)
                logger.info(
                    "prices: auto-pruned dead ticker {} ({} no-data cycles)",
                    t,
                    _MAX_EMPTY_STRIKES,
                )
            else:
                # Curated symbol with no Yahoo data — stop re-striking, just
                # leave it quiet (noise is already muted).
                _EMPTY_STRIKES[t] = _MAX_EMPTY_STRIKES


def _poll_intraday() -> None:
    market_open = _is_market_open_now()
    assets = _pollable_tickers(market_open=market_open)
    if not assets:
        logger.info("prices: nothing pollable right now (market_open={})", market_open)
        return

    inserted = _download_into_bars(assets, period="1d", interval="1m")
    _recompute_contexts(list(assets))
    logger.info(
        "prices: inserted {} new bars across {} tickers (market_open={})",
        inserted,
        len(assets),
        market_open,
    )


def _persist_bars(ticker: str, df: "pd.DataFrame") -> int:
    """One bulk `INSERT … ON CONFLICT DO NOTHING` per ticker. The old
    select-then-insert-per-row pattern held the single SQLite write lock for
    the whole Python loop (hundreds of round-trips) — the real source of the
    `database is locked` contention. Dedup is now enforced by the existing
    UniqueConstraint(ticker, ts), so the lock is held for one fast statement
    (ms, not seconds). Junk close (NaN/≤0) is still rejected at the source.
    """
    if df is None or df.empty:
        return 0
    values: list[dict] = []
    for ts, row in df.iterrows():
        close = row.get("Close")
        if pd.isna(close) or float(close) <= 0:
            continue
        ts_utc = (
            ts.tz_convert("UTC").to_pydatetime()
            if ts.tzinfo
            else ts.tz_localize("UTC").to_pydatetime()
        )
        values.append(
            {
                "ticker": ticker,
                "ts": ts_utc,
                "open": float(row.get("Open") or 0.0),
                "high": float(row.get("High") or 0.0),
                "low": float(row.get("Low") or 0.0),
                "close": float(close),
                "volume": int(row.get("Volume") or 0),
            }
        )
    if not values:
        return 0
    with session_scope() as session:
        result = session.execute(
            sqlite_insert(PriceBar)
            .values(values)
            .on_conflict_do_nothing()  # dedup via uix_pricebar_ticker_ts
        )
    n = result.rowcount
    return n if isinstance(n, int) and n >= 0 else len(values)


def _poll_daily() -> None:
    """Daily-bar refresh. Always refreshes crypto/futures (24/7); equities and
    rate proxies only on trading days.
    """
    assets = _watchlist_assets()
    if not assets:
        return
    if not _is_trading_day(date.today()):
        assets = {t: c for t, c in assets.items() if c in _ALWAYS_ON}
        if not assets:
            logger.info("prices: not a trading day, only alt-assets — skipping")
            return

    inserted = _download_into_bars(assets, period="30d", interval="1d")
    _recompute_contexts(list(assets))
    logger.info("prices: daily refresh inserted {} bars", inserted)


def _recompute_contexts(tickers: list[str]) -> None:
    """Recompute PriceContext rows for the given tickers from PriceBar history.

    All updates batched in a single session — 540 separate commits is ~10s
    of SQLite overhead per cycle.
    """
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        for ticker in tickers:
            try:
                _recompute_one(session, ticker, now)
            except Exception as e:
                logger.debug("recompute_context({}) failed: {}", ticker, e)


def _recompute_one(session, ticker: str, now: datetime) -> None:
    bars = session.exec(
        select(PriceBar)
        .where(PriceBar.ticker == ticker)
        .order_by(PriceBar.ts.desc())
        .limit(20 * 400)
    ).all()
    if not bars:
        return
    last_price = bars[0].close

    # Group by date — latest bar per date wins for close, sum for volume.
    daily_close: dict[date, float] = {}
    daily_volume: dict[date, int] = {}
    for b in bars:
        d = b.ts.date()
        if d not in daily_close:
            daily_close[d] = b.close
        daily_volume[d] = daily_volume.get(d, 0) + b.volume

    daily_sorted = sorted(daily_close.items(), reverse=True)
    change_1d = 0.0
    change_5d = 0.0
    vol_ratio = 0.0
    if len(daily_sorted) >= 2:
        prev_close = daily_sorted[1][1]
        if prev_close:
            change_1d = (last_price - prev_close) / prev_close
    if len(daily_sorted) >= 6:
        five_d_close = daily_sorted[5][1]
        if five_d_close:
            change_5d = (last_price - five_d_close) / five_d_close
    if len(daily_sorted) >= 2:
        today_vol = daily_volume.get(daily_sorted[0][0], 0)
        prior = [daily_volume[d] for d, _ in daily_sorted[1:21] if d in daily_volume]
        avg = sum(prior) / len(prior) if prior else 0
        if avg:
            vol_ratio = today_vol / avg

    existing = session.get(PriceContext, ticker)
    if existing is None:
        session.add(
            PriceContext(
                ticker=ticker,
                last_price=last_price,
                change_1d_pct=change_1d,
                change_5d_pct=change_5d,
                volume_vs_20d_avg=vol_ratio,
                last_updated=now,
            )
        )
    else:
        existing.last_price = last_price
        existing.change_1d_pct = change_1d
        existing.change_5d_pct = change_5d
        existing.volume_vs_20d_avg = vol_ratio
        existing.last_updated = now
        session.add(existing)
