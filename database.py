"""SQLite persistence (stdlib sqlite3, WAL mode, one short-lived connection per call).

Short-lived connections plus WAL keep the API thread and the background worker
from ever holding a write lock for longer than a single statement batch.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import config

PENDING = "PENDING"
RUNNING = "RUNNING"
PAUSED = "PAUSED"
COMPLETED = "COMPLETED"
STOPPED = "STOPPED"
FAILED = "FAILED"
ALL_STATUSES = (PENDING, RUNNING, PAUSED, COMPLETED, STOPPED, FAILED)
TERMINAL_STATUSES = (COMPLETED, STOPPED, FAILED)

_db_path: Path = config.DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('PENDING','RUNNING','PAUSED','COMPLETED','STOPPED','FAILED')),
    max_duration_seconds INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    search_terms_json TEXT NOT NULL DEFAULT '[]',
    negative_keywords_json TEXT NOT NULL DEFAULT '[]',
    detected_category TEXT,
    elapsed_seconds REAL NOT NULL DEFAULT 0,
    message TEXT,
    updated_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS visited_urls (
    url TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    visited_at TIMESTAMP NOT NULL,
    outcome TEXT
);
CREATE TABLE IF NOT EXISTS audited_products (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    title TEXT,
    platform TEXT,
    price REAL,
    unit_price REAL,
    trust_score INTEGER,
    contradictions_json TEXT NOT NULL DEFAULT '[]',
    attributes_json TEXT NOT NULL DEFAULT '{}',
    product_url TEXT,
    currency TEXT,
    shipping_cost REAL,
    units_in_pack INTEGER,
    in_stock INTEGER,
    created_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_products_task ON audited_products (task_id);
CREATE INDEX IF NOT EXISTS idx_products_url ON audited_products (product_url);
CREATE INDEX IF NOT EXISTS idx_visited_task ON visited_urls (task_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        with conn:  # commits on success, rolls back on exception
            yield conn
    finally:
        conn.close()


def init_db(path: Path | str | None = None) -> None:
    global _db_path
    if path is not None:
        _db_path = Path(path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


# --- tasks ----------------------------------------------------------------

def _task_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    task = dict(row)
    task["search_terms"] = json.loads(task.pop("search_terms_json") or "[]")
    task["negative_keywords"] = json.loads(task.pop("negative_keywords_json") or "[]")
    return task


def create_task(
    task_id: str,
    description: str,
    search_terms: list[str],
    negative_keywords: list[str],
    max_duration_seconds: int,
    detected_category: str | None = None,
) -> dict[str, Any]:
    ts = now_iso()
    with connect() as conn:
        conn.execute(
            """INSERT INTO tasks (task_id, status, max_duration_seconds, created_at, description,
                   search_terms_json, negative_keywords_json, detected_category, message, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, PENDING, int(max_duration_seconds), ts, description,
                json.dumps(search_terms), json.dumps(negative_keywords), detected_category,
                "Queued", ts,
            ),
        )
    return get_task(task_id)  # type: ignore[return-value]


def get_task(task_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        task = _task_dict(row)
        if task:
            task["product_count"] = conn.execute(
                "SELECT COUNT(*) FROM audited_products WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
            task["visited_count"] = conn.execute(
                "SELECT COUNT(*) FROM visited_urls WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
        return task


def list_tasks(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT t.*, (SELECT COUNT(*) FROM audited_products p WHERE p.task_id = t.task_id)
                   AS product_count
               FROM tasks t ORDER BY t.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_task_dict(r) for r in rows]  # type: ignore[misc]


def get_status(task_id: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return row["status"] if row else None


def transition_status(
    task_id: str,
    from_statuses: tuple[str, ...] | list[str],
    to_status: str,
    message: str | None = None,
) -> bool:
    """Compare-and-set status change. Returns True when the row was updated."""
    placeholders = ",".join("?" for _ in from_statuses)
    with connect() as conn:
        cur = conn.execute(
            f"""UPDATE tasks SET status = ?, message = COALESCE(?, message), updated_at = ?
                WHERE task_id = ? AND status IN ({placeholders})""",
            (to_status, message, now_iso(), task_id, *from_statuses),
        )
    return cur.rowcount == 1


def update_progress(task_id: str, elapsed_seconds: float | None = None, message: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE tasks SET elapsed_seconds = COALESCE(?, elapsed_seconds),
                   message = COALESCE(?, message), updated_at = ?
               WHERE task_id = ?""",
            (elapsed_seconds, message, now_iso(), task_id),
        )


def recover_orphaned_tasks() -> int:
    """Tasks left RUNNING/PENDING by a crashed or restarted server become PAUSED."""
    with connect() as conn:
        cur = conn.execute(
            """UPDATE tasks SET status = ?, message = 'Server restarted; press Resume to continue',
                   updated_at = ? WHERE status IN (?, ?)""",
            (PAUSED, now_iso(), RUNNING, PENDING),
        )
    return cur.rowcount


# --- visited urls ---------------------------------------------------------

def is_visited(url: str) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM visited_urls WHERE url = ?", (url,)).fetchone() is not None


def visited_by(url: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT task_id FROM visited_urls WHERE url = ?", (url,)).fetchone()
    return row["task_id"] if row else None


def mark_visited(url: str, task_id: str, outcome: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO visited_urls (url, task_id, visited_at, outcome) VALUES (?, ?, ?, ?)",
            (url, task_id, now_iso(), outcome),
        )


# --- audited products -----------------------------------------------------

PRODUCT_COLUMNS = (
    "id", "task_id", "title", "platform", "price", "unit_price", "trust_score",
    "contradictions_json", "attributes_json", "product_url", "currency", "shipping_cost",
    "units_in_pack", "in_stock", "created_at",
)


def save_audit(product: dict[str, Any], visited_url: str) -> None:
    """Persist an audited product and its visited marker in one transaction."""
    row = {col: product.get(col) for col in PRODUCT_COLUMNS}
    row["created_at"] = row["created_at"] or now_iso()
    if isinstance(row["contradictions_json"], (list, tuple)):
        row["contradictions_json"] = json.dumps(row["contradictions_json"])
    if isinstance(row["attributes_json"], dict):
        row["attributes_json"] = json.dumps(row["attributes_json"])
    with connect() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO audited_products ({','.join(PRODUCT_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in PRODUCT_COLUMNS)})",
            tuple(row[c] for c in PRODUCT_COLUMNS),
        )
        conn.execute(
            "INSERT OR IGNORE INTO visited_urls (url, task_id, visited_at, outcome) VALUES (?, ?, ?, ?)",
            (visited_url, product["task_id"], now_iso(), "audited"),
        )


def copy_product(product: dict[str, Any]) -> None:
    """Insert an audited product for another task, leaving visited_urls untouched."""
    row = {col: product.get(col) for col in PRODUCT_COLUMNS}
    row["created_at"] = now_iso()
    with connect() as conn:
        conn.execute(
            f"INSERT OR IGNORE INTO audited_products ({','.join(PRODUCT_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in PRODUCT_COLUMNS)})",
            tuple(row[c] for c in PRODUCT_COLUMNS),
        )


def find_product_by_url(url: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM audited_products WHERE product_url = ? ORDER BY created_at DESC LIMIT 1",
            (url,),
        ).fetchone()
    return dict(row) if row else None


def product_exists(product_id: str) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM audited_products WHERE id = ?", (product_id,)).fetchone() is not None


def count_products(task_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM audited_products WHERE task_id = ?", (task_id,)
        ).fetchone()[0]


_SORTS = {
    "unit_price": "unit_price IS NULL, unit_price ASC",
    "-unit_price": "unit_price IS NULL, unit_price DESC",
    "trust_score": "trust_score IS NULL, trust_score ASC",
    "-trust_score": "trust_score IS NULL, trust_score DESC",
    "price": "price IS NULL, price ASC",
    "-price": "price IS NULL, price DESC",
    "-created_at": "created_at DESC",
}


def list_products(task_id: str, sort: str = "unit_price") -> list[dict[str, Any]]:
    order = _SORTS.get(sort, _SORTS["unit_price"])
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM audited_products WHERE task_id = ? ORDER BY {order}", (task_id,)
        ).fetchall()
    products = []
    for r in rows:
        p = dict(r)
        p["contradictions"] = json.loads(p.pop("contradictions_json") or "[]")
        p["attributes"] = json.loads(p.pop("attributes_json") or "{}")
        p["in_stock"] = None if p["in_stock"] is None else bool(p["in_stock"])
        products.append(p)
    return products
