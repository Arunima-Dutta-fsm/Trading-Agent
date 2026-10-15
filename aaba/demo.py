"""Read-only demo mode (AABA_DEMO_MODE=1), used for the public Streamlit demo.

In demo mode every function that could change something calls guard() first and is
refused: orders, SELL approvals and decisions, config changes, workflow runs, the
scheduler, account checks and reconciliation. The real Alpaca client cannot even be
created. The data is a simulated sample built once by app/demo_seed.py, which is the
only code allowed past the guard, and only on its own thread while it runs.
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


def seeding_active() -> bool:
    return bool(getattr(_local, "seeding", False))


def guard(action: str) -> None:
    """Refuse `action` in demo mode (except while this thread builds the sample data)."""
    if is_demo() and not seeding_active():
        raise ReadOnlyDemo(f"read-only demo: {action} is disabled")


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
