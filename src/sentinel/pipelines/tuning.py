"""Monthly tuning pipeline per SPEC §7.

On the 1st of each month at 12:00 UTC:
1. Sample up to 20 filings with 👍 in the last 30d, 20 with 👎.
2. If either sample has <5 rows, skip — not enough signal.
3. Ask the heavy model to suggest a `proposed_prompt_delta` against the
   materiality scorer.
4. Post the suggestion to #meta and register it as pending; the feedback
   handler applies on ✅ or rejects on ❌.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlmodel import select

from .. import discord_client, feedback
from ..config import settings
from ..db import session_scope
from ..llm import get_llm, parse_json_response
from ..models import Feedback, Filing
from ..prompts import get_prompt


async def run_monthly_tuning() -> None:
    try:
        await _run()
    except Exception as e:
        logger.exception("run_monthly_tuning top-level failure: {}", e)
        try:
            await discord_client.post_meta(f"⚠️ tuning error: {e}")
        except Exception:
            pass


async def _run() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    up_rows: list[dict] = []
    down_rows: list[dict] = []
    with session_scope() as session:
        feedback_rows = session.exec(
            select(Feedback).where(Feedback.created_at >= cutoff)
        ).all()
        by_msg: dict[str, list[str]] = {}
        for fb in feedback_rows:
            by_msg.setdefault(fb.message_id, []).append(fb.emoji)

        for msg_id, emojis in by_msg.items():
            filing = session.exec(
                select(Filing).where(Filing.message_id == msg_id)
            ).first()
            if filing is None:
                continue
            row = _filing_summary(filing)
            if "👍" in emojis and len(up_rows) < 20:
                up_rows.append(row)
            if "👎" in emojis and len(down_rows) < 20:
                down_rows.append(row)

    if len(up_rows) < 5 or len(down_rows) < 5:
        logger.info(
            "tuning: insufficient signal ({} up, {} down) — skipping",
            len(up_rows),
            len(down_rows),
        )
        return

    payload = {"up": up_rows, "down": down_rows}
    llm = get_llm()
    tmpl = get_prompt("tuning_suggest")
    rendered = tmpl.safe_substitute(feedback_data_json=json.dumps(payload, default=str))
    raw = await asyncio.to_thread(
        llm.complete, rendered, model="heavy", json_mode=True, max_tokens=600
    )
    parsed = parse_json_response(raw, expect=dict)
    if parsed is None:
        logger.error("tuning: LLM error or parse failure")
        return
    try:
        delta = str(parsed["proposed_prompt_delta"]).strip()
        issue = str(parsed.get("current_issue", ""))
        rationale = str(parsed.get("rationale", ""))
    except (KeyError, TypeError) as e:
        logger.error("tuning missing required field: {}", e)
        return
    if not delta:
        logger.info("tuning: empty delta, skipping")
        return

    content = (
        f"🛠️ **Monthly tuning proposal**\n"
        f"**Issue:** {issue}\n"
        f"**Rationale:** {rationale}\n\n"
        f"**Proposed delta to append to `materiality` prompt:**\n```\n{delta}\n```\n"
        f"React ✅ to apply, ❌ to reject."
    )
    try:
        chan = discord_client.get_bot().get_channel(settings.DISCORD_META_CHANNEL_ID)
        if chan is None:
            chan = await discord_client.get_bot().fetch_channel(
                settings.DISCORD_META_CHANNEL_ID
            )
        msg = await chan.send(content[:2000])
        try:
            await msg.add_reaction("✅")
            await msg.add_reaction("❌")
        except Exception as e:
            logger.debug("could not pre-add reactions: {}", e)
        feedback.register_pending_tuning(str(msg.id), delta)
        logger.info("tuning proposal posted as msg {}", msg.id)
    except Exception as e:
        logger.exception("tuning post failed: {}", e)


def _filing_summary(f: Filing) -> dict:
    return {
        "ticker": f.ticker,
        "form_type": f.form_type,
        "materiality_score": f.materiality_score,
        "materiality_reason": f.materiality_reason,
        "summary_excerpt": (f.summary or "")[:300],
    }
