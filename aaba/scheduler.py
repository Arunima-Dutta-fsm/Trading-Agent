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
from datetime import timedelta

from . import clock, controller, db, demo, monitoring, paths
from .market_data import get_provider
from .runlock import RunLocked
from .settings import load_strategy, load_students

log = logging.getLogger("aaba.scheduler")


DAILY = "HEALTHCHECK_DAILY_URL"
HEARTBEAT = "HEALTHCHECK_HEARTBEAT_URL"
CATCH_UP_STOP_BEFORE_CLOSE_MIN = 30     # no catch-up run in the last 30 minutes of a session
MAX_CATCH_UPS_PER_DAY = 2               # a crash loop cannot keep re-running the workflow


def _today_session():
    """(now in New York, today's session or None) from the market calendar (Alpaca in real use)."""
    p = get_provider()
    now_ny = p.market_clock().timestamp
    today = now_ny.date()
    sess = [s for s in p.sessions(today, today) if s.date == today]
    return now_ny, (sess[0] if sess else None)


def _is_trading_day() -> tuple[bool, str]:
    now_ny, sess = _today_session()
    return bool(sess), f"{now_ny.date()}: {'trading day' if sess else 'market holiday/weekend'}"


def daily_job(trigger: str = "scheduler"):
    demo.guard("starting scheduled runs")
    from agents.trading_crew import run_workflow
    cfg = load_strategy()
    monitoring.ping(DAILY, "start")
    try:
        ok, why = _is_trading_day()
    except Exception as e:  # noqa: BLE001
        db.log_event("ERROR", "scheduler", f"calendar check failed: {type(e).__name__}: {e}")
        monitoring.ping(DAILY, "fail", f"calendar check failed: {type(e).__name__}")
        return
    if not ok:
        db.log_event("INFO", "scheduler", f"skipped: {why}")
        monitoring.ping(DAILY, "", f"skipped: {why}")          # a holiday is a normal, successful day
        return
    try:
        res = run_workflow(cfg.schedule.mode, trigger)
        log.info("run %s finished: %s", res["run_id"], res["status"])
        summary = f"{res['run_id']} {res['mode']} {res['status']} agent_error={res.get('agent_error')}"
        monitoring.ping(DAILY, "" if res["status"] == "DONE" else "fail", summary)
    except RunLocked as e:
        db.log_event("WARNING", "scheduler", f"skipped: {e}")
        monitoring.ping(DAILY, "fail", f"skipped, another run holds the lock: {e}")
    except Exception as e:  # noqa: BLE001
        db.log_event("ERROR", "scheduler", f"run failed: {type(e).__name__}: {e}")
        monitoring.ping(DAILY, "fail", f"run failed: {type(e).__name__}")


def intraday_job():
    """One intraday decision cycle (cfg.intraday): fresh candles, fresh decisions, BUYs that pass
    every check, SELL approval requests. Runs only inside the configured window on an open market."""
    demo.guard("starting intraday cycles")
    from datetime import time as dtime
    cfg = load_strategy()
    ic = cfg.intraday
    if not ic.enabled:
        return "intraday disabled"
    try:
        now_ny, sess = _today_session()
        is_open = get_provider().market_clock().is_open
    except Exception as e:  # noqa: BLE001
        db.log_event("ERROR", "scheduler", f"intraday cycle: calendar check failed: {type(e).__name__}: {e}")
        return "calendar check failed"
    first, last = (dtime(*map(int, t.split(":"))) for t in (ic.first_cycle, ic.last_cycle))
    if not sess or not is_open:
        return "market closed"
    if not first <= now_ny.time() <= last or now_ny >= sess.close - timedelta(minutes=10):
        return "outside the intraday window"
    from agents.trading_crew import run_workflow
    try:
        res = run_workflow(cfg.schedule.mode, "intraday")
        log.info("intraday cycle %s: %s", res["run_id"], res["status"])
        if res["status"] != "DONE":
            monitoring.ping(DAILY, "fail", f"intraday cycle {res['run_id']} {res['status']}")
        return res["status"]
    except RunLocked:
        db.log_event("INFO", "scheduler", "intraday cycle skipped: another run is in progress")
        return "skipped: locked"
    except Exception as e:  # noqa: BLE001
        db.log_event("ERROR", "scheduler", f"intraday cycle failed: {type(e).__name__}: {e}")
        monitoring.ping(DAILY, "fail", f"intraday cycle failed: {type(e).__name__}")
        return "failed"


def notify_job() -> dict:
    """Every minute, day and night: expire overdue SELL requests, then send queued emails."""
    demo.guard("sending notifications")
    from . import approvals, notify
    try:
        approvals.expire_stale()
        return notify.flush()
    except Exception as e:  # noqa: BLE001
        db.log_event("ERROR", "notify", f"notification job failed: {type(e).__name__}: {e}")
        return {"error": type(e).__name__}


def catch_up_if_missed() -> str:
    """After a restart: if today's scheduled run was missed (server down, crash), run it now,
    but only on a trading day, after the scheduled time and well before today's close.
    Returns what it decided (also logged)."""
    demo.guard("starting scheduled runs")
    from datetime import datetime, timedelta
    cfg = load_strategy()
    try:
        now_ny, sess = _today_session()
    except Exception as e:  # noqa: BLE001
        return _log_catch_up(f"no catch-up: calendar check failed ({type(e).__name__})")
    if not sess:
        return _log_catch_up(f"no catch-up: {now_ny.date()} is not a trading day")
    first = min(cfg.schedule.run_times)
    due = datetime(now_ny.year, now_ny.month, now_ny.day, *map(int, first.split(":")), tzinfo=now_ny.tzinfo)
    latest = sess.close - timedelta(minutes=CATCH_UP_STOP_BEFORE_CLOSE_MIN)
    if now_ny < due:
        return _log_catch_up(f"no catch-up: before the {first} run")
    if now_ny > latest:
        return _log_catch_up(f"no catch-up: too close to today's close ({sess.close:%H:%M} New York)")
    from .runlock import run_lock
    try:
        with run_lock("catch-up check"):
            for old in db.close_abandoned_runs():      # a run left RUNNING by the crash is not "done"
                db.log_event("WARNING", "recovery", f"run {old} was left unfinished; marked ABANDONED", old)
    except RunLocked:
        return _log_catch_up("no catch-up: a run is in progress")
    start = now_ny.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(clock.UTC)
    today = db.q("SELECT trigger, status FROM runs WHERE started_at>=? AND mode=? AND trigger IN "
                 "('scheduler','catch-up')", (clock.iso(start), cfg.schedule.mode))
    if any(r["status"].startswith("DONE") or r["status"] == "RUNNING" for r in today):
        return _log_catch_up("no catch-up: today's scheduled run already happened")
    if sum(r["trigger"] == "catch-up" for r in today) >= MAX_CATCH_UPS_PER_DAY:
        return _log_catch_up(f"no catch-up: already tried {MAX_CATCH_UPS_PER_DAY} times today")
    _log_catch_up(f"catch-up: today's {first} run was missed; running now")
    daily_job("catch-up")
    return "ran"


def _log_catch_up(msg: str) -> str:
    db.log_event("WARNING" if msg.startswith("catch-up") else "INFO", "scheduler", msg)
    log.info(msg)
    return msg


def approved_sell_job():
    demo.guard("scheduled SELL execution")
    monitoring.ping(HEARTBEAT)          # the scheduler process is alive (every 15 min, day and night)
    try:
        if not get_provider().market_clock().is_open:
            return
    except Exception:  # noqa: BLE001
        return
    from .runlock import run_lock
    try:
        with run_lock("approved-sell check"):      # never alongside a workflow run; try again next cycle
            for st in load_students().enabled:
                try:
                    if db.student_approvals(st.student_id, ("APPROVED",)):
                        controller.process_approved_sells(st.student_id)
                    controller.reconcile_student(st.student_id)
                except Exception as e:  # noqa: BLE001
                    db.log_event("ERROR", "scheduler", f"approved-sell check failed: {type(e).__name__}: {e}",
                                 student_id=st.student_id)
    except RunLocked:
        db.log_event("INFO", "scheduler", "approved-sell check skipped: a workflow run is in progress")


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
    sched.add_job(notify_job, IntervalTrigger(minutes=1, timezone=tz), id="notify", max_instances=1, coalesce=True)
    if cfg.intraday.enabled:
        sched.add_job(intraday_job, CronTrigger(day_of_week="mon-fri", hour="9-15",
                                                minute=f"*/{cfg.intraday.cycle_minutes}", timezone=tz),
                      id="intraday", max_instances=1, coalesce=True, misfire_grace_time=120)
    # after a restart, run today's workflow if it was missed (checked once, 30 s after start)
    from apscheduler.triggers.date import DateTrigger
    from datetime import datetime
    sched.add_job(catch_up_if_missed, DateTrigger(run_date=datetime.now(sched.timezone) + timedelta(seconds=30)),
                  id="catch-up", max_instances=1)
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
