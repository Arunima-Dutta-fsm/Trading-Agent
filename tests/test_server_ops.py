"""Always-on server behaviour: missed-run catch-up after a restart, failure alerts,
data-freshness warnings and the read-only live dashboard."""
import json
from datetime import datetime, timezone

import pytest
from conftest import SECRETS, add_signal, make_run

from aaba import approvals, clock, controller, db, monitoring, scheduler, settings
from aaba import broker as brokers
from aaba.demo import ReadOnlyDemo
from aaba.runlock import run_lock
from aaba.settings import load_students

# Fri 16 Oct 2026; New York is UTC-4 (EDT)
NY_1030 = "2026-10-16T14:30:00+00:00"
NY_0900 = "2026-10-16T13:00:00+00:00"
NY_1545 = "2026-10-16T19:45:00+00:00"
SATURDAY = "2026-10-17T14:30:00+00:00"


@pytest.fixture
def at(monkeypatch):
    def set_now(iso):
        monkeypatch.setenv("AABA_SYNTHETIC_NOW", iso)
        from aaba import market_data
        market_data.set_provider(None)
    return set_now


@pytest.fixture
def ran(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "daily_job", lambda trigger="scheduler": calls.append(trigger))
    return calls


def _run_today(status, trigger="scheduler", started="2026-10-16T13:46:00+00:00"):
    db.ex("INSERT INTO runs(run_id, started_at, mode, trigger, status, simulated) VALUES (?,?,?,?,?,1)",
          (db.new_id("run"), started, settings.load_strategy().schedule.mode, trigger, status))


# ------------------------------------------------------------------ catch-up after a restart
def test_missed_run_is_caught_up_on_a_trading_day(at, ran):
    at(NY_1030)
    assert scheduler.catch_up_if_missed() == "ran" and ran == ["catch-up"]


@pytest.mark.parametrize("now,why", [(NY_0900, "before the"), (NY_1545, "too close"), (SATURDAY, "not a trading day")])
def test_no_catch_up_outside_the_window(at, ran, now, why):
    at(now)
    assert why in scheduler.catch_up_if_missed() and ran == []


def test_no_catch_up_when_today_already_ran(at, ran):
    at(NY_1030)
    _run_today("DONE")
    assert "already happened" in scheduler.catch_up_if_missed() and ran == []


def test_crashed_run_is_closed_and_caught_up(at, ran):
    at(NY_1030)
    _run_today("RUNNING")                       # left behind by a crash; no live lock holder
    assert scheduler.catch_up_if_missed() == "ran"
    assert db.q1("SELECT status FROM runs")["status"] == "ABANDONED"


def test_no_catch_up_while_a_run_holds_the_lock(at, ran):
    at(NY_1030)
    with run_lock("live run"):
        assert "in progress" in scheduler.catch_up_if_missed() and ran == []


def test_catch_up_attempts_are_limited_per_day(at, ran):
    at(NY_1030)
    _run_today("FAILED", "catch-up")
    _run_today("FAILED", "catch-up")
    assert "already tried" in scheduler.catch_up_if_missed() and ran == []


# ------------------------------------------------------------------ alerts
@pytest.fixture
def pings(monkeypatch):
    sent = []

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=10):
        sent.append((req.full_url, req.data.decode()))
        return Resp()
    monkeypatch.setattr(monitoring.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("HEALTHCHECK_DAILY_URL", "https://hc-ping.com/daily-uuid")
    monkeypatch.setenv("HEALTHCHECK_HEARTBEAT_URL", "https://hc-ping.com/beat-uuid")
    return sent


def test_no_alert_urls_means_no_network(monkeypatch):
    monkeypatch.delenv("HEALTHCHECK_DAILY_URL", raising=False)
    monkeypatch.setattr(monitoring.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network used"))
    assert monitoring.ping("HEALTHCHECK_DAILY_URL") is False


def test_daily_job_pings_start_and_success(at, pings, monkeypatch):
    import agents.trading_crew as tc
    at(NY_1030)
    monkeypatch.setattr(tc, "run_workflow", lambda mode, trig: {"run_id": "r1", "mode": mode, "status": "DONE"})
    scheduler.daily_job()
    assert [u for u, _ in pings] == ["https://hc-ping.com/daily-uuid/start", "https://hc-ping.com/daily-uuid"]


def test_daily_job_reports_failure(at, pings, monkeypatch):
    import agents.trading_crew as tc
    at(NY_1030)

    def boom(mode, trig):
        raise RuntimeError("broker down")
    monkeypatch.setattr(tc, "run_workflow", boom)
    scheduler.daily_job()
    assert pings[-1][0] == "https://hc-ping.com/daily-uuid/fail" and "RuntimeError" in pings[-1][1]


def test_holiday_counts_as_success(at, pings):
    at(SATURDAY)
    scheduler.daily_job()
    assert pings[-1][0] == "https://hc-ping.com/daily-uuid" and "holiday/weekend" in pings[-1][1]


def test_heartbeat_even_when_market_closed(at, pings):
    at(SATURDAY)
    scheduler.approved_sell_job()
    assert pings == [("https://hc-ping.com/beat-uuid", "")]


def test_alert_failure_never_breaks_the_job(at, monkeypatch):
    def down(*a, **k):
        raise OSError("no route")
    monkeypatch.setattr(monitoring.urllib.request, "urlopen", down)
    monkeypatch.setenv("HEALTHCHECK_HEARTBEAT_URL", "https://hc-ping.com/beat-uuid")
    at(SATURDAY)
    scheduler.approved_sell_job()                 # must not raise


# ------------------------------------------------------------------ freshness
def _now(iso):
    return datetime.fromisoformat(iso).astimezone(timezone.utc)


def test_freshness_warns_when_no_run_today():
    from app import dashboard_data as dd
    f = dd.freshness(_now(NY_1030))
    assert not f["ran_today"] and any("No workflow run" in w for w in f["warnings"])
    assert any("stale" in w for w in f["warnings"])


def test_freshness_is_quiet_on_weekends():
    from app import dashboard_data as dd
    assert dd.freshness(_now(SATURDAY))["warnings"] == []


def test_freshness_ok_with_recent_run_and_snapshot():
    from app import dashboard_data as dd
    _run_today("DONE", started="2026-10-16T13:46:00+00:00")
    db.ex("INSERT INTO snapshots(at, student_id, equity, cash) VALUES ('2026-10-16T14:20:00+00:00','S1',1,1)")
    f = dd.freshness(_now(NY_1030))
    assert f["warnings"] == [] and f["snapshot_age_min"] == 10


def test_freshness_flags_failed_run():
    from app import dashboard_data as dd
    _run_today("FAILED")
    assert any("FAILED" in w for w in dd.freshness(_now(SATURDAY))["warnings"])


# ------------------------------------------------------------------ read-only live dashboard
def test_read_only_mode_refuses_changes_but_keeps_real_names(monkeypatch):
    run = make_run("paper", prices={"AMD": 150.0})
    buy = add_signal(run, "S2", "AMD", "BUY", 150.0)
    monkeypatch.setenv("AABA_READ_ONLY", "1")
    for fn in (lambda: controller.submit_buy(buy["signal_id"]), lambda: controller.reconcile_student("S2"),
               lambda: brokers.verify_accounts(), lambda: settings.save_strategy(settings.load_strategy().model_dump()),
               lambda: scheduler.daily_job(), lambda: brokers.AlpacaPaperBroker(load_students().get("S2"))):
        with pytest.raises(ReadOnlyDemo, match="read-only dashboard"):
            fn()
    with pytest.raises(approvals.ApprovalError, match="read-only dashboard"):
        approvals.decide("apv-x", "S2", "anything", "approve")
    assert settings.paper_orders_enabled() is False
    st = load_students().get("S2")
    assert st.name == "Arunima Dutta" and st.alpaca_keys() == ("", "") and st.approval_pin() == ""
    assert db.q1("SELECT outcome FROM signals WHERE signal_id=?", (buy["signal_id"],))["outcome"] is None


@pytest.mark.parametrize("page", ["overview", "student-S2", "approvals", "config", "history", "system"])
def test_read_only_dashboard_has_no_action_controls(monkeypatch, page):
    from streamlit.testing.v1 import AppTest
    from conftest import ROOT
    monkeypatch.setenv("AABA_READ_ONLY", "1")
    monkeypatch.setenv("AABA_DASHBOARD_PAGE", page)
    at_ = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=60).run()
    assert not at_.exception, [e.value for e in at_.exception]
    assert [b.label for b in at_.button if b.label != "Download CSV"] == []
    assert [t.label for t in at_.text_input if t.label != "Search text"] == []
    captions = " ".join(c.value for c in at_.caption)
    assert "refreshes every 60 s" in captions and "READ-ONLY" in captions
    shown = json.dumps([m.value for m in at_.markdown] + [c.value for c in at_.caption])
    assert not any(s in shown for s in SECRETS)
