"""Call resolution — the visible half of accountability.

synthesis / why_moved / convergence make directional CALLs; scorecard marks
them to market. Until now an outcome only ever surfaced as an aggregate
hit-rate via `!scorecard`. This posts the *verdict* on each notable call once
its 5d scoring horizon matures, so the bot publicly owns its hits AND misses
— the half of accountability that actually builds trust.

Discipline (why this isn't noise):
- A verdict is ARITHMETIC, never an LLM opinion: did the realized move agree
  with the called direction (`scorecard._hit`, the single source of truth).
  No model is consulted — fabricating a verdict would be the exact
  anti-pattern this codebase rejects.
- Gated: only conviction ≥ _MIN_CONVICTION calls, or |move| ≥ _BIG_MOVE_PCT,
  earn a post. A timid call that drifted 0.5% is not worth attention.
- Resolved on the 5d horizon (scorecard's scoring basis); a 1d read is used
  only when the call was retired (settled) without ever getting a 5d mark.
- Batched: one embed per cycle, one line per call, capped — never a burst.
- Every *finalized* call is stamped `resolved_posted_at` whether or not it
  was posted, so the scan is permanently bounded and nothing double-fires
  (same idempotency contract as reddit_feed.posted_at).
- Conservative backlink: the original post is linked only when exactly one
  narrative event unambiguously matches the call. Never a wrong link.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import discord
from loguru import logger
from sqlmodel import or_, select

from .. import discord_client, ui
from ..config import settings
from ..db import session_scope
from ..models import NarrativeEvent, TradingCall
from ..scorecard import _hit, track_record_brief

_BIG_MOVE_PCT = 6.0    # |scoring return| that makes a call worth a verdict
_MIN_CONVICTION = 4    # bold calls always get a verdict, win or lose
_MAX_LINES = 14        # hard cap on verdict lines per embed
_LINK_WINDOW = timedelta(hours=2)  # call ↔ narrative-event match tolerance


def _channel() -> int:
    return (
        settings.DISCORD_CALLS_CHANNEL_ID
        or settings.DISCORD_DIGEST_CHANNEL_ID
        or settings.DISCORD_META_CHANNEL_ID
    )


def _utc_naive(dt: datetime) -> datetime:
    """Drop tz for delta math — stored datetimes are UTC; sqlite hands some
    back naive and some aware depending on path. Normalise before subtracting."""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _age_days(created: datetime, now: datetime) -> int:
    a, b = _utc_naive(created), _utc_naive(now)
    return max(0, (b - a).days)


def _backlink(session, call: TradingCall) -> str | None:
    """Jump URL to the post that made this call — only if exactly one
    narrative event unambiguously matches (same ticker + kind==source, within
    ±_LINK_WINDOW of the call). Ambiguous/none → no link (never a wrong one)."""
    base = _utc_naive(call.created_at)
    rows = session.exec(
        select(NarrativeEvent)
        .where(NarrativeEvent.ticker == call.ticker)
        .where(NarrativeEvent.kind == call.source)
    ).all()
    near = [
        r
        for r in rows
        if r.message_id
        and r.channel_id
        and abs((_utc_naive(r.ts) - base).total_seconds())
        <= _LINK_WINDOW.total_seconds()
    ]
    if len(near) != 1:
        return None
    return discord_client.jump_url(near[0].channel_id, near[0].message_id)


def _collect(session, now: datetime) -> tuple[list[dict], list[int]]:
    """Returns (verdicts_to_post, all_finalized_ids).

    A call is *finalized* once it has a usable scoring read (5d, or 1d if it
    was retired) — those ids get stamped so they never re-scan. The posted
    subset is the *notable* finalized calls (bold or big move).
    """
    cands = session.exec(
        select(TradingCall)
        .where(TradingCall.resolved_posted_at.is_(None))
        .where(
            or_(
                TradingCall.ret_5d_pct.is_not(None),
                TradingCall.settled == True,  # noqa: E712
            )
        )
        .order_by(TradingCall.created_at)
    ).all()

    finalized: list[int] = []
    posts: list[dict] = []
    for c in cands:
        if c.ret_5d_pct is not None:
            ret, horizon = c.ret_5d_pct, "5d"
        elif c.settled and c.ret_1d_pct is not None:
            ret, horizon = c.ret_1d_pct, "1d"
        else:
            # Settled but never scoreable (no price at call). Finalize so it
            # stops scanning; never invent a verdict for it.
            finalized.append(c.id)
            continue
        finalized.append(c.id)
        if c.conviction < _MIN_CONVICTION and abs(ret) < _BIG_MOVE_PCT:
            continue  # matured but not worth a verdict
        posts.append(
            {
                "ticker": c.ticker,
                "direction": c.direction,
                "conviction": c.conviction,
                "source": c.source,
                "ret": ret,
                "horizon": horizon,
                "hit": _hit(c.direction, ret),
                "age_days": _age_days(c.created_at, now),
                "backlink": _backlink(session, c),
            }
        )
    posts.sort(key=lambda p: abs(p["ret"]), reverse=True)
    return posts, finalized


def _line(p: dict) -> str:
    mark = "✅" if p["hit"] else "❌"
    tick = f"${p['ticker']}"
    if p["backlink"]:
        tick = f"[{tick}]({p['backlink']})"
    return (
        f"{mark} {tick} {p['direction'].upper()} "
        f"{p['ret']:+.1f}% ({p['horizon']}) · conv{p['conviction']} · "
        f"`{p['source']}` · {p['age_days']}d ago"
    )


def _stamp(ids: list[int]) -> None:
    if not ids:
        return
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        for c in s.exec(
            select(TradingCall).where(TradingCall.id.in_(ids))
        ).all():
            c.resolved_posted_at = now
            s.add(c)


async def run_call_review() -> None:
    try:
        await _run()
    except Exception as e:
        logger.exception("run_call_review top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ call_review error: {e}")
        except Exception:
            pass


async def _run() -> None:
    chan = _channel()
    if not chan:
        logger.debug("call_review: no channel resolved, skipping")
        return

    def _pick() -> tuple[list[dict], list[int], str]:
        with session_scope() as s:
            posts, finalized = _collect(s, datetime.now(timezone.utc))
        # track_record_brief opens its own session — still off the loop here.
        return posts, finalized, track_record_brief()

    posts, finalized, brief = await asyncio.to_thread(_pick)
    if not finalized:
        logger.info("call_review: nothing matured, skipping")
        return
    if not posts:
        # Things matured but none worth a verdict — bound the scan, no post.
        await asyncio.to_thread(_stamp, finalized)
        logger.info("call_review: {} matured, none notable", len(finalized))
        return

    shown = posts[:_MAX_LINES]
    lines = [_line(p) for p in shown]
    extra = len(posts) - len(shown)
    if extra > 0:
        lines.append(f"_+{extra} more resolved — see `!scorecard`_")

    hits = sum(1 for p in shown if p["hit"])
    misses = len(shown) - hits
    embed = discord.Embed(
        title="📒 Called It — verdicts",
        description="\n".join(lines)[:4000],
        color=ui.tally_color(hits, misses),
    )
    embed.set_footer(text=f"Track record: {brief}"[:2048])

    msg = await discord_client.post_embed(chan, embed)
    if msg is None:
        # Post failed — do NOT stamp; the whole batch retries next cycle.
        logger.warning("call_review: post failed, will retry next cycle")
        return
    await asyncio.to_thread(_stamp, finalized)
    logger.info(
        "call_review: posted {} verdict(s), finalized {} call(s)",
        len(shown),
        len(finalized),
    )
