"""Copilot endpoint — same context as Discord's @-mention / !ask, so
the dashboard chat sees the same world the Discord chat does."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field


router = APIRouter()


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    deep: bool = Field(
        default=True,
        description=(
            "When true (default), the model is given access to the "
            "market-tool registry — it can pull a chart, ATR, peer "
            "movers, news, filings, correlation, microstructure, or "
            "the book on demand mid-reasoning. False uses the cheap "
            "one-shot light-model path."
        ),
    )


@router.post("/copilot/ask")
async def ask(body: AskRequest) -> dict:
    from .. import chat

    # 1100 cap covers the long-form "summarise the book" answer while
    # being half the previous 1600 default. Tools + pre-built context
    # carry most of the weight; the model rarely needs more than 600.
    res = await chat.answer_question_with_meta(
        body.question, max_tokens=1100, use_tools=body.deep,
    )
    return res
