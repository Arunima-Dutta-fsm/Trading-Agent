"""Daily scheduler (APScheduler, New York time).

Jobs:
- The main workflow at each time in strategy.json -> schedule.run_times (default 09:45 New
  York, Monday-Friday). Before running it asks Alpaca's calendar whether today is a
  trading day; holidays are skipped.
- Every N minutes during market hours: send SELLs that students have approved since
  the last run (re-checked first), and refresh order statuses.

max_instances=1 and the run lock stop overlapping runs. Times are New York time, so
the US daylight-saving change (1 Nov 2026) is handled automatically. In India,
09:45 New York is 19:15 IST until 31 Oct and 20:15 IST from 2 Nov.
"""
from __future__ import annotations

import logging

from . import controller, db, demo, paths
from .market_data import get_provider
from .runlock import RunLocked
from .settings import load_strategy, load_students

log = logging.getLogger("aaba.scheduler")


def _is_trading_day() -> tuple[bool, str]:
    p = get_provider()
    c = p.market_clock()
    today = c.timestamp.date()
    sess = [s for s in p.sessions(today, today) if s.date == today]
    return bool(sess), f"{today}: {'trading day' if sess else 'market holiday/weekend'}"


def daily_job():
    demo.guard("starting scheduled runs")
    from agents.trading_crew import run_workflow
    cfg = load_strategy()
    try:
        ok, why = _is_trading_day()
    except Exception as e:  # noqa: BLE001
        db.log_event("ERROR", "scheduler", f"calendar check failed: {type(e).__name__}: {e}")
        return
    if not ok:
        db.log_event("INFO", "scheduler", f"skipped: {why}")
        return
    try:
        res = run_workflow(cfg.schedule.mode, "scheduler")
        log.info("run %s finished: %s", res["run_id"], res["status"])
    except RunLocked as e:
        db.log_event("WARNING", "scheduler", f"skipped: {e}")
    except Exception as e:  # noqa: BLE001
        db.log_event("ERROR", "scheduler", f"run failed: {type(e).__name__}: {e}")


def approved_sell_job():
    demo.guard("scheduled SELL execution")
    try:
        if not get_provider().market_clock().is_open:
            return
    except Exception:  # noqa: BLE001
        return
    for st in load_students().enabled:
        try:
            if db.student_approvals(st.student_id, ("APPROVED",)):
                controller.process_approved_sells(st.student_id)
            controller.reconcile_student(st.student_id)
        except Exception as e:  # noqa: BLE001
            db.log_event("ERROR", "scheduler", f"approved-sell check failed: {type(e).__name__}: {e}",
                         student_id=st.student_id)


def start():
    demo.guard("starting the scheduler")
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    paths.ensure_dirs()
    db.init_db()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.FileHandler(paths.LOG_DIR / "scheduler.log"), logging.StreamHandler()])
    cfg = load_strategy()
    tz = cfg.schedule.timezone
    sched = BlockingScheduler(timezone=tz)
    for t in cfg.schedule.run_times:
        hh, mm = t.split(":")
        sched.add_job(daily_job, CronTrigger(day_of_week="mon-fri", hour=int(hh), minute=int(mm), timezone=tz),
                      id=f"daily-{t}", max_instances=1, coalesce=True, misfire_grace_time=900)
    sched.add_job(approved_sell_job, IntervalTrigger(minutes=cfg.schedule.approved_sell_check_minutes, timezone=tz),
                  id="approved-sells", max_instances=1, coalesce=True)
    db.log_event("INFO", "scheduler", f"started: runs at {cfg.schedule.run_times} {tz}, mode={cfg.schedule.mode}")
    print(f"Scheduler started. Workflow at {', '.join(cfg.schedule.run_times)} ({tz}), Mon-Fri, "
          f"mode '{cfg.schedule.mode}'. Approved SELL check every {cfg.schedule.approved_sell_check_minutes} min. "
          f"Press Ctrl+C to stop.")
    print("  next workflow runs: " + "; ".join(next_runs()) + " (holidays are skipped on the day)")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("Scheduler stopped.")


def next_runs() -> list[str]:
    """For the dashboard: the next few scheduled times (computed, not read from a running scheduler)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    cfg = load_strategy()
    tz = ZoneInfo(cfg.schedule.timezone)
    now = datetime.now(tz)
    out = []
    d = now.date()
    while len(out) < 3 and d < now.date() + timedelta(days=10):
        if d.weekday() < 5:
            for t in sorted(cfg.schedule.run_times):
                hh, mm = map(int, t.split(":"))
                dt = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
                if dt > now:
                    out.append(dt.strftime("%a %d %b %H:%M %Z"))
        d += timedelta(days=1)
    return out[:3]
