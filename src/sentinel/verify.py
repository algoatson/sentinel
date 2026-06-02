"""Fact-verification layer — the enforcement half of "never fabricate".

`grounding.py` *prevents* (a trust-the-data preamble on every LLM call);
nothing until now *checked* the numbers the model emits. This module is the
check: at the two accountability chokepoints (`scorecard.record_call` and
`discord_client.post_embed`) it extracts the hard, ticker-bound numeric
claims from generated text and compares them against the DB ground truth
(`PriceContext`), then flags and annotates the mismatches.

Design rules (load-bearing — don't relax them):

- **Annotate + flag, never block.** A contradiction adds a warning field /
  floors a call's conviction; it never drops a call or holds a post.
- **Fail-open.** Extraction unavailable or anything throwing → the item
  proceeds *unverified* (``grounded=None`` at the call sites), never crashes a
  pipeline.
- **Only check ground-truthable, ticker-bound numbers.** Closed metric set:
  ``price``, ``change_1d_pct``, ``change_5d_pct``, ``vol_mult``, ``direction``.
  Anything forward-looking, qualitative, or macro is *unverifiable* and never
  penalized — a false contradiction erodes trust in the whole layer, so we err
  hard toward "unverifiable" over "contradicted".

`check_claims` is pure/deterministic (tested at every tolerance edge).
`extract_claims` / `verify_text` add the light-LLM extraction + persistence on
top and are fail-open around it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional


from .config import settings
from .db import session_scope
from .models import PriceContext

# The closed, ground-truthable metric set. Anything else is unverifiable.
Metric = Literal["price", "change_1d_pct", "change_5d_pct", "vol_mult", "direction"]
_METRICS: frozenset[str] = frozenset(
    ("price", "change_1d_pct", "change_5d_pct", "vol_mult", "direction")
)

Status = Literal["supported", "contradicted", "unverifiable"]


@dataclass
class Claim:
    """One hard, ticker-bound numeric assertion pulled from generated text.

    `value` is the number as stated by the model — for the %-move metrics it
    is in *percentage points* (e.g. ``11.6`` for +11.6%), matching how the
    pipelines display `change_*_pct` (the DB stores them as fractions). For
    ``direction`` the number is irrelevant and `direction_word` ("up"/"down")
    carries the claim instead.
    """

    ticker: str
    metric: str
    value: Optional[float] = None
    direction_word: Optional[str] = None
    raw: str = ""


@dataclass
class ClaimVerdict:
    claim: Claim
    status: Status
    actual: Optional[float] = None
    detail: str = ""


@dataclass
class VerifyResult:
    verdicts: list[ClaimVerdict] = field(default_factory=list)
    n_checked: int = 0
    n_supported: int = 0
    n_contradicted: int = 0
    n_unverifiable: int = 0
    grounded: bool = True
    note: str = ""
    ok: bool = True


# ── ground-truth normalisation ────────────────────────────────────────────


def _naive_utc(dt: datetime) -> datetime:
    """Coerce a (possibly tz-aware) timestamp to naive-UTC for comparison —
    PriceContext.last_updated is stored naive-UTC like the rest of the DB."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _verdict_for(claim: Claim, pc: PriceContext | None, now: datetime) -> ClaimVerdict:
    """Compare a single claim against the price-context row. Pure: all
    tolerances come from `settings`. Errs toward 'unverifiable' — a false
    'contradicted' is worse than a miss."""
    metric = claim.metric
    if metric not in _METRICS:
        return ClaimVerdict(claim, "unverifiable", None, f"unknown metric '{metric}'")
    if pc is None:
        return ClaimVerdict(claim, "unverifiable", None, "no price context for ticker")

    # Stale ground truth can't fairly verify a figure (e.g. a weekend gap).
    age_h = (now - _naive_utc(pc.last_updated)).total_seconds() / 3600.0
    if age_h > settings.VERIFY_CONTEXT_STALE_HOURS:
        return ClaimVerdict(
            claim, "unverifiable", None, f"price context stale ({age_h:.0f}h old)"
        )

    if metric == "direction":
        return _check_direction(claim, pc)

    if claim.value is None:
        return ClaimVerdict(claim, "unverifiable", None, "no numeric value")

    if metric == "price":
        return _check_price(claim, pc)
    if metric in ("change_1d_pct", "change_5d_pct"):
        return _check_pct(claim, pc, metric)
    if metric == "vol_mult":
        return _check_vol(claim, pc)
    # Defensive — _METRICS guards this, but keep the fall-through explicit.
    return ClaimVerdict(claim, "unverifiable", None, f"unhandled metric '{metric}'")


def _check_price(claim: Claim, pc: PriceContext) -> ClaimVerdict:
    lp = pc.last_price
    if not lp:
        return ClaimVerdict(claim, "unverifiable", None, "no last price on record")
    diff_pct = abs(claim.value - lp) / abs(lp) * 100.0
    if diff_pct <= settings.VERIFY_PRICE_TOL_PCT:
        return ClaimVerdict(
            claim, "supported", lp,
            f"{claim.ticker} price {claim.value:g} ≈ {lp:g} ({diff_pct:.1f}% off)",
        )
    return ClaimVerdict(
        claim, "contradicted", lp,
        f"{claim.ticker} price stated {claim.value:g} vs actual {lp:g} "
        f"({diff_pct:.1f}% off)",
    )


def _check_pct(claim: Claim, pc: PriceContext, metric: str) -> ClaimVerdict:
    # DB stores the move as a fraction (0.116); the model states percentage
    # points (11.6). Convert ground truth to pp explicitly before comparing.
    frac = pc.change_1d_pct if metric == "change_1d_pct" else pc.change_5d_pct
    actual_pp = frac * 100.0
    diff = abs(claim.value - actual_pp)
    # Looser of an absolute pp band OR a 25% relative band — a big move has a
    # wider acceptable rounding error than a small one.
    rel = 0.25 * abs(actual_pp)
    label = "1d" if metric == "change_1d_pct" else "5d"
    if diff <= settings.VERIFY_PCT_TOL_PP or diff <= rel:
        return ClaimVerdict(
            claim, "supported", actual_pp,
            f"{claim.ticker} {label} {claim.value:+g}% ≈ {actual_pp:+.2f}%",
        )
    return ClaimVerdict(
        claim, "contradicted", actual_pp,
        f"{claim.ticker} {label} stated {claim.value:+g}% vs actual "
        f"{actual_pp:+.2f}%",
    )


def _check_vol(claim: Claim, pc: PriceContext) -> ClaimVerdict:
    actual = pc.volume_vs_20d_avg
    if abs(claim.value - actual) <= settings.VERIFY_VOL_TOL:
        return ClaimVerdict(
            claim, "supported", actual,
            f"{claim.ticker} vol {claim.value:g}× ≈ {actual:g}×",
        )
    return ClaimVerdict(
        claim, "contradicted", actual,
        f"{claim.ticker} vol stated {claim.value:g}× vs actual {actual:g}×",
    )


def _check_direction(claim: Claim, pc: PriceContext) -> ClaimVerdict:
    word = (claim.direction_word or "").strip().lower()
    if word not in ("up", "down"):
        return ClaimVerdict(claim, "unverifiable", None, "no up/down direction word")
    actual = pc.change_1d_pct  # fraction; sign is all that matters
    if actual == 0:
        return ClaimVerdict(
            claim, "unverifiable", 0.0, f"{claim.ticker} flat on the day"
        )
    actual_up = actual > 0
    claimed_up = word == "up"
    actual_pp = actual * 100.0
    if claimed_up == actual_up:
        return ClaimVerdict(
            claim, "supported", actual_pp,
            f"{claim.ticker} {word} matches 1d {actual_pp:+.2f}%",
        )
    return ClaimVerdict(
        claim, "contradicted", actual_pp,
        f"{claim.ticker} stated {word} but 1d is {actual_pp:+.2f}%",
    )


def _check_claims(claims: list[Claim], session) -> VerifyResult:
    now = _naive_utc(datetime.now(timezone.utc))
    verdicts: list[ClaimVerdict] = []
    # Cache one PriceContext fetch per ticker across its claims.
    cache: dict[str, PriceContext | None] = {}
    for c in claims:
        tkr = (c.ticker or "").upper()
        if tkr not in cache:
            cache[tkr] = session.get(PriceContext, tkr) if tkr else None
        verdicts.append(_verdict_for(c, cache[tkr], now))

    n_sup = sum(1 for v in verdicts if v.status == "supported")
    n_con = sum(1 for v in verdicts if v.status == "contradicted")
    n_unv = sum(1 for v in verdicts if v.status == "unverifiable")
    contradictions = [v for v in verdicts if v.status == "contradicted"]
    note = "; ".join(v.detail for v in contradictions[:3])
    return VerifyResult(
        verdicts=verdicts,
        n_checked=len(verdicts),
        n_supported=n_sup,
        n_contradicted=n_con,
        n_unverifiable=n_unv,
        grounded=(n_con == 0),
        note=note,
        ok=True,
    )


def check_claims(claims: list[Claim], *, session=None) -> VerifyResult:
    """Deterministically verify a list of claims against PriceContext.

    Pure given the DB state — no LLM, no network. Empty claims → a grounded,
    ok result with nothing checked. Opens its own read session when one isn't
    supplied."""
    if not claims:
        return VerifyResult(grounded=True, ok=True)
    if session is not None:
        return _check_claims(claims, session)
    with session_scope() as s:
        return _check_claims(claims, s)
