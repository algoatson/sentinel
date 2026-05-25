"""Health + system metrics endpoints — for the System tab."""

from __future__ import annotations

from fastapi import APIRouter, Query

from .. import health as _health_mod
from ..dashboard import logbuf, sysinfo


router = APIRouter()


@router.get("/health")
def health_report() -> dict:
    return _health_mod.health_report()


@router.get("/health/system")
def system_metrics() -> dict:
    return sysinfo.snapshot()


@router.get("/health/logs")
def system_logs(n: int = Query(220, ge=1, le=500)) -> dict:
    """Last `n` lines of the bot's loguru ring buffer. Same source the
    v1 cockpit's Live-log panel reads — see `dashboard/logbuf.py`.

    Returns `{"lines": [...]}` so the response shape can grow later
    (e.g. add a `version` or `next_token` for incremental polls)
    without breaking the client. Each line is the rendered loguru
    record, including timestamp + level + module:line - message."""
    return {"lines": logbuf.tail(n)}
