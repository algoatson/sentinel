"""Watchlist builder per SPEC §7.

Pulls S&P 500 and Nasdaq 100 constituents from Wikipedia, resolves to CIKs via
the EDGAR ticker map, mirrors tracked entities from config, and runs activity
promotion (skipped if Filing table is empty on first run).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import httpx
import yaml
from bs4 import BeautifulSoup
from loguru import logger
from sqlmodel import func, select

from ..config import CONFIG_DIR
from ..db import session_scope
from ..models import Filing, TrackedEntity, Watchlist
from .client import EdgarClient


_WIKI_UA = "sentinel/0.1 (https://github.com/local)"


def _synthetic_cik(ticker: str) -> str:
    """Non-equity assets have no CIK. Generate a stable ≤10-char surrogate so
    Watchlist's cik column (max_length=10, indexed) stays satisfied. The "X"
    prefix guarantees it can never collide with a real numeric EDGAR CIK, so
    the filings pipeline naturally skips these rows.
    """
    return "X" + hashlib.sha1(ticker.encode()).hexdigest()[:9]


def _load_alt_assets(session, now: datetime) -> None:
    """Load crypto + macro (futures/rates) instruments from config into the
    watchlist with synthetic CIKs and an explicit asset_class.
    """
    specs: list[tuple[str, str, str]] = []  # (config_file, top_key→class map)

    crypto_path = CONFIG_DIR / "crypto.yaml"
    if crypto_path.exists():
        cfg = yaml.safe_load(crypto_path.read_text()) or {}
        for group, syms in cfg.items():
            for sym in syms or []:
                specs.append((str(sym).upper(), "crypto", "crypto"))
    else:
        logger.warning("{} missing — no crypto added", crypto_path)

    macro_path = CONFIG_DIR / "macro_assets.yaml"
    if macro_path.exists():
        cfg = yaml.safe_load(macro_path.read_text()) or {}
        for sym in cfg.get("futures") or []:
            specs.append((str(sym).upper(), "macro", "future"))
        for sym in cfg.get("rates") or []:
            specs.append((str(sym).upper(), "macro", "rate"))
    else:
        logger.warning("{} missing — no macro instruments added", macro_path)

    added = 0
    for ticker, source, asset_class in specs:
        cik = _synthetic_cik(ticker)
        existing = session.exec(
            select(Watchlist)
            .where(Watchlist.cik == cik)
            .where(Watchlist.source == source)
        ).first()
        if existing is not None:
            # Backfill asset_class on rows created before this column existed.
            if existing.asset_class != asset_class:
                existing.asset_class = asset_class
                session.add(existing)
            continue
        session.add(
            Watchlist(
                cik=cik,
                ticker=ticker,
                source=source,
                asset_class=asset_class,
                added_at=now,
                expires_at=None,
            )
        )
        added += 1

    # Config-sync: drop curated crypto/macro rows no longer in the YAML
    # (e.g. RNDR-USD after the RENDER rebrand) so the watchlist tracks the
    # config and dead symbols don't linger forever.
    wanted = {(t, src) for t, src, _ in specs}
    removed = 0
    for row in session.exec(
        select(Watchlist).where(Watchlist.source.in_(["crypto", "macro"]))
    ).all():
        if (row.ticker, row.source) not in wanted:
            session.delete(row)
            removed += 1
    logger.info(
        "alt-assets: added {} crypto/macro tickers, pruned {} stale",
        added,
        removed,
    )


def build_watchlist() -> None:
    """Public entry point. Catches all errors and logs — never raises."""
    try:
        _build()
    except Exception as e:
        logger.exception("watchlist build failed: {}", e)


def _build() -> None:
    client = EdgarClient()
    now = datetime.now(timezone.utc)

    indices_path = CONFIG_DIR / "indices.yaml"
    if indices_path.exists():
        indices_cfg = yaml.safe_load(indices_path.read_text()) or {}
        indices = set(indices_cfg.get("indices", []) or [])
    else:
        logger.warning("{} missing — no index tickers will be added", indices_path)
        indices = set()

    tickers: set[str] = set()
    if "sp500" in indices:
        tickers |= _fetch_sp500_tickers()
    if "nasdaq100" in indices:
        tickers |= _fetch_nasdaq100_tickers()
    logger.info("resolved {} index tickers from Wikipedia", len(tickers))

    # ETFs from config/etfs.yaml — tracked alongside indices so cashtag mentions
    # for popular sector / leveraged ETFs (SOXL, TQQQ, KORU, etc.) get ingested
    # and the price-context layer covers them.
    etf_path = CONFIG_DIR / "etfs.yaml"
    etf_tickers: set[str] = set()
    if etf_path.exists():
        etf_cfg = yaml.safe_load(etf_path.read_text()) or {}
        etf_tickers = {str(t).upper() for t in (etf_cfg.get("etfs") or [])}
        tickers |= etf_tickers
        logger.info("added {} ETF tickers from {}", len(etf_tickers), etf_path)

    ticker_map = client.get_ticker_to_cik_map()
    resolved: dict[str, str] = {}
    for t in tickers:
        # Try the ticker as-is, then with hyphens replaced by dots and vice versa
        # (Wikipedia uses "BRK.B", EDGAR uses "BRK-B" or "BRK").
        for candidate in (t, t.replace("-", "."), t.replace(".", "-"), t.split("-")[0], t.split(".")[0]):
            cik = ticker_map.get(candidate.upper())
            if cik:
                resolved[t] = cik
                break
    logger.info("resolved {} tickers to CIKs", len(resolved))

    with session_scope() as session:
        for ticker, cik in resolved.items():
            existing = session.exec(
                select(Watchlist)
                .where(Watchlist.cik == cik)
                .where(Watchlist.source == "index")
            ).first()
            if existing is None:
                session.add(
                    Watchlist(
                        cik=cik,
                        ticker=ticker,
                        source="index",
                        added_at=now,
                        expires_at=None,
                    )
                )

        te_path = CONFIG_DIR / "tracked_entities.yaml"
        if te_path.exists():
            te_cfg = yaml.safe_load(te_path.read_text()) or {}
            for ent in te_cfg.get("entities", []) or []:
                cik = str(ent["cik"]).zfill(10)
                try:
                    client.get_company_submissions(cik)
                except Exception as e:
                    logger.warning(
                        "tracked entity {} CIK {} not found on EDGAR: {}",
                        ent.get("name"),
                        cik,
                        e,
                    )
                    continue
                existing_te = session.exec(
                    select(TrackedEntity).where(TrackedEntity.cik == cik)
                ).first()
                if existing_te is None:
                    session.add(
                        TrackedEntity(
                            name=ent["name"],
                            cik=cik,
                            type=ent["type"],
                            notes=ent.get("notes"),
                        )
                    )
                existing_wl = session.exec(
                    select(Watchlist)
                    .where(Watchlist.cik == cik)
                    .where(Watchlist.source == "tracked_entity")
                ).first()
                if existing_wl is None:
                    session.add(
                        Watchlist(
                            cik=cik,
                            ticker=None,
                            source="tracked_entity",
                            added_at=now,
                            expires_at=None,
                        )
                    )

        # Crypto + macro instruments (synthetic CIKs, never hit EDGAR).
        _load_alt_assets(session, now)

        # Activity promotion — only if Filing table has rows.
        filing_count = session.exec(select(func.count()).select_from(Filing)).one()
        if filing_count and filing_count > 0:
            cutoff_30d = now - timedelta(days=30)
            cutoff_7d = now - timedelta(days=7)
            # CIKs with ≥3 filings in last 30d
            heavy_filers = session.exec(
                select(Filing.cik, func.count(Filing.id).label("c"))
                .where(Filing.filed_at >= cutoff_30d)
                .group_by(Filing.cik)
            ).all()
            heavy_ciks = {row[0] for row in heavy_filers if row[1] >= 3}
            # CIKs with any 8-K in last 7d
            recent_8k = session.exec(
                select(Filing.cik).where(
                    Filing.filed_at >= cutoff_7d,
                    Filing.form_type.in_(["8-K", "8-K/A"]),
                ).distinct()
            ).all()
            recent_8k_ciks = {row if isinstance(row, str) else row[0] for row in recent_8k}
            candidates = heavy_ciks | recent_8k_ciks

            for cik in candidates:
                already = session.exec(
                    select(Watchlist).where(Watchlist.cik == cik)
                ).first()
                if already is not None:
                    continue
                session.add(
                    Watchlist(
                        cik=cik,
                        ticker=None,
                        source="activity",
                        added_at=now,
                        expires_at=now + timedelta(days=60),
                    )
                )

        # Expired activity cleanup.
        expired = session.exec(
            select(Watchlist).where(
                Watchlist.source == "activity",
                Watchlist.expires_at.is_not(None),
                Watchlist.expires_at < now,
            )
        ).all()
        for row in expired:
            session.delete(row)

    logger.info("watchlist build complete")


def _fetch_sp500_tickers() -> set[str]:
    try:
        r = httpx.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": _WIKI_UA},
            timeout=30.0,
            follow_redirects=True,
        )
        r.raise_for_status()
    except Exception as e:
        logger.warning("S&P 500 fetch failed: {}", e)
        return set()
    soup = BeautifulSoup(r.text, "lxml")
    table = soup.find("table", {"id": "constituents"})
    if table is None:
        return set()
    out: set[str] = set()
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if not cells:
            continue
        sym = cells[0].get_text(strip=True)
        if sym:
            # Wikipedia uses "BRK.B"; EDGAR map uses bare or hyphenated. We try
            # several forms in the resolver above.
            out.add(sym.upper())
    return out


def _fetch_nasdaq100_tickers() -> set[str]:
    try:
        r = httpx.get(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            headers={"User-Agent": _WIKI_UA},
            timeout=30.0,
            follow_redirects=True,
        )
        r.raise_for_status()
    except Exception as e:
        logger.warning("Nasdaq 100 fetch failed: {}", e)
        return set()
    soup = BeautifulSoup(r.text, "lxml")
    for table in soup.find_all("table", {"class": "wikitable"}):
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [th.get_text(strip=True).lower() for th in header_row.find_all("th")]
        ticker_idx = None
        for i, h in enumerate(headers):
            if h in ("ticker", "symbol"):
                ticker_idx = i
                break
        if ticker_idx is None:
            continue
        out: set[str] = set()
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) > ticker_idx:
                sym = cells[ticker_idx].get_text(strip=True)
                if sym and len(sym) <= 6:
                    out.add(sym.upper())
        if out:
            return out
    return set()
