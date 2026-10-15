"""Read-only public demo (AABA_DEMO_MODE=1): every change is refused on the server side,
no real account or secret can be used, students are anonymized, and every dashboard page
renders without controls."""
import json
import subprocess
import sys

import pytest
from conftest import ROOT, SECRETS, add_signal, give_position, make_run

from aaba import approvals, controller, db, settings
from aaba import broker as brokers
from aaba.demo import ReadOnlyDemo
from aaba.settings import load_strategy, load_students

REAL_NAMES = ["Akshita", "Arunima", "Navya", "065009", "065014", "065015"]


def _demo_on(monkeypatch):
    monkeypatch.setenv("AABA_DEMO_MODE", "1")
    monkeypatch.setenv("ENABLE_PAPER_ORDERS", "true")      # must be ignored in demo mode
    monkeypatch.setenv("AABA_BROKER", "alpaca")            # must be ignored in demo mode
    monkeypatch.setenv("AABA_DATA_PROVIDER", "alpaca")     # must be ignored in demo mode


@pytest.fixture
def demo(monkeypatch):
    _demo_on(monkeypatch)


def test_demo_refuses_every_change_server_side(monkeypatch):
    from agents.trading_crew import run_workflow
    from aaba import scheduler
    from aaba.backtest import run_backtest
    from aaba.validation import validate_symbols

    run = make_run("paper", prices={"AMD": 150.0})          # normal mode: a signal and an approval
    buy = add_signal(run, "S2", "AMD", "BUY", 150.0)
    give_position("S2", "TSLA", 5, 300.0)
    sell = add_signal(run, "S2", "TSLA", "SELL", 300.0)
    aid = controller.create_sell_approval(sell["signal_id"])["approval_id"]
    _demo_on(monkeypatch)

    refused = [
        lambda: controller.submit_buy(buy["signal_id"]),
        lambda: controller.record_hold(buy["signal_id"], "x"),
        lambda: controller.create_sell_approval(sell["signal_id"]),
        lambda: controller.preview_buy(buy["signal_id"]),
        lambda: controller.execute_approved_sell(aid),
        lambda: controller.process_approved_sells("S2", run),
        lambda: controller.reconcile_student("S2"),
        lambda: controller.prepare_market_data(run),
        lambda: controller.evaluate_student(run, "S2"),
        lambda: approvals.create_request(run, sell, "S2", None, 5, "x"),
        lambda: brokers.verify_accounts(),
        lambda: brokers.AlpacaPaperBroker(load_students().get("S2")),
        lambda: brokers.FakeBroker(load_students().get("S2")).submit_market_order("AMD", 1, "buy", "c1", 1.0),
        lambda: settings.save_strategy(load_strategy().model_dump()),
        lambda: settings.set_student_enabled("S2", False),
        lambda: run_workflow("analysis", "demo-test", use_llm=False),
        lambda: scheduler.start(),
        lambda: scheduler.daily_job(),
        lambda: scheduler.approved_sell_job(),
        lambda: validate_symbols(),
        lambda: run_backtest("S2", tune=False),
    ]
    for i, fn in enumerate(refused):
        with pytest.raises(ReadOnlyDemo):
            fn()
        assert i >= 0
    with pytest.raises(approvals.ApprovalError, match="read-only demo"):
        approvals.decide(aid, "S2", "234223", "approve")
    assert db.q1("SELECT outcome FROM signals WHERE signal_id=?", (buy["signal_id"],))["outcome"] is None
    assert db.q1("SELECT status FROM approvals WHERE approval_id=?", (aid,))["status"] == "PENDING"
    assert db.q("SELECT * FROM orders") == []


def test_demo_uses_no_real_account_or_secret(demo):
    st = load_students().get("S2")
    assert settings.paper_orders_enabled() is False
    assert settings.broker_backend() == "fake" and settings.data_provider() == "synthetic"
    assert isinstance(brokers.get_broker(st), brokers.FakeBroker)
    assert st.alpaca_keys() == ("", "") and st.approval_pin() == ""
    group = load_students()
    shown = json.dumps(group.model_dump())
    assert not any(n in shown for n in REAL_NAMES)
    assert [s.name for s in group.students] == ["Student 1", "Student 2", "Student 3"]


def test_demo_seed_builds_sample_then_locks(demo, monkeypatch):
    from app import demo_seed
    monkeypatch.setenv("AABA_SYNTHETIC_NOW", demo_seed.DEMO_DAY)
    run_id = demo_seed.build()
    assert db.get_run(run_id)["status"] == "DONE"
    orders = db.q("SELECT * FROM orders")
    assert orders and all(o["account_number"].startswith("FAKE-") for o in orders)
    sigs = db.q("SELECT * FROM signals WHERE run_id=?", (run_id,))
    assert len(sigs) == 36 and all(s["outcome"] for s in sigs)
    # after seeding, nothing can change: not even re-running the seed's own steps
    for fn in (lambda: controller.submit_buy(sigs[0]["signal_id"]), lambda: demo_seed.build(),
               lambda: controller.reconcile_student("S1")):
        with pytest.raises(ReadOnlyDemo):
            fn()


@pytest.mark.parametrize("page", ["overview", "student-S1", "student-S2", "student-S3", "approvals", "config",
                                  "history", "system"])
def test_demo_pages_render_without_controls_or_personal_data(page):
    r = subprocess.run([sys.executable, str(ROOT / "tests" / "demo_apptest_runner.py"), page],
                       capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["exception"] == [], out["exception"]
    assert out["env_demo"] == "1"
    assert "READ-ONLY DEMO" in out["text"]
    # no action controls anywhere (History keeps only its read-only search box)
    assert not [b for b in out["buttons"] if b != "Download CSV"], out["buttons"]
    assert [t for t in out["text_inputs"] if t not in ("Search text",)] == [], out["text_inputs"]
    assert not any(n in out["text"] for n in REAL_NAMES)
    assert not any(s in out["text"] for s in SECRETS)
    assert "aaba-demo" in out["db"] and str(ROOT / "data") not in out["db"]
