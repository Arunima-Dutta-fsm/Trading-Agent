"""Gmail notifications for SELL approvals (outbox) and PIN approvals on the private dashboard."""
import pytest
from conftest import ROOT, add_signal, give_position, make_run

from aaba import approvals, controller, db, notify
from aaba import broker as brokers
from aaba.settings import load_students

PIN = {"S1": "481516", "S2": "234223", "S3": "908172"}


def pending_sell(sid="S2", sym="AMD", qty=20, price=150.0):
    run = make_run("paper", prices={sym: price})
    give_position(sid, sym, qty, 140.0, price)
    sig = add_signal(run, sid, sym, "SELL", price)
    return controller.create_sell_approval(sig["signal_id"], "exit rule")["approval_id"]


def notes(kind=None):
    rows = db.q("SELECT * FROM notifications ORDER BY id")
    return [r for r in rows if kind is None or r["kind"] == kind]


# ------------------------------------------------------------------ PIN hashes
def test_pin_hash_round_trip_and_format():
    h = approvals.hash_pin("834927", iterations=1000)
    assert h.startswith("pbkdf2_sha256:1000:") and "$" not in h and "834927" not in h
    assert approvals.check_pin_hash("834927", h) and not approvals.check_pin_hash("834928", h)
    assert not approvals.check_pin_hash("834927", "garbage") and not approvals.check_pin_hash("", h)
    assert approvals.hash_pin("834927", 1000) != approvals.hash_pin("834927", 1000)       # salted


# ------------------------------------------------------------------ the outbox: every step queues an email
def test_created_request_queues_an_email_with_the_details():
    aid = pending_sell()
    (n,) = notes("created")
    assert n["approval_id"] == aid and n["student_id"] == "S2" and n["sent_at"] is None
    assert "SELL 20 AMD" in n["subject"] and "Arunima Dutta" in n["subject"]
    for part in ("Stock: AMD", "Quantity: 20 shares", "Reason: test SELL", "Expires:", "IST", "Approval Centre",
                 "Never send your PIN"):
        assert part in n["body"], part
    assert "FAKE-S2" not in n["body"]                                   # no account numbers in email
    for pin in PIN.values():
        assert pin not in n["body"]


def test_approve_reject_execute_and_expire_each_queue_an_email():
    aid = pending_sell()
    approvals.decide(aid, "S2", PIN["S2"], "approve", "go")
    assert notes("approved") and "Decided by: Arunima Dutta" in notes("approved")[0]["body"]
    res = controller.execute_approved_sell(aid)
    assert res["result"] == "EXECUTED"
    ex = notes("executed")[0]
    assert "filled 20 shares at $150.00" in ex["body"]
    aid2 = pending_sell("S3", "MU", 5, 100.0)
    approvals.decide(aid2, "S3", PIN["S3"], "reject", "keep it")
    assert notes("rejected") and "keep it" in notes("rejected")[0]["body"]
    aid3 = pending_sell("S1", "NVDA", 3, 100.0)
    db.ex("UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE approval_id=?", (aid3,))
    assert approvals.expire_stale() == 1
    assert notes("expired")[0]["approval_id"] == aid3
    assert approvals.expire_stale() == 0 and len(notes("expired")) == 1               # once only


def test_invalidated_sell_queues_an_email():
    aid = pending_sell(qty=20)
    approvals.decide(aid, "S2", PIN["S2"], "approve")
    give_position("S2", "AMD", 25, 140.0, 150.0)                         # holding changed
    assert controller.execute_approved_sell(aid)["result"] == "INVALIDATED"
    assert "holding matches" in notes("invalidated")[0]["body"]


# ------------------------------------------------------------------ sending
@pytest.fixture
def smtp(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_send", lambda to, subject, body: sent.append((to, subject, body)))
    monkeypatch.setenv("SMTP_USER", "agent@gmail.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("NOTIFY_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("STUDENT2_EMAIL", "s2@example.com")
    return sent


def test_flush_sends_to_the_owning_student_and_admin(smtp):
    pending_sell()
    out = notify.flush()
    assert out == {"sent": 1, "failed": 0, "skipped": 0}
    assert smtp[0][0] == ["admin@example.com", "s2@example.com"]
    assert notes()[0]["sent_at"] and notify.flush()["sent"] == 0                    # never twice


def test_unconfigured_email_keeps_messages_queued(monkeypatch):
    monkeypatch.delenv("SMTP_USER", raising=False)
    pending_sell()
    assert notify.flush() == {"sent": 0, "failed": 0, "skipped": 1}
    assert notes()[0]["sent_at"] is None and notes()[0]["attempts"] == 0


def test_send_failures_are_retried_then_given_up_without_leaking_the_password(smtp, monkeypatch):
    def boom(to, subject, body):
        raise OSError("connection refused")
    monkeypatch.setattr(notify, "_send", boom)
    pending_sell()
    for _ in range(notify.MAX_ATTEMPTS + 2):
        notify.flush()
    n = notes()[0]
    assert n["sent_at"] is None and n["attempts"] == notify.MAX_ATTEMPTS and "OSError" in n["error"]
    assert "abcd" not in (n["error"] or "")


def test_missing_student_address_still_reaches_admin(smtp, monkeypatch):
    monkeypatch.delenv("STUDENT2_EMAIL")
    pending_sell()
    notify.flush()
    assert smtp[0][0] == ["admin@example.com"]


def test_demo_never_queues(monkeypatch):
    monkeypatch.setenv("AABA_DEMO_MODE", "1")
    notify.queue("created", {"student_id": "S2", "approval_id": "x", "symbol": "AMD", "qty": 1})
    assert notes() == []


# ------------------------------------------------------------------ approvals on the private dashboard
@pytest.fixture
def dashboard(monkeypatch):
    def on(enabled=True):
        monkeypatch.setenv("AABA_READ_ONLY", "1")
        if enabled:
            monkeypatch.setenv("AABA_APPROVALS_ON_DASHBOARD", "1")
        for sid, prefix in (("S1", "STUDENT1"), ("S2", "STUDENT2"), ("S3", "STUDENT3")):
            monkeypatch.setenv(f"{prefix}_APPROVAL_PIN_HASH", approvals.hash_pin(PIN[sid], 1000))
    return on


def test_dashboard_records_the_decision_with_the_owners_pin_only(dashboard):
    aid = pending_sell()
    dashboard()
    with pytest.raises(approvals.ApprovalError, match="wrong PIN"):
        approvals.decide(aid, "S2", PIN["S1"], "approve")                # someone else's PIN
    with pytest.raises(approvals.ApprovalError, match="only the student"):
        approvals.decide(aid, "S1", PIN["S1"], "approve")                # someone else's request
    res = approvals.decide(aid, "S2", PIN["S2"], "approve", "ok")
    assert res["status"] == "APPROVED" and res["decided_by"].startswith("Arunima Dutta")
    assert db.q("SELECT * FROM orders") == []                           # the dashboard never trades
    assert brokers.FakeBroker(load_students().get("S2"))._load()["orders"] == {}
    assert notes("approved")


def test_dashboard_lockout_still_applies(dashboard):
    aid = pending_sell()
    dashboard()
    for _ in range(5):
        with pytest.raises(approvals.ApprovalError):
            approvals.decide(aid, "S2", "000000", "approve")
    with pytest.raises(approvals.ApprovalError, match="too many"):
        approvals.decide(aid, "S2", PIN["S2"], "approve")


def test_dashboard_approvals_off_unless_enabled(dashboard, monkeypatch):
    aid = pending_sell()
    dashboard(enabled=False)
    with pytest.raises(approvals.ApprovalError, match="disabled"):
        approvals.decide(aid, "S2", PIN["S2"], "approve")
    monkeypatch.setenv("AABA_APPROVALS_ON_DASHBOARD", "1")
    monkeypatch.setenv("AABA_DEMO_MODE", "1")
    with pytest.raises(approvals.ApprovalError, match="demo"):
        approvals.decide(aid, "S2", PIN["S2"], "approve")


def test_dashboard_without_a_hash_refuses(dashboard, monkeypatch):
    aid = pending_sell()
    dashboard()
    monkeypatch.delenv("STUDENT2_APPROVAL_PIN_HASH")
    with pytest.raises(approvals.ApprovalError, match="not set up"):
        approvals.decide(aid, "S2", PIN["S2"], "approve")


def test_approved_request_is_executed_by_the_engine_not_the_dashboard(dashboard, monkeypatch):
    aid = pending_sell()
    dashboard()
    approvals.decide(aid, "S2", PIN["S2"], "approve")
    monkeypatch.delenv("AABA_READ_ONLY")                                  # now the trading engine
    res = controller.process_approved_sells("S2", make_run("paper", prices={"AMD": 150.0}))
    assert res["approved_processed"][0]["result"] == "EXECUTED"


@pytest.mark.parametrize("enabled", [True, False])
def test_approval_centre_page(dashboard, enabled, monkeypatch):
    from streamlit.testing.v1 import AppTest
    aid = pending_sell()
    dashboard(enabled)
    monkeypatch.setenv("AABA_DASHBOARD_PAGE", "approvals")          # kept for every rerun of the page
    at = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    pins = [t for t in at.text_input if t.label == "Your approval PIN"]
    if not enabled:
        assert pins == [] and not [b for b in at.button if "Approve" in b.label]
        return
    assert len(pins) == 1
    pins[0].input(PIN["S2"])
    approve = [b for b in at.button if b.label == "Approve SELL"][0]
    approve.click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert db.q1("SELECT status FROM approvals WHERE approval_id=?", (aid,))["status"] == "APPROVED"
    assert any("trading engine will re-check" in i.value for i in at.info)


def test_scheduler_registers_the_minute_notification_job(monkeypatch):
    from apscheduler.schedulers.blocking import BlockingScheduler

    from aaba import scheduler
    jobs = {}
    monkeypatch.setattr(BlockingScheduler, "start", lambda self: jobs.update({j.id: str(j.trigger) for j in self.get_jobs()}))
    scheduler.start()
    assert "interval[0:01:00]" in jobs["notify"]


def test_notify_job_expires_and_sends(smtp):
    from aaba import scheduler
    aid = pending_sell()
    db.ex("UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE approval_id=?", (aid,))
    out = scheduler.notify_job()
    assert out["sent"] == 2 and [s[1].split(":")[0] for s in smtp] == ["[AABA] SELL approval needed",
                                                                       "[AABA] SELL request expired"]
