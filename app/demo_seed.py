"""Builds the simulated sample data for the read-only demo (once per process).

It runs the normal controller code (rules engine, risk checks, fake broker) on simulated
prices for one fixed day, inside demo.seeding(), so the dashboard has something to show.
No LLM, no network, no real account. After this, the demo guard refuses every change.
"""
from __future__ import annotations

import json
import threading

DEMO_DAY = "2026-09-30T14:30:00+00:00"     # a simulated day with several BUY setups
NOTE = "Read-only demo: simulated prices and a simulated broker, built by the rules engine without the LLM."

_lock = threading.Lock()
_done = False


def ensure_seeded() -> None:
    global _done
    with _lock:
        if _done:
            return
        from aaba import paths
        marker = paths.DATA_DIR / "demo_seeded.ok"
        if not marker.exists():
            build()
            marker.write_text("ok", encoding="utf-8")
        _done = True


def build() -> str:
    from aaba import controller, db, paths, reports
    from aaba.demo import seeding
    from aaba.settings import load_students

    paths.ensure_dirs()
    db.init_db()
    students = load_students().enabled
    with seeding():
        run_id = db.create_run("paper", "demo-seed", "rules-only (demo sample)", None, None, True)
        controller.prepare_market_data(run_id)
        for st in students:
            controller.evaluate_student(run_id, st.student_id)
            for sig in controller.unhandled_signals(run_id):
                if sig["student_id"] != st.student_id:
                    continue
                if sig["action"] == "BUY":
                    controller.submit_buy(sig["signal_id"], "demo sample: rules engine BUY", "code")
                elif sig["action"] == "SELL":
                    controller.create_sell_approval(sig["signal_id"], "demo sample: rules engine SELL", "code")
            controller.reconcile_student(st.student_id, run_id)
        files = [reports.student_report(run_id, st.student_id, None, NOTE) for st in students]
        files.append(reports.group_report(run_id, None, [], NOTE))
        db.finish_run(run_id, "DONE", notes=json.dumps({"reports": [f.name for f in files], "demo": True}))
    return run_id
