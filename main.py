"""FastAPI entry point: REST control API plus the static dual-view dashboard.

Run with:  uvicorn main:app --host 127.0.0.1 --port 8000
Use a single worker process: background tasks live inside this process.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import config
import database as db
from core import agents
from core.task_engine import TaskEngine, to_thread
from llm_adapter import LLMError

logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

engine = TaskEngine()


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    recovered = db.recover_orphaned_tasks()
    if recovered:
        log.info("Marked %d interrupted task(s) as PAUSED", recovered)
    yield
    await engine.shutdown()


app = FastAPI(title="Product Discovery and Audit Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


class Clarification(BaseModel):
    question: str = Field(max_length=500)
    answer: str = Field(max_length=500)


class ResolveRequest(BaseModel):
    description: str = Field(min_length=2, max_length=500)
    clarifications: List[Clarification] = Field(default_factory=list, max_length=10)


class CreateTaskRequest(BaseModel):
    description: str = Field(default="", max_length=500)
    search_terms: List[str] = Field(min_length=1, max_length=5)
    negative_keywords: List[str] = Field(default_factory=list, max_length=20)
    detected_category: Optional[str] = None
    requirements: List[str] = Field(default_factory=list, max_length=10)
    max_duration_seconds: int = Field(default=config.DEFAULT_MAX_DURATION_SECONDS, ge=60, le=7 * 24 * 3600)

    @field_validator("search_terms")
    @classmethod
    def _terms(cls, v: List[str]) -> List[str]:
        v = [" ".join(t.split()) for t in v if t.strip()]
        if not v:
            raise ValueError("at least one non-empty search term is required")
        return v

    @field_validator("requirements")
    @classmethod
    def _requirements(cls, v: List[str]) -> List[str]:
        return [" ".join(r.split())[:80] for r in v if r.strip()][:6]

    @field_validator("negative_keywords")
    @classmethod
    def _negatives(cls, v: List[str]) -> List[str]:
        return [k.strip().lstrip("-").strip().lower() for k in v if k.strip().lstrip("-").strip()]


def _task_or_404(task_id: str) -> Dict[str, Any]:
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    task["worker_alive"] = engine.is_alive(task_id)
    return task


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "index.html")


@app.post("/api/resolve")
async def resolve(req: ResolveRequest) -> Dict[str, Any]:
    try:
        result = await to_thread(
            agents.resolve, req.description, [c.model_dump() for c in req.clarifications]
        )
    except LLMError as exc:
        raise HTTPException(502, f"Resolver failed: {exc}") from exc
    return result.model_dump()


@app.post("/api/tasks", status_code=201)
async def create_task(req: CreateTaskRequest) -> Dict[str, Any]:
    task_id = uuid.uuid4().hex[:12]
    db.create_task(
        task_id,
        req.description,
        req.search_terms,
        req.negative_keywords,
        req.max_duration_seconds,
        req.detected_category,
        req.requirements,
    )
    engine.start(task_id)
    return _task_or_404(task_id)


@app.get("/api/tasks")
async def list_tasks() -> List[Dict[str, Any]]:
    tasks = db.list_tasks()
    for t in tasks:
        t["worker_alive"] = engine.is_alive(t["task_id"])
    return tasks


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str) -> Dict[str, Any]:
    return _task_or_404(task_id)


@app.post("/api/tasks/{task_id}/pause")
async def pause_task(task_id: str) -> Dict[str, Any]:
    _task_or_404(task_id)
    if not db.transition_status(task_id, (db.RUNNING, db.PENDING), db.PAUSED, "Pause requested"):
        raise HTTPException(409, "Only running or pending tasks can be paused")
    return _task_or_404(task_id)


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: str) -> Dict[str, Any]:
    _task_or_404(task_id)
    if not db.transition_status(task_id, (db.PAUSED,), db.RUNNING, "Resume requested"):
        raise HTTPException(409, "Only paused tasks can be resumed")
    engine.start(task_id)  # no-op when the worker is alive and waiting in its pause loop
    return _task_or_404(task_id)


@app.post("/api/tasks/{task_id}/stop")
async def stop_task(task_id: str) -> Dict[str, Any]:
    _task_or_404(task_id)
    if not db.transition_status(task_id, (db.RUNNING, db.PENDING, db.PAUSED), db.STOPPED, "Stopped by user"):
        raise HTTPException(409, "Task is already finished")
    return _task_or_404(task_id)


@app.get("/api/tasks/{task_id}/products")
async def list_products(
    task_id: str, sort: str = "unit_price", include_rejected: bool = False
) -> List[Dict[str, Any]]:
    _task_or_404(task_id)
    return db.list_products(task_id, sort, include_rejected)


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "claude_bin": config.CLAUDE_BIN, "max_products": config.MAX_AUDITED_PRODUCTS}
