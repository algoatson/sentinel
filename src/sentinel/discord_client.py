"""Discord bot wrapper.

post_filing renders the SPEC §9 filing embed; post_embed is the generic
helper used by every other pipeline. Both attach the PostActionsView
(🤖 Ask AI / 👍 / 👎). The Ask AI button opens a discussion thread on demand
(interactions.py); posts are no longer auto-threaded. #meta skips the view.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import discord
from loguru import logger

from .config import settings
from .interactions import PostActionsView
from .models import Filing

if TYPE_CHECKING:
    from .pipelines.enrich import EnrichmentContext


FORM_TYPE_COLORS = {
    "8-K": 0xD64545,
    "8-K/A": 0xD64545,
    "4": 0x4B7BEC,
    "4/A": 0x4B7BEC,
    "10-Q": 0x2ECC71,
    "10-Q/A": 0x2ECC71,
    "10-K": 0x2ECC71,
    "10-K/A": 0x2ECC71,
    "13F-HR": 0x8E44AD,
    "13F-HR/A": 0x8E44AD,
    "S-1": 0xE67E22,
    "S-1/A": 0xE67E22,
    "DEF 14A": 0xF1C40F,
    "PRE 14A": 0xF1C40F,
}


def _color_for(form_type: str) -> int:
    if form_type in FORM_TYPE_COLORS:
        return FORM_TYPE_COLORS[form_type]
    if form_type.startswith("424B"):
        return 0xE67E22
    return 0x95A5A6


class FilingRadarBot(discord.Client):
    """Discord client wrapper. Event handlers (on_ready, on_message,
    on_raw_reaction_add) are registered externally via @bot.event in main,
    chat, and feedback modules so this class stays a pure transport layer.
    """

    def __init__(self) -> None:
        intents = discord.Intents.default()
        # message_content is a privileged intent — must also be toggled on in
        # the Discord developer portal under Bot → Privileged Gateway Intents.
        intents.message_content = True
        intents.reactions = True
        # Single-user bot watching its own channels — it never needs to ping.
        # Neutering mentions globally kills the reply-ping that fires on every
        # in-thread answer AND any stray <@user> in post content. Mentions
        # still render as text, they just don't notify.
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )


_bot: Optional[FilingRadarBot] = None


def get_bot() -> FilingRadarBot:
    global _bot
    if _bot is None:
        _bot = FilingRadarBot()
    return _bot


async def _channel(channel_id: int) -> discord.abc.Messageable:
    bot = get_bot()
    chan = bot.get_channel(channel_id)
    if chan is None:
        chan = await bot.fetch_channel(channel_id)
    return chan


# Threads are no longer auto-spawned on every post (that buried channels in
# empty threads). Discussion threads are now opened on demand by the "Ask AI"
# button — see interactions.py.


# At-a-glance triage: every actionable post carries an importance 1-5.
_IMPORTANCE_BADGE = {5: "🔴", 4: "🟠", 3: "🟡", 2: "🔵", 1: "⚪"}


def _apply_importance(
    embed: discord.Embed, level: Optional[int], note: str = ""
) -> None:
    """Prefix the embed title with an importance dot and add a field, so the
    user can scan a channel and see what actually matters."""
    if not level or level not in _IMPORTANCE_BADGE:
        return
    badge = _IMPORTANCE_BADGE[level]
    if embed.title and not embed.title.startswith(tuple(_IMPORTANCE_BADGE.values())):
        embed.title = f"{badge} {embed.title}"[:256]
    embed.add_field(
        name="Importance",
        value=f"{badge} {level}/5{(' — ' + note) if note else ''}"[:1024],
        inline=True,
    )


def _finalize(embed: discord.Embed) -> discord.Embed:
    """Last touch before every send: stamp a consistent UTC timestamp if the
    caller didn't set one. Discord renders it as a tidy localized footer time,
    so every post across the server reads uniformly — for free."""
    if embed.timestamp is None:
        embed.timestamp = datetime.now(timezone.utc)
    return embed


async def post_filing(
    filing: Filing,
    *,
    enrichment: Optional["EnrichmentContext"] = None,
    channel_id: Optional[int] = None,
) -> str:
    """Post a filing embed and return the Discord message id.

    If `enrichment` is supplied, its footer is rendered. If
    `filing.materiality_score` is set, a Materiality field is added.
    """
    channel_id = channel_id or settings.DISCORD_FILINGS_CHANNEL_ID
    chan = await _channel(channel_id)

    title = f"[{filing.ticker or filing.cik}] {filing.form_type}"
    description = (filing.summary or "(no summary)")[:4000]

    embed = discord.Embed(
        title=title[:256],
        description=description,
        url=filing.primary_doc_url,
        color=_color_for(filing.form_type),
    )
    embed.add_field(
        name="Filed",
        value=filing.filed_at.strftime("%Y-%m-%d %H:%M UTC"),
        inline=True,
    )
    if filing.materiality_score is not None:
        reason = filing.materiality_reason or ""
        embed.add_field(
            name="Materiality",
            value=f"{filing.materiality_score}/3 — {reason}"[:1024],
            inline=True,
        )
    from .portfolio import is_held

    if is_held(filing.ticker):
        embed.add_field(name="📌 Your book", value="touches a holding", inline=True)
    # Materiality 0-3 → importance 1-5 so filings scan the same as everything.
    _MAT_TO_IMP = {3: 5, 2: 4, 1: 2, 0: 1}
    if filing.materiality_score is not None:
        _apply_importance(
            embed,
            _MAT_TO_IMP.get(filing.materiality_score),
            (filing.materiality_reason or "")[:80],
        )
    if enrichment is not None:
        from .pipelines.enrich import render_footer

        footer = render_footer(enrichment)
        if footer:
            embed.set_footer(text=footer[:2048])

    msg = await chan.send(embed=_finalize(embed), view=PostActionsView())
    return str(msg.id)


async def post_embed(
    channel_id: int,
    embed: discord.Embed,
    *,
    content: Optional[str] = None,
    with_actions: bool = True,
    importance: Optional[int] = None,
    importance_note: str = "",
) -> Optional[discord.Message]:
    """Generic embed-poster used by every pipeline. Attaches the actions view
    (🤖 Ask AI / 👍 / 👎) by default; pass `with_actions=False` for posts that
    aren't user-actionable (e.g. #meta error notifications). `importance`
    (1-5) renders the triage badge.

    Returns the Message on success, None on send failure (already logged).
    """
    _apply_importance(embed, importance, importance_note)
    try:
        chan = await _channel(channel_id)
    except Exception as e:
        logger.exception("post_embed: channel {} unreachable: {}", channel_id, e)
        return None
    view = PostActionsView() if with_actions else None
    try:
        return await chan.send(content=content, embed=_finalize(embed), view=view)
    except Exception as e:
        logger.exception("post_embed: send failed in channel {}: {}", channel_id, e)
        return None


async def post_meta(content: str) -> Optional[str]:
    """Plain text to #meta. No actions view — errors aren't reactable."""
    try:
        chan = await _channel(settings.DISCORD_META_CHANNEL_ID)
        msg = await chan.send(content[:2000])
        return str(msg.id)
    except Exception as e:
        logger.debug("post_meta failed: {}", e)
        return None


def jump_url(channel_id: int | None, message_id: str | None) -> Optional[str]:
    """Build a Discord deep-link to a prior message, or None if we can't
    (guild/channel/message unknown). Used to trace a resolved call back to
    the post that made it."""
    gid = settings.DISCORD_GUILD_ID
    if not (gid and channel_id and message_id):
        return None
    return f"https://discord.com/channels/{gid}/{channel_id}/{message_id}"


async def run_with_bot(coro_factory) -> None:
    """Boot a short-lived Discord client, await on_ready, run coro_factory(), close.

    coro_factory is a zero-arg async callable invoked once the bot is ready.
    """
    bot = get_bot()
    ready = asyncio.Event()

    @bot.event
    async def on_ready() -> None:  # noqa: F811
        logger.info("Discord connected as {}", bot.user)
        ready.set()

    start_task = asyncio.create_task(bot.start(settings.DISCORD_TOKEN))
    try:
        # Race the ready event against the start task — if start fails before
        # ready fires, surface that error instead of hanging.
        done, _ = await asyncio.wait(
            {asyncio.create_task(ready.wait()), start_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not ready.is_set():
            for t in done:
                if t is start_task and t.exception() is not None:
                    raise t.exception()
            raise RuntimeError("Discord client exited before ready")

        await coro_factory()
    finally:
        await bot.close()
        try:
            await start_task
        except Exception:
            pass
