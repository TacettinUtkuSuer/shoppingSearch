"""Background deep-search worker.

A task runs as an asyncio.Task inside the FastAPI process, so it is independent of
any browser tab. All blocking work (curl_cffi, Claude CLI) runs in worker threads.
The SQLite `status` column is the single control channel: the loop reads it before
every unit of work, so Pause/Stop from the API take effect between URLs.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from typing import Any, Callable

import config
import database as db
from core import agents
from core.scraper import (
    RateLimitError,
    ScrapeError,
    Scraper,
    canonicalize_url,
    contains_negative,
    prune_html,
)
from llm_adapter import LLMError

log = logging.getLogger(__name__)


class _Stop(Exception):
    """Internal signal: the run must end with the given final status."""

    def __init__(self, status: str | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class RunClock:
    """Counts active (non-paused) seconds, continuing from a persisted value."""

    def __init__(self, base: float = 0.0) -> None:
        self._base = base
        self._started: float | None = time.monotonic()

    def elapsed(self) -> float:
        if self._started is None:
            return self._base
        return self._base + time.monotonic() - self._started

    def pause(self) -> None:
        if self._started is not None:
            self._base = self.elapsed()
            self._started = None

    def resume(self) -> None:
        if self._started is None:
            self._started = time.monotonic()


def product_id(task_id: str, url: str) -> str:
    return hashlib.sha1(f"{task_id}|{url}".encode()).hexdigest()[:16]


class TaskEngine:
    def __init__(
        self,
        scraper_factory: Callable[[], Any] = Scraper,
        auditor: Callable[[Any], agents.AuditResult] = agents.audit,
        poll_seconds: float | None = None,
        fetch_delay: tuple[float, float] | None = None,
    ) -> None:
        self._scraper_factory = scraper_factory
        self._auditor = auditor
        self._poll = poll_seconds if poll_seconds is not None else config.STATUS_POLL_SECONDS
        self._delay = fetch_delay or (config.FETCH_DELAY_MIN_SECONDS, config.FETCH_DELAY_MAX_SECONDS)
        self._workers: dict[str, asyncio.Task] = {}

    # --- lifecycle -------------------------------------------------------

    def is_alive(self, task_id: str) -> bool:
        worker = self._workers.get(task_id)
        return worker is not None and not worker.done()

    def start(self, task_id: str) -> bool:
        """Spawn the worker unless one is already alive. Returns True if spawned."""
        if self.is_alive(task_id):
            return False
        worker = asyncio.create_task(self._run(task_id), name=f"deep-search-{task_id}")
        self._workers[task_id] = worker

        def _cleanup(t: asyncio.Task) -> None:
            if self._workers.get(task_id) is t:
                del self._workers[task_id]

        worker.add_done_callback(_cleanup)
        return True

    async def wait(self, task_id: str) -> None:
        worker = self._workers.get(task_id)
        if worker:
            await asyncio.shield(worker)

    async def shutdown(self) -> None:
        workers = list(self._workers.values())
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    # --- control gate ----------------------------------------------------

    async def _gate(self, task_id: str, clock: RunClock) -> None:
        """Honor PAUSED/STOPPED and the duration/product limits. Raises _Stop to end."""
        paused_noted = False
        while True:
            status = db.get_status(task_id)
            if status is None:
                raise _Stop(None, "Task deleted")
            if status == db.STOPPED:
                raise _Stop(None, "Stopped by user")
            if status in (db.COMPLETED, db.FAILED):
                raise _Stop(None, "Task already finished")
            if status != db.PAUSED:
                break
            if not paused_noted:
                clock.pause()
                db.update_progress(task_id, elapsed_seconds=clock.elapsed(), message="Paused")
                paused_noted = True
            await asyncio.sleep(self._poll)
        if paused_noted:
            clock.resume()
            db.update_progress(task_id, message="Resumed")
        task = db.get_task(task_id)
        if clock.elapsed() >= task["max_duration_seconds"]:
            raise _Stop(db.COMPLETED, f"Reached max duration of {task['max_duration_seconds']}s")
        if task["product_count"] >= config.MAX_AUDITED_PRODUCTS:
            raise _Stop(db.COMPLETED, f"Reached {config.MAX_AUDITED_PRODUCTS} audited products")
        db.update_progress(task_id, elapsed_seconds=clock.elapsed())

    # --- main loop -------------------------------------------------------

    async def _run(self, task_id: str) -> None:
        task = db.get_task(task_id)
        if task is None or task["status"] in db.TERMINAL_STATUSES:
            return
        db.transition_status(task_id, (db.PENDING,), db.RUNNING, "Starting")
        clock = RunClock(task["elapsed_seconds"] or 0.0)
        try:
            scraper = await asyncio.to_thread(self._scraper_factory)
            candidates = await self._discover(task, scraper, clock)
            db.update_progress(task_id, message=f"Auditing {len(candidates)} candidate URLs")
            llm_failures = 0
            for index, url in enumerate(candidates):
                await self._gate(task_id, clock)
                outcome = await self._process_url(task, url, scraper)
                if outcome == "llm_error":
                    llm_failures += 1
                    if llm_failures >= config.LLM_MAX_CONSECUTIVE_FAILURES:
                        db.transition_status(
                            task_id, (db.RUNNING,), db.PAUSED,
                            "Paused: Claude CLI failed repeatedly. Check the CLI, then Resume.",
                        )
                        llm_failures = 0
                        continue
                else:
                    llm_failures = 0
                db.update_progress(task_id, message=f"Processed {index + 1}/{len(candidates)} candidates")
                if outcome in ("audited", "rate_limited", "skipped", "error"):
                    await asyncio.sleep(random.uniform(*self._delay))
            await self._gate(task_id, clock)
            raise _Stop(db.COMPLETED, "Candidate list exhausted")
        except _Stop as stop:
            db.update_progress(task_id, elapsed_seconds=clock.elapsed())
            if stop.status:
                db.transition_status(task_id, (db.RUNNING, db.PAUSED, db.PENDING), stop.status, stop.message)
            else:
                db.update_progress(task_id, message=stop.message)
            log.info("Task %s ended: %s", task_id, stop.message)
        except asyncio.CancelledError:
            db.update_progress(task_id, elapsed_seconds=clock.elapsed())
            db.transition_status(task_id, (db.RUNNING, db.PENDING), db.PAUSED, "Interrupted by server shutdown")
            raise
        except Exception as exc:  # never leave a task stuck in RUNNING
            log.exception("Task %s crashed", task_id)
            db.update_progress(task_id, elapsed_seconds=clock.elapsed())
            db.transition_status(task_id, (db.RUNNING, db.PAUSED, db.PENDING), db.FAILED, f"Error: {exc}"[:500])

    async def _discover(self, task: dict[str, Any], scraper: Any, clock: RunClock) -> list[str]:
        """Run every search term through the search engine. Zero LLM tokens."""
        task_id = task["task_id"]
        negatives = task["negative_keywords"]
        seen: set[str] = set()
        candidates: list[str] = []
        for term in task["search_terms"]:
            await self._gate(task_id, clock)
            db.update_progress(task_id, message=f"Searching: {term}")
            try:
                results = await asyncio.to_thread(scraper.search, term, negatives)
            except ScrapeError as exc:
                log.warning("Search failed for %r: %s", term, exc)
                continue
            for r in results:
                url = canonicalize_url(r.url)
                if url in seen:
                    continue
                seen.add(url)
                owner = db.visited_by(url)
                if owner == task_id:
                    continue  # already handled in an earlier run of this task
                candidates.append(url)
        # Interleave by platform so one marketplace's throttling does not stall the whole run.
        by_platform: dict[str, list[str]] = {}
        for url in candidates:
            by_platform.setdefault(url.split("/")[2], []).append(url)
        ordered: list[str] = []
        while any(by_platform.values()):
            for urls in by_platform.values():
                if urls:
                    ordered.append(urls.pop(0))
        if not ordered and not seen:
            raise _Stop(db.COMPLETED, "Search returned no usable results")
        return ordered

    async def _process_url(self, task: dict[str, Any], url: str, scraper: Any) -> str:
        task_id = task["task_id"]
        owner = db.visited_by(url)
        if owner == task_id:
            return "duplicate"
        if owner is not None:
            # Another task already paid for this URL; reuse its audit at zero cost.
            prior = db.find_product_by_url(url)
            if prior:
                prior.update(id=product_id(task_id, url), task_id=task_id)
                db.copy_product(prior)
                return "reused"
            return "duplicate"

        try:
            fetched = await asyncio.to_thread(scraper.fetch, url)
        except RateLimitError as exc:
            log.warning("Giving up on %s for now: %s", url, exc)
            return "rate_limited"  # not marked visited, so a resumed run retries it
        except ScrapeError as exc:
            db.mark_visited(url, task_id, "fetch_error")
            log.info("Skipping %s: %s", url, exc)
            return "error"

        page = await asyncio.to_thread(prune_html, fetched.html, canonicalize_url(fetched.final_url))
        page.url = url
        if not page.looks_like_product():
            db.mark_visited(url, task_id, "not_product")
            return "skipped"
        hit = contains_negative(page.title or "", task["negative_keywords"])
        if hit:
            db.mark_visited(url, task_id, f"negative:{hit}")
            return "skipped"

        try:
            result = await asyncio.to_thread(self._auditor, page)
        except LLMError as exc:
            log.warning("Audit failed for %s: %s", url, exc)
            return "llm_error"  # not marked visited: retried on resume

        db.save_audit(
            {
                "id": product_id(task_id, url),
                "task_id": task_id,
                "title": page.title,
                "platform": page.platform,
                "price": page.price,
                "unit_price": result.normalized_unit_price,
                "trust_score": result.trust_score,
                "contradictions_json": result.contradictions,
                "attributes_json": result.dynamic_attributes,
                "product_url": url,
                "currency": page.currency,
                "shipping_cost": page.shipping_cost,
                "units_in_pack": result.units_in_pack,
                "in_stock": None if result.in_stock is None else int(result.in_stock),
            },
            url,
        )
        return "audited"

