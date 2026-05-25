"""Unified position-book endpoints.

`/api/positions/open` returns every open trade across every wallet
in one batched query — what the dashboard's /book view consumes.

`POST /api/positions/{id}/close` closes one position at the current
mark and publishes a `trade` SSE event so the bell + feed update
in real time."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import funds as _funds


router = APIRouter()


@router.get("/positions/open")
def open_positions() -> list[dict]:
    return _funds.open_positions_all()


@router.post("/positions/{trade_id}/close")
def close_position(trade_id: int, reason: str | None = None) -> dict:
    res = _funds.close_trade_by_id(trade_id, reason or "manual")
    if not res["ok"]:
        raise HTTPException(400, res["message"])
    return res
