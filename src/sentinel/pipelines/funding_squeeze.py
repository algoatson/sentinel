"""Funding/OI squeeze detector — the high-edge crypto signal the bot was missing.

Crypto perpetual-futures funding rates reveal positioning that price
alone doesn't show. The single highest-signal crypto pattern:

  * Funding flips DEEPLY NEGATIVE (perp shorts paying longs >0.05%/8h) AND
    price is up materially in the last day → shorts are squeezed,
    capitulation imminent. Bias LONG.

  * Funding goes DEEPLY POSITIVE (perp longs paying shorts >0.08%/8h) AND
    price is flat/down → longs are crowded, premium will mean-revert.
    Bias FADE (i.e. short the long, or stay out).

  * Open interest is SURGING (>20% in 24h) — fresh leverage is entering.
    Combined with price action this is the precondition for a violent
    move. Direction comes from which side the funding is leaning.

Pure detector: no LLM at runtime. Findings post to #crypto (degrades
to #news / #pulse) and log a TradingCall with source="funding_squeeze"
so the scorecard measures whether this edge is real over time. The
crypto wallet listens to the source via _POLICIES; existing risk gates
(require_micro, ATR stop, drawdown taper) apply on top.

Runs every 20 min — funding only changes per 8h exchange tick, and OI
updates several times per hour. More frequent polling adds noise.

Cooldown per ticker: 4h. A funding flip is a state, not an event;
re-firing on every cycle while the state holds would spam the channel
and the scorecard.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import discord
from loguru import logger
from sqlmodel import select

from .. import discord_client, ui
from ..config import settings
from ..crypto_regime import blocks_entry, market_regime
from ..db import session_scope
from ..ingesters.crypto_micro import MICRO_STALE_MINUTES
from ..models import CryptoMicro, PriceContext, Watchlist


# Funding bands — these are 8h-anchored percentages (Binance funding fires
# every 8 hours). 0.05% / 8h ≈ 5.5% annualised; 0.10% / 8h ≈ 11% annualised.
# Anything past these bands is positioning, not pricing.
_FUNDING_DEEP_NEG_PCT = -0.05    # squeeze long if price up
_FUNDING_DEEP_POS_PCT = 0.08     # fade-long / wait if price flat-down

# Price action thresholds. We want the SETUP, not the chase — so a coin
# already +30% is too late. Look for early-stage divergences.
_PRICE_UP_THR = 4.0       # +4% 24h, with deeply negative funding → squeeze
_PRICE_UP_CAP = 20.0      # but skip when already ≥+20% (chase regime)
_PRICE_FLAT_BAND = 2.0    # |chg_1d| < this counts as flat for fade signal

# OI surge alone (no funding alignment) doesn't fire; OI is a co-signal
# that confirms a setup detected via funding.
_OI_SURGE_PCT = 20.0

# Orderbook imbalance is (bid−ask)/(bid+ask) ∈ [−1,+1]: +1 all bids (buy
# pressure), −1 all asks (sell pressure). When the resting book leans this
# hard AGAINST the setup's direction, the flow contradicts the thesis — skip.
# Only applied when imbalance is present (OKX/feed gaps leave it None, and a
# missing book shouldn't veto an otherwise-clean funding signal).
_OB_CONTRA = 0.35

_COOLDOWN = timedelta(hours=4)
_RECENT: dict[str, datetime] = {}

# Bound the per-cycle scan so a noisy day can't burn the whole budget.
# Crypto micro covers ≤25 tickers anyway, but defensive.
_MAX_PER_CYCLE = 5

# Conviction levels for the autonomous wallet (1–5). Funding squeezes
# are high-conviction setups when *fully* aligned; OI-only confirmations
# slot lower.
_CONV_FULL_SQUEEZE = 4    # funding + price-up alignment
_CONV_FUNDING_FADE = 3    # funding extreme + flat/down price → fade
_CONV_OI_CONFIRM = 3      # OI surge confirming a price move


def _channel() -> int:
    return (
        settings.DISCORD_CRYPTO_CHANNEL_ID
        or settings.DISCORD_NEWS_CHANNEL_ID
        or settings.DISCORD_PULSE_CHANNEL_ID
    )


def _crypto_tickers(session) -> list[str]:
    """Curated crypto watchlist. Same scope as crypto_micro ingester."""
    rows = session.exec(
        select(Watchlist.ticker)
        .where(Watchlist.asset_class == "crypto")
        .where(Watchlist.ticker.is_not(None))
    ).all()
    return sorted({t for t in rows if t})


def _on_cooldown(ticker: str, now: datetime) -> bool:
    last = _RECENT.get(ticker)
    return last is not None and (now - last) < _COOLDOWN


def _orderbook_contradicts(direction: str, imbalance: float | None) -> bool:
    """True when the resting orderbook leans hard AGAINST `direction` — a heavy
    ask wall under a long, or a heavy bid wall under a short. None (no book
    data) never vetoes."""
    if imbalance is None:
        return False
    if direction == "long" and imbalance <= -_OB_CONTRA:
        return True
    if direction == "short" and imbalance >= _OB_CONTRA:
        return True
    return False


def _micro_is_stale(micro: CryptoMicro, now: datetime) -> bool:
    if micro.updated_at is None:
        return True
    upd = micro.updated_at
    if upd.tzinfo is None:
        upd = upd.replace(tzinfo=timezone.utc)
    return (now - upd) > timedelta(minutes=MICRO_STALE_MINUTES)


def _evaluate(session, ticker: str, now: datetime, regime) -> dict | None:
    """Pure assessment — does this ticker's current funding/OI/price state
    qualify for a squeeze, fade, or OI-confirm call? Returns the finding dict
    (suitable for post + record_call), or None.

    Beyond the funding/OI/price rules, a candidate must clear three gates:
    fresh micro data, the BTC market regime (no counter-trend entries), and
    the resting orderbook (flow must not lean hard against the direction)."""
    micro = session.get(CryptoMicro, ticker)
    pc = session.get(PriceContext, ticker)
    if micro is None or pc is None:
        return None
    # Stale funding/OI (a failed ingest run) is worse than none — never fire
    # a leverage signal on data we can't trust is current.
    if _micro_is_stale(micro, now):
        return None
    funding = micro.funding_rate
    oi_chg = micro.oi_change_24h_pct
    imbalance = micro.orderbook_imbalance
    price_1d = (pc.change_1d_pct or 0.0) * 100  # percent
    funding_pct = (funding or 0.0) * 100        # to 8h percent

    finding: dict | None = None

    # Setup 1 — full squeeze: deeply negative funding + price already up.
    # Most reliable. Shorts are paying to hold; one push and they capitulate.
    if (
        funding is not None
        and funding_pct <= _FUNDING_DEEP_NEG_PCT
        and _PRICE_UP_THR <= price_1d < _PRICE_UP_CAP
    ):
        oi_bit = (
            f" · OI +{oi_chg * 100:.1f}%/24h" if oi_chg and oi_chg > 0 else ""
        )
        finding = {
            "kind": "squeeze_long",
            "direction": "long",
            "conviction": _CONV_FULL_SQUEEZE,
            "headline": f"${ticker} short squeeze setup",
            "evidence": (
                f"Funding {funding_pct:.3f}%/8h (perp shorts paying), "
                f"price {price_1d:+.1f}% 24h{oi_bit}. Capitulation risk "
                f"as shorts cover."
            ),
        }

    # Setup 2 — funding fade: deeply positive funding + flat/down price.
    # Longs are crowded paying premium. Mean reversion / fade signal.
    elif (
        funding is not None
        and funding_pct >= _FUNDING_DEEP_POS_PCT
        and abs(price_1d) < _PRICE_FLAT_BAND
    ):
        finding = {
            "kind": "funding_fade",
            "direction": "short",
            "conviction": _CONV_FUNDING_FADE,
            "headline": f"${ticker} long-side over-extended",
            "evidence": (
                f"Funding {funding_pct:+.3f}%/8h (perp longs paying), "
                f"price {price_1d:+.1f}% 24h. Crowded long premium will "
                f"mean-revert."
            ),
        }

    # Setup 3 — OI surge confirms a price move. Direction follows price.
    # Lower conviction since we lack the funding extreme; OI alone could
    # just be vol expansion.
    elif (
        oi_chg is not None
        and oi_chg * 100 >= _OI_SURGE_PCT
        and abs(price_1d) >= _PRICE_UP_THR
    ):
        finding = {
            "kind": "oi_confirm",
            "direction": "long" if price_1d > 0 else "short",
            "conviction": _CONV_OI_CONFIRM,
            "headline": f"${ticker} OI surge + move",
            "evidence": (
                f"OI +{oi_chg * 100:.1f}%/24h, price {price_1d:+.1f}% 24h, "
                f"funding {funding_pct:+.3f}%/8h. Fresh leverage entering "
                f"on the move."
            ),
        }

    if finding is None:
        return None

    direction = finding["direction"]
    # Market-regime gate — don't fight BTC's tape. A per-coin long while the
    # whole complex is risk-off (or a fade while it's ripping) is the classic
    # way the crypto leg bleeds.
    if blocks_entry(regime, direction, ticker):
        logger.debug(
            "funding_squeeze: skip ${} {} — counter-regime ({})",
            ticker, direction, regime.reason,
        )
        return None
    # Orderbook gate — the resting book must not lean hard against us.
    if _orderbook_contradicts(direction, imbalance):
        logger.debug(
            "funding_squeeze: skip ${} {} — orderbook {:+.2f} contradicts",
            ticker, direction, imbalance,
        )
        return None

    finding["funding_pct"] = funding_pct
    finding["price_1d_pct"] = price_1d
    finding["oi_chg_pct"] = (oi_chg or 0.0) * 100
    finding["orderbook_imbalance"] = imbalance
    finding["regime"] = regime.state
    if regime.state != "neutral":
        finding["evidence"] += f" Market: {regime.reason}."
    return finding


def _findings(session, now: datetime) -> list[dict]:
    """Scan the crypto watchlist; rank by abs(funding_pct) so the
    deepest extremes go first when the per-cycle cap clips."""
    # One BTC-regime read per cycle, shared across every coin's evaluation.
    regime = market_regime(session, now)
    out: list[dict] = []
    for ticker in _crypto_tickers(session):
        if _on_cooldown(ticker, now):
            continue
        finding = _evaluate(session, ticker, now, regime)
        if finding is not None:
            finding["ticker"] = ticker
            out.append(finding)
    out.sort(
        key=lambda f: abs(f.get("funding_pct") or 0.0),
        reverse=True,
    )
    return out[:_MAX_PER_CYCLE]


def _embed(f: dict) -> discord.Embed:
    color = ui.BULLISH if f["direction"] == "long" else ui.BEARISH
    arrow = "🟢" if f["direction"] == "long" else "🔴"
    title = f"{arrow} {f['headline']}"
    desc = f["evidence"]
    emb = discord.Embed(title=title[:256], description=desc[:2000], color=color)
    emb.add_field(
        name="Funding (8h)",
        value=f"{f['funding_pct']:+.3f}%",
        inline=True,
    )
    emb.add_field(
        name="Price 24h",
        value=f"{f['price_1d_pct']:+.1f}%",
        inline=True,
    )
    if f.get("oi_chg_pct"):
        emb.add_field(
            name="OI 24h",
            value=f"{f['oi_chg_pct']:+.1f}%",
            inline=True,
        )
    ob = f.get("orderbook_imbalance")
    if ob is not None:
        emb.add_field(name="Book", value=f"{ob:+.2f}", inline=True)
    footer = f"funding_squeeze · conv {f['conviction']}/5"
    regime = f.get("regime")
    if regime and regime != "neutral":
        footer += f" · regime {regime}"
    emb.set_footer(text=footer)
    return emb


async def run_funding_squeeze() -> None:
    try:
        await _run()
    except Exception as e:
        logger.exception("run_funding_squeeze top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ funding squeeze error: {e}")
        except Exception:
            pass


async def _run() -> None:
    chan = _channel()
    if not chan:
        logger.debug("funding_squeeze: no channel resolved, skipping")
        return

    now = datetime.now(timezone.utc)

    def _pick() -> list[dict]:
        with session_scope() as s:
            return _findings(s, now)

    findings = await asyncio.to_thread(_pick)
    if not findings:
        logger.info("funding_squeeze: no qualifying setups")
        return

    from ..narrative import record_event
    from ..scorecard import record_call

    posted = 0
    for f in findings:
        embed = _embed(f)
        msg = await discord_client.post_embed(
            chan, embed, importance=f["conviction"],
        )
        if msg is None:
            # post failed → don't stamp cooldown or record, retry next cycle
            continue
        # Record as a TradingCall so the scorecard measures the source's
        # edge and the per-source risk multiplier (the new control loop)
        # eventually presses on / fades it.
        record_call(
            f["ticker"], f["direction"], "funding_squeeze",
            f"{f['headline']}: {f['evidence']}", f["conviction"],
        )
        # Narrative log — same dedup contract as why_moved / convergence so
        # the tier-2 coalescer can spot funding_squeeze adjacent to a
        # convergence on the same coin and not double-narrate it.
        record_event(
            f["ticker"], "funding_squeeze",
            f["headline"], tier=2,
            detail=f["evidence"][:600],
            channel_id=chan,
            message_id=str(msg.id),
        )
        _RECENT[f["ticker"]] = now
        posted += 1

    if posted:
        logger.info("funding_squeeze: posted {} setup(s)", posted)
