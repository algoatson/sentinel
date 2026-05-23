"""call_review resolution contract.

`_collect` is the gate: it must (a) only ever post a verdict that is
arithmetically true, (b) never fabricate one for an unscoreable call,
(c) gate to notable calls only, (d) finalize everything it evaluates so the
scan stays bounded and nothing double-fires. Each of those is pinned here —
a regression silently lies about the bot's track record or spams the channel.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import sentinel.config as cfg
from sentinel.db import session_scope
from sentinel.models import NarrativeEvent, TradingCall
from sentinel.pipelines.call_review import (
    _MAX_LINES,
    _collect,
    _line,
    _stamp,
)

NOW = datetime(2026, 5, 17, 18, 0, tzinfo=timezone.utc)


def _call(s, **kw):
    base = dict(
        ticker="NVDA",
        direction="long",
        conviction=3,
        source="synthesis",
        thesis="t",
        price_at_call=100.0,
        created_at=NOW - timedelta(days=6),
        ret_1d_pct=None,
        ret_5d_pct=None,
        settled=False,
        resolved_posted_at=None,
    )
    base.update(kw)
    c = TradingCall(**base)
    s.add(c)
    s.flush()
    return c.id


def _collect_now():
    with session_scope() as s:
        return _collect(s, NOW)


def test_unmatured_call_is_neither_finalized_nor_posted():
    with session_scope() as s:
        _call(s)  # ret_5d None, not settled → not a candidate yet
    posts, finalized = _collect_now()
    assert posts == [] and finalized == []


def test_bold_call_gets_a_verdict_even_on_a_tiny_move():
    with session_scope() as s:
        cid = _call(s, conviction=5, ret_5d_pct=0.4)
    posts, finalized = _collect_now()
    assert finalized == [cid]
    assert len(posts) == 1
    assert posts[0]["horizon"] == "5d" and posts[0]["hit"] is True


def test_big_move_gets_a_verdict_even_at_low_conviction():
    with session_scope() as s:
        _call(s, conviction=2, ret_5d_pct=9.1)
    posts, finalized = _collect_now()
    assert len(posts) == 1 and len(finalized) == 1


def test_timid_call_small_move_is_finalized_but_not_posted():
    with session_scope() as s:
        cid = _call(s, conviction=3, ret_5d_pct=1.2)
    posts, finalized = _collect_now()
    assert finalized == [cid]  # bounded — won't rescan
    assert posts == []          # but not worth a post


def test_short_direction_verdict_is_arithmetically_correct():
    with session_scope() as s:
        _call(s, ticker="TSLA", direction="short", conviction=5, ret_5d_pct=-7.0)
        _call(s, ticker="AMD", direction="short", conviction=5, ret_5d_pct=7.0)
    posts, _ = _collect_now()
    by = {p["ticker"]: p["hit"] for p in posts}
    assert by == {"TSLA": True, "AMD": False}  # short wins when price falls


def test_settled_unscoreable_call_is_finalized_with_no_fabricated_verdict():
    with session_scope() as s:
        cid = _call(s, conviction=5, settled=True, ret_1d_pct=None, ret_5d_pct=None)
    posts, finalized = _collect_now()
    assert finalized == [cid]  # stop scanning it
    assert posts == []          # never invent a grade off no price


def test_one_day_fallback_only_applies_once_settled():
    with session_scope() as s:
        # retired with only a 1d read → resolves on 1d
        _call(s, ticker="A", conviction=5, settled=True, ret_1d_pct=-8.0)
        # only a 1d read but NOT settled → still maturing, not a candidate
        _call(s, ticker="B", conviction=5, settled=False, ret_1d_pct=-8.0)
    posts, finalized = _collect_now()
    assert len(finalized) == 1
    assert len(posts) == 1
    assert posts[0]["ticker"] == "A" and posts[0]["horizon"] == "1d"


def test_already_resolved_calls_are_excluded():
    with session_scope() as s:
        _call(s, conviction=5, ret_5d_pct=9.0, resolved_posted_at=NOW)
    assert _collect_now() == ([], [])


def test_stamp_makes_collect_idempotent():
    with session_scope() as s:
        _call(s, conviction=5, ret_5d_pct=9.0)
    posts, finalized = _collect_now()
    assert len(posts) == 1
    _stamp(finalized)
    assert _collect_now() == ([], [])  # second cycle: nothing re-fires


def test_posts_sorted_by_absolute_move_desc():
    with session_scope() as s:
        _call(s, ticker="SMALL", conviction=5, ret_5d_pct=1.0)
        _call(s, ticker="HUGE", conviction=5, ret_5d_pct=-22.0)
        _call(s, ticker="MID", conviction=5, ret_5d_pct=8.0)
    posts, _ = _collect_now()
    assert [p["ticker"] for p in posts] == ["HUGE", "MID", "SMALL"]


def test_all_notable_are_finalized_even_beyond_the_render_cap():
    with session_scope() as s:
        for i in range(_MAX_LINES + 5):
            _call(s, ticker=f"T{i}", conviction=5, ret_5d_pct=10.0 + i)
    posts, finalized = _collect_now()
    # _collect returns everything (the cap is applied at render time in _run);
    # critically, every evaluated call is finalized so none rescans.
    assert len(finalized) == _MAX_LINES + 5
    assert len(posts) == _MAX_LINES + 5


def test_backlink_only_when_exactly_one_narrative_match():
    cfg.settings.DISCORD_GUILD_ID = 42
    try:
        # exactly one matching event → linked
        with session_scope() as s:
            _call(s, ticker="NVDA", source="why_moved", conviction=5,
                  ret_5d_pct=9.0)
            s.add(NarrativeEvent(
                ticker="NVDA", ts=NOW - timedelta(days=6), kind="why_moved",
                tier=1, headline="h", channel_id=7, message_id="99"))
        posts, _ = _collect_now()
        assert posts[0]["backlink"] == (
            "https://discord.com/channels/42/7/99"
        )
        assert "https://discord.com/channels/42/7/99" in _line(posts[0])

        # add a second match in-window → ambiguous → no link
        with session_scope() as s:
            s.add(NarrativeEvent(
                ticker="NVDA", ts=NOW - timedelta(days=6, hours=1),
                kind="why_moved", tier=1, headline="h2",
                channel_id=7, message_id="100"))
        posts2, _ = _collect_now()
        assert posts2[0]["backlink"] is None
    finally:
        cfg.settings.DISCORD_GUILD_ID = 0
