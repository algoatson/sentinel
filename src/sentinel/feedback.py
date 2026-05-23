"""Reaction-only handler for the monthly tuning proposal.

General 👍/👎 feedback has moved to button interactions (see interactions.py).
This module is now narrow: it only watches ✅/❌ reactions on a single
pending tuning proposal posted to #meta, and applies / rejects the proposed
materiality-prompt delta.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger
from sqlmodel import select

from .config import settings
from .db import session_scope
from .models import PendingTuning, PromptVersion

if TYPE_CHECKING:
    import discord


def register_pending_tuning(message_id: str, delta: str) -> None:
    """Persist a posted tuning proposal so it survives a restart until the
    user reacts ✅/❌ (set by pipelines/tuning.py)."""
    with session_scope() as session:
        existing = session.get(PendingTuning, message_id)
        if existing is not None:
            existing.delta = delta
            session.add(existing)
        else:
            session.add(
                PendingTuning(
                    message_id=message_id,
                    delta=delta,
                    created_at=datetime.now(timezone.utc),
                )
            )


def register_feedback_handlers(bot) -> None:
    @bot.event
    async def on_raw_reaction_add(payload: "discord.RawReactionActionEvent") -> None:
        try:
            await _handle(payload, bot)
        except Exception as e:
            logger.exception("reaction handler failure: {}", e)

    logger.info("feedback handler registered")


async def _handle(payload, bot) -> None:
    if payload.user_id == getattr(bot.user, "id", None):
        return
    if payload.channel_id != settings.DISCORD_META_CHANNEL_ID:
        return

    message_id = str(payload.message_id)
    emoji = str(payload.emoji)
    if emoji == "✅":
        _apply_tuning(message_id)
    elif emoji == "❌":
        with session_scope() as session:
            pending = session.get(PendingTuning, message_id)
            if pending is None:
                return
            session.delete(pending)
        logger.info("tuning proposal {} rejected", message_id)


def _apply_tuning(message_id: str) -> None:
    from .prompts import ALL_PROMPTS

    with session_scope() as session:
        pending = session.get(PendingTuning, message_id)
        if pending is None:
            return
        delta = pending.delta
        session.delete(pending)

        current = session.exec(
            select(PromptVersion)
            .where(PromptVersion.prompt_name == "materiality")
            .where(PromptVersion.active == True)  # noqa: E712
        ).first()
        base_content = (
            current.content if current is not None else ALL_PROMPTS["materiality"].template
        )
        new_content = f"{base_content}\n\n{delta.strip()}"

        if current is not None:
            current.active = False
            session.add(current)
        session.add(
            PromptVersion(
                prompt_name="materiality",
                content=new_content,
                created_at=datetime.now(timezone.utc),
                active=True,
            )
        )
    logger.info("tuning applied: new materiality prompt version saved")
