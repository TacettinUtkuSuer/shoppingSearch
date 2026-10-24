import asyncio
import threading

import pytest

import config
from core import agents
from core.scraper import FetchResult, RateLimitError, SearchResult
from core.task_engine import TaskEngine
from conftest import fixture_text

PRODUCT_HTML = fixture_text("product_page.html")
BLOG_HTML = "<html><body><h1>Top 10 pumps</h1><p>blog</p></body></html>"


class FakeScraper:
    """Records fetches; optionally blocks on a gate so tests can pause mid-run."""

    def __init__(self, urls, html_for=None, rate_limited=()):
        self.urls = urls
        self.html_for = html_for or {}
        self.rate_limited = set(rate_limited)
        self.fetched = []
        self.lock = threading.Lock()

    def search(self, term, negatives):
        return [SearchResult(url=u, title="t", snippet="") for u in self.urls]

    def fetch(self, url):
        with self.lock:
            self.fetched.append(url)
        if url in self.rate_limited:
            raise RateLimitError("captcha")
        return FetchResult(url, url, 200, self.html_for.get(url, PRODUCT_HTML))


FAKE_CLI_OUTPUT = (
    'Here is the audit:\n```json\n{"units_in_pack": 2, "normalized_unit_price": 1, "in_stock": true, '
    '"contradictions": ["c1"], "trust_score": 55, "dynamic_attributes": {"port": "Type-C"}}\n```'
)


def fake_audit(page):
    """The real auditor with only the Claude CLI subprocess replaced."""
    return agents.audit(page, runner=lambda prompt: FAKE_CLI_OUTPUT)


def make_engine(scraper, auditor=fake_audit):
    return TaskEngine(scraper_factory=lambda: scraper, auditor=auditor, poll_seconds=0.01, fetch_delay=(0, 0))


def create(db, task_id="t1", terms=("pump",), max_duration=3600):
    return db.create_task(task_id, "desc", list(terms), ["aquarium"], max_duration)


URLS = [f"https://shop{i % 3}.com/p/{i}" for i in range(8)]


def test_full_run_audits_and_completes(fresh_db):
    db = fresh_db
    scraper = FakeScraper(URLS, html_for={URLS[1]: BLOG_HTML})

    async def main():
        create(db)
        engine = make_engine(scraper)
        engine.start("t1")
        await engine.wait("t1")

    asyncio.run(main())
    task = db.get_task("t1")
    assert task["status"] == db.COMPLETED
    assert task["product_count"] == 7  # blog page skipped with zero LLM calls
    assert task["visited_count"] == 8
    products = db.list_products("t1")
    assert products[0]["unit_price"] == 249.5  # (499 + free shipping) / 2 computed in Python
    assert products[0]["attributes"] == {"port": "Type-C"}


def test_pause_then_resume_does_not_refetch_visited_urls(fresh_db):
    db = fresh_db
    scraper = FakeScraper(URLS)
    audited = []

    def pausing_audit(page):
        audited.append(page.url)
        if len(audited) == 3:
            db.transition_status("t1", (db.RUNNING,), db.PAUSED, "Pause requested")
        return fake_audit(page)

    async def main():
        create(db)
        engine = make_engine(scraper, pausing_audit)
        engine.start("t1")
        for _ in range(200):
            await asyncio.sleep(0.01)
            if db.get_task("t1")["message"] == "Paused":
                break
        assert db.get_status("t1") == db.PAUSED
        fetched_while_paused = list(scraper.fetched)
        await asyncio.sleep(0.1)
        assert scraper.fetched == fetched_while_paused  # truly idle while paused
        await engine.shutdown()  # simulate server restart: worker gone

        # A brand-new engine (new process) resumes the task.
        db.transition_status("t1", (db.PAUSED,), db.RUNNING, "Resume requested")
        engine2 = make_engine(scraper, pausing_audit)
        engine2.start("t1")
        await engine2.wait("t1")

    asyncio.run(main())
    assert db.get_status("t1") == db.COMPLETED
    assert len(scraper.fetched) == len(URLS)
    assert len(set(scraper.fetched)) == len(URLS)  # no URL fetched twice
    assert db.count_products("t1") == len(URLS)


def test_stop_terminates_without_losing_data(fresh_db):
    db = fresh_db
    scraper = FakeScraper(URLS)

    def stopping_audit(page):
        if len(scraper.fetched) == 2:
            db.transition_status("t1", (db.RUNNING,), db.STOPPED, "Stopped by user")
        return fake_audit(page)

    async def main():
        create(db)
        engine = make_engine(scraper, stopping_audit)
        engine.start("t1")
        await engine.wait("t1")

    asyncio.run(main())
    assert db.get_status("t1") == db.STOPPED
    assert db.count_products("t1") == 2
    assert len(scraper.fetched) == 2


def test_product_limit(fresh_db, monkeypatch):
    db = fresh_db
    monkeypatch.setattr(config, "MAX_AUDITED_PRODUCTS", 3)

    async def main():
        create(db)
        engine = make_engine(FakeScraper(URLS))
        engine.start("t1")
        await engine.wait("t1")

    asyncio.run(main())
    task = db.get_task("t1")
    assert task["status"] == db.COMPLETED and task["product_count"] == 3
    assert "3 audited products" in task["message"]


def test_duration_limit(fresh_db):
    db = fresh_db
    create(db, max_duration=60)
    db.update_progress("t1", elapsed_seconds=61)

    async def main():
        engine = make_engine(FakeScraper(URLS))
        engine.start("t1")
        await engine.wait("t1")

    asyncio.run(main())
    task = db.get_task("t1")
    assert task["status"] == db.COMPLETED and "max duration" in task["message"]
    assert task["product_count"] == 0


def test_rate_limited_urls_stay_unvisited_for_retry(fresh_db):
    db = fresh_db
    scraper = FakeScraper(URLS[:3], rate_limited={URLS[0]})

    async def main():
        create(db)
        engine = make_engine(scraper)
        engine.start("t1")
        await engine.wait("t1")

    asyncio.run(main())
    assert not db.is_visited(URLS[0])
    assert db.count_products("t1") == 2


def test_repeated_llm_failures_pause_the_task(fresh_db):
    from llm_adapter import LLMError

    db = fresh_db

    def broken(page):
        raise LLMError("Claude CLI binary not found")

    async def main():
        create(db)
        engine = make_engine(FakeScraper(URLS), broken)
        engine.start("t1")
        for _ in range(300):
            await asyncio.sleep(0.01)
            if db.get_status("t1") == db.PAUSED:
                break
        await engine.shutdown()

    asyncio.run(main())
    assert db.get_status("t1") == db.PAUSED
    assert db.get_task("t1")["visited_count"] == 0  # failed audits are retried on resume


def test_second_task_reuses_audits_without_refetching(fresh_db):
    db = fresh_db
    scraper = FakeScraper(URLS[:4])

    async def main():
        create(db, "t1")
        create(db, "t2")
        engine = make_engine(scraper)
        engine.start("t1")
        await engine.wait("t1")
        engine.start("t2")
        await engine.wait("t2")

    asyncio.run(main())
    assert len(scraper.fetched) == 4
    assert db.count_products("t2") == 4
