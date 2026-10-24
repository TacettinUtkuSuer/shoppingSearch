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


def cli_output(match_score=90, unmet="[]"):
    return (
        'Here is the audit:\n```json\n{"units_in_pack": 2, "normalized_unit_price": 1, "in_stock": true, '
        '"contradictions": ["c1"], "trust_score": 55, "dynamic_attributes": {"port": "Type-C"}, '
        f'"match_score": {match_score}, "unmet_requirements": {unmet}}}\n```'
    )


def fake_audit(page, request):
    """The real auditor with only the Claude CLI subprocess replaced."""
    return agents.audit(page, request, runner=lambda prompt: cli_output())


def make_engine(scraper, auditor=fake_audit):
    return TaskEngine(scraper_factory=lambda: scraper, auditor=auditor, poll_seconds=0.01, fetch_delay=(0, 0))


def create(db, task_id="t1", terms=("pump",), max_duration=3600):
    return db.create_task(task_id, "desc", list(terms), ["aquarium"], max_duration, None, ["19 L", "Type-C"])


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

    def pausing_audit(page, request):
        audited.append(page.url)
        if len(audited) == 3:
            db.transition_status("t1", (db.RUNNING,), db.PAUSED, "Pause requested")
        return fake_audit(page, request)

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

    def stopping_audit(page, request):
        if len(scraper.fetched) == 2:
            db.transition_status("t1", (db.RUNNING,), db.STOPPED, "Stopped by user")
        return fake_audit(page, request)

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
    assert not db.is_visited(URLS[0], "t1")
    assert db.count_products("t1") == 2


def test_repeated_llm_failures_pause_the_task(fresh_db):
    from llm_adapter import LLMError

    db = fresh_db

    def broken(page, request):
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


def test_second_task_audits_independently_against_its_own_request(fresh_db):
    """An audit is only valid for the request it was made for, so tasks never share results."""
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
    assert len(scraper.fetched) == 8
    assert db.count_products("t1") == 4 and db.count_products("t2") == 4
    assert db.get_task("t2")["visited_count"] == db.get_task("t1")["visited_count"] == 4


def test_irrelevant_products_are_stored_but_hidden_and_not_counted(fresh_db):
    db = fresh_db
    scraper = FakeScraper(URLS[:4])
    scores = iter([95, 10, 80, 5])
    seen_prompts = []

    def auditor(page, request):
        score = next(scores)

        def runner(prompt):
            seen_prompts.append(prompt)
            return cli_output(score, '["Type-C"]' if score < 50 else "[]")

        return agents.audit(page, request, runner=runner)

    async def main():
        create(db)
        engine = make_engine(scraper, auditor)
        engine.start("t1")
        await engine.wait("t1")

    asyncio.run(main())
    task = db.get_task("t1")
    assert task["product_count"] == 2 and task["rejected_count"] == 2
    assert [p["match_score"] for p in db.list_products("t1", "-match_score")] == [95, 80]
    everything = db.list_products("t1", "-match_score", include_rejected=True)
    assert [p["matches"] for p in everything] == [True, True, False, False]
    assert everything[-1]["unmet_requirements"] == ["Type-C"]
    assert '"requirements": [\n    "19 L",\n    "Type-C"\n  ]' in seen_prompts[0]


def test_rejected_products_do_not_use_up_the_product_cap(fresh_db, monkeypatch):
    db = fresh_db
    monkeypatch.setattr(config, "MAX_AUDITED_PRODUCTS", 2)
    scores = iter([5, 5, 5, 90, 90, 90, 90, 90])

    def auditor(page, request):
        return agents.audit(page, request, runner=lambda p: cli_output(next(scores)))

    async def main():
        create(db)
        engine = make_engine(FakeScraper(URLS), auditor)
        engine.start("t1")
        await engine.wait("t1")

    asyncio.run(main())
    assert db.count_products("t1") == 2
    assert db.count_products("t1", matching_only=False) == 5


LISTING_LINKS = "".join(f'<a href="/p/pump-{i}">Pump {i}</a>' for i in range(12))
LISTING_HTML = f"<html><body><h1>Water pumps</h1><main>{LISTING_LINKS}</main></body></html>"


def test_listing_pages_are_expanded_one_level_into_their_products(fresh_db):
    db = fresh_db
    category = "https://shop0.com/c/water-pumps"
    html_for = {category: LISTING_HTML}
    scraper = FakeScraper([category], html_for=html_for)

    async def main():
        create(db)
        engine = make_engine(scraper)
        engine.start("t1")
        await engine.wait("t1")

    asyncio.run(main())
    # The category page itself is never audited; its first 8 product links are.
    assert db.count_products("t1") == config.LISTING_LINKS_PER_PAGE
    assert scraper.fetched[0] == category
    assert all("/p/pump-" in u for u in scraper.fetched[1:])


def test_blocked_search_engine_fails_with_a_clear_message(fresh_db):
    db = fresh_db

    class BlockedScraper:
        def search(self, term, negatives):
            raise RateLimitError("captcha")

    async def main():
        create(db)
        engine = make_engine(BlockedScraper())
        engine.start("t1")
        await engine.wait("t1")

    asyncio.run(main())
    task = db.get_task("t1")
    assert task["status"] == db.FAILED and "blocked" in task["message"]


def test_old_database_is_migrated_in_place(tmp_path):
    import sqlite3

    import database

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT NOT NULL, max_duration_seconds INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL, description TEXT NOT NULL DEFAULT '', search_terms_json TEXT NOT NULL DEFAULT '[]',
            negative_keywords_json TEXT NOT NULL DEFAULT '[]', detected_category TEXT, elapsed_seconds REAL NOT NULL DEFAULT 0,
            message TEXT, updated_at TIMESTAMP);
        CREATE TABLE visited_urls (url TEXT PRIMARY KEY, task_id TEXT NOT NULL, visited_at TIMESTAMP NOT NULL, outcome TEXT);
        CREATE TABLE audited_products (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, title TEXT, platform TEXT, price REAL,
            unit_price REAL, trust_score INTEGER, contradictions_json TEXT NOT NULL DEFAULT '[]',
            attributes_json TEXT NOT NULL DEFAULT '{}', product_url TEXT, currency TEXT, shipping_cost REAL,
            units_in_pack INTEGER, in_stock INTEGER, created_at TIMESTAMP);
        INSERT INTO tasks (task_id, status, max_duration_seconds, created_at) VALUES ('old', 'COMPLETED', 60, 'x');
        INSERT INTO visited_urls VALUES ('https://a/p/1', 'old', 'x', 'audited');
        INSERT INTO audited_products (id, task_id, title) VALUES ('p1', 'old', 'Legacy product');
    """)
    conn.commit()
    conn.close()

    database.init_db(path)
    database.init_db(path)  # idempotent
    task = database.get_task("old")
    assert task["requirements"] == [] and task["product_count"] == 1  # legacy rows count as matching
    assert database.is_visited("https://a/p/1", "old")
    assert not database.is_visited("https://a/p/1", "other")
    database.mark_visited("https://a/p/1", "other", "audited")  # now allowed for a second task
    assert database.list_products("old")[0]["title"] == "Legacy product"
