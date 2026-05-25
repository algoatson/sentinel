"""Catalyst-calendar endpoint — upcoming macro events + earnings."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter


router = APIRouter()


@router.get("/catalysts")
async def upcoming() -> dict:
    """Next 14 days of macro + earnings events. The pipeline's
    `_run_sync` is sync (yaml read + yfinance earnings lookup) so we
    wrap it in to_thread to avoid blocking the loop."""
    from ..pipelines import catalysts as _c

    text, events = await asyncio.to_thread(_c._run_sync)
    return {"text": text, "events": events}
