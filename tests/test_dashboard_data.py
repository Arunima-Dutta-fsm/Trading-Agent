"""What the dashboard shows for transactions: proposed price vs broker-confirmed fill, reasons,
approvals with their fills, problems with their reasons, combined totals, and persistence."""
import importlib

from conftest import add_signal, give_position, make_run

from aaba import approvals, controller, db
from aaba import broker as brokers
from aaba.broker import OrderResult
from aaba.settings import load_students
from app import dashboard_data as dd

PIN_S2 = "234223"


def _flow():
    run = make_run("paper", prices={"AMD": 150.0, "TSLA": 300.0, "KO": 60.0})
    buy = add_signal(run, "S2", "AMD", "BUY", 150.0, atr=4.0)
    assert controller.submit_buy(buy["signal_id"], "momentum looks fine")["result"] == "BUY_SUBMITTED"
    give_position("S2", "TSLA", 5, 280.0, 300.0)
    sell = add_signal(run, "S2", "TSLA", "SELL", 300.0)
    aid = controller.create_sell_approval(sell["signal_id"], "exit rule")["approval_id"]
    approvals.decide(aid, "S2", PIN_S2, "approve", "ok")
    assert controller.execute_approved_sell(aid)["result"] == "EXECUTED"
    blocked = add_signal(run, "S2", "KO", "BUY", 60.0)
    brokers.FakeBroker(load_students().get("S2")).set_state(cash=10.0)
    assert controller.submit_buy(blocked["signal_id"])["result"] == "BUY_BLOCKED"
    for st in load_students().students:
        controller.reconcile_student(st.student_id, run)
    return run, aid


def test_orders_show_reason_and_broker_fill_separately():
    _flow()
    o = dd.orders_view("S2").set_index("symbol")
    amd = o.loc["AMD"]
    assert amd["side"] == "BUY" and amd["status"] == "filled" and amd["fill_qty"] == amd["qty"] == 66
    assert amd["proposed_price"] == 150.0 and amd["fill_price"] == 150.0
    assert "test BUY" in amd["reason"] and "agent: momentum looks fine" in amd["reason"]
    tsla = o.loc["TSLA"]
    assert tsla["side"] == "SELL" and tsla["fill_qty"] == 5 and tsla["realized_pnl"] == 100.0
    assert tsla["approved_by"].startswith("Arunima Dutta") and tsla["approved_at"]
    assert "test SELL" in tsla["reason"]


def test_unfilled_order_never_shows_a_fill_price():
    run = make_run("paper", prices={"AMD": 150.0})
    sig = add_signal(run, "S2", "AMD", "BUY", 150.0, atr=4.0)

    class AcceptedOnly(brokers.FakeBroker):
        def submit_market_order(self, symbol, qty, side, client_order_id, est_price=None):
            return OrderResult("x-1", client_order_id, "accepted", 0.0, None)

        def order_by_client_id(self, cid):
            return None

    brokers.set_broker("S2", AcceptedOnly(load_students().get("S2")))
    assert controller.submit_buy(sig["signal_id"])["result"] == "BUY_SUBMITTED"
    row = dd.orders_view("S2").iloc[0]
    assert row["status"] == "accepted" and row["proposed_price"] == 150.0
    assert row["fill_price"] is None and row["fill_qty"] == 0


def test_approval_view_links_decision_to_fill():
    _, aid = _flow()
    a = dd.approvals_view("S2").set_index("approval_id").loc[aid]
    assert a["status"] == "EXECUTED" and a["decided_by"].startswith("Arunima Dutta")
    assert a["proposed_price"] == 300.0 and a["fill_qty"] == 5 and a["fill_price"] == 300.0
    assert a["realized_pnl"] == 100.0 and a["order_status"] == "filled"


def test_problems_view_lists_reasons():
    _flow()
    run = make_run("paper", prices={"MU": 100.0})
    give_position("S3", "MU", 3, 90.0)
    sell = add_signal(run, "S3", "MU", "SELL", 100.0)
    aid = controller.create_sell_approval(sell["signal_id"])["approval_id"]
    approvals.decide(aid, "S3", "908172", "reject", "keep it")
    p2 = dd.problems_view("S2")
    ko = p2[p2["symbol"] == "KO"].iloc[0]
    assert ko["result"] == "BUY_BLOCKED" and "enough cash" in ko["reason"]
    p3 = dd.problems_view("S3")
    assert (p3["result"] == "REJECTED").any() and "keep it" in p3[p3["result"] == "REJECTED"].iloc[0]["reason"]
    assert "KO" not in set(p3["symbol"])                      # never another student's records


def test_combined_view_keeps_accounts_separate_and_totals_add_up():
    _flow()
    g = dd.group_summary()
    rows = g["students"]
    assert g["current_value"] == round(sum(r["equity"] for r in rows), 2)
    assert g["cash"] == round(sum(r["cash"] for r in rows), 2)
    assert g["total_pnl"] == round(sum(r["total_pnl"] for r in rows), 2)
    gh = dd.group_holdings()
    assert set(gh["student"]) == {load_students().get("S2").label}
    assert set(gh["symbol"]) == {"AMD"}                       # TSLA was sold
    assert gh["weight_pct"].iloc[0] > 0
    perf = dd.group_performance()
    assert len(perf) == 1 and perf["equity"].iloc[0] == g["current_value"]


def test_dashboard_data_survives_restart():
    import pandas as pd
    _flow()
    views = ("orders_view", "approvals_view", "problems_view", "holdings")
    before = {v: getattr(dd, v)("S2") for v in views}
    total = dd.group_summary()["current_value"]
    controller.reset_cache()                     # same as restarting: memory gone, database file kept
    importlib.reload(db)
    importlib.reload(dd)
    for v in views:
        pd.testing.assert_frame_equal(before[v].reset_index(drop=True), getattr(dd, v)("S2").reset_index(drop=True))
    assert dd.group_summary()["current_value"] == total
    assert len(before["orders_view"]) == 2 and len(before["approvals_view"]) == 1
