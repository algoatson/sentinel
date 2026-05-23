"""Discord interaction handlers — buttons under every post.

Three buttons:
  🤖 Ask AI  — opens a discussion thread on THIS post (on demand, not auto)
              and seeds it with an LLM explanation. Follow-ups continue in
              the thread, answered by chat.py (with company research).
  👍 Useful  — records a positive Feedback row for monthly tuning.
  👎 Noise   — records a negative Feedback row for monthly tuning.

Posts are no longer auto-threaded; the thread only exists if the user asks
for it. The View is registered persistently at startup so buttons keep
working across restarts. Custom_ids are stable (`sentinel:explain|up|down`)
so buttons on pre-existing posts still resolve.

Feedback is deduped per (message_id, user_id, button) — multiple clicks don't
inflate the tuning sample.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from string import Template

import discord
from loguru import logger
from sqlmodel import select

from .db import session_scope
from .llm import LLM_ERROR_SENTINEL, get_llm
from .models import Feedback


_THREAD_NAME_RE = re.compile(r"[*_`~|>#]+")

EXPLAIN_PROMPT = Template("""\
You are Sentinel — the user's personal trading copilot. They clicked
"Ask AI" on this post. Brief them like a sharp trader friend: what's going
on, why it matters, and your actual read — this is a private paper-trading
tool, so a real opinion is wanted, not a disclaimer.

Rules:
- Lead with the explanation. No preamble. Add the context the post omits
  using your own knowledge, and give your take on what it implies.
- Plain English; define jargon in passing.
- A directional lean and what you'd watch/where it invalidates is welcome;
  state the risk honestly alongside it. No compliance hedging or refusals.
- Voice: crypto-native trader who knows markets — light, natural slang
  ("ser", "degen", "bags", "conviction play", "rekt", "LFG", "fade")
  sprinkled in, not forced. Subtle and earned; analysis leads, never cringe.
- Use $$TICKER form. Length: as much as the question genuinely warrants —
  no word cap — but every line earns its place, zero padding. End by
  inviting a follow-up here.

Post:
$post_text
""")


# The seed message posted into a fresh Ask-AI thread the instant it's
# created — so the thread is never an empty box the user feels they must
# type into, and so the brief is *deterministically the first bot message*.
# `_handle_explain` edits THIS message into the brief; chat._handle_thread
# refuses to answer while a message equal to this is still present (brief
# still generating) — that's what kills the "you talk → it briefs → it
# answers you" inversion. Shared, exact, single source of truth.
SEED_PENDING = "🤖 *Reading the post — one sec…*"


# Stored as feedback row emoji values so the existing tuning pipeline (which
# samples by 👍 / 👎 emoji) keeps working unchanged.
_UP_EMOJI = "👍"
_DOWN_EMOJI = "👎"


class PostActionsView(discord.ui.View):
    """Persistent 2-button view attached to every actionable bot post."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        emoji="🤖",
        label="Ask AI",
        style=discord.ButtonStyle.primary,
        custom_id="sentinel:explain",
    )
    async def explain(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await _handle_explain(interaction)

    @discord.ui.button(
        emoji="👍",
        label="Useful",
        style=discord.ButtonStyle.success,
        custom_id="sentinel:up",
    )
    async def upvote(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await _record_feedback(
            interaction, _UP_EMOJI, "👍 recorded — fed into monthly tuning."
        )

    @discord.ui.button(
        emoji="👎",
        label="Noise",
        style=discord.ButtonStyle.danger,
        custom_id="sentinel:down",
    )
    async def downvote(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await _record_feedback(
            interaction, _DOWN_EMOJI, "👎 recorded — fed into monthly tuning."
        )


# Backwards-compatible alias — older imports referenced FilingActionsView.
FilingActionsView = PostActionsView


async def _record_feedback(
    interaction: discord.Interaction,
    emoji: str,
    ack_text: str,
) -> None:
    message_id = str(interaction.message.id)
    user_id = str(interaction.user.id)

    def _persist() -> bool:
        """Insert a Feedback row idempotently. Returns True if a new row was
        created, False if the user has already voted this way."""
        with session_scope() as session:
            existing = session.exec(
                select(Feedback)
                .where(Feedback.message_id == message_id)
                .where(Feedback.user_id == user_id)
                .where(Feedback.emoji == emoji)
            ).first()
            if existing is not None:
                return False
            session.add(
                Feedback(
                    message_id=message_id,
                    emoji=emoji,
                    user_id=user_id,
                    created_at=datetime.now(timezone.utc),
                )
            )
            return True

    try:
        created = await asyncio.to_thread(_persist)
    except Exception as e:
        logger.exception("feedback record failed: {}", e)
        await interaction.response.send_message(
            "couldn't record that — check the logs.", ephemeral=True
        )
        return

    ack = ack_text if created else "already recorded."
    await interaction.response.send_message(ack, ephemeral=True)


def _thread_name(seed: str) -> str:
    """Discord thread names cap at 100 chars and read badly with markdown."""
    name = _THREAD_NAME_RE.sub("", seed or "").strip().replace("\n", " ")
    name = re.sub(r"\s+", " ", name)
    return (name[:96] + "…") if len(name) > 97 else (name or "Ask AI")


async def _handle_explain(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=False)
    msg = interaction.message

    post_text = extract_post_text(msg)
    if not post_text:
        await interaction.followup.send(
            "couldn't read the post content — nothing to explain.",
            ephemeral=True,
        )
        return

    thread = msg.thread
    if thread is None:
        seed = msg.embeds[0].title if msg.embeds else (msg.content or "Ask AI")
        try:
            thread = await msg.create_thread(
                name=_thread_name(seed), auto_archive_duration=1440
            )
        except Exception as e:
            logger.exception("Ask AI thread create failed: {}", e)
            await interaction.followup.send(
                "couldn't open a thread — I need the *Create Public Threads* "
                "permission in this channel.",
                ephemeral=True,
            )
            return

    # 1. Seed the thread IMMEDIATELY — before the (slow) LLM call — so it's
    #    never empty and the brief is guaranteed to be the first bot message.
    try:
        seed = await thread.send(SEED_PENDING)
    except Exception as e:
        logger.exception("Ask AI seed send failed: {}", e)
        await interaction.followup.send(
            f"opened {thread.mention}, but I can't post in it "
            "(I need *Send Messages in Threads*).",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        f"opened {thread.mention} — briefing you there now.", ephemeral=True
    )

    # 2. Generate the brief, guarding the LLM so a timeout/exception turns
    #    into a graceful in-thread message instead of a dead empty thread.
    rendered = EXPLAIN_PROMPT.safe_substitute(post_text=post_text[:3000])
    try:
        body = await asyncio.to_thread(
            get_llm().complete, rendered, model="light", max_tokens=1600
        )
    except Exception as e:
        logger.exception("Ask AI brief LLM failed: {}", e)
        body = ""

    from .utils import chunk_text, highlight_markdown

    if not body or body == LLM_ERROR_SENTINEL:
        # Clear the placeholder (so the thread is unblocked for Q&A) and tell
        # the user to just ask — chat.py will answer grounded in the post.
        await seed.edit(
            content="🤖 Couldn't auto-brief (model busy) — ask your "
            "question here and I'll answer it grounded in the post."
        )
        return

    # 3. The brief REPLACES the placeholder (stays the first message), with
    #    any overflow as follow-on messages.
    parts = chunk_text(highlight_markdown(body)) or [body[:1900]]
    await seed.edit(content=parts[0])
    for _part in parts[1:]:
        await thread.send(_part)


def extract_post_text(msg: discord.Message) -> str:
    """Flatten a Discord message into LLM-friendly text.

    Bot posts are usually a single embed. Pull title + description + fields
    + footer. Fall back to plain content if no embeds. Public so the thread
    Q&A handler (chat.py) can reuse it for grounding.
    """
    if not msg.embeds:
        return (msg.content or "").strip()

    parts: list[str] = []
    for embed in msg.embeds:
        if embed.title:
            parts.append(embed.title)
        if embed.description:
            parts.append(embed.description)
        for field in embed.fields:
            parts.append(f"{field.name}: {field.value}")
        if embed.footer and embed.footer.text:
            parts.append(f"({embed.footer.text})")
    return "\n".join(parts).strip()


def register_actions(bot: discord.Client) -> None:
    """Register the persistent View so buttons on older posts still work."""
    bot.add_view(PostActionsView())
    logger.info("post actions view registered")
