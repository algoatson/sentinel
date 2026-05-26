"""Unified position-book endpoints.

- ``GET /api/positions/open`` — every open trade across every wallet
  (one batched query), with risk-mgmt fields + per-row r_multiple +
  pct_of_equity. Powers the /book table.
- ``POST /api/positions/{id}/close`` — close one position at the
  current mark; publishes a `trade` SSE event.
- ``PATCH /api/positions/{id}/risk`` — set/clear stop / target /
  trailing / notes. The auto_exits pipeline enforces stops every 5min.
- ``POST /api/positions/bulk-close`` — close N trades in one call
  ("close all losers" / "flatten degen" workflows).
- ``GET /api/positions/export.csv`` — Excel-friendly CSV of the
  current open book.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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


class RiskRequest(BaseModel):
    """Partial-update payload. Missing field → leave alone. To
    explicitly clear a value, send its name in `clear`."""
    stop_price: float | None = Field(default=None, gt=0)
    target_price: float | None = Field(default=None, gt=0)
    trailing_stop_pct: float | None = Field(default=None, gt=0, lt=1)
    notes: str | None = Field(default=None, max_length=2000)
    clear: list[str] | None = Field(default=None)


@router.patch("/positions/{trade_id}/risk")
def update_risk(trade_id: int, body: RiskRequest) -> dict:
    res = _funds.update_trade_risk(
        trade_id,
        stop_price=body.stop_price,
        target_price=body.target_price,
        trailing_stop_pct=body.trailing_stop_pct,
        notes=body.notes,
        clear=body.clear,
    )
    if not res["ok"]:
        raise HTTPException(400, res["message"])
    return res


class BulkCloseRequest(BaseModel):
    trade_ids: list[int] = Field(..., min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=200)


@router.post("/positions/bulk-close")
def bulk_close(body: BulkCloseRequest) -> dict:
    """Close N positions in one request. Continues on per-trade
    failures and returns the per-trade result map so the UI can
    report "closed 5 of 7, see details"."""
    reason = body.reason or "manual bulk"
    results: dict[int, dict] = {}
    closed = 0
    total_pnl = 0.0
    for tid in body.trade_ids:
        res = _funds.close_trade_by_id(tid, reason)
        results[tid] = res
        if res.get("ok"):
            closed += 1
            if res.get("realized_pnl") is not None:
                total_pnl += res["realized_pnl"]
    return {
        "ok": closed > 0,
        "closed": closed,
        "attempted": len(body.trade_ids),
        "total_realized_pnl": round(total_pnl, 2),
        "results": results,
    }


def _csv_payload() -> bytes:
    rows = _funds.open_positions_all()
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    cols = [
        "id", "fund", "ticker", "side", "qty",
        "entry", "mark", "upnl", "upnl_pct", "r_multiple",
        "stop_price", "target_price", "trailing_stop_pct",
        "watermark_price", "dist_to_stop_pct", "dist_to_target_pct",
        "pct_of_equity", "notional",
        "age_h", "entry_at",
        "open_reason", "notes",
    ]
    writer.writerow(cols)
    for r in rows:
        writer.writerow([
            "" if r.get(c) is None else r.get(c)
            for c in cols
        ])
    return buf.getvalue().encode("utf-8")


@router.get("/positions/export.csv")
def export_csv() -> StreamingResponse:
    """One-shot CSV export of the open book. UTF-8 BOM so Excel
    auto-detects encoding and renders unicode tickers correctly."""
    payload = b"\xef\xbb\xbf" + _csv_payload()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    return StreamingResponse(
        iter([payload]),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="sentinel_book_{stamp}.csv"'
            ),
        },
    )
