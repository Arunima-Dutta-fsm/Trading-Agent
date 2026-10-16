"""Engine v3: stock-specific profiles, confirmed exits, deliberate holding, cooldown, and the
tune-on-old / evaluate-on-new protocol."""
import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from aaba import adaptive as ad
from aaba import market_data
from aaba.indicators import add_indicators, colnames
from aaba.intraday import PositionPlan
from aaba.portfolio import Holding
from aaba.settings import load_strategy, load_students

COLS = colnames(load_strategy().indicators)


def cfg3(**adaptive):
    c = load_strategy()
    return c.model_copy(update={"intraday": c.intraday.model_copy(update={"enabled": True, "engine": "v3"}),
                                "adaptive": c.adaptive.model_copy(update=adaptive)})


def frame(drift=0.002, vol=0.01, n=300, seed=1, revert=False):
    rng = np.random.default_rng(seed)
    if revert:                                   # zig-zag around a slowly rising mean
        r = np.array([vol if i % 2 else -vol for i in range(n)]) + drift + rng.normal(0, vol / 4, n)
    else:
        r = rng.normal(drift, vol, n)
    close = 100 * np.exp(np.cumsum(r))
    ts = pd.bdate_range(end="2026-10-12", periods=n, tz="America/New_York").tz_convert("UTC")
    df = pd.DataFrame({"timestamp": ts, "open": close, "high": close * (1 + vol), "low": close * (1 - vol),
                       "close": close, "volume": 1e6})
    return add_indicators(df, load_strategy().indicators)


# ------------------------------------------------------------------ profiles are stock-specific
def test_profiles_differ_by_behaviour():
    trend = ad.profile(frame(0.004, 0.006), COLS, cfg3())
    choppy = ad.profile(frame(0.0005, 0.02, seed=3, revert=True), COLS, cfg3())
    assert trend["style"] == "trend" and choppy["style"] in ("mean_revert", "steady")
    assert choppy["autocorr"] < trend["autocorr"]
    assert 3.0 <= trend["stop_atr"] <= 7.0 and 3.0 <= choppy["stop_atr"] <= 7.0


def test_stop_scale_widens_the_stock_specific_stop():
    f = frame(0.002, 0.02, seed=5)
    assert ad.profile(f, COLS, cfg3(stop_scale=1.5))["stop_atr"] >= ad.profile(f, COLS, cfg3(stop_scale=1.0))["stop_atr"]


# ------------------------------------------------------------------ decisions
PROF = {"style": "trend", "normal_pullback_atr": 3.5, "stop_atr": 4.0, "efficiency": 0.4, "autocorr": 0.0,
        "ema50_slope": 1.0, "ema200": 80.0, "last_closes": [100.0] * 10, "high20": 101.0}
MOM = {"atr": 2.0, "atr_pct": 0.02, "ema50": 95.0, "r20": 0.03, "r60": 0.08, "r120": 0.15, "score": 5.0,
       "close": 100.0}
TGT = {"target_value": 10000.0, "weight": 0.10, "rank": 2, "why": "target 10.0% (trend stock, rank 2, regime bull)",
       "momentum": MOM, "profile": PROF, "veto": False}
BAR = {"close": 100.0, "vwap": 100.1, "i_ema9": 100.0, "i_ema21": 99.9, "i_ema21_slope": 0.01, "i_rsi": 55.0,
       "lower_lows6": 1}
OK = {"known": True, "change_pct": 0.1, "below_vwap": False}
HELD = PositionPlan(qty=100, avg_cost=95.0, anchor_stop=85.0, entries=1)


def run(target=None, plan=None, hold=None, bar=None, days_since_exit=None, **cfg):
    return ad.decide_v3("X", cfg3(**cfg), target or TGT, {**BAR, **(bar or {})}, plan or PositionPlan(),
                        hold or Holding(), OK, days_since_exit)


def tgt(**prof):
    return {**TGT, "profile": {**PROF, **prof}}


def test_normal_pullback_is_held_deliberately():
    # 3 ATR below the high: inside this stock's normal 3.5-ATR pullback and above its 4-ATR stop
    d = run(target=tgt(last_closes=[94.0] * 10), plan=HELD, hold=Holding(value=9400, highest_close=100.0))
    assert d.action == "HOLD" and "holding deliberately" in d.reasons[0]


def test_one_close_below_the_stop_is_not_enough():
    # 1 of the last 2 closes below the trailing stop (100 - 4 x 2 = 92)
    d = run(target=tgt(last_closes=[95.0] * 8 + [93.0, 91.0], ema50_slope=-1.0), plan=HELD,
            hold=Holding(value=9100, highest_close=100.0))
    assert d.action != "SELL"


def test_confirmed_break_with_weakening_trend_sells():
    d = run(target=tgt(last_closes=[95.0] * 8 + [91.5, 91.0], ema50_slope=-1.0), plan=HELD,
            hold=Holding(value=9100, highest_close=100.0))
    assert d.action == "SELL" and "trend deterioration" in d.reasons[0]


def test_confirmed_break_but_trend_still_rising_is_held():
    d = run(target=tgt(last_closes=[95.0] * 8 + [91.5, 91.0], ema50_slope=2.0), plan=HELD,
            hold=Holding(value=9100, highest_close=100.0))
    assert d.action != "SELL"


def test_loss_cap_always_exits():
    d = run(target=tgt(last_closes=[90.0] * 9 + [84.0], ema50_slope=5.0), plan=HELD,
            hold=Holding(value=8400, highest_close=100.0), bar={"close": 84.0})
    assert d.action == "SELL" and "loss cap" in d.reasons[0]
    crash = run(plan=HELD, hold=Holding(value=8200, highest_close=100.0), bar={"close": 82.0})
    assert crash.action == "SELL" and "intraday" in crash.reasons[0]


def test_weak_stock_is_exited_on_evidence():
    weak = {**TGT, "momentum": {**MOM, "r120": -0.1}, "profile": {**PROF, "last_closes": [79.0] * 10}}
    d = run(target=weak, plan=PositionPlan(qty=100, avg_cost=82.0, anchor_stop=70.0, entries=1),
            hold=Holding(value=7900, highest_close=81.0))
    assert d.action == "SELL" and "weak stock" in d.reasons[0]


def test_cooldown_blocks_quick_reentry_unless_new_high():
    blocked = run(days_since_exit=2)
    assert blocked.action == "HOLD" and "cooldown" in blocked.reasons[0]
    breakout = run(target=tgt(last_closes=[100.0] * 9 + [102.0], high20=102.0), days_since_exit=2)
    assert breakout.action == "BUY"


def test_mean_reverting_stock_bought_only_at_or_below_vwap():
    mr = tgt(style="mean_revert")
    assert run(target=mr, bar={"close": 100.3, "vwap": 100.0}).action == "HOLD"
    assert run(target=mr, bar={"close": 99.9, "vwap": 100.0}).action == "BUY"


def test_entry_uses_the_loss_cap_as_stop():
    d = run()
    assert d.action == "BUY" and d.stop == pytest.approx(100.0 - 5.0 * 2.0)


# ------------------------------------------------------------------ targets: holding has hysteresis
def test_held_stock_keeps_its_place_when_entry_rules_no_longer_hold():
    st = load_students().get("S2")
    frames = {s: frame(0.002, 0.01, seed=i) for i, s in enumerate(st.symbols)}
    # AMD: flat recent trend (no new entry) but still above EMA200
    frames["AMD"] = frame(0.0002, 0.01, seed=42)
    regime = {"label": "bull", "gross": 0.98}
    t_new = ad.compute_targets(st, frames, {}, COLS, cfg3(), regime, held=set())
    t_held = ad.compute_targets(st, frames, {}, COLS, cfg3(), regime, held={"AMD"})
    assert t_held["AMD"]["hold_ok"]
    if not t_new["AMD"]["eligible"]:
        assert t_held["AMD"]["eligible"]
    tot = sum(v["weight"] for v in t_held.values())
    assert tot <= 0.98 + 1e-6 and all(v["weight"] <= 0.15 + 1e-9 for v in t_held.values())


# ------------------------------------------------------------------ the tuning protocol
def test_tune_freeze_evaluate_protocol_on_simulated_data(monkeypatch, tmp_path):
    from aaba import backtest_v3
    monkeypatch.setenv("AABA_SYNTHETIC_NOW", "2026-10-13T21:00:00+00:00")
    market_data.set_provider(None)
    frozen = tmp_path / "v3_frozen.json"
    r = backtest_v3.run(grid={"confirm_days": [2, 3], "stop_scale": [1.0], "loss_cap_atr": [5.0],
                              "cooldown_days": [5], "max_tilt": [0.5]}, frozen_path=frozen,
                        tune=("tuning", date(2026, 9, 14), date(2026, 9, 25)),
                        evaluate=("evaluation", date(2026, 9, 28), date(2026, 10, 9)))
    fz = json.loads(frozen.read_text())
    assert len(fz["table"]) == 2 and fz["chosen"]["confirm_days"] in (2, 3)
    assert fz["tuning_window"] == ["2026-09-14", "2026-09-25"]
    assert set(r["combined"]) == {"v3_frozen", "v2", "daily_strategy", "buy_and_hold", "spy"}
    for sid in ("S1", "S2", "S3"):
        row = r["students"][sid]["v3_frozen"]
        assert {"total_return_pct", "max_drawdown_pct", "sharpe", "costs_total", "fees", "slippage"} <= set(row)


def test_v3_cycle_end_to_end(monkeypatch, tmp_path):
    from test_intraday import write_strategy

    from agents import trading_crew as tc
    from aaba import db
    write_strategy(monkeypatch, tmp_path, engine="v3")
    monkeypatch.setenv("AABA_SYNTHETIC_NOW", "2026-09-30T15:12:00+00:00")
    market_data.set_provider(None)
    res = tc.run_workflow("paper", "intraday", use_llm=False)
    assert res["status"] == "DONE", res["error"]
    sigs = db.q("SELECT * FROM signals WHERE run_id=?", (res["run_id"],))
    assert len(sigs) == 36 and all(s["outcome"] for s in sigs)
    ev = json.loads(sigs[0]["indicators"])
    assert ev["v2"]["engine"] == "v3" and ev["v2"]["style"] in ("trend", "mean_revert", "steady")