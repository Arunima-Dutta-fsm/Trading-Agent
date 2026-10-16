"""Intraday decisions: evidence-based dips vs sustained weakness, disciplined scaling in, anchored
stops, SELL requests, risk sizing, duplicate protection, scheduling and LLM use."""
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from conftest import NOW, add_signal, give_position, make_run

from aaba import controller, db, intraday, market_data, paths, settings
from aaba import broker as brokers
from aaba.intraday import PositionPlan, decide, risk_qty
from aaba.risk import BuyContext, check_buy
from aaba.settings import load_strategy, load_students


def write_strategy(monkeypatch, tmp_path, **intraday):
    """A copy of strategy.json with intraday enabled (plus overrides), used by this process AND by
    the MCP server child process (through AABA_STRATEGY_FILE)."""
    raw = json.loads(paths.STRATEGY_FILE.read_text(encoding="utf-8"))
    raw["intraday"] = {**raw.get("intraday", {}), "enabled": True, **intraday}
    f = tmp_path / "strategy-intraday.json"
    f.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(paths, "STRATEGY_FILE", f)
    monkeypatch.setenv("AABA_STRATEGY_FILE", str(f))
    return load_strategy()


@pytest.fixture
def intraday_on(monkeypatch, tmp_path):
    return write_strategy(monkeypatch, tmp_path)


def cfg_on():
    c = load_strategy()
    return c.model_copy(update={"intraday": c.intraday.model_copy(update={"enabled": True})})


DAILY = {"close": 100.0, "high": 103.0, "low": 97.5, "ema_fast": 98.0, "ema_mid": 95.0, "ema_slow": 90.0,
         "ema_mid_5ago": 94.0, "rsi": 55.0, "atr": 2.0, "bb_upper": 104.0, "bb_lower": 92.0}
DIP = {"close": 99.0, "open": 98.5, "high": 99.1, "low": 98.4, "vwap": 99.6, "i_ema9": 98.8, "i_ema21": 99.0,
       "i_ema21_slope": 0.02, "i_rsi": 47.0, "i_rsi_min3": 38.0, "rel_vol": 1.3, "sess_high": 102.0,
       "sess_low": 98.3, "or_low": 98.5, "or_high": 101.0, "lower_lows6": 2, "up_bar": True, "bars_today": 12,
       "day_open": 100.5}
MARKET_OK = {"known": True, "change_pct": 0.2, "below_vwap": False}


def run_decide(bar=None, daily=None, pos=None, market=None, daily_signal="HOLD", blocked=None, cat="core"):
    return decide("TEST", cat, cfg_on(), daily or DAILY, {**DIP, **(bar or {})}, daily_signal, ["daily reason"],
                  blocked or [], market or MARKET_OK, pos or PositionPlan())


# ------------------------------------------------------------------ evidence, not just a lower price
def test_temporary_dip_in_healthy_trend_is_a_buy():
    d = run_decide()
    assert d.action == "BUY" and d.kind == "initial", d.reasons
    assert d.stop == 98.0 and d.target == 102.0 and d.reward_risk >= 1.5          # support-based stop, half-ATR minimum


def test_falling_price_with_sustained_weakness_is_not_a_buy():
    downtrend = {"vwap": 100.2, "i_ema9": 98.9, "i_ema21": 99.4, "i_ema21_slope": -0.3, "close": 99.0}
    d = run_decide(bar=downtrend)
    assert d.action == "HOLD" and "sustained weakness" in d.reasons[0] and "downtrend" in d.reasons[0]


@pytest.mark.parametrize("change,why", [
    ({"bar": {"lower_lows6": 5, "up_bar": False}}, "no entry"),                      # staircase, no bounce
    ({"daily": {**DAILY, "low": 99.8}}, "prior day"),                                # broke yesterday's low
    ({"market": {"known": True, "change_pct": -1.6, "below_vwap": True}}, "weak market"),
    ({"blocked": ["news veto: sentiment -0.6 over 3 article(s)"]}, "news veto"),
    ({"bar": {"rel_vol": 0.4}}, "no entry"),                                         # bounce without volume
    ({"daily": {**DAILY, "ema_slow": 101.0}}, "no entry"),                           # long-term trend broken
])
def test_buy_refused_without_evidence(change, why):
    d = run_decide(**change)
    assert d.action == "HOLD" and why in " ".join(d.reasons), d.reasons


def test_poor_reward_to_risk_is_not_traded():
    d = run_decide(bar={"sess_high": 99.6})                     # target barely above the price
    assert d.action == "HOLD"


def test_daily_buy_needs_intraday_confirmation():
    strong = {"close": 101.0, "vwap": 100.4, "i_ema9": 100.8, "i_ema21": 100.3, "i_rsi": 62.0, "sess_high": 101.2,
              "up_bar": True, "i_rsi_min3": 58.0}
    assert run_decide(bar=strong, daily_signal="BUY", daily={**DAILY, "high": 106.0}).action == "BUY"
    assert run_decide(bar={**strong, "i_rsi": 80.0}, daily_signal="BUY").action == "HOLD"     # overbought intraday


# ------------------------------------------------------------------ scaling in with discipline
HELD = PositionPlan(qty=10, avg_cost=101.0, anchor_stop=97.0, entries=1, last_entry_price=101.0,
                    minutes_since_last_entry=60)


def test_add_on_a_confirmed_dip_keeps_the_anchored_stop():
    d = run_decide(pos=HELD, bar={"sess_high": 103.0})        # room back to the session high
    assert d.action == "BUY" and d.kind == "add" and d.stop == 97.0, d.reasons


@pytest.mark.parametrize("plan,why", [
    (PositionPlan(**{**HELD.__dict__, "minutes_since_last_entry": 10}), "min ago"),
    (PositionPlan(**{**HELD.__dict__, "entries": 3}), "entries"),
    (PositionPlan(**{**HELD.__dict__, "last_entry_price": 99.2}), "no new evidence"),   # not far enough below
    (PositionPlan(**{**HELD.__dict__, "sell_pending": True}), "SELL approval"),
    (PositionPlan(**{**HELD.__dict__, "buys_today_student": 10}), "daily BUY limit"),
])
def test_add_refused_by_the_rules(plan, why):
    d = run_decide(pos=plan)
    assert d.action == "HOLD" and why in " ".join(d.reasons), d.reasons


def test_no_averaging_down_into_weakness():
    downtrend = {"vwap": 100.2, "i_ema9": 98.9, "i_ema21": 99.4, "i_ema21_slope": -0.3}
    d = run_decide(bar=downtrend, pos=HELD)
    assert d.action == "HOLD" and "weakness" in " ".join(d.reasons)


# ------------------------------------------------------------------ exits
def test_stop_breach_raises_a_sell_request_once():
    pos = PositionPlan(**{**HELD.__dict__, "anchor_stop": 99.5})
    assert run_decide(pos=pos).action == "SELL"
    again = run_decide(pos=PositionPlan(**{**pos.__dict__, "sell_pending": True}))
    assert again.action == "HOLD" and "already waiting" in again.reasons[0]


def test_daily_sell_and_stretched_price_raise_sell_requests():
    assert run_decide(pos=HELD, daily_signal="SELL").action == "SELL"
    stretched = {"close": 105.0, "i_rsi": 85.0}
    assert run_decide(pos=HELD, bar=stretched).action == "SELL"
    assert run_decide(bar=stretched).action != "SELL"                       # never sells what is not held


# ------------------------------------------------------------------ sizing and risk checks
def test_risk_sizing_initial_and_add():
    cfg = cfg_on()
    assert risk_qty(cfg, 100_000, 100.0, 98.0, 0, None, "initial") == 500          # 1% risk / $2 per share
    # add: whole position to the anchored stop within 1.5%: held 300 sh @ 101 -> $1,200 used, $300 left / $2
    assert risk_qty(cfg, 100_000, 99.0, 97.0, 300, 101.0, "add") == 150
    assert risk_qty(cfg, 100_000, 99.0, 99.5, 0, None, "initial") == 0             # stop above price


def _ctx(**kw):
    st = load_students().get("S2")
    base = dict(student=st, cfg=cfg_on(), symbol="AMD", category="satellite", signal_action="BUY", data_quality="ok",
                data_fresh=True, atr=2.0, orders_allowed=True, asset={"status": "active", "tradable": True},
                account={"cash": 100000, "equity": 100000, "last_equity": 100000}, positions={}, open_orders=[],
                latest_price=99.0, price_time=NOW, now=NOW, market_open=True, already_bought_today=True,
                buys_this_run=0, intraday=True, entry_kind="initial", stop=97.0)
    base.update(kw)
    return BuyContext(**base)


def test_intraday_risk_checks_allow_a_second_buy_today_but_not_blind_adds():
    assert check_buy(_ctx()).passed                                         # bought earlier today: allowed
    add_ok = _ctx(entry_kind="add", entries=1, last_entry_price=100.0, minutes_since_last_entry=45,
                  positions={"AMD": {"qty": 50, "avg_entry": 100.0, "market_value": 4950}})
    assert check_buy(add_ok).passed
    too_high = _ctx(entry_kind="add", entries=1, last_entry_price=99.2, minutes_since_last_entry=45)
    assert "add only below the last entry" in check_buy(too_high).summary()
    for kw, name in ((dict(entries=3), "entries per position"), (dict(minutes_since_last_entry=5), "time since"),
                     (dict(sell_pending=True), "no SELL waiting"), (dict(buys_today_student=10), "student BUYs"),
                     (dict(stop=99.5), "stop below price")):
        assert name in check_buy(_ctx(**kw)).summary()


def test_daily_mode_keeps_one_buy_per_day():
    r = check_buy(_ctx(intraday=False))
    assert not r.passed and "not already bought today" in r.summary()


# ------------------------------------------------------------------ data: completed candles, no look-ahead
def _candles(day="2026-10-13", n=40, start_px=100.0):
    ts = pd.date_range(f"{day} 09:30", periods=n, freq="5min", tz="America/New_York").tz_convert("UTC")
    px = start_px + pd.Series(range(n)).mul(0.05).values
    return pd.DataFrame({"symbol": "TEST", "timestamp": ts, "open": px, "high": px + 0.1, "low": px - 0.1,
                         "close": px + 0.02, "volume": 1000.0})


def test_only_completed_regular_hours_candles_are_used():
    df = _candles(n=6)
    pre = df.iloc[[0]].copy()
    pre["timestamp"] = pre["timestamp"] - pd.Timedelta(hours=1)                    # 08:30 pre-market
    now = df["timestamp"].iloc[-1] + pd.Timedelta(minutes=2)                        # last candle still forming
    out = market_data.completed_regular_bars(pd.concat([pre, df]), 5, now.to_pydatetime())
    assert len(out) == 5 and out["timestamp"].min() == df["timestamp"].iloc[0]


def test_features_have_no_look_ahead():
    df = pd.concat([_candles("2026-10-12"), _candles("2026-10-13", start_px=102.0)], ignore_index=True)
    full = intraday.add_intraday_features(df, 5)
    for k in (25, 45, 70):
        part = intraday.add_intraday_features(df.iloc[:k + 1], 5)
        a, b = full.iloc[k], part.iloc[-1]
        for col in ("vwap", "i_ema9", "i_ema21", "i_rsi", "rel_vol", "sess_high", "or_low", "lower_lows6"):
            assert a[col] == pytest.approx(b[col], nan_ok=True), (k, col)
    day2 = full[full["session"] == full["session"].iloc[-1]]
    assert day2["sess_high"].iloc[0] == pytest.approx(day2["high"].iloc[0])        # session values reset daily


def test_synthetic_intraday_bars_stop_at_the_simulated_now():
    now = datetime(2026, 10, 13, 14, 32, tzinfo=timezone.utc)                       # 10:32 New York
    p = market_data.SyntheticMarketData(now=now, market_open=True)
    res = p.intraday_bars(["AMD"], 5, 3)
    today = res.bars[res.bars["timestamp"].dt.tz_convert("America/New_York").dt.date == now.date()]
    assert len(today) == 12 and (res.bars["timestamp"] + pd.Timedelta(minutes=5) <= now).all()


# ------------------------------------------------------------------ several BUYs of one stock in a day
def test_scaling_in_creates_separate_orders_under_the_rules(intraday_on):
    run = make_run("paper", prices={"AMD": 150.0})
    sig = add_signal(run, "S2", "AMD", "BUY", 150.0, atr=4.0)
    db.ex("UPDATE signals SET indicators=? WHERE signal_id=?",
          (json.dumps({"ATR14": 4.0, "stop": 142.0, "entry_kind": "initial"}), sig["signal_id"]))
    assert controller.submit_buy(sig["signal_id"])["result"] == "BUY_SUBMITTED"
    first = db.q1("SELECT * FROM orders WHERE signal_id=?", (sig["signal_id"],))
    assert first["client_order_id"] == f"aaba-{sig['signal_id']}-buy"
    db.ex("UPDATE orders SET submitted_at=? WHERE order_pk=?",
          ((datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(timespec="seconds"), first["order_pk"]))
    # 45 minutes later, a lower price, an add decision from a NEW cycle
    run2 = make_run("paper", prices={"AMD": 147.0})
    add = add_signal(run2, "S2", "AMD", "BUY", 147.0, atr=4.0)
    db.ex("UPDATE signals SET indicators=? WHERE signal_id=?",
          (json.dumps({"ATR14": 4.0, "stop": 142.0, "entry_kind": "add"}), add["signal_id"]))
    assert controller.submit_buy(add["signal_id"])["result"] == "BUY_SUBMITTED", \
        db.q1("SELECT outcome_detail FROM signals WHERE signal_id=?", (add["signal_id"],))
    orders = db.q("SELECT * FROM orders WHERE student_id='S2' AND symbol='AMD' ORDER BY order_pk")
    assert len(orders) == 2 and len({o["client_order_id"] for o in orders}) == 2
    # the same decision can never be sent twice
    assert controller.submit_buy(add["signal_id"])["result"] == "not processed"
    # an immediate third "add" at a HIGHER price is blocked
    run3 = make_run("paper", prices={"AMD": 149.0})
    up = add_signal(run3, "S2", "AMD", "BUY", 149.0, atr=4.0)
    db.ex("UPDATE signals SET indicators=? WHERE signal_id=?",
          (json.dumps({"ATR14": 4.0, "stop": 142.0, "entry_kind": "add"}), up["signal_id"]))
    res = controller.submit_buy(up["signal_id"])
    assert res["result"] == "BUY_BLOCKED" and "time since last entry" in res["detail"]
    assert len(db.q("SELECT * FROM orders WHERE symbol='AMD'")) == 2


def test_position_plan_reads_entries_and_anchored_stop(intraday_on):
    run = make_run("paper", prices={"AMD": 150.0})
    sig = add_signal(run, "S2", "AMD", "BUY", 150.0, atr=4.0)
    db.ex("UPDATE signals SET indicators=? WHERE signal_id=?",
          (json.dumps({"ATR14": 4.0, "stop": 142.0, "entry_kind": "initial"}), sig["signal_id"]))
    controller.submit_buy(sig["signal_id"])
    p = brokers.FakeBroker(load_students().get("S2")).positions()["AMD"]
    plan = controller.position_plan("S2", "AMD", p["qty"], p["avg_entry"], datetime.now(timezone.utc))
    assert plan.entries == 1 and plan.anchor_stop == 142.0 and plan.last_entry_price == 150.0
    assert plan.buys_today_student == 1 and not plan.sell_pending
    flat = controller.position_plan("S2", "AMD", 0, None, datetime.now(timezone.utc))
    assert flat.entries == 0 and flat.anchor_stop is None


def test_repeated_sell_decision_reuses_the_waiting_approval(intraday_on):
    run = make_run("paper", prices={"AMD": 150.0})
    give_position("S2", "AMD", 20, 160.0, 150.0)
    a1 = controller.create_sell_approval(add_signal(run, "S2", "AMD", "SELL", 150.0)["signal_id"])
    run2 = make_run("paper", prices={"AMD": 149.0})
    a2 = controller.create_sell_approval(add_signal(run2, "S2", "AMD", "SELL", 149.0)["signal_id"])
    assert a1["result"] == a2["result"] == "SELL_PENDING_APPROVAL" and a2["approval_id"] == a1["approval_id"]
    assert len(db.q("SELECT * FROM approvals")) == 1


# ------------------------------------------------------------------ end to end on simulated data
def test_intraday_cycle_end_to_end_on_simulated_data(intraday_on, monkeypatch):
    from agents import trading_crew as tc
    monkeypatch.setenv("AABA_SYNTHETIC_NOW", "2026-09-30T15:12:00+00:00")             # 11:12 New York
    market_data.set_provider(None)
    res = tc.run_workflow("paper", "intraday", use_llm=False)
    assert res["status"] == "DONE", res
    sigs = db.q("SELECT * FROM signals WHERE run_id=?", (res["run_id"],))
    assert len(sigs) == 36 and all(s["outcome"] for s in sigs)
    ev = json.loads(sigs[0]["indicators"])
    assert "intraday" in ev and "vwap" in ev["intraday"]
    for o in db.q("SELECT * FROM orders WHERE run_id=?", (res["run_id"],)):
        assert o["client_order_id"].startswith("aaba-sig-")


def test_quiet_intraday_cycle_does_not_call_the_llm(monkeypatch, tmp_path):
    from scripted_llm import ScriptedLLM

    from agents import trading_crew as tc
    write_strategy(monkeypatch, tmp_path, max_buys_per_student_per_day=0)   # nothing can be a BUY; nothing held
    monkeypatch.setenv("AABA_SYNTHETIC_NOW", "2026-09-30T15:12:00+00:00")
    market_data.set_provider(None)
    llm = ScriptedLLM(model="scripted-test")
    monkeypatch.setattr(tc, "_build_llm", lambda: (llm, "scripted", "scripted-test", None))
    res = tc.run_workflow("paper", "intraday", use_llm=True)
    assert res["status"] == "DONE" and llm.calls == 0 and res["reports"] == []


# ------------------------------------------------------------------ scheduling
@pytest.mark.parametrize("now,expect", [("2026-10-16T14:30:00+00:00", "ran"),          # 10:30
                                        ("2026-10-16T13:40:00+00:00", "outside"),     # 09:40
                                        ("2026-10-16T19:55:00+00:00", "outside"),     # 15:55
                                        ("2026-10-17T15:00:00+00:00", "closed")])     # Saturday
def test_intraday_cycles_only_inside_the_session_window(intraday_on, monkeypatch, now, expect):
    import agents.trading_crew as tc
    from aaba import scheduler
    monkeypatch.setenv("AABA_SYNTHETIC_NOW", now)
    monkeypatch.delenv("AABA_SYNTHETIC_MARKET_OPEN", raising=False)
    market_data.set_provider(None)
    monkeypatch.setattr(tc, "run_workflow", lambda mode, trig: {"run_id": "r", "status": "DONE"})
    out = scheduler.intraday_job()
    assert (out == "DONE") if expect == "ran" else (expect in out), out


def test_scheduler_registers_ten_minute_cycles(intraday_on, monkeypatch):
    from apscheduler.schedulers.blocking import BlockingScheduler

    from aaba import scheduler
    jobs = {}
    monkeypatch.setattr(BlockingScheduler, "start", lambda self: jobs.update(
        {j.id: (j.max_instances, str(j.trigger)) for j in self.get_jobs()}))
    scheduler.start()
    assert jobs["intraday"][0] == 1 and "minute='*/10'" in jobs["intraday"][1] and "hour='9-15'" in jobs["intraday"][1]


def test_intraday_off_by_default():
    assert settings.load_strategy().intraday.enabled is False
