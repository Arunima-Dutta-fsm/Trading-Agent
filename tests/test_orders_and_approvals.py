"""BUY automation, SELL approvals, HOLD, duplicates and safe failure."""
from datetime import timedelta

import pytest
from conftest import NOW, add_signal, give_position, make_run

from aaba import approvals, clock, controller, db
from aaba import broker as brokers
from aaba.settings import load_students

PIN = {"S1": "481516", "S2": "234223", "S3": "908172"}


def fake_orders(sid):
    return brokers.FakeBroker(load_students().get(sid))._load()["orders"]


# ------------------------------------------------------------------ BUY
def test_eligible_buy_goes_to_the_right_paper_account():
    run = make_run("paper", prices={"AMD": 150.0})
    sig = add_signal(run, "S2", "AMD", "BUY", 150.0, atr=4.0)
    res = controller.submit_buy(sig["signal_id"], "test")
    assert res["result"] == "BUY_SUBMITTED"
    o = list(fake_orders("S2").values())
    assert len(o) == 1 and o[0]["symbol"] == "AMD" and o[0]["side"] == "buy"
    # sizing: risk $1,000 / (2 x ATR 4) = 125 sh, allocation $10,000 / $150 = 66 sh -> 66
    assert o[0]["qty"] == 66
    assert fake_orders("S1") == {} and fake_orders("S3") == {}
    row = db.q1("SELECT * FROM orders WHERE signal_id=?", (sig["signal_id"],))
    assert row["student_id"] == "S2" and row["account_number"] == "FAKE-S2"


@pytest.mark.parametrize("change,expect", [
    ("market_closed", "market open"),
    ("stale", "daily bars up to date"),
    ("account", "account verified"),
    ("no_price", "latest price fresh"),
    ("old_price", "latest price fresh"),
    ("no_cash", "enough cash"),
    ("full_position", "position within allocation"),
    ("open_order", "no open order"),
    ("loss_halt", "daily loss halt"),
    ("untradable", "asset active and tradable"),
])
def test_buy_blocked_when_a_risk_check_fails(change, expect):
    run = make_run("paper", market_open=(change != "market_closed"), fresh=(change != "stale"),
                   prices={} if change == "no_price" else {"AMD": 150.0}, accounts_ok=(change != "account"))
    st = load_students().get("S2")
    b = brokers.FakeBroker(st)
    if change == "old_price":
        from aaba.market_data import LatestPrice
        controller._RUNS[run].prices["AMD"] = LatestPrice("AMD", 150.0, NOW - timedelta(hours=2))
    if change == "no_cash":
        b.set_state(cash=50.0)
    if change == "full_position":
        give_position("S2", "AMD", 70, 150.0)
    if change == "open_order":
        s = b._load()
        s["orders"]["x"] = {"id": "x", "symbol": "AMD", "side": "buy", "qty": 1, "client_order_id": "x",
                            "status": "new", "filled_qty": 0, "filled_avg_price": None}
        b._save(s)
    if change == "loss_halt":
        b.set_state(last_equity=120000.0)
    if change == "untradable":
        b.set_state(assets_untradable=["AMD"])
    before = len(fake_orders("S2"))
    sig = add_signal(run, "S2", "AMD", "BUY", 150.0)
    res = controller.submit_buy(sig["signal_id"], "test")
    assert res["result"] == "BUY_BLOCKED", res
    assert expect in res["detail"]
    assert len(fake_orders("S2")) == before


def test_buy_needs_both_paper_mode_and_switch(monkeypatch):
    monkeypatch.setenv("ENABLE_PAPER_ORDERS", "false")
    run = make_run("paper", prices={"AMD": 150.0})
    sig = add_signal(run, "S2", "AMD", "BUY", 150.0)
    assert controller.submit_buy(sig["signal_id"])["result"] == "BLOCKED_ORDERS_DISABLED"
    run2 = make_run("analysis", prices={"AMD": 150.0})
    sig2 = add_signal(run2, "S2", "AMD", "BUY", 150.0)
    assert controller.submit_buy(sig2["signal_id"])["result"] == "ANALYSIS_ONLY"
    run3 = make_run("dry_run", prices={"AMD": 150.0})
    sig3 = add_signal(run3, "S2", "AMD", "BUY", 150.0)
    assert controller.submit_buy(sig3["signal_id"])["result"] == "DRY_RUN_NOT_SENT"
    assert fake_orders("S2") == {}


def test_duplicate_buy_is_prevented():
    run = make_run("paper", prices={"AMD": 150.0})
    sig = add_signal(run, "S2", "AMD", "BUY", 150.0)
    assert controller.submit_buy(sig["signal_id"])["result"] == "BUY_SUBMITTED"
    again = controller.submit_buy(sig["signal_id"])
    assert again["result"] == "not processed" and "already handled" in again["detail"]
    # a second BUY signal for the same stock on the same day is also blocked
    run2 = make_run("paper", prices={"AMD": 150.0})
    sig2 = add_signal(run2, "S2", "AMD", "BUY", 150.0)
    res = controller.submit_buy(sig2["signal_id"])
    assert res["result"] == "BUY_BLOCKED"
    assert len(fake_orders("S2")) == 1


def test_broker_failure_is_recorded_not_crashed():
    run = make_run("paper", prices={"AMD": 150.0})
    brokers.FakeBroker(load_students().get("S2")).set_state(fail_submit=True)
    sig = add_signal(run, "S2", "AMD", "BUY", 150.0)
    res = controller.submit_buy(sig["signal_id"])
    assert res["result"] == "BUY_FAILED"
    row = db.q1("SELECT * FROM orders WHERE signal_id=?", (sig["signal_id"],))
    assert row["status"] == "SUBMIT_ERROR" and "outage" in row["error"]


def test_missing_credentials_fail_safely(monkeypatch):
    monkeypatch.setenv("AABA_BROKER", "alpaca")
    monkeypatch.delenv("STUDENT3_ALPACA_API_KEY")
    with pytest.raises(brokers.BrokerError):
        brokers.AlpacaPaperBroker(load_students().get("S3"))
    res = brokers.verify_accounts()
    assert res["S3"]["orders_allowed"] is False and "missing" in res["S3"]["problems"][0]


def test_alpaca_client_is_always_paper():
    b = brokers.AlpacaPaperBroker(load_students().get("S1"))     # no network call in the constructor
    assert b.base_url == "https://paper-api.alpaca.markets"
    assert b.key_prefix_ok


# ------------------------------------------------------------------ HOLD
def test_hold_never_submits_an_order():
    run = make_run("paper", prices={"KO": 60.0, "AMD": 150.0})
    hold = add_signal(run, "S2", "KO", "HOLD", 60.0)
    assert hold["outcome"] == "HOLD_RECORDED"
    assert controller.submit_buy(hold["signal_id"])["result"] == "not processed"
    buy = add_signal(run, "S2", "AMD", "BUY", 150.0)
    assert controller.record_hold(buy["signal_id"], "news looks bad")["result"] == "AGENT_VETO_HOLD"
    assert controller.submit_buy(buy["signal_id"])["result"] == "not processed"
    assert fake_orders("S2") == {}


# ------------------------------------------------------------------ SELL
def _pending_sell(sid="S2", sym="AMD", qty=20, price=150.0, market_open=True):
    run = make_run("paper", prices={sym: price}, market_open=market_open)
    give_position(sid, sym, qty, 140.0, price)
    sig = add_signal(run, sid, sym, "SELL", price)
    res = controller.create_sell_approval(sig["signal_id"], "exit rule")
    assert res["result"] == "SELL_PENDING_APPROVAL"
    return run, res["approval_id"]


def test_sell_creates_approval_and_never_executes_immediately():
    run, aid = _pending_sell()
    ap = db.q1("SELECT * FROM approvals WHERE approval_id=?", (aid,))
    assert ap["status"] == "PENDING" and ap["student_id"] == "S2" and ap["qty"] == 20
    assert fake_orders("S2") == {}
    assert controller.execute_approved_sell(aid)["result"] == "not executed"   # still pending
    assert fake_orders("S2") == {}


def test_agent_cannot_cancel_a_sell():
    run = make_run("paper", prices={"AMD": 150.0})
    sig = add_signal(run, "S2", "AMD", "SELL", 150.0)
    assert controller.record_hold(sig["signal_id"], "no")["result"] == "refused"


def test_wrong_student_cannot_approve():
    _, aid = _pending_sell()
    with pytest.raises(approvals.ApprovalError, match="only the student"):
        approvals.decide(aid, "S1", PIN["S1"], "approve")        # S1 with S1's own valid PIN
    with pytest.raises(approvals.ApprovalError, match="wrong PIN"):
        approvals.decide(aid, "S2", PIN["S1"], "approve")        # right student, someone else's PIN
    assert db.q1("SELECT status FROM approvals WHERE approval_id=?", (aid,))["status"] == "PENDING"


def test_admin_pin_is_separate_from_student_pins(monkeypatch):
    assert approvals.verify_admin_pin("777333")
    assert not approvals.verify_admin_pin("777334") and not approvals.verify_admin_pin("")
    assert not any(approvals.verify_admin_pin(p) for p in PIN.values())
    _, aid = _pending_sell()
    with pytest.raises(approvals.ApprovalError, match="wrong PIN"):
        approvals.decide(aid, "S2", "777333", "approve")      # the admin PIN cannot approve a SELL
    monkeypatch.setenv("DASHBOARD_ADMIN_PIN", "12345")         # too short: never valid
    assert not approvals.verify_admin_pin("12345")


@pytest.mark.parametrize("bad", ["065014", "S2", "123456", "999999", "345678", "98765", "Arunima Dutta"])
def test_guessable_pin_cannot_approve(monkeypatch, bad):
    monkeypatch.setenv("STUDENT2_APPROVAL_PIN", bad)
    _, aid = _pending_sell()
    with pytest.raises(approvals.ApprovalError, match="cannot be used"):
        approvals.decide(aid, "S2", bad, "approve")
    assert db.q1("SELECT status FROM approvals WHERE approval_id=?", (aid,))["status"] == "PENDING"
    monkeypatch.setenv("DASHBOARD_ADMIN_PIN", bad)
    assert not approvals.verify_admin_pin(bad)


def test_lockout_after_repeated_wrong_pins():
    _, aid = _pending_sell()
    for _ in range(5):
        with pytest.raises(approvals.ApprovalError):
            approvals.decide(aid, "S2", "000000", "approve")
    with pytest.raises(approvals.ApprovalError, match="too many"):
        approvals.decide(aid, "S2", PIN["S2"], "approve")


def test_rejected_and_expired_cannot_execute():
    _, aid = _pending_sell()
    approvals.decide(aid, "S2", PIN["S2"], "reject")
    assert controller.execute_approved_sell(aid)["result"] == "not executed"
    with pytest.raises(approvals.ApprovalError):
        approvals.decide(aid, "S2", PIN["S2"], "approve")       # cannot flip a decided request
    _, aid2 = _pending_sell(sym="TSLA", qty=5, price=400.0)
    db.ex("UPDATE approvals SET expires_at=? WHERE approval_id=?", (clock.iso(clock.now_utc() - timedelta(minutes=1)), aid2))
    with pytest.raises(approvals.ApprovalError, match="expired"):
        approvals.decide(aid2, "S2", PIN["S2"], "approve")
    assert controller.execute_approved_sell(aid2)["result"] == "not executed"
    assert fake_orders("S2") == {}


def test_approved_but_expired_before_execution_is_not_sent():
    _, aid = _pending_sell()
    approvals.decide(aid, "S2", PIN["S2"], "approve")
    db.ex("UPDATE approvals SET expires_at=? WHERE approval_id=?", (clock.iso(clock.now_utc() - timedelta(minutes=1)), aid))
    assert controller.execute_approved_sell(aid)["result"] == "not executed"
    assert db.q1("SELECT status FROM approvals WHERE approval_id=?", (aid,))["status"] == "EXPIRED"


def test_approved_sell_is_revalidated_and_sent_once():
    _, aid = _pending_sell()
    approvals.decide(aid, "S2", PIN["S2"], "approve")
    res = controller.execute_approved_sell(aid)
    assert res["result"] == "EXECUTED", res
    orders = list(fake_orders("S2").values())
    assert len(orders) == 1 and orders[0]["side"] == "sell" and orders[0]["qty"] == 20
    assert orders[0]["client_order_id"] == f"aaba-{aid}"
    assert controller.execute_approved_sell(aid)["result"] == "not executed"     # retry cannot resend
    assert len(fake_orders("S2")) == 1


def test_approved_sell_invalidated_when_holding_changed():
    _, aid = _pending_sell(qty=20)
    approvals.decide(aid, "S2", PIN["S2"], "approve")
    give_position("S2", "AMD", 25, 140.0, 150.0)      # holding no longer matches the approved 20
    res = controller.execute_approved_sell(aid)
    assert res["result"] == "INVALIDATED" and "holding matches" in res["detail"]
    assert fake_orders("S2") == {}


def test_approved_sell_invalidated_when_price_moved_too_far(monkeypatch):
    _, aid = _pending_sell(price=150.0)
    approvals.decide(aid, "S2", PIN["S2"], "approve")
    db.ex("UPDATE approvals SET signal_price=? WHERE approval_id=?", (100.0, aid))
    res = controller.execute_approved_sell(aid)
    # fingerprint does not include price, but the re-check compares the live price to the signal price
    assert res["result"] == "INVALIDATED" and "price close" in res["detail"]


def test_approved_sell_waits_when_market_closed():
    _, aid = _pending_sell(market_open=False)
    approvals.decide(aid, "S2", PIN["S2"], "approve")
    res = controller.execute_approved_sell(aid)
    assert res["result"] == "WAITING"
    assert db.q1("SELECT status FROM approvals WHERE approval_id=?", (aid,))["status"] == "APPROVED"
    assert fake_orders("S2") == {}


def test_new_sell_signal_supersedes_old_request():
    run, aid = _pending_sell()
    sig2 = add_signal(make_run("paper", prices={"AMD": 150.0}), "S2", "AMD", "SELL", 150.0)
    res = controller.create_sell_approval(sig2["signal_id"], "newer")
    assert db.q1("SELECT status FROM approvals WHERE approval_id=?", (aid,))["status"] == "SUPERSEDED"
    assert res["approval_id"] != aid


def test_restart_keeps_approvals_and_audit_history():
    _, aid = _pending_sell()
    n_events = len(db.q("SELECT * FROM events"))
    controller.reset_cache()                 # same as restarting the app: memory is gone, the file stays
    import importlib
    importlib.reload(db)
    ap = db.q1("SELECT * FROM approvals WHERE approval_id=?", (aid,))
    assert ap["status"] == "PENDING"
    assert len(db.q("SELECT * FROM events")) == n_events
    approvals.decide(aid, "S2", PIN["S2"], "approve")
    assert controller.execute_approved_sell(aid)["result"] == "EXECUTED"


def test_analysis_run_never_sends_an_approved_sell():
    _, aid = _pending_sell()
    approvals.decide(aid, "S2", PIN["S2"], "approve")
    analysis = make_run("analysis", prices={"AMD": 150.0})
    res = controller.process_approved_sells("S2", analysis)
    assert "skipped" in res and fake_orders("S2") == {}
    paper = make_run("paper", prices={"AMD": 150.0})
    res = controller.process_approved_sells("S2", paper)
    assert res["approved_processed"][0]["result"] == "EXECUTED"
