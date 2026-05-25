"""Wallet endpoints — standings, per-wallet detail, full trade history
(works for any wallet including the user-directed `research` one)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import funds as _funds


router = APIRouter()


@router.get("/wallets")
def list_wallets() -> list[dict]:
    """Standings across every active fund — same shape the old
    `_funds_panel` rendered, but as JSON."""
    return _funds.fund_standings()


@router.get("/wallets/meta")
def wallet_meta() -> dict:
    """Edge-experiment readout: per-fund per-source / per-conviction /
    per-asset breakdowns + the autonomous edge-experiment verdicts."""
    return _funds.wallet_meta()


@router.get("/wallets/{name}")
def wallet_positions(name: str) -> dict:
    """Open positions snapshot for one autonomous wallet."""
    d = _funds.fund_positions(name)
    if d is None:
        raise HTTPException(404, f"wallet {name!r} not found or unseeded")
    return d


@router.get("/wallets/{name}/history")
def wallet_history(name: str, days: int = 90) -> dict:
    """Open + closed trades on a wallet (90d default). Accepts ANY
    wallet name including `research` (no `_POLICIES` gating). Closed
    trades include open_reason + close_reason for the audit trail."""
    d = _funds.trade_history(name, days)
    if d is None:
        raise HTTPException(404, f"wallet {name!r} not found")
    return d
