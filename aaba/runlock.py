"""Stops two workflow runs from overlapping (scheduler + 'Run now' at the same time)."""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager

from . import paths

STALE_AFTER_SECONDS = 45 * 60


class RunLocked(RuntimeError):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def current_holder() -> dict | None:
    try:
        return json.loads(paths.LOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@contextmanager
def run_lock(label: str):
    paths.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    holder = current_holder()
    if holder:
        age = time.time() - holder.get("t", 0)
        if _pid_alive(int(holder.get("pid", -1))) and age < STALE_AFTER_SECONDS:
            raise RunLocked(f"another run is in progress ({holder.get('label')}, pid {holder.get('pid')})")
        paths.LOCK_FILE.unlink(missing_ok=True)   # stale lock from a crashed run
    try:
        fd = os.open(paths.LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RunLocked("another run started at the same moment")
    with os.fdopen(fd, "w") as f:
        json.dump({"pid": os.getpid(), "t": time.time(), "label": label}, f)
    try:
        yield
    finally:
        paths.LOCK_FILE.unlink(missing_ok=True)
