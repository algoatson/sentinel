"""Tests for sentinel.source_tags — the structured ticker-tag sources that feed
the LLM resolver an anchored candidate set.

Deterministic throughout: the v1-search parse runs against a captured fixture,
`related_tickers_for` monkeypatches `httpx.get`, and normalization + HTML
parsing are pure. Pins the contamination/normalization contract the resolver
relies on (query ticker kept but demotable, foreign/index/private dropped,
class-share + crypto canonicalised to the watchlist's storage form).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from sentinel import source_tags

_FIXTURE = Path(__file__).parent / "fixtures" / "yahoo_search_nvda.json"


def _load_fixture() -> dict:
    return json.loads(_FIXTURE.read_text())


# ── normalization edges ──────────────────────────────────────────────────


def test_normalize_canonicalizes_and_drops_junk():
    n = source_tags.normalize
    # equities + cashtag strip
    assert n("NVDA") == "NVDA"
    assert n("$nvda") == "NVDA"
    # class share: dash (Yahoo) ↔ dot (watchlist) → dot canonical
    assert n("BRK-B") == "BRK.B"
    assert n("BRK.B") == "BRK.B"
    # crypto: the SUFFIXED form sources emit is kept; $-prefix stripped
    assert n("$btc-usd") == "BTC-USD"
    assert n("ETH-USD") == "ETH-USD"
    # futures: suffixed form kept
    assert n("ES=F") == "ES=F"
    # bare 2-letter roots are EQUITIES, never re-suffixed — "ES" is Eversource,
    # "CL" is Colgate, NOT the S&P/crude future (the mistag we removed).
    assert n("ES") == "ES"
    assert n("CL") == "CL"
    assert n("BTC") == "BTC"   # bare; watchlist gate (which stores BTC-USD) drops it
    # junk: foreign listing, private placeholder, index → dropped
    assert n("2454.TW") is None
    assert n("ANTH.PVT") is None
    assert n("^GSPC") is None
    # empty / blank
    assert n("") is None
    assert n("   ") is None
    assert n("$") is None


# ── v1-search parse (fixture) ────────────────────────────────────────────


def test_parse_search_news_yields_normalized_related_per_article():
    data = _load_fixture()
    out = source_tags._parse_search_news(data)
    assert len(out) == 4

    # Order preserved; every documented field present.
    first = out[0]
    assert first["title"].startswith("Walmart raises")
    assert first["url"].startswith("https://finance.yahoo.com/")
    assert first["uuid"] == "11111111-aaaa-bbbb-cccc-000000000001"
    assert first["pub"] == 1748000000

    # WMT-under-NVDA: subject-first, query ticker (NVDA) KEPT for the caller
    # to demote — not dropped here.
    assert first["related"] == ["WMT", "NVDA", "COST", "TGT"]


def test_parse_search_news_drops_foreign_index_and_canonicalizes_class_share():
    data = _load_fixture()
    out = source_tags._parse_search_news(data)
    berkshire = out[2]
    # BRK-B → BRK.B; ^GSPC + 2454.TW dropped; NVDA kept.
    assert berkshire["related"] == ["BRK.B", "NVDA"]


def test_parse_search_news_canonicalizes_crypto():
    data = _load_fixture()
    out = source_tags._parse_search_news(data)
    crypto = out[3]
    assert crypto["related"] == ["BTC-USD", "MSTR", "NVDA"]


def test_bare_equity_roots_not_aliased_to_derivatives():
    """Regression: 'CL' (Colgate) / 'ES' (Eversource) / 'NG' (NovaGold) are real
    EQUITY tickers — they must NOT be re-suffixed into crude/S&P/natural-gas
    futures. Yahoo emits the future as 'CL=F', so a bare 'CL' is the equity."""
    data = {"news": [{
        "title": "Colgate-Palmolive raises dividend",
        "link": "https://finance.yahoo.com/news/cl",
        "uuid": "x", "relatedTickers": ["CL", "ES", "NG", "PG"],
    }]}
    out = source_tags._parse_search_news(data)
    assert out[0]["related"] == ["CL", "ES", "NG", "PG"]
    assert "CL=F" not in out[0]["related"]
    assert "ES=F" not in out[0]["related"]


def test_parse_search_news_tolerates_garbage():
    assert source_tags._parse_search_news({}) == []
    assert source_tags._parse_search_news({"news": "nope"}) == []
    assert source_tags._parse_search_news([]) == []
    # missing title/link items are skipped, valid ones survive
    out = source_tags._parse_search_news(
        {"news": [{"relatedTickers": ["NVDA"]}, {"title": "t", "link": "u", "relatedTickers": ["NVDA"]}]}
    )
    assert len(out) == 1 and out[0]["related"] == ["NVDA"]


# ── related_tickers_for (monkeypatched httpx) ────────────────────────────


def _stub_get(monkeypatch, *, status=200, payload=None, raises=False):
    class _Resp:
        status_code = status
        def __init__(self, data): self._data = data
        def json(self): return self._data

    def _get(url, **kw):
        if raises:
            raise httpx.ConnectError("synthetic down")
        return _Resp(payload)

    monkeypatch.setattr(source_tags.httpx, "get", _get)


def test_related_tickers_for_parses_live_shape(monkeypatch):
    _stub_get(monkeypatch, payload=_load_fixture())
    out = source_tags.related_tickers_for("NVDA")
    assert [a["related"] for a in out] == [
        ["WMT", "NVDA", "COST", "TGT"],
        ["NVDA", "TSM", "AMD"],
        ["BRK.B", "NVDA"],
        ["BTC-USD", "MSTR", "NVDA"],
    ]


def test_related_tickers_for_fails_open_on_error(monkeypatch):
    _stub_get(monkeypatch, raises=True)
    assert source_tags.related_tickers_for("NVDA") == []


def test_related_tickers_for_fails_open_on_http_error(monkeypatch):
    _stub_get(monkeypatch, status=429, payload={})
    assert source_tags.related_tickers_for("NVDA") == []


def test_related_tickers_for_empty_query():
    assert source_tags.related_tickers_for("") == []


def test_related_tickers_for_logs_when_no_related(monkeypatch, caplog):
    # Items present but every relatedTickers empty → coverage log fires, and we
    # still return the (tickerless) items so the LLM path can run on titles.
    payload = {"news": [{"title": "t", "link": "u", "relatedTickers": []}]}
    _stub_get(monkeypatch, payload=payload)
    with caplog.at_level("INFO"):
        out = source_tags.related_tickers_for("ZZZZ")
    assert len(out) == 1 and out[0]["related"] == []


# ── article-page HTML tags (Phase 2) ─────────────────────────────────────


def test_from_html_extracts_curated_set():
    html = (
        '<html><head>'
        '<meta name="keywords" content="$bnb-usd;$h;$btc-usd">'
        '</head><body>'
        '<script>window.x = {"stockTickers":[{"symbol":"H"},{"symbol":"BTC-USD"}]};</script>'
        '<a class="ticker-tag" data-symbol="BNB-USD">BNB</a>'
        '<a class="ticker-tag" data-symbol="2454.TW">foreign</a>'
        '</body></html>'
    )
    out = source_tags.from_html(html)
    # H (equity), BTC-USD + BNB-USD (crypto) kept; foreign 2454.TW dropped.
    assert "H" in out
    assert "BTC-USD" in out
    assert "BNB-USD" in out
    assert "2454.TW" not in out
    # deduped
    assert len(out) == len(set(out))


def test_from_html_empty():
    assert source_tags.from_html("") == []
    assert source_tags.from_html("<html></html>") == []
