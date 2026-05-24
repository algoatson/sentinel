"""Article-body extraction contract.

This is what makes the news dossier sound aware of the article instead
of confabulating from the headline. Pin:

- Direct extraction picks `<article>` first, then `<main>`, then the
  largest `<div>`. Boilerplate (script/nav/footer/etc.) is gone.
- Below the stub threshold the body falls through to Jina; both empty
  → a "stub" row is persisted so subsequent opens don't re-fetch.
- Cache returns the stored body on the second call (no extra HTTP).
- `force=True` bypasses the cache.
- All paths handle HTTP errors and bad HTML without raising.
"""

from __future__ import annotations


import httpx

from sentinel import article_fetch
from sentinel.db import session_scope
from sentinel.models import ArticleBody


# ── HTML fixtures ────────────────────────────────────────────────────────


_GOOD_ARTICLE = """
<html><head><title>x</title></head>
<body>
  <nav>NavLink1 NavLink2 NavLink3</nav>
  <header>Site Header</header>
  <article>
    <h1>Trump announces $2B quantum computing investment</h1>
    <p>President Donald Trump on Thursday announced a $2 billion federal
    investment in quantum computing research, framing it as a strategic
    response to Chinese advances in the field. The funding will flow
    through the Department of Energy and the National Science Foundation
    over a three-year horizon.</p>
    <p>Industry observers said the move was likely to benefit IonQ
    ($IONQ), Rigetti ($RGTI), and D-Wave ($QBTS) — the three publicly
    traded pure-plays in the space. Larger players including IBM ($IBM)
    and Alphabet ($GOOGL) also stand to gain through their existing
    research programmes.</p>
    <p>Critics noted that the federal investment is small relative to
    China's reported $10 billion programme.</p>
  </article>
  <footer>(c) Test News</footer>
  <script>tracking()</script>
</body></html>
"""


_STUB_PAYWALL = """
<html><body>
  <header>Site</header>
  <main>
    <h1>Subscribe to read this article</h1>
    <p>Already a subscriber?</p>
  </main>
  <footer>©</footer>
</body></html>
"""


# ── helpers ──────────────────────────────────────────────────────────────


def _stub_httpx(monkeypatch, *, direct_status=200, direct_body="",
                jina_status=200, jina_body="",
                direct_raises=False, jina_raises=False):
    """Patch `httpx.Client` so tests don't hit the network. The stub
    distinguishes direct vs Jina by URL prefix."""

    class _Resp:
        def __init__(self, status_code: int, text: str):
            self.status_code = status_code
            self.text = text

    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

        def get(self, url, headers=None):
            if url.startswith("https://r.jina.ai/"):
                if jina_raises:
                    raise httpx.ConnectError("synthetic jina down")
                return _Resp(jina_status, jina_body)
            if direct_raises:
                raise httpx.ConnectError("synthetic direct down")
            return _Resp(direct_status, direct_body)

    monkeypatch.setattr(article_fetch.httpx, "Client", _Client)


def _purge_cache() -> None:
    from sqlmodel import select
    with session_scope() as s:
        for row in s.exec(select(ArticleBody)).all():
            s.delete(row)


# ── extraction heuristic ──────────────────────────────────────────────────


def test_extract_body_picks_article_tag():
    out = article_fetch._extract_body(_GOOD_ARTICLE)
    # core content is present
    assert "quantum computing" in out
    assert "$IONQ" in out
    # boilerplate is stripped
    assert "NavLink1" not in out
    assert "Site Header" not in out
    assert "Test News" not in out
    assert "tracking()" not in out


def test_extract_body_returns_empty_for_empty_input():
    assert article_fetch._extract_body("") == ""


def test_extract_body_survives_malformed_html():
    # malformed → bs4 still emits something; we just want no raise
    out = article_fetch._extract_body("<html><body><p>hi</")
    assert isinstance(out, str)


# ── direct path ───────────────────────────────────────────────────────────


def test_fetch_uses_direct_when_body_is_substantial(monkeypatch):
    _purge_cache()
    _stub_httpx(monkeypatch, direct_status=200, direct_body=_GOOD_ARTICLE)
    out = article_fetch.fetch_article_text("https://example.com/post-1")
    assert out is not None
    assert "quantum computing" in out
    # cached with source="direct"
    meta = article_fetch.cache_meta("https://example.com/post-1")
    assert meta is not None and meta["source"] == "direct"
    assert meta["char_count"] > 0


def test_fetch_falls_through_to_jina_on_stub_paywall(monkeypatch):
    _purge_cache()
    jina_text = (
        "Title: Trump's $2B quantum investment\n\n"
        "President Trump announced a $2 billion federal investment in "
        "quantum computing research on Thursday, naming IonQ, Rigetti, "
        "and D-Wave as likely beneficiaries. The funding flows through "
        "DOE and NSF over three years, framed as a response to China's "
        "reported $10 billion programme. Critics argue the US figure is "
        "small relative to the geopolitical stakes."
    )
    _stub_httpx(
        monkeypatch,
        direct_status=200, direct_body=_STUB_PAYWALL,
        jina_status=200, jina_body=jina_text,
    )
    out = article_fetch.fetch_article_text("https://example.com/paywalled")
    assert out is not None
    assert "IonQ" in out  # Jina text won
    meta = article_fetch.cache_meta("https://example.com/paywalled")
    assert meta is not None and meta["source"] == "jina"


def test_fetch_persists_stub_when_both_paths_empty(monkeypatch):
    """When direct returns nothing useful AND Jina is unreachable, we
    persist a stub row so next time we don't waste 18s re-trying."""
    _purge_cache()
    _stub_httpx(
        monkeypatch,
        direct_status=200, direct_body=_STUB_PAYWALL,
        jina_status=500, jina_body="",
    )
    out = article_fetch.fetch_article_text("https://example.com/dead")
    # may return the stub text (whatever paragraphs were salvageable)
    # or None — both are acceptable, but the cache row must exist
    _ = out
    meta = article_fetch.cache_meta("https://example.com/dead")
    assert meta is not None
    assert meta["source"] == "stub"


def test_fetch_returns_none_for_bad_url_shape():
    # not http(s) → None without any fetch attempt
    assert article_fetch.fetch_article_text("javascript:alert(1)") is None
    assert article_fetch.fetch_article_text("") is None


def test_fetch_swallows_direct_path_exceptions(monkeypatch):
    """A connect error / timeout must not bubble — the news dossier is
    waiting on this and we'd rather degrade than crash the modal."""
    _purge_cache()
    _stub_httpx(monkeypatch, direct_raises=True, jina_raises=True)
    out = article_fetch.fetch_article_text("https://example.com/x")
    assert out is None  # neither path succeeded
    # stub row persisted so retries are bounded
    meta = article_fetch.cache_meta("https://example.com/x")
    assert meta is not None and meta["source"] == "stub"


# ── cache ────────────────────────────────────────────────────────────────


def test_cache_returns_stored_body_on_second_call(monkeypatch):
    _purge_cache()
    _stub_httpx(monkeypatch, direct_status=200, direct_body=_GOOD_ARTICLE)
    a = article_fetch.fetch_article_text("https://example.com/cached")
    # Now flip httpx to ALWAYS raise — cache hit must still return the body
    _stub_httpx(monkeypatch, direct_raises=True, jina_raises=True)
    b = article_fetch.fetch_article_text("https://example.com/cached")
    assert a == b
    assert "quantum" in b


def test_force_bypasses_cache(monkeypatch):
    _purge_cache()
    _stub_httpx(monkeypatch, direct_status=200, direct_body=_GOOD_ARTICLE)
    a = article_fetch.fetch_article_text("https://example.com/refetch")
    # Now feed different content + force a refetch
    new_html = "<html><body><article><p>" + ("x " * 400) + "</p></article></body></html>"
    _stub_httpx(monkeypatch, direct_status=200, direct_body=new_html)
    b = article_fetch.fetch_article_text("https://example.com/refetch", force=True)
    assert b != a
    assert "x x x" in b
