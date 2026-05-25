"""Research-desk endpoints — list tasks, get one, run a new prompt,
execute the recommended trade."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import research_desk as _rd


router = APIRouter()


class RunRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000)


@router.get("/research")
def list_recent(n: int = 30) -> list[dict]:
    return _rd.list_recent(n)


@router.get("/research/{task_id}")
def get_task(task_id: int) -> dict:
    t = _rd.get_task(task_id)
    if t is None:
        raise HTTPException(404, f"task #{task_id} not found")
    return t


@router.post("/research/run")
async def run(body: RunRequest) -> dict:
    """Kick off a research task. Returns task_id; the frontend then
    GETs `/research/{id}` to read the result."""
    task_id = await _rd.run_research(body.prompt)
    return {"task_id": task_id}


@router.post("/research/{task_id}/execute")
def execute(task_id: int) -> dict:
    result = _rd.execute(task_id)
    return {"task_id": task_id, **result}


@router.get("/research/meta/executions-remaining")
def remaining_today() -> dict:
    return {"remaining": _rd.executions_remaining_today()}
