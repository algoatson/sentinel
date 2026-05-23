"""Ask-AI thread ordering.

The bug was: thread created empty → slow seed brief → user types → the
follow-up handler answers *that* before the brief lands ("you talk → it
briefs → it answers you"). The fix makes the brief the deterministic first
message and has the follow-up handler refuse to answer while the placeholder
seed is still present. That gate is `_seed_still_pending` — pinned here.
"""

from __future__ import annotations

import asyncio

from sentinel.chat import _seed_still_pending
from sentinel.interactions import SEED_PENDING


class _Msg:
    def __init__(self, author, content):
        self.author = author
        self.content = content


class _Hist:
    def __init__(self, msgs):
        self._it = iter(msgs)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _Chan:
    def __init__(self, msgs, *, raise_on_history=False):
        self._msgs = msgs
        self._raise = raise_on_history

    def history(self, limit=12):
        if self._raise:
            raise RuntimeError("history forbidden")
        return _Hist(self._msgs)


class _Bot:
    def __init__(self):
        self.user = object()


def _run(chan, bot):
    return asyncio.run(_seed_still_pending(chan, bot))


def test_pending_while_placeholder_present():
    bot = _Bot()
    chan = _Chan([
        _Msg(object(), "a user message"),
        _Msg(bot.user, SEED_PENDING),          # brief still generating
    ])
    assert _run(chan, bot) is True             # → follow-up handler holds off


def test_not_pending_once_brief_replaced_placeholder():
    bot = _Bot()
    chan = _Chan([
        _Msg(bot.user, "Here's the read on $NVDA: …"),  # seed edited → brief
        _Msg(object(), "follow-up question"),
    ])
    assert _run(chan, bot) is False            # → normal Q&A proceeds


def test_not_pending_when_empty_or_history_blocked():
    bot = _Bot()
    assert _run(_Chan([]), bot) is False
    assert _run(_Chan([], raise_on_history=True), bot) is False  # never raises


def test_placeholder_from_a_non_bot_author_does_not_count():
    bot = _Bot()
    # someone else literally typing the placeholder text must NOT gate us
    chan = _Chan([_Msg(object(), SEED_PENDING)])
    assert _run(chan, bot) is False
