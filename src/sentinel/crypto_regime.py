"""BTC market-regime gate — the market-context filter the crypto leg lacked.

Every crypto signal the bot generates (funding_squeeze, plus why_moved /
convergence / synthesis calls on coins) was fired per-ticker with zero
awareness of what the broader crypto market was doing. But alts carry ~80%
beta to BTC: a textbook per-coin "short-squeeze long" taken while BTC is
breaking down gets dragged under regardless of the coin's own funding setup.
That's the single biggest structural hole in the crypto leg and the most
reliable way it bleeds.

This module reads BTC (the market proxy) and classifies the tape into
risk_on / neutral / risk_off from its 1d and 5d returns. Callers use
`blocks_entry` to refuse counter-trend crypto entries:

  * risk_off  → no new LONGS  (don't catch a falling market)
  * risk_on   → no new SHORTS (don't fade a ripping market)
  * neutral   → no gating (mean-reversion / fades are fine)

BTC itself is never gated against itself (it *is* the regime). The gate
fails OPEN to neutral when BTC data is missing or stale — it's a filter to
stop bad trades, not a load-bearing kill switch, and the crypto wallet's
`require_micro` gate is a separate safety net. Pure + deterministic: no LLM,
reads one PriceContext row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import PriceContext

# Market proxy. BTC leads the entire complex; ETH is too correlated to add
# signal and minor coins are too noisy to define "the market".
BTC_TICKER = "BTC-USD"

# BTC PriceContext older than this → treat the regime as unknown (neutral).
# Prices poll every ~3 min, so 30 min is ~10 missed ticks: a real outage,
# not a slow cycle.
_PRICE_STALE_MIN = 30

# Regime thresholds, in PERCENT (PriceContext stores fractions; we ×100).
# A sharp 1d move alone sets the regime; a milder 1d move needs the 5d
# trend to agree, so a single choppy day doesn't whipsaw the gate.
_HARD_1D = 3.0          # |1d| ≥ this → regime on the 1d alone
_SOFT_1D = 1.5          # |1d| ≥ this AND 5d agreeing → regime
_TREND_5D = 8.0         # |5d| ≥ this → trend confirmation for the soft band


@dataclass(frozen=True)
class Regime:
    state: str                     # "risk_on" | "neutral" | "risk_off"
    btc_1d_pct: Optional[float]    # percent, None when unknown
    btc_5d_pct: Optional[float]
    reason: str

    @property
    def is_off(self) -> bool:
        return self.state == "risk_off"

    @property
    def is_on(self) -> bool:
        return self.state == "risk_on"


def _stale(ts: datetime, now: datetime) -> bool:
    aware = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return (now - aware) > timedelta(minutes=_PRICE_STALE_MIN)


def market_regime(session, now: Optional[datetime] = None) -> Regime:
    """Classify the crypto tape from BTC's recent returns. Fails open to
    neutral on missing/stale data."""
    now = now or datetime.now(timezone.utc)
    pc = session.get(PriceContext, BTC_TICKER)
    if pc is None:
        return Regime("neutral", None, None, "no BTC price context")
    if pc.last_updated is not None and _stale(pc.last_updated, now):
        return Regime("neutral", None, None, "BTC price stale")

    d1 = (pc.change_1d_pct or 0.0) * 100
    d5 = (pc.change_5d_pct or 0.0) * 100

    # Mutually exclusive: risk_off needs d1 ≤ −1.5, risk_on needs d1 ≥ +1.5,
    # so at most one fires. A hard ±3% 1d move sets the regime outright; a
    # milder move needs the 5d trend to agree (so one choppy day doesn't
    # whipsaw the gate).
    reason = f"BTC {d1:+.1f}% 1d / {d5:+.1f}% 5d"
    if d1 <= -_HARD_1D or (d1 <= -_SOFT_1D and d5 <= -_TREND_5D):
        return Regime("risk_off", round(d1, 2), round(d5, 2), reason)
    if d1 >= _HARD_1D or (d1 >= _SOFT_1D and d5 >= _TREND_5D):
        return Regime("risk_on", round(d1, 2), round(d5, 2), reason)
    return Regime("neutral", round(d1, 2), round(d5, 2), reason)


def blocks_entry(regime: Regime, direction: str, ticker: str) -> bool:
    """True if `regime` argues against opening `direction` on `ticker`.

    The market proxy (BTC) is never gated against itself. Counter-trend
    entries are blocked; with-trend and neutral-regime entries pass.
    """
    if (ticker or "").upper() == BTC_TICKER:
        return False
    if regime.is_off and direction == "long":
        return True
    if regime.is_on and direction == "short":
        return True
    return False
