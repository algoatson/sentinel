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


@router.get("/positions/closed")
def closed_positions(
    limit: int = 100,
    fund: str | None = None,
) -> list[dict]:
    """Closed trades across every wallet (or filtered by `fund`),
    newest first. Used by /journal — see funds.closed_trades_recent."""
    return _funds.closed_trades_recent(limit=limit, fund_name=fund)


class JournalRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=2000)


@router.patch("/positions/{trade_id}/journal")
def update_journal(trade_id: int, body: JournalRequest) -> dict:
    """Edit the notes/journal on any trade (open OR closed). Returns
    `{"ok": bool, ...}`."""
    res = _funds.update_trade_journal(trade_id, body.notes)
    if not res["ok"]:
        raise HTTPException(400, res["message"])
    return res


@router.get("/positions/{trade_id}/lifecycle")
def trade_lifecycle(trade_id: int) -> dict:
    """News, filings, and trading-calls about the trade's ticker that
    happened between entry and exit (or now, for open trades). Powers
    the journal "what happened while I was in" drill-in."""
    res = _funds.trade_lifecycle(trade_id)
    if res is None:
        raise HTTPException(404, f"no trade #{trade_id}")
    return res


@router.post("/positions/{trade_id}/close")
def close_position(trade_id: int, reason: str | None = None) -> dict:
    res = _funds.close_trade_by_id(trade_id, reason or "manual")
    if not res["ok"]:
        raise HTTPException(400, res["message"])
    return res


class OpenRequest(BaseModel):
    """Manual paper-trade open. Pass ONE of qty / notional /
    (risk_pct + stop_price) for sizing."""
    fund_name: str = Field(..., min_length=1, max_length=64)
    ticker: str = Field(..., min_length=1, max_length=16)
    side: str = Field(..., pattern="^(long|short)$")
    qty: float | None = Field(default=None, gt=0)
    notional: float | None = Field(default=None, gt=0)
    risk_pct: float | None = Field(default=None, gt=0, lt=0.5)
    stop_price: float | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/positions/open")
def open_position(body: OpenRequest) -> dict:
    res = _funds.open_trade_manual(
        fund_name=body.fund_name,
        ticker=body.ticker,
        side=body.side,
        qty=body.qty,
        notional=body.notional,
        risk_pct=body.risk_pct,
        stop_price=body.stop_price,
        note=body.note,
    )
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
