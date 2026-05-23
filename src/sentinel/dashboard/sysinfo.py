"""Process + datastore resource snapshot for the cockpit's System card.

Deliberately about *this* process (the bot), not the whole box: a single-user
bot's health question is "is my bot bloating / leaking fds / pegging a core /
is the DB ballooning", not host averages. `_db_path` is pulled out pure so it
can be unit-tested without a running process; the psutil reads are inherently
live but cheap (no blocking calls — `cpu_percent(None)` is non-sampling).
"""

from __future__ import annotations

import os
import time

import psutil

from ..db import DB_URL
from ..llm import llm_stats

_PROC = psutil.Process()
# Prime the non-blocking CPU meter so the first real read is a delta, not 0.0
# against process start.
try:
    _PROC.cpu_percent(None)
except Exception:
    pass


def _db_path(db_url: str) -> str | None:
    """Filesystem path of a sqlite URL, else None (non-sqlite has no file)."""
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        return None
    return db_url[len(prefix):] or None


def _db_bytes() -> int:
    """On-disk size of the sqlite DB + its WAL/SHM siblings (the WAL can be a
    big chunk of real footprint, so count it)."""
    path = _db_path(DB_URL)
    if not path:
        return 0
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(path + suffix)
        except OSError:
            pass
    return total


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} GB"


def snapshot() -> dict:
    """A flat dict of cheap process/DB/LLM gauges. Never raises — a missing
    metric degrades to a dash rather than breaking the card."""
    out: dict = {}
    try:
        with _PROC.oneshot():
            out["cpu_pct"] = round(_PROC.cpu_percent(None), 1)
            out["rss_mb"] = round(_PROC.memory_info().rss / 1024 / 1024, 1)
            out["threads"] = _PROC.num_threads()
            try:
                out["fds"] = _PROC.num_fds()  # POSIX only
            except Exception:
                out["fds"] = None
            out["uptime_s"] = int(time.time() - _PROC.create_time())
    except Exception:
        out.setdefault("cpu_pct", None)
        out.setdefault("rss_mb", None)
        out.setdefault("threads", None)
        out.setdefault("fds", None)
        out.setdefault("uptime_s", None)

    db = _db_bytes()
    out["db_bytes"] = db
    out["db_human"] = _human(db) if db else "—"

    try:
        ls = llm_stats()
        out["llm_calls"] = ls.get("calls", 0)
        out["llm_errors"] = ls.get("errors", 0)
    except Exception:
        out["llm_calls"] = out["llm_errors"] = 0

    return out


def fmt_uptime(seconds: int | None) -> str:
    if not seconds or seconds < 0:
        return "—"
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"
