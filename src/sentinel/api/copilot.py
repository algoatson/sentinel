"""Copilot endpoint — same context as Discord's @-mention / !ask, so
the dashboard chat sees the same world the Discord chat does."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field


router = APIRouter()


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)


@router.post("/copilot/ask")
async def ask(body: AskRequest) -> dict:
    from .. import chat

    answer = await chat.answer_question(body.question, max_tokens=1600)
    return {"answer": answer}
