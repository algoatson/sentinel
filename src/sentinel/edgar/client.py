"""EDGAR HTTP client per SPEC §7. Rate-limited to 8 req/s (SEC limit is 10)."""

from __future__ import annotations

import re
import threading
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import feedparser
import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from loguru import logger

from ..config import settings


# Many EDGAR primary docs are iXBRL — XHTML/XML hybrids. BS4 warns when its
# HTML parser sees XML content. We extract plain text either way; the warning
# is noise.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


_GETCURRENT_LINK_RE = re.compile(
    r"/Archives/edgar/data/(\d+)/\d{18}/(\d{10}-\d{2}-\d{6})-index\.htm"
)


@dataclass
class FilingMeta:
    cik: str  # zero-padded to 10
    ticker: Optional[str]
    form_type: str
    accession_number: str
    filed_at: datetime
    primary_doc: str
    primary_doc_url: str


class _RateLimiter:
    def __init__(self, rate_per_sec: float) -> None:
        self.min_interval = 1.0 / rate_per_sec
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last = time.monotonic()


class EdgarClient:
    DATA_BASE = "https://data.sec.gov"
    WWW_BASE = "https://www.sec.gov"
    EFTS_BASE = "https://efts.sec.gov/LATEST/search-index"

    _limiter = _RateLimiter(8.0)
    _ticker_cik_cache: dict[str, str] | None = None
    _ticker_cik_cache_ts: float = 0.0
    _cik_name_cache: dict[str, str] = {}
    _cache_lock = threading.Lock()

    # Single httpx.Client per process — every call uses the same UA and rate
    # limiter, so a shared connection pool is correct and avoids socket leaks
    # from short-lived EdgarClient() instances scattered across pipelines.
    _http_client: httpx.Client | None = None

    @classmethod
    def _get_http_client(cls) -> httpx.Client:
        if cls._http_client is None:
            cls._http_client = httpx.Client(
                headers={
                    "User-Agent": settings.EDGAR_USER_AGENT,
                    "Accept-Encoding": "gzip, deflate",
                },
                timeout=30.0,
                follow_redirects=True,
            )
        return cls._http_client

    @classmethod
    def close(cls) -> None:
        """Close the shared httpx client. Call on shutdown."""
        if cls._http_client is not None:
            cls._http_client.close()
            cls._http_client = None

    def _get(self, url: str) -> httpx.Response:
        self._limiter.acquire()
        r = self._get_http_client().get(url)
        r.raise_for_status()
        return r

    def get_company_submissions(self, cik: str) -> dict:
        cik_padded = cik.zfill(10)
        return self._get(f"{self.DATA_BASE}/submissions/CIK{cik_padded}.json").json()

    def get_company_name(self, cik: str) -> Optional[str]:
        """Return the issuer's company name for a CIK, cached per process.

        Returns None on lookup failure rather than raising — callers fall back
        to ticker-only search.
        """
        cik_padded = cik.zfill(10)
        with EdgarClient._cache_lock:
            cached = EdgarClient._cik_name_cache.get(cik_padded)
        if cached is not None:
            return cached
        try:
            data = self.get_company_submissions(cik_padded)
        except Exception as e:
            logger.debug("get_company_name({}) failed: {}", cik_padded, e)
            return None
        name = data.get("name") or None
        if name:
            with EdgarClient._cache_lock:
                EdgarClient._cik_name_cache[cik_padded] = name
        return name

    def list_recent_filings(self, cik: str, since: datetime) -> list[FilingMeta]:
        try:
            data = self.get_company_submissions(cik)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return []
            raise

        recent = data.get("filings", {}).get("recent", {})
        accs = recent.get("accessionNumber", [])
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        primary_docs = recent.get("primaryDocument", [])
        tickers = data.get("tickers") or []
        ticker = tickers[0] if tickers else None

        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

        out: list[FilingMeta] = []
        for i, acc in enumerate(accs):
            try:
                filed_at = datetime.strptime(dates[i], "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except (ValueError, IndexError):
                continue
            if filed_at < since:
                continue
            primary = primary_docs[i] if i < len(primary_docs) else ""
            acc_clean = acc.replace("-", "")
            url = (
                f"{self.WWW_BASE}/Archives/edgar/data/{int(cik)}/{acc_clean}/{primary}"
            )
            out.append(
                FilingMeta(
                    cik=cik.zfill(10),
                    ticker=ticker,
                    form_type=forms[i] if i < len(forms) else "",
                    accession_number=acc,
                    filed_at=filed_at,
                    primary_doc=primary,
                    primary_doc_url=url,
                )
            )
        return out

    def fetch_primary_document(self, url: str, max_chars: int = 100_000) -> str:
        """Fetch and text-extract a filing's primary document.

        Callers pass a full URL (constructed from FilingMeta.primary_doc_url).
        HTML / iXBRL → stripped to text via BeautifulSoup; everything else
        returned as-is. Truncated to `max_chars` to keep LLM prompts bounded.
        """
        r = self._get(url)
        ctype = r.headers.get("content-type", "").lower()
        is_html = "html" in ctype or url.lower().endswith((".htm", ".html"))
        if is_html:
            soup = BeautifulSoup(r.text, "lxml")
            for tag in soup(["script", "style"]):
                tag.decompose()
            text_content = soup.get_text("\n", strip=True)
        else:
            text_content = r.text
        return text_content[:max_chars]

    def get_ticker_to_cik_map(self) -> dict[str, str]:
        with EdgarClient._cache_lock:
            if (
                EdgarClient._ticker_cik_cache is not None
                and (time.time() - EdgarClient._ticker_cik_cache_ts) < 86_400
            ):
                return EdgarClient._ticker_cik_cache

        r = self._get(f"{self.WWW_BASE}/files/company_tickers.json")
        data = r.json()
        mapping: dict[str, str] = {}
        for entry in data.values():
            ticker = str(entry.get("ticker", "")).upper()
            cik = str(entry.get("cik_str", "")).zfill(10)
            if ticker and cik:
                mapping[ticker] = cik

        with EdgarClient._cache_lock:
            EdgarClient._ticker_cik_cache = mapping
            EdgarClient._ticker_cik_cache_ts = time.time()
        logger.info("loaded {} ticker→CIK mappings from EDGAR", len(mapping))
        return mapping

    def fetch_recent_filings_global(self, since: datetime, count: int = 100) -> list[dict]:
        """Fetch SEC EDGAR's 'getcurrent' Atom feed — all recent filings across
        all of EDGAR in chronological order. Used as a cheap discovery probe
        so we only deep-poll the small subset of watchlist CIKs that actually
        had a filing in the window.

        Returns dicts with keys: cik (zero-padded), accession_number,
        form_type, filed_at, index_url. Filters items older than `since`.
        """
        url = (
            f"{self.WWW_BASE}/cgi-bin/browse-edgar?action=getcurrent"
            f"&type=&company=&dateb=&owner=include"
            f"&count={count}&output=atom"
        )
        r = self._get(url)
        feed = feedparser.parse(r.text)

        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

        out: list[dict] = []
        for entry in feed.entries:
            link = entry.get("link") or ""
            m = _GETCURRENT_LINK_RE.search(link)
            if not m:
                continue
            cik_int, accession = m.group(1), m.group(2)

            # Form type: prefer the atom category term, fall back to title prefix.
            form_type = ""
            tags = entry.get("tags") or []
            if tags:
                form_type = (tags[0].get("term", "") or "").strip()
            if not form_type:
                title = entry.get("title", "") or ""
                if " - " in title:
                    form_type = title.split(" - ", 1)[0].strip()

            upd = entry.get("updated_parsed")
            if upd:
                try:
                    filed_at = datetime(*upd[:6], tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    filed_at = datetime.now(timezone.utc)
            else:
                filed_at = datetime.now(timezone.utc)

            if filed_at < since:
                continue

            out.append(
                {
                    "cik": cik_int.zfill(10),
                    "accession_number": accession,
                    "form_type": form_type,
                    "filed_at": filed_at,
                    "index_url": link,
                }
            )
        return out

    def full_text_search(self, query: str) -> list[dict]:
        r = self._get(f"{self.EFTS_BASE}?q={query}")
        return r.json().get("hits", {}).get("hits", [])
