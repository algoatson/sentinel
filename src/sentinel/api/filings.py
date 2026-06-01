"""Filing endpoints — list recent SEC filings with materiality scores.

The bot summarises filings at ingest time (`pipelines/filings.py`), so
the list/detail endpoints serve the cached `Filing.summary` +
`materiality_score` directly. `/ask` adds an on-demand follow-up Q&A on
top of that stored read (no full-document text — the LLM is told so)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import select

from .. import dossier as _dossier
from ..db import session_scope
from ..models import Filing


router = APIRouter()


def _aware_iso(t: datetime | None) -> str | None:
    if t is None:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).isoformat()


@router.get("/filings")
def list_recent(
    hours: int = Query(48, ge=1, le=720),
    ticker: str | None = Query(None),
    form: str | None = Query(None),
    min_materiality: int = Query(0, ge=0, le=10),
    limit: int = Query(80, ge=1, le=200),
) -> list[dict]:
    """Newest-first filings inside `hours`. Filters: ticker, form_type,
    minimum materiality score."""
    cutoff_naive = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).replace(tzinfo=None)
    with session_scope() as s:
        q = select(Filing).where(Filing.filed_at >= cutoff_naive)
        if ticker:
            q = q.where(Filing.ticker == ticker.upper())
        if form:
            q = q.where(Filing.form_type == form)
        if min_materiality > 0:
            q = q.where(Filing.materiality_score >= min_materiality)
        rows = s.exec(
            q.order_by(Filing.filed_at.desc()).limit(limit)
        ).all()
        return [
            {
                "id": r.id,
                "cik": r.cik,
                "ticker": r.ticker,
                "form_type": r.form_type,
                "accession_number": r.accession_number,
                "filed_at": _aware_iso(r.filed_at),
                "primary_doc_url": r.primary_doc_url,
                "summary": r.summary,
                "materiality_score": r.materiality_score,
                "materiality_reason": r.materiality_reason,
            }
            for r in rows
        ]


@router.get("/filings/{filing_id}")
def get_one(filing_id: int) -> dict:
    with session_scope() as s:
        f = s.get(Filing, filing_id)
        if f is None:
            raise HTTPException(404, f"filing #{filing_id} not found")
        return {
            "id": f.id,
            "cik": f.cik,
            "ticker": f.ticker,
            "form_type": f.form_type,
            "accession_number": f.accession_number,
            "filed_at": _aware_iso(f.filed_at),
            "primary_doc_url": f.primary_doc_url,
            "summary": f.summary,
            "materiality_score": f.materiality_score,
            "materiality_reason": f.materiality_reason,
        }


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=600)


@router.post("/filings/{filing_id}/ask")
def ask_about_filing(filing_id: int, body: AskRequest) -> dict:
    """Follow-up chat about a filing — reasons from the stored summary +
    materiality read + price context (not the full document). NOT cached."""
    answer = _dossier.ask_about_filing(filing_id, body.question)
    return {"filing_id": filing_id, "answer": answer}
