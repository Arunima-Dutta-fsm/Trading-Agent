"""SQLite storage for runs, signals, orders, approvals, events and snapshots.

Every student-level table has a student_id column, and every read helper that
returns student data requires a student_id, so one student's records cannot be
mixed into another's view. Data survives restarts because it lives in a file
(data/aaba.db by default).
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from . import clock, paths
from .settings import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    engine TEXT,
    llm_provider TEXT,
    llm_model TEXT,
    data_source TEXT,
    data_asof TEXT,
    simulated INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS market_snapshots (
    run_id TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    source TEXT NOT NULL,
    feed TEXT,
    latest_bar_date TEXT,
    expected_bar_date TEXT,
    fresh INTEGER NOT NULL,
    symbols_ok INTEGER,
    symbols_failed TEXT,
    batch_calls INTEGER,
    csv_path TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    category TEXT NOT NULL,
    action TEXT NOT NULL,
    score REAL,
    reasons TEXT,
    indicators TEXT,
    price REAL,
    bar_date TEXT,
    data_quality TEXT,
    news TEXT,
    held_qty REAL,
    created_at TEXT NOT NULL,
    outcome TEXT,
    outcome_detail TEXT,
    outcome_by TEXT,
    outcome_at TEXT,
    UNIQUE(run_id, student_id, symbol)
);
CREATE TABLE IF NOT EXISTS orders (
    order_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT UNIQUE NOT NULL,
    student_id TEXT NOT NULL,
    account_number TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    est_price REAL,
    notional REAL,
    signal_id TEXT,
    approval_id TEXT,
    run_id TEXT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    broker_order_id TEXT,
    filled_qty REAL DEFAULT 0,
    filled_avg_price REAL,
    avg_entry_at_submit REAL,
    realized_pnl REAL,
    submitted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    account_number TEXT,
    symbol TEXT NOT NULL,
    qty REAL NOT NULL,
    signal_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    signal_price REAL,
    signal_time TEXT NOT NULL,
    reason TEXT,
    evidence TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT,
    client_order_id TEXT,
    execution_detail TEXT,
    fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    approval_id TEXT,
    claimed_student_id TEXT,
    target_student_id TEXT,
    success INTEGER NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    run_id TEXT,
    student_id TEXT,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    run_id TEXT,
    student_id TEXT NOT NULL,
    account_number TEXT,
    equity REAL,
    cash REAL,
    long_value REAL,
    unrealized_pnl REAL,
    realized_pnl REAL,
    positions TEXT,
    spy_price REAL,
    simulated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS accounts (
    student_id TEXT PRIMARY KEY,
    account_number TEXT,
    verified_at TEXT,
    paper_verified INTEGER NOT NULL DEFAULT 0,
    status TEXT,
    detail TEXT,
    baseline_equity REAL,
    baseline_at TEXT,
    baseline_spy REAL
);
CREATE INDEX IF NOT EXISTS ix_signals_student ON signals(student_id, created_at);
CREATE INDEX IF NOT EXISTS ix_orders_student ON orders(student_id, submitted_at);
CREATE INDEX IF NOT EXISTS ix_approvals_student ON approvals(student_id, status);
CREATE INDEX IF NOT EXISTS ix_events_at ON events(at);
CREATE INDEX IF NOT EXISTS ix_snapshots_student ON snapshots(student_id, at);
"""


def _share_with_group(p) -> None:
    """On the server the scheduler (user aaba) and the dashboard (user aabaweb) share the database
    through the 'aabadata' group. SQLite creates files as 0644 and gives its -wal/-shm files the
    database file's mode, so the database must be group-writable or a restart can leave the
    trading process unable to write ('attempt to write a readonly database')."""
    if os.name != "posix":
        return
    for f in (p, p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm")):
        try:
            st = f.stat()
            if st.st_uid == os.getuid() and not st.st_mode & 0o020:
                os.chmod(f, st.st_mode | 0o060)
        except OSError:
            pass


def connect() -> sqlite3.Connection:
    p = paths.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30, isolation_level=None)  # autocommit; explicit BEGIN for multi-step
    _share_with_group(p)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db() -> None:
    with connect() as con:
        con.executescript(SCHEMA)


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """A short write transaction (BEGIN IMMEDIATE takes the write lock up front)."""
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def q(sql: str, params: tuple | list = ()) -> list[dict]:
    con = connect()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def q1(sql: str, params: tuple | list = ()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def ex(sql: str, params: tuple | list = ()) -> int:
    con = connect()
    try:
        cur = con.execute(sql, params)
        return cur.rowcount
    finally:
        con.close()


def j(obj: Any) -> str:
    return redact(json.dumps(obj, default=str))


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- events
def log_event(level: str, kind: str, message: str, run_id: str | None = None,
              student_id: str | None = None, data: Any = None) -> None:
    ex("INSERT INTO events(at, run_id, student_id, level, kind, message, data) VALUES (?,?,?,?,?,?,?)",
       (clock.iso(), run_id, student_id, level.upper(), kind, redact(message)[:4000],
        j(data) if data is not None else None))


# --------------------------------------------------------------------------- runs
def create_run(mode: str, trigger: str, engine: str, llm_provider: str | None,
               llm_model: str | None, simulated: bool) -> str:
    run_id = "run-" + clock.now_utc().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    ex("INSERT INTO runs(run_id, started_at, mode, trigger, status, engine, llm_provider, llm_model, simulated)"
       " VALUES (?,?,?,?,?,?,?,?,?)",
       (run_id, clock.iso(), mode, trigger, "RUNNING", engine, llm_provider, llm_model, int(simulated)))
    return run_id


def finish_run(run_id: str, status: str, error: str | None = None, notes: str | None = None) -> None:
    ex("UPDATE runs SET finished_at=?, status=?, error=?, notes=COALESCE(?, notes) WHERE run_id=?",
       (clock.iso(), status, redact(error) if error else None, notes, run_id))


def close_abandoned_runs() -> list[str]:
    """Mark runs left RUNNING by a crashed process as ABANDONED, so their signals can no
    longer be acted on. Call only while holding the run lock (no other run is active)."""
    rows = q("SELECT run_id FROM runs WHERE status='RUNNING'")
    for r in rows:
        ex("UPDATE runs SET status='ABANDONED', finished_at=?, error=COALESCE(error, ?) "
           "WHERE run_id=? AND status='RUNNING'", (clock.iso(), "process stopped before the run finished",
                                                  r["run_id"]))
    return [r["run_id"] for r in rows]


def get_run(run_id: str) -> dict | None:
    return q1("SELECT * FROM runs WHERE run_id=?", (run_id,))


# --------------------------------------------------------------------------- student-scoped reads
def student_signals(student_id: str, limit: int = 500, run_id: str | None = None) -> list[dict]:
    if run_id:
        return q("SELECT * FROM signals WHERE student_id=? AND run_id=? ORDER BY symbol", (student_id, run_id))
    return q("SELECT * FROM signals WHERE student_id=? ORDER BY created_at DESC LIMIT ?", (student_id, limit))


def student_orders(student_id: str, limit: int = 500) -> list[dict]:
    return q("SELECT * FROM orders WHERE student_id=? ORDER BY submitted_at DESC LIMIT ?", (student_id, limit))


def student_approvals(student_id: str, statuses: tuple[str, ...] | None = None) -> list[dict]:
    if statuses:
        marks = ",".join("?" * len(statuses))
        return q(f"SELECT * FROM approvals WHERE student_id=? AND status IN ({marks}) ORDER BY created_at DESC",
                 (student_id, *statuses))
    return q("SELECT * FROM approvals WHERE student_id=? ORDER BY created_at DESC", (student_id,))


def student_snapshots(student_id: str) -> list[dict]:
    return q("SELECT * FROM snapshots WHERE student_id=? ORDER BY at", (student_id,))


def latest_snapshot(student_id: str) -> dict | None:
    return q1("SELECT * FROM snapshots WHERE student_id=? ORDER BY at DESC, id DESC LIMIT 1", (student_id,))


def account_row(student_id: str) -> dict | None:
    return q1("SELECT * FROM accounts WHERE student_id=?", (student_id,))
