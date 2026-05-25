"""Lookup endpoint — same chat.lookup() the Discord !cmds use.

`kind` is one of: ticker, news, filing, timeline, recent, catalysts,
status. Returns the markdown blob exactly as Discord sees it; the
frontend renders it with the existing Markdown component."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query


router = APIRouter()


_KINDS = {"ticker", "news", "filing", "timeline", "recent", "catalysts", "status"}


@router.get("/lookup/{kind}")
async def lookup(kind: str, arg: str = Query("", max_length=200)) -> dict:
    """Run a lookup. Wrapped in `asyncio.to_thread` because `chat.lookup`
    is sync (it makes DB queries directly), and we don't want to block
    the event loop on a heavy ticker timeline."""
    kind = kind.strip().lower()
    if kind not in _KINDS:
        return {
            "kind": kind,
            "arg": arg,
            "body": (
                f"_unknown lookup kind `{kind}`. "
                f"Valid: {sorted(_KINDS)}._"
            ),
        }
    from .. import chat

    body = await asyncio.to_thread(chat.lookup, kind, arg)
    return {"kind": kind, "arg": arg, "body": body or "_(no result)_"}
