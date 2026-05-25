"""Natural-language custom watches.

`!watch tell me if any insider buys >$1M at a company r/wallstreetbets is
hyping` → the LLM compiles the sentence into a constrained JSON spec, stored
on the Watch row and evaluated every cycle against the DB. On a match the bot
posts what tripped it.

Deliberately constrained: the compiler can only emit the fields below, all
optional and AND-ed. That keeps evaluation deterministic and predictable —
no arbitrary code, no model-in-the-loop at evaluation time. It surfaces
what tripped the watch — and a read on what it means is welcome, not withheld.

Spec schema:
{
  "ticker": "PLTR" | null,                       # specific name
  "asset_class": "equity|crypto|future|rate"|null,
  "filing": {"form_types": [..], "min_materiality": 0-3} | null,
  "price":  {"metric": "change_1d_pct|change_5d_pct|volume_vs_20d_avg",
             "op": ">=|<=", "value": <number>} | null,
  "social": {"min_reddit_24h": <int>, "subreddit": <str|null>} | null,
  "news_keyword": "<substring>" | null
}
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from string import Template

import discord
from loguru import logger
from sqlmodel import func, select

from .. import discord_client
from ..config import settings
from ..db import session_scope
from ..llm import LLM_ERROR_SENTINEL, get_llm, parse_json_response
from ..models import Filing, NewsItem, PriceContext, RedditMention, Watch, Watchlist


_COOLDOWN = timedelta(hours=6)
_WINDOW = timedelta(hours=24)
_VALID_METRICS = {"change_1d_pct", "change_5d_pct", "volume_vs_20d_avg"}
_VALID_CLASSES = {"equity", "crypto", "future", "rate"}


COMPILE_PROMPT = Template("""\
Compile the user's alert request into STRICT JSON matching EXACTLY this
schema. Every field optional; omit (use null) what the user didn't ask for.
All present conditions are AND-ed.

{
  "ticker": <UPPERCASE symbol or null>,
  "asset_class": <"equity"|"crypto"|"future"|"rate"|null>,
  "filing": {"form_types": [<SEC form strings>], "min_materiality": <0-3>} | null,
  "price": {"metric": <"change_1d_pct"|"change_5d_pct"|"volume_vs_20d_avg">,
            "op": <">="|"<=">, "value": <number, percents as decimals e.g. 0.05>} | null,
  "social": {"min_reddit_24h": <int>, "subreddit": <name without r/ or null>} | null,
  "news_keyword": <substring or null>
}

Rules:
- "insider buying" → filing.form_types ["4"], min_materiality 2.
- "big move / spikes / drops X%" → price change_1d_pct with op and decimal value.
- "unusual volume" → price volume_vs_20d_avg ">=" (e.g. 3).
- "people talking about / hyped on reddit" → social.min_reddit_24h (~5).
- Output JSON only. No prose.

User request: $request
""")


# ---- compile / store -------------------------------------------------------


def _validate(spec: dict) -> dict | None:
    """Coerce + sanity-check the compiled spec. Returns cleaned spec or None
    if nothing actionable survived."""
    out: dict = {}
    if isinstance(spec.get("ticker"), str) and spec["ticker"].strip():
        out["ticker"] = spec["ticker"].strip().upper().lstrip("$")
    if spec.get("asset_class") in _VALID_CLASSES:
        out["asset_class"] = spec["asset_class"]

    f = spec.get("filing")
    if isinstance(f, dict):
        forms = [str(x).upper() for x in (f.get("form_types") or []) if x]
        mm = f.get("min_materiality")
        out["filing"] = {
            "form_types": forms,
            "min_materiality": int(mm) if isinstance(mm, (int, float)) else 0,
        }

    p = spec.get("price")
    if isinstance(p, dict) and p.get("metric") in _VALID_METRICS:
        if p.get("op") in (">=", "<=") and isinstance(p.get("value"), (int, float)):
            out["price"] = {
                "metric": p["metric"],
                "op": p["op"],
                "value": float(p["value"]),
            }

    s = spec.get("social")
    if isinstance(s, dict) and isinstance(s.get("min_reddit_24h"), (int, float)):
        out["social"] = {
            "min_reddit_24h": int(s["min_reddit_24h"]),
            "subreddit": (
                str(s["subreddit"]) if s.get("subreddit") else None
            ),
        }

    if isinstance(spec.get("news_keyword"), str) and spec["news_keyword"].strip():
        out["news_keyword"] = spec["news_keyword"].strip()

    # Need at least one real condition.
    if not any(k in out for k in ("filing", "price", "social", "news_keyword")):
        return None
    return out


async def add_watch(raw_text: str) -> str:
    llm = get_llm()
    rendered = COMPILE_PROMPT.safe_substitute(request=raw_text[:500])
    raw = await asyncio.to_thread(
        llm.complete, rendered, model="light", json_mode=True, max_tokens=400
    )
    if not raw or raw == LLM_ERROR_SENTINEL:
        return "LLM unreachable — couldn't compile that watch, try again."
    parsed = parse_json_response(raw, expect=dict)
    if parsed is None:
        return "couldn't parse that into a condition — try rephrasing more concretely."
    spec = _validate(parsed)
    if spec is None:
        return (
            "that didn't compile to anything I can check. Be concrete: a "
            "ticker, a % move, a form type, reddit volume, or a news keyword."
        )
    with session_scope() as sess:
        row = Watch(
            raw_text=raw_text[:500],
            condition_json=json.dumps(spec),
            created_at=datetime.now(timezone.utc),
            active=True,
        )
        sess.add(row)
        sess.flush()
        wid = row.id
    return f"🔔 watch **#{wid}** set: `{json.dumps(spec)}`\nI'll post when it trips (6h cooldown)."


def remove_watch(wid: int | str) -> dict:
    """Delete a watch by id. Shared by !unwatch and the dashboard Watches
    panel. Same return shape as the rest of the chokepoint family:
    ``{"ok": bool, "message": str, "watch_id": int | None}``."""
    try:
        wid_i = int(str(wid).lstrip("#"))
    except (TypeError, ValueError):
        return {"ok": False, "message": f"bad watch id `{wid}`",
                "watch_id": None}
    with session_scope() as s:
        row = s.get(Watch, wid_i)
        if row is None:
            return {"ok": False, "message": f"no watch #{wid_i}",
                    "watch_id": wid_i}
        s.delete(row)
    return {"ok": True, "message": f"removed watch #{wid_i}",
            "watch_id": wid_i}


def list_watches() -> list[dict]:
    """Structured watches with active flag, trigger count, last hit."""
    out: list[dict] = []
    with session_scope() as s:
        for w in s.exec(select(Watch).order_by(Watch.created_at)).all():
            out.append({
                "id": w.id,
                "raw_text": w.raw_text,
                "active": w.active,
                "trigger_count": w.trigger_count,
                "last_triggered_at": w.last_triggered_at,
                "created_at": w.created_at,
            })
    return out


# ---- evaluation ------------------------------------------------------------


def _scope_tickers(session, spec: dict) -> list[str] | None:
    """Ticker universe for this watch, or None when global (news_keyword-only
    with no ticker/asset_class)."""
    if spec.get("ticker"):
        return [spec["ticker"]]
    if spec.get("asset_class"):
        rows = session.exec(
            select(Watchlist.ticker)
            .where(Watchlist.asset_class == spec["asset_class"])
            .where(Watchlist.ticker.is_not(None))
        ).all()
        return sorted({t for t in rows if t})
    if {"filing", "price", "social"} & set(spec):
        # Bound a "any name" watch to recently-active tickers.
        cut = datetime.now(timezone.utc) - _WINDOW
        active = set(
            session.exec(
                select(Filing.ticker)
                .where(Filing.filed_at >= cut)
                .where(Filing.ticker.is_not(None))
            ).all()
        )
        active |= set(
            session.exec(
                select(RedditMention.ticker)
                .where(RedditMention.created_at >= cut)
            ).all()
        )
        return sorted({t for t in active if t})
    return None


def _eval_watch(session, spec: dict) -> list[str]:
    """Return human-readable evidence lines if the watch matches, else []."""
    now = datetime.now(timezone.utc)
    cut = now - _WINDOW
    scope = _scope_tickers(session, spec)

    # Global news-keyword-only watch.
    if scope is None:
        kw = spec["news_keyword"]
        hits = session.exec(
            select(NewsItem)
            .where(NewsItem.published_at >= cut)
            .where(NewsItem.title.ilike(f"%{kw}%"))
            .order_by(NewsItem.published_at.desc())
            .limit(5)
        ).all()
        return [f"📰 “{h.title[:160]}” ({h.source.split(':')[-1]})" for h in hits]

    matched: list[str] = []
    for ticker in scope[:400]:
        parts: list[str] = []

        if "filing" in spec:
            q = (
                select(Filing)
                .where(Filing.ticker == ticker)
                .where(Filing.filed_at >= cut)
                .where(
                    Filing.materiality_score
                    >= spec["filing"]["min_materiality"]
                )
            )
            forms = spec["filing"]["form_types"]
            if forms:
                q = q.where(Filing.form_type.in_(forms))
            f = session.exec(q.order_by(Filing.filed_at.desc()).limit(1)).first()
            if f is None:
                continue
            parts.append(
                f"{f.form_type} (mat {f.materiality_score}) — "
                f"{(f.materiality_reason or '')[:90]}"
            )

        if "price" in spec:
            pc = session.get(PriceContext, ticker)
            if pc is None:
                continue
            val = getattr(pc, spec["price"]["metric"], None)
            if val is None:
                continue
            thr = spec["price"]["value"]
            ok = val >= thr if spec["price"]["op"] == ">=" else val <= thr
            if not ok:
                continue
            parts.append(
                f"{spec['price']['metric']}={val:.3g} {spec['price']['op']} {thr}"
            )

        if "social" in spec:
            sq = (
                select(func.count())
                .select_from(RedditMention)
                .where(RedditMention.ticker == ticker)
                .where(RedditMention.created_at >= cut)
            )
            if spec["social"].get("subreddit"):
                sq = sq.where(
                    RedditMention.subreddit == spec["social"]["subreddit"]
                )
            cnt = session.exec(sq).one() or 0
            if cnt < spec["social"]["min_reddit_24h"]:
                continue
            parts.append(f"reddit×{cnt}/24h")

        if "news_keyword" in spec:
            kw = spec["news_keyword"]
            n = session.exec(
                select(NewsItem)
                .where(NewsItem.ticker == ticker)
                .where(NewsItem.published_at >= cut)
                .where(NewsItem.title.ilike(f"%{kw}%"))
                .limit(1)
            ).first()
            if n is None:
                continue
            parts.append(f"news “{n.title[:90]}”")

        if parts:
            matched.append(f"**${ticker}** — " + " · ".join(parts))
        if len(matched) >= 8:
            break
    return matched


async def run_watch_cycle() -> None:
    try:
        await asyncio.to_thread(_run)
    except Exception as e:
        logger.exception("watch cycle top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ watch cycle error: {e}")
        except Exception:
            pass
    await _flush_posts()


# Posts are queued in _run (sync, in a thread) then sent here on the loop.
_PENDING: list[tuple[int, str, list[str]]] = []


def _run() -> None:
    now = datetime.now(timezone.utc)
    _PENDING.clear()
    with session_scope() as session:
        watches = session.exec(select(Watch).where(Watch.active == True)).all()  # noqa: E712
        for w in watches:
            if (
                w.last_triggered_at is not None
                and now - w.last_triggered_at.replace(tzinfo=timezone.utc)
                < _COOLDOWN
            ):
                continue
            try:
                spec = json.loads(w.condition_json)
            except json.JSONDecodeError:
                continue
            evidence = _eval_watch(session, spec)
            if not evidence:
                continue
            w.last_triggered_at = now
            w.trigger_count += 1
            session.add(w)
            _PENDING.append((w.id, w.raw_text, evidence))
            try:
                from .. import events
                events.publish("watch", {
                    "id": w.id,
                    "raw_text": w.raw_text,
                    "evidence": evidence[:3],
                })
            except Exception as e:
                logger.debug("events.publish(watch) failed: {}", e)
    if _PENDING:
        logger.info("watches: {} tripped", len(_PENDING))


async def _flush_posts() -> None:
    if not _PENDING:
        return
    channel = settings.DISCORD_NEWS_CHANNEL_ID or settings.DISCORD_PULSE_CHANNEL_ID
    for wid, raw_text, evidence in _PENDING:
        embed = discord.Embed(
            title=f"🔔 Watch #{wid} tripped",
            description=(f"_{raw_text}_\n\n" + "\n".join(evidence))[:4000],
            color=0xE67E22,
        )
        await discord_client.post_embed(channel, embed)
    _PENDING.clear()
