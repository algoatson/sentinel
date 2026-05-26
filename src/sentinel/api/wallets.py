"""Wallet endpoints — standings, per-wallet detail, full trade history
(works for any wallet including the user-directed `research` one)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from .. import funds as _funds
from ..db import session_scope
from ..models import Fund


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


# ── Editable policy + active toggle ────────────────────────────────────


def _policy_payload(fund: Fund) -> dict:
    """Render a fund's effective policy as JSON. Includes the resolved
    values (with code defaults filled in) plus a flag per field showing
    whether it's a DB override or the seed default — the UI uses that
    to render "reset to default" affordances."""
    base = _funds._POLICIES.get(fund.name) or {}
    out: dict = {
        "name": fund.name,
        "mandate": fund.mandate,
        "active": fund.active,
        "starting_cash": fund.starting_cash,
        "cash": round(fund.cash, 2),
        "knobs": {},
    }
    for k in (
        "size_pct", "max_positions", "stop_pct", "take_pct",
        "max_hold_days", "min_conviction", "max_opens_per_day",
    ):
        db_v = getattr(fund, k, None)
        default = base.get(k)
        out["knobs"][k] = {
            "value": db_v if db_v is not None else default,
            "default": default,
            "overridden": db_v is not None,
        }
    # Read-only seed metadata so the editor can show what the wallet
    # listens for (sources / asset classes) — not editable yet.
    out["sources"] = sorted(list(base.get("sources") or []))
    out["asset_classes"] = sorted(list(base.get("asset_classes") or [])) or None
    return out


@router.get("/wallets/{name}/policy")
def wallet_policy(name: str) -> dict:
    """Resolved policy for a wallet — code defaults overlaid with DB
    overrides. UI calls this to populate the edit drawer."""
    with session_scope() as s:
        fund = s.exec(select(Fund).where(Fund.name == name)).first()
        if fund is None:
            raise HTTPException(404, f"wallet {name!r} not found")
        return _policy_payload(fund)


class PolicyPatch(BaseModel):
    """Partial-update of a wallet's editable knobs. Send `null` to
    *reset to default* (the DB column clears, engine falls back to the
    code default). `active` toggles the wallet's autonomous trading."""
    active: bool | None = Field(default=None)
    mandate: str | None = Field(default=None, max_length=400)
    size_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    max_positions: int | None = Field(default=None, ge=1, le=50)
    stop_pct: float | None = Field(default=None, ge=-0.95, le=0.0)
    take_pct: float | None = Field(default=None, ge=0.0, le=10.0)
    max_hold_days: int | None = Field(default=None, ge=1, le=365)
    min_conviction: int | None = Field(default=None, ge=1, le=5)
    max_opens_per_day: int | None = Field(default=None, ge=1, le=50)
    # Explicit list of fields the caller wants to "clear" (set to NULL
    # in the DB so the engine falls back to the code default). Without
    # this we couldn't distinguish "user didn't include this field"
    # from "user wants the default".
    clear: list[str] | None = Field(default=None)


_EDITABLE_KNOBS = (
    "size_pct", "max_positions", "stop_pct", "take_pct",
    "max_hold_days", "min_conviction", "max_opens_per_day",
)


@router.patch("/wallets/{name}/policy")
def update_wallet_policy(name: str, body: PolicyPatch) -> dict:
    """Update one or more wallet knobs (or `active`). Engine picks up
    the change on the next `_run()` cycle — no restart needed.

    `clear=["size_pct"]` resets that knob to the code default. Pass
    None on any knob to leave it untouched.
    """
    payload = body.model_dump(exclude_unset=True)
    clear = set(payload.pop("clear", None) or [])
    bad = clear - set(_EDITABLE_KNOBS)
    if bad:
        raise HTTPException(
            400, f"unknown knob in clear: {sorted(bad)}",
        )
    with session_scope() as s:
        fund = s.exec(select(Fund).where(Fund.name == name)).first()
        if fund is None:
            raise HTTPException(404, f"wallet {name!r} not found")
        for k, v in payload.items():
            # Explicit None on a normal field is treated as "no change"
            # — that's what `clear` is for. exclude_unset already
            # ensured these keys are explicitly set, so we only skip
            # the literal-None body of explicit ones (rare).
            if v is None and k not in ("active",):
                continue
            setattr(fund, k, v)
        for k in clear:
            setattr(fund, k, None)
        s.add(fund)
        # Re-read after commit to render the resolved policy.
        s.flush()
        s.refresh(fund)
        return _policy_payload(fund)
