"""Read-only modes.

AABA_DEMO_MODE=1 (public Streamlit demo): simulated data and broker, anonymized students.
AABA_READ_ONLY=1 (private live dashboard on the server): the REAL database, shown read-only.

In either mode every function that could change something calls guard() first and is
refused: orders, SELL approvals and decisions, config changes, workflow runs, the
scheduler, account checks and reconciliation. The real Alpaca client cannot even be
created, so the dashboard process needs no broker keys. In demo mode the data is a
simulated sample built once by app/demo_seed.py, which is the only code allowed past the
guard, and only on its own thread while it runs.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager

_local = threading.local()
_seed_lock = threading.Lock()
_seeded_once = False


class ReadOnlyDemo(RuntimeError):
    pass


def is_demo() -> bool:
    return os.getenv("AABA_DEMO_MODE", "").strip().lower() in ("1", "true", "yes", "on")


def is_read_only() -> bool:
    """True for the demo and for the private live dashboard (AABA_READ_ONLY=1)."""
    return is_demo() or os.getenv("AABA_READ_ONLY", "").strip().lower() in ("1", "true", "yes", "on")


def seeding_active() -> bool:
    return bool(getattr(_local, "seeding", False))


def guard(action: str) -> None:
    """Refuse `action` in a read-only mode (except while this thread builds the demo sample)."""
    if is_demo() and not seeding_active():
        raise ReadOnlyDemo(f"read-only demo: {action} is disabled")
    if is_read_only() and not is_demo():
        raise ReadOnlyDemo(f"read-only dashboard: {action} is disabled here")


@contextmanager
def seeding():
    """Lets the current thread build the simulated sample data, once per process and only
    into an empty database. Demo mode only."""
    global _seeded_once
    if not is_demo():
        raise ReadOnlyDemo("seeding is only for demo mode")
    with _seed_lock:
        from . import db
        if _seeded_once or db.q1("SELECT run_id FROM runs LIMIT 1"):
            raise ReadOnlyDemo("read-only demo: the sample data is already built")
        _seeded_once = True
    _local.seeding = True
    try:
        yield
    finally:
        _local.seeding = False
