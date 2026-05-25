"""Health + system metrics endpoints — for the System tab."""

from __future__ import annotations

from fastapi import APIRouter

from .. import health as _health_mod
from ..dashboard import sysinfo


router = APIRouter()


@router.get("/health")
def health_report() -> dict:
    return _health_mod.health_report()


@router.get("/health/system")
def system_metrics() -> dict:
    return sysinfo.snapshot()
