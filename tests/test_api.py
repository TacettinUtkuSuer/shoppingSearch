import time

import pytest
from fastapi.testclient import TestClient

import config


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    import database
    import main

    monkeypatch.setattr(database, "_db_path", tmp_path / "api.db")
    # Keep the worker from touching the network: an engine whose searches return nothing.
    from core.task_engine import TaskEngine

    class NoNetScraper:
        def search(self, term, negatives):
            time.sleep(0.05)
            return []

    monkeypatch.setattr(main, "engine", TaskEngine(scraper_factory=NoNetScraper, poll_seconds=0.01))
    with TestClient(main.app) as c:
        yield c


def test_index_and_static_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Clarify scope" in r.text
    assert client.get("/static/app.js").status_code == 200


def test_resolve_endpoint(client, monkeypatch):
    from core import agents

    monkeypatch.setattr(
        agents, "resolve",
        lambda d, c: agents.ResolverResult(needs_clarification=True, clarification_question="Which one?"),
    )
    r = client.post("/api/resolve", json={"description": "water thingy", "clarifications": []})
    assert r.status_code == 200 and r.json()["clarification_question"] == "Which one?"


def test_resolve_endpoint_reports_cli_errors(client, monkeypatch):
    from core import agents
    from llm_adapter import LLMError

    def boom(d, c):
        raise LLMError("Claude CLI binary not found: claude")

    monkeypatch.setattr(agents, "resolve", boom)
    r = client.post("/api/resolve", json={"description": "pump"})
    assert r.status_code == 502 and "not found" in r.json()["detail"]


def test_task_lifecycle(client):
    r = client.post("/api/tasks", json={"search_terms": ["pump"], "negative_keywords": ["-Aquarium"], "max_duration_seconds": 600})
    assert r.status_code == 201
    task = r.json()
    assert task["negative_keywords"] == ["aquarium"]
    tid = task["task_id"]

    assert client.post(f"/api/tasks/{tid}/pause").status_code == 200
    assert client.get(f"/api/tasks/{tid}").json()["status"] == "PAUSED"
    assert client.post(f"/api/tasks/{tid}/pause").status_code == 409
    assert client.post(f"/api/tasks/{tid}/resume").status_code == 200

    for _ in range(100):
        if client.get(f"/api/tasks/{tid}").json()["status"] == "COMPLETED":
            break
        time.sleep(0.02)
    assert client.get(f"/api/tasks/{tid}").json()["status"] == "COMPLETED"
    assert client.post(f"/api/tasks/{tid}/stop").status_code == 409
    assert client.get(f"/api/tasks/{tid}/products").json() == []
    assert any(t["task_id"] == tid for t in client.get("/api/tasks").json())


def test_validation_and_404(client):
    assert client.post("/api/tasks", json={"search_terms": ["  "]}).status_code == 422
    assert client.get("/api/tasks/nope").status_code == 404
