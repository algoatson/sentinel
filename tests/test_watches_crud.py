"""`watches.list_watches` / `remove_watch` — read/delete chokepoints shared
by Discord !watches/!unwatch and the dashboard Watches panel.

Not testing `add_watch` here — it goes through an LLM. The list/remove
contract is pure DB and lives here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sentinel.db import session_scope
from sentinel.models import Watch
from sentinel.pipelines import watches as W

UTC = timezone.utc


def _seed_watch(text: str, *, active: bool = True,
                trips: int = 0,
                last: datetime | None = None) -> int:
    with session_scope() as s:
        row = Watch(
            raw_text=text,
            condition_json="{}",
            created_at=datetime.now(UTC) - timedelta(minutes=1),
            active=active,
            trigger_count=trips,
            last_triggered_at=last,
        )
        s.add(row)
        s.flush()
        return row.id


def test_list_empty_when_none_set():
    assert W.list_watches() == []


def test_list_carries_state_and_orders_by_created_at():
    a = _seed_watch("first one", trips=2)
    b = _seed_watch("paused one", active=False, trips=0)
    rows = W.list_watches()
    assert [r["id"] for r in rows] == [a, b]
    assert rows[0]["raw_text"] == "first one"
    assert rows[0]["active"] is True and rows[0]["trigger_count"] == 2
    assert rows[1]["active"] is False


def test_remove_happy_and_unknown_and_bad_id():
    wid = _seed_watch("to remove")
    ok = W.remove_watch(wid)
    assert ok["ok"] and ok["watch_id"] == wid
    again = W.remove_watch(wid)
    assert not again["ok"] and "no watch" in again["message"]
    bad = W.remove_watch("not-a-number")
    assert not bad["ok"] and "bad watch id" in bad["message"]


def test_remove_accepts_hash_prefix_and_string():
    wid = _seed_watch("hash-prefixable")
    res = W.remove_watch(f"#{wid}")
    assert res["ok"] and res["watch_id"] == wid
