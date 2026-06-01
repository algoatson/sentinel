"""Cockpit seams.

The NiceGUI page itself is an I/O shell (not unit-tested, per this
codebase's convention). What IS pinned here are the pure pieces it leans on
and the shared Q&A chokepoint it must not fork:

- `sysinfo._db_path`: a sqlite URL → file path mapping (drives the DB-size
  gauge; a wrong split would silently report 0 B forever);
- `logbuf`: the bounded ring + idempotent sink install (an unbounded buffer
  in a long-lived bot is a slow leak);
- `app._verdict_marker`: health-text → header chip severity;
- `chat.answer_question`: the one Q&A path Discord AND the dashboard use —
  the whole point of the refactor is that there is exactly one.
"""

from __future__ import annotations

import asyncio

from sentinel import chat
from sentinel.dashboard import logbuf, sysinfo
from sentinel.dashboard.app import _log_html, _swap_chart, _verdict_marker


# ── sysinfo ─────────────────────────────────────────────────────────────────


def test_db_path_maps_sqlite_url_to_file_and_ignores_others():
    assert sysinfo._db_path("sqlite:///./data/radar.db") == "./data/radar.db"
    assert sysinfo._db_path("sqlite:////abs/x.db") == "/abs/x.db"
    assert sysinfo._db_path("postgresql://h/db") is None
    assert sysinfo._db_path("sqlite:///") is None


def test_human_and_uptime_format():
    assert sysinfo._human(0) == "0 B"
    assert sysinfo._human(2048) == "2.0 KB"
    assert sysinfo._human(5 * 1024 * 1024) == "5.0 MB"
    assert sysinfo.fmt_uptime(None) == "—"
    assert sysinfo.fmt_uptime(90) == "1m"
    assert sysinfo.fmt_uptime(3 * 3600 + 5 * 60) == "3h 5m"
    assert sysinfo.fmt_uptime(2 * 86400 + 3600) == "2d 1h 0m"


def test_snapshot_is_total_and_never_raises():
    s = sysinfo.snapshot()
    # contract: every gauge key present so the card never KeyErrors
    for k in ("cpu_pct", "rss_mb", "threads", "db_human",
              "llm_calls", "llm_errors"):
        assert k in s


# ── verdict marker ──────────────────────────────────────────────────────────


def test_tick_now_gates_on_active_tab(monkeypatch):
    """Tab-aware pausing: a `_tick_now(tab='X')` body only runs when
    `_ACTIVE_TAB == 'X'`. First-paint tick is unconditional so the
    panel isn't blank when the user later switches to it."""
    import asyncio as _asyncio
    from sentinel.dashboard import app

    calls = {"normal": 0, "first": 0}

    async def _coro():
        calls["normal"] += 1

    # Fake `ui.timer` that immediately calls the function once if it's
    # the once=True kind, and once otherwise — enough to exercise both
    # paths without needing a real event loop.
    class _FakeUI:
        @staticmethod
        def timer(interval, callback, once=False):
            if once:
                # First-paint: always runs (force=True path)
                async def _go():
                    calls["first"] += 1
                    await callback()
                _asyncio.run(_go())
            else:
                # Interval-tick: respects the active-tab gate
                _asyncio.run(callback())

    # Wrong tab → only the first-paint fires; interval body is gated out
    monkeypatch.setattr(app, "_ACTIVE_TAB", "markets")
    app._tick_now(_FakeUI, _coro, 0.0, tab="overview")
    assert calls == {"normal": 1, "first": 1}  # first-paint forced, interval skipped

    # Right tab → both fire
    calls["normal"] = 0
    calls["first"] = 0
    monkeypatch.setattr(app, "_ACTIVE_TAB", "overview")
    app._tick_now(_FakeUI, _coro, 0.0, tab="overview")
    assert calls == {"normal": 2, "first": 1}  # first-paint + interval


def test_tick_now_with_no_tab_always_runs(monkeypatch):
    """Backwards-compat: `_tick_now(...)` without `tab=` keeps the
    old always-on behaviour. Important for any legacy panels that
    haven't been migrated."""
    import asyncio as _asyncio
    from sentinel.dashboard import app

    counter = {"n": 0}

    async def _coro():
        counter["n"] += 1

    class _FakeUI:
        @staticmethod
        def timer(interval, callback, once=False):
            _asyncio.run(callback())

    monkeypatch.setattr(app, "_ACTIVE_TAB", "anything")
    app._tick_now(_FakeUI, _coro, 0.0)  # no tab kwarg
    # Both the first-paint and the interval ran → 2 calls
    assert counter["n"] == 2


def test_swap_chart_mutates_dict_in_place_and_updates():
    """Regression: NiceGUI 3.12's `EChart.options` is read-only — direct
    assignment raises 'property has no setter' and the chart never updates.
    `_swap_chart` must mutate the dict in place and trigger `update()`.

    Caught live on 2026-05-23 — every chart refresh on the Pi was logging
    the setter error; charts were frozen on initial empty specs."""
    class _FakeChart:
        def __init__(self) -> None:
            self._opts = {"old": True, "stale": 1}
            self.updated = 0

        @property
        def options(self) -> dict:  # mirrors NiceGUI's getter-only property
            return self._opts

        def update(self) -> None:
            self.updated += 1

    c = _FakeChart()
    _swap_chart(c, {"backgroundColor": "transparent", "series": []})
    # dict identity preserved (clear+update, not replace) → anyone holding
    # a ref to the old options sees the new content
    assert "old" not in c.options
    assert c.options["backgroundColor"] == "transparent"
    assert c.options["series"] == []
    assert c.updated == 1


def test_verdict_marker_picks_worst_then_defaults():
    assert _verdict_marker("✅ all nominal") == "✅"
    assert _verdict_marker("⚠️ 2 warnings\n✅ x") == "⚠️"
    # critical dominates even when warnings/ok also appear in the text
    assert _verdict_marker("🔴 1 critical, ⚠️ 1 warning ✅") == "🔴"
    assert _verdict_marker("no markers here") == "•"


# ── log viewer rendering ────────────────────────────────────────────────────


def test_log_html_colours_by_level_and_escapes():
    html = _log_html([
        "12:00:01 | INFO    | sentinel.x:10 - started <ok>",
        "12:00:02 | ERROR   | sentinel.y:20 - boom & crash",
    ])
    # level drives the line class (CSS colours it)
    assert 'class="ln l-INFO"' in html
    assert 'class="ln l-ERROR"' in html
    # the timestamp + level get their own spans
    assert '<span class="tm">12:00:01</span>' in html
    assert '<span class="lv l-ERROR">ERROR</span>' in html
    # message text is HTML-escaped (no raw < & in output)
    assert "&lt;ok&gt;" in html and "boom &amp; crash" in html
    assert "<ok>" not in html


def test_log_html_keeps_unparseable_lines_visible():
    # a traceback continuation has no "ts | LEVEL | msg" shape — must still
    # render (uncoloured), never silently vanish
    html = _log_html(["    File \"x.py\", line 3, in <module>"])
    assert 'class="ln l-INFO"' in html
    assert "&lt;module&gt;" in html
    # empty tail still yields a non-empty placeholder (card never blank)
    assert _log_html([]) != ""


# ── logbuf ──────────────────────────────────────────────────────────────────


def test_ring_is_bounded_and_tail_is_chronological():
    logbuf._RING.clear()
    for i in range(600):
        logbuf._RING.append(str(i))
    assert len(logbuf._RING) == logbuf._RING.maxlen == 500   # oldest dropped
    assert logbuf.tail(3) == ["597", "598", "599"]            # newest-last
    assert logbuf.tail(0) == []
    assert logbuf.text(2) == "598\n599"


def test_sink_captures_and_never_raises():
    logbuf._RING.clear()
    logbuf._sink("hello world\n")
    logbuf._sink(12345)  # non-str must not raise
    assert logbuf._RING[0] == "hello world"


def test_install_is_idempotent():
    logbuf.install()
    first = logbuf._sink_id
    logbuf.install()
    assert logbuf._sink_id == first and first is not None


# ── shared Q&A chokepoint ───────────────────────────────────────────────────


class _FakeLLM:
    def __init__(self, out):
        self.out = out
        self.seen: list[str] = []

    def complete(self, prompt, *, model=None, max_tokens=None):
        self.seen.append(prompt)
        return self.out


def test_answer_question_blank_short_circuits_without_llm(monkeypatch):
    def _boom():
        raise AssertionError("LLM must not be touched for an empty question")

    monkeypatch.setattr(chat, "get_llm", _boom)
    assert asyncio.run(chat.answer_question("   ")) == ""


# `use_tools=False` exercises the one-shot `complete` chokepoint these
# fakes mock. The default (use_tools=True) routes through the tool loop,
# which calls the API via llm_tools — bypassing a `chat.get_llm` patch and
# hitting the network. These tests target the success/sentinel/empty
# mapping, which lives in the one-shot path.
def test_answer_question_returns_text_on_success(monkeypatch):
    fake = _FakeLLM("Here's the read: $SPY looks heavy.")
    monkeypatch.setattr(chat, "get_llm", lambda: fake)
    out = asyncio.run(chat.answer_question("what looks good today", use_tools=False))
    assert out == "Here's the read: $SPY looks heavy."
    assert fake.seen and "what looks good today" in fake.seen[0]


def test_answer_question_maps_failure_to_sentinel(monkeypatch):
    monkeypatch.setattr(chat, "get_llm", lambda: _FakeLLM("[LLM_ERROR] down"))
    assert asyncio.run(chat.answer_question("anything", use_tools=False)) == "[LLM_ERROR]"
    monkeypatch.setattr(chat, "get_llm", lambda: _FakeLLM(""))
    assert asyncio.run(chat.answer_question("anything", use_tools=False)) == "[LLM_ERROR]"
