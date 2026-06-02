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

from loguru import logger

from .config import settings
from .db import session_scope
from .models import PriceContext

# Cap on how much generated text we hand the extractor — keeps the light-LLM
# token cost bounded; the hard numbers worth checking live in the lead anyway.
_MAX_EXTRACT_CHARS = 2400

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


# ── extraction (light LLM, fail-open) ──────────────────────────────────────


def _coerce_float(v) -> Optional[float]:
    """Tolerantly pull a float out of whatever the model emitted (number,
    or a string like '+11.6%' / '$203.40' / '2.3x'). None if not numeric."""
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    cleaned = v.strip().lstrip("+").replace("$", "").replace("%", "")
    cleaned = cleaned.replace("x", "").replace("×", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _claims_from_json(parsed: list, tickers: set[str]) -> list[Claim]:
    """Build validated Claims from the parsed JSON array. Drops anything whose
    ticker isn't in the candidate set or whose metric isn't in the closed enum
    — extraction is allowed to be noisy; this gate is where it gets clean."""
    out: list[Claim] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        tkr = item.get("ticker")
        metric = item.get("metric")
        if not isinstance(tkr, str) or not isinstance(metric, str):
            continue
        tkr = tkr.strip().upper()
        metric = metric.strip()
        if tkr not in tickers or metric not in _METRICS:
            continue
        word = item.get("direction_word")
        word = word.strip().lower() if isinstance(word, str) else None
        out.append(
            Claim(
                ticker=tkr,
                metric=metric,
                value=_coerce_float(item.get("value")),
                direction_word=word,
                raw=str(item.get("raw", ""))[:200],
            )
        )
    return out


def _run_extraction(text: str, tickers: list[str]) -> tuple[list[Claim], bool]:
    """Light-LLM claim extraction. Returns (claims, available) — `available`
    is False when the extractor couldn't run (LLM error / parse failure /
    exception), which the caller surfaces as an unverified result. Never
    raises."""
    from .llm import LLM_ERROR_SENTINEL, get_llm, parse_json_response
    from .prompts import get_prompt

    cand = {t.strip().upper() for t in tickers if t and t.strip()}
    if not text or not text.strip() or not cand:
        return [], True  # nothing to extract, but the extractor was available

    try:
        rendered = get_prompt("extract_claims").safe_substitute(
            text=text[:_MAX_EXTRACT_CHARS],
            tickers=", ".join(sorted(cand)),
        )
        raw = get_llm().complete(
            rendered, model="light", json_mode=True, max_tokens=500,
            grounded=False,
        )
    except Exception as e:
        logger.debug("extract_claims LLM call failed: {}", e)
        return [], False
    if raw == LLM_ERROR_SENTINEL:
        return [], False
    parsed = parse_json_response(raw, expect=list)
    if parsed is None:
        return [], False
    return _claims_from_json(parsed, cand), True


def extract_claims(text: str, tickers: list[str]) -> list[Claim]:
    """Public fail-open extractor: the hard, ticker-bound numeric claims in
    `text`, constrained to `tickers` and the closed metric enum. Returns []
    on any failure (the caller can't tell 'none found' from 'unavailable' —
    use `verify_text` when that distinction matters)."""
    return _run_extraction(text, tickers)[0]


def verify_text(
    text: str,
    tickers: list[str],
    *,
    surface: str,
    source: str,
    session=None,
) -> VerifyResult:
    """Extract claims from `text` and check them against ground truth.

    `surface` ("call"|"post") and `source` are recorded for telemetry. Sync-
    callable and fully fail-open: extraction unavailable → `ok=False` (the
    call sites treat that as *unverified*, never as a contradiction). Never
    raises."""
    try:
        claims, available = _run_extraction(text, tickers)
    except Exception as e:  # belt-and-suspenders — _run_extraction is fail-open
        logger.debug("verify_text extraction failed: {}", e)
        claims, available = [], False
    try:
        result = check_claims(claims, session=session)
    except Exception as e:
        logger.debug("verify_text check failed: {}", e)
        result = VerifyResult(grounded=True, ok=False)
    if not available:
        result.ok = False
    # Persist + broadcast only runs that actually examined ticker-bound numbers
    # — an unverified or claimless run isn't telemetry, it's just noise.
    if result.ok and result.n_checked > 0:
        _record(result, surface=surface, source=source, tickers=tickers, text=text)
    return result


def _record(
    result: VerifyResult, *, surface: str, source: str, tickers: list[str], text: str
) -> None:
    """Best-effort ClaimCheck row + live event. Never fatal."""
    cand = {t.strip().upper() for t in tickers if t and t.strip()}
    tkr = next(iter(cand)) if len(cand) == 1 else None
    try:
        from .models import ClaimCheck

        with session_scope() as s:
            s.add(
                ClaimCheck(
                    ts=datetime.now(timezone.utc),
                    surface=surface[:8],
                    source=(source or "")[:120],
                    ticker=tkr,
                    n_claims=result.n_checked,
                    n_contradicted=result.n_contradicted,
                    grounded=result.grounded,
                    note=result.note[:500],
                    sample=(text or "")[:500],
                )
            )
    except Exception as e:
        logger.debug("verify ClaimCheck persist failed: {}", e)
    try:
        from . import events

        events.publish(
            "claim_check",
            {
                "surface": surface,
                "source": source,
                "ticker": tkr,
                "n_claims": result.n_checked,
                "n_contradicted": result.n_contradicted,
                "grounded": result.grounded,
                "note": result.note[:200],
            },
        )
    except Exception as e:
        logger.debug("events.publish(claim_check) failed: {}", e)
