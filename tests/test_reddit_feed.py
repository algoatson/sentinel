"""reddit_feed contract.

Two pure seams, both pinned here because a regression in either re-floods or
silences the dedicated channel:

- `_candidates`: bounded RECALL. Dedups threads, ranks by a cheap prior,
  truncates to the LLM budget. It must NOT hard-gate on move/surge anymore
  (that was the "unrelated posts" bug) — quiet posts still reach the curator.
- `_apply_curation`: maps the LLM verdict → cards, defensively. Hallucinated
  indices / bad categories / missing hooks must never produce a card.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sentinel.db import session_scope
from sentinel.models import PriceContext, RedditMention
from sentinel.pipelines.reddit_feed import (
    _LLM_BUDGET,
    _apply_curation,
    _candidates,
    _render_candidates,
)

NOW = datetime(2026, 5, 17, 18, 0, tzinfo=timezone.utc)


def _mention(s, *, post_id, ticker, sub="stocks", title=None, body="", age_h=1):
    s.add(
        RedditMention(
            subreddit=sub,
            post_id=post_id,
            comment_id=None,
            ticker=ticker,
            author="u/x",
            score=0,
            num_comments=0,
            created_at=NOW - timedelta(hours=age_h),
            title=title or f"{ticker} thread {post_id}",
            body_excerpt=body,
            permalink=f"https://reddit.com/{post_id}",
        )
    )


def _price(s, ticker, chg_1d):
    # Call sites pass a human percent (6.5 = 6.5%); production stores
    # change_1d_pct as a FRACTION, so model that here (reddit_feed *100s it).
    s.add(
        PriceContext(
            ticker=ticker,
            last_price=100.0,
            change_1d_pct=chg_1d / 100.0,
            change_5d_pct=0.0,
            volume_vs_20d_avg=1.0,
            last_updated=NOW,
        )
    )


def _cands_now():
    with session_scope() as s:
        return _candidates(s, NOW)


# ── _candidates: recall, not gating ─────────────────────────────────────────


def test_no_candidates_when_empty():
    assert _cands_now() == []


def test_quiet_post_is_STILL_a_candidate():
    """The key behavioural change: a flat, single-post ticker is no longer
    excluded — it goes to the curator, which decides relevance/quality."""
    with session_scope() as s:
        _mention(s, post_id="p1", ticker="AAPL")
        _price(s, "AAPL", 0.3)
    cands = _cands_now()
    assert len(cands) == 1 and cands[0]["lead"] == "AAPL"


def test_stale_and_already_posted_are_excluded():
    with session_scope() as s:
        _mention(s, post_id="old", ticker="NVDA", age_h=9)   # > 6h
        _mention(s, post_id="done", ticker="NVDA")
        _price(s, "NVDA", 8.0)
        s.flush()
        row = s.exec(
            __import__("sqlmodel").select(RedditMention).where(
                RedditMention.post_id == "done"
            )
        ).first()
        row.posted_at = NOW
        s.add(row)
    assert _cands_now() == []


def test_multi_ticker_thread_collapses_lead_is_the_mover():
    with session_scope() as s:
        _mention(s, post_id="dual", ticker="F")
        _mention(s, post_id="dual", ticker="TSLA")
        _price(s, "F", 0.1)
        _price(s, "TSLA", 6.5)
    cands = _cands_now()
    assert len(cands) == 1
    assert cands[0]["lead"] == "TSLA"
    assert set(cands[0]["tickers"]) == {"F", "TSLA"}


def test_ranked_by_prior_and_truncated_to_budget():
    with session_scope() as s:
        # _LLM_BUDGET + 4 distinct moving tickers; biggest move ranks first.
        for i in range(_LLM_BUDGET + 4):
            _mention(s, post_id=f"t{i}", ticker=f"TK{i}")
            _price(s, f"TK{i}", float(i))  # TK0 flat … TK{n} biggest
    cands = _cands_now()
    assert len(cands) == _LLM_BUDGET                 # bounded
    assert cands[0]["lead"] == f"TK{_LLM_BUDGET + 3}"  # top prior first
    assert "TK0" not in {c["lead"] for c in cands}     # weakest dropped


def test_body_excerpt_lifts_prior_over_a_bare_post():
    with session_scope() as s:
        _mention(s, post_id="bare", ticker="AAA")          # no body, no move
        _mention(s, post_id="rich", ticker="BBB", body="real DD here")
    cands = _cands_now()
    assert cands[0]["lead"] == "BBB"  # the substantive one ranks first


def test_render_candidates_is_one_based_and_aligned():
    with session_scope() as s:
        _mention(s, post_id="p1", ticker="GME", title="squeeze?")
        _price(s, "GME", 4.0)
    block = _render_candidates(_cands_now())
    assert block.startswith("1. $GME")
    assert "squeeze?" in block


# ── _apply_curation: defensive mapping ──────────────────────────────────────


def _cand(post_id, lead, **kw):
    base = dict(
        post_id=post_id, lead=lead, tickers=[lead], title=f"{lead} t",
        subreddit="stocks", body_excerpt="", permalink="u",
        lead_move=1.0, surge_n=0,
    )
    base.update(kw)
    return base


C = [_cand("a", "AAA"), _cand("b", "BBB"), _cand("c", "CCC")]


def test_curation_maps_picks_in_model_order_with_hook_and_category():
    picks = [
        {"i": 2, "category": "funny", "hook": "peak WSB"},
        {"i": 1, "category": "important", "hook": "real catalyst"},
    ]
    out = _apply_curation(C, picks, max_keep=5)
    assert [c["lead"] for c in out] == ["BBB", "AAA"]   # model order kept
    assert out[0]["category"] == "funny" and out[0]["hook"] == "peak WSB"


def test_curation_drops_hallucinated_index_and_garbage_items():
    picks = [
        {"i": 99, "category": "hype", "hook": "x"},      # out of range
        "not a dict",
        {"i": "nope", "category": "hype", "hook": "x"},  # bad index
        {"i": 3, "category": "hype", "hook": "legit"},
    ]
    out = _apply_curation(C, picks, max_keep=5)
    assert [c["lead"] for c in out] == ["CCC"]


def test_curation_bad_category_defaults_and_missing_hook_drops():
    picks = [
        {"i": 1, "category": "spicy", "hook": "kept w/ default cat"},
        {"i": 2, "category": "funny", "hook": ""},   # no hook → no card
        {"i": 3, "category": "funny"},               # missing hook → no card
    ]
    out = _apply_curation(C, picks, max_keep=5)
    assert len(out) == 1
    assert out[0]["lead"] == "AAA" and out[0]["category"] == "interesting"


def test_curation_one_card_per_ticker_and_capped():
    cands = [_cand("a", "AAA"), _cand("b", "AAA"), _cand("c", "CCC"),
             _cand("d", "DDD")]
    picks = [
        {"i": 1, "category": "hype", "hook": "first AAA"},
        {"i": 2, "category": "hype", "hook": "dupe AAA — dropped"},
        {"i": 3, "category": "hype", "hook": "CCC"},
        {"i": 4, "category": "hype", "hook": "DDD over cap"},
    ]
    out = _apply_curation(cands, picks, max_keep=2)
    assert [c["lead"] for c in out] == ["AAA", "CCC"]  # dedup + cap=2


def test_curation_non_list_verdict_is_safe():
    assert _apply_curation(C, {"i": 1}, max_keep=5) == []
    assert _apply_curation(C, None, max_keep=5) == []


# ── top-comment enrichment ──────────────────────────────────────────────────


def _comment(body, score, author="u", stickied=False):
    return {"kind": "t1", "data": {
        "body": body, "score": score, "author": author, "stickied": stickied,
    }}


def test_parse_comments_ranks_filters_and_truncates():
    from sentinel.ingesters.reddit import _parse_comments

    payload = [
        {"data": {}},  # post listing (ignored)
        {"data": {"children": [
            _comment("Solid point, the float is actually 12M not 40M", 50),
            _comment("[deleted]", 999),
            _comment("nice", 200),                      # too short (<25)
            _comment("AutoMod boilerplate here you go", 80, "AutoModerator"),
            _comment("pinned rules thread, read before posting", 80, "u",
                     stickied=True),
            _comment("counter: guidance was already priced in last week", 9),
            _comment("x" * 400, 7),                     # long → truncated
            {"kind": "more", "data": {}},               # not a comment
        ]}},
    ]
    out = _parse_comments(payload, limit=5)
    assert out[0].startswith("(+50) Solid point")        # ranked by score
    assert out[1].startswith("(+9) counter")
    assert out[2].startswith("(+7) ") and out[2].endswith("…")  # truncated
    assert len(out) == 3                                  # junk all dropped
    assert all("[deleted]" not in c and "AutoMod" not in c for c in out)


def test_parse_comments_bad_payload_is_safe():
    from sentinel.ingesters.reddit import _parse_comments

    for bad in ([], {}, [1], [{}, {}], [{}, {"data": {}}], None):
        assert _parse_comments(bad) == []


def test_fetch_top_comments_guards(monkeypatch):
    from sentinel.ingesters import reddit

    # non-Reddit / empty permalink → no fetch, no raise
    assert reddit.fetch_top_comments("") == []
    assert reddit.fetch_top_comments("https://news.google.com/x") == []
    # breaker open → must not even attempt (deepens the block)
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(
        reddit, "_direct_cooldown_until",
        datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    assert reddit.direct_blocked() is True
    assert reddit.fetch_top_comments(
        "https://www.reddit.com/r/x/comments/abc/slug/"
    ) == []


def test_render_candidates_includes_top_replies():
    cand = {
        "lead": "GME", "tickers": ["GME"], "lead_move": 4.0, "surge_n": 0,
        "subreddit": "stocks", "title": "is the squeeze back?",
        "body_excerpt": "asking for a friend",
        "top_comments": ["(+88) no, the float math is wrong because…"],
    }
    block = _render_candidates([cand])
    assert "↳ (+88) no, the float math is wrong" in block
