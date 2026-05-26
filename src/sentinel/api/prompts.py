"""Prompt editor endpoints — list, get, save, rollback.

Backed by the existing `PromptVersion` table + `prompts.get_prompt()`
DB-overrides-code switching path. Saving a new prompt deactivates the
previous active row for that name and inserts a new one; the next
pipeline call picks it up live (no restart).

Old versions are kept for rollback — listing returns them with
created_at so the user can see what was changed when.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from ..db import session_scope
from ..models import PromptVersion
from ..prompts import ALL_PROMPTS


router = APIRouter()


def _seed_content(name: str) -> str:
    """The code-constant template body for `name` — what the prompt
    would be without any DB overrides. Used as the "Reset to default"
    target and shown alongside the active version for diffing."""
    tmpl = ALL_PROMPTS.get(name)
    return tmpl.template if tmpl else ""


@router.get("/prompts")
def list_prompts() -> list[dict]:
    """Every known prompt name + the active version's metadata. Body
    is omitted from the list view — callers fetch /prompts/{name} to
    get the full content."""
    out: list[dict] = []
    with session_scope() as s:
        active_rows = {
            r.prompt_name: r
            for r in s.exec(
                select(PromptVersion).where(PromptVersion.active == True)  # noqa: E712
            ).all()
        }
    for name in sorted(ALL_PROMPTS.keys()):
        row = active_rows.get(name)
        seed = _seed_content(name)
        overridden = row is not None and row.content != seed
        out.append({
            "name": name,
            "active_id": row.id if row else None,
            "created_at": (
                (row.created_at.replace(tzinfo=timezone.utc)
                 if row.created_at.tzinfo is None else row.created_at).isoformat()
                if row else None
            ),
            "overridden": overridden,
            "seed_len": len(seed),
            "active_len": len(row.content) if row else len(seed),
        })
    return out


@router.get("/prompts/{name}")
def get_prompt_versions(name: str) -> dict:
    """Active version + the last N inactive versions (audit trail) +
    the code-constant seed for diffing."""
    if name not in ALL_PROMPTS:
        raise HTTPException(404, f"unknown prompt {name!r}")
    seed = _seed_content(name)
    with session_scope() as s:
        rows = s.exec(
            select(PromptVersion)
            .where(PromptVersion.prompt_name == name)
            .order_by(PromptVersion.created_at.desc())
            .limit(20)
        ).all()
    active = next((r for r in rows if r.active), None)
    history = [
        {
            "id": r.id,
            "created_at": (
                r.created_at.replace(tzinfo=timezone.utc)
                if r.created_at.tzinfo is None else r.created_at
            ).isoformat(),
            "active": r.active,
            "len": len(r.content),
        }
        for r in rows if not r.active
    ]
    return {
        "name": name,
        "seed": seed,
        "active": (
            {
                "id": active.id,
                "content": active.content,
                "created_at": (
                    active.created_at.replace(tzinfo=timezone.utc)
                    if active.created_at.tzinfo is None
                    else active.created_at
                ).isoformat(),
            } if active else None
        ),
        # When there's no DB row, the "active" content is the code seed.
        "active_content": active.content if active else seed,
        "overridden": active is not None and active.content != seed,
        "history": history,
    }


class PromptSave(BaseModel):
    content: str = Field(..., min_length=1, max_length=20000)


@router.put("/prompts/{name}")
def save_prompt(name: str, body: PromptSave) -> dict:
    """Save `body.content` as the new active version of `name`. The
    previous active row is flipped to inactive (kept for rollback).
    Returns the new active payload."""
    if name not in ALL_PROMPTS:
        raise HTTPException(404, f"unknown prompt {name!r}")
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "content cannot be empty")
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        # Deactivate any current active rows for this name.
        existing = s.exec(
            select(PromptVersion)
            .where(PromptVersion.prompt_name == name)
            .where(PromptVersion.active == True)  # noqa: E712
        ).all()
        for r in existing:
            r.active = False
            s.add(r)
        new_row = PromptVersion(
            prompt_name=name,
            content=content,
            created_at=now,
            active=True,
        )
        s.add(new_row)
        s.flush()
        s.refresh(new_row)
        new_id = new_row.id
    return {
        "name": name,
        "active_id": new_id,
        "created_at": now.isoformat(),
        "len": len(content),
        "overridden": content != _seed_content(name),
    }


@router.post("/prompts/{name}/reset")
def reset_prompt(name: str) -> dict:
    """Deactivate any active DB row so `get_prompt` falls back to the
    code constant. Old rows stay for audit."""
    if name not in ALL_PROMPTS:
        raise HTTPException(404, f"unknown prompt {name!r}")
    with session_scope() as s:
        rows = s.exec(
            select(PromptVersion)
            .where(PromptVersion.prompt_name == name)
            .where(PromptVersion.active == True)  # noqa: E712
        ).all()
        for r in rows:
            r.active = False
            s.add(r)
    return {"name": name, "reset": True, "deactivated": len(rows)}


@router.post("/prompts/{name}/restore/{version_id}")
def restore_prompt(name: str, version_id: int) -> dict:
    """Bring an older version back to active. Useful for rollback
    after a regression."""
    if name not in ALL_PROMPTS:
        raise HTTPException(404, f"unknown prompt {name!r}")
    with session_scope() as s:
        target = s.get(PromptVersion, version_id)
        if target is None or target.prompt_name != name:
            raise HTTPException(404, f"version {version_id} not found for {name!r}")
        # Deactivate every other row for this name.
        for r in s.exec(
            select(PromptVersion).where(PromptVersion.prompt_name == name)
        ).all():
            r.active = (r.id == version_id)
            s.add(r)
    return {"name": name, "active_id": version_id}
