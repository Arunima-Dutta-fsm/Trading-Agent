"""Engine v2: regime-scaled, momentum-ranked, volatility-weighted targets; trend-following exits;
risk checks against the target; news scoring."""
import numpy as np
import pandas as pd
import pytest
from conftest import NOW

from aaba import portfolio as pf
from aaba.indicators import add_indicators, colnames
from aaba.intraday import PositionPlan
from aaba.news import recency_weight, score_text
from aaba.risk import BuyContext, check_buy
from aaba.settings import load_strategy, load_students


def cfg_v2(**portfolio):
    c = load_strategy()
    return c.model_copy(update={"intraday": c.intraday.model_copy(update={"enabled": True, "engine": "v2"}),
                                "portfolio": c.portfolio.model_copy(update=portfolio)})


COLS = colnames(load_strategy().indicators)


def series(drift: float, vol: float = 0.01, n: int = 260, start: float = 100.0, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    ts = pd.bdate_range(end="2026-10-12", periods=n, tz="America/New_York").tz_convert("UTC")
    df = pd.DataFrame({"timestamp": ts, "open": close, "high": close * (1 + vol), "low": close * (1 - vol),
                       "close": close, "volume": 1e6})
    return add_indicators(df, load_strategy().indicators)


# ------------------------------------------------------------------ momentum and regime
def test_momentum_ranks_strong_uptrends_and_rejects_downtrends():
    up, down = pf.momentum(series(0.003), COLS, [0.5, 0.3, 0.2]), pf.momentum(series(-0.003), COLS, [0.5, 0.3, 0.2])
    assert up["trend_ok"] and up["raw"] > 0 and up["score"] > 0
    assert not down["trend_ok"] and down["raw"] < 0
    assert pf.momentum(series(0.003, n=100), COLS, [0.5, 0.3, 0.2]) is None          # not enough history


def test_regime_sets_how_much_is_invested():
    cfg = cfg_v2()
    uni = [f"S{i}" for i in range(10)]
    bull = {"SPY": series(0.002), **{s: series(0.002, seed=i) for i, s in enumerate(uni)}}
    bear = {"SPY": series(-0.002), **{s: series(-0.002, seed=i) for i, s in enumerate(uni)}}
    rb, rr = pf.market_regime(bull, uni, COLS, cfg), pf.market_regime(bear, uni, COLS, cfg)
    assert rb["label"] == "bull" and rb["gross"] == 0.95
    assert rr["label"] == "bear" and rr["gross"] == 0.25


def test_weights_respect_the_cap_and_the_budget():
    w = pf._cap_weights({"A": 10, "B": 1, "C": 1}, 0.40, 0.15)
    assert max(w.values()) <= 0.15 + 1e-9 and abs(sum(w.values()) - 0.40) < 1e-9
    w2 = pf._cap_weights({"A": 1, "B": 1}, 0.60, 0.15)                 # cannot place more than 2 x 15%
    assert abs(sum(w2.values()) - 0.30) < 1e-9


def test_targets_use_cash_keep_the_40_60_split_and_skip_weak_or_bad_news():
    st = load_students().get("S2")
    frames = {s: series(0.002 + 0.0003 * i, vol=0.01 + 0.002 * (i % 3), seed=i) for i, s in enumerate(st.symbols)}
    frames["TSLA"] = series(-0.003, seed=99)                              # downtrend: excluded
    regime = {"label": "bull", "gross": 0.95}
    news = {"GOOGL": {"score": -0.8, "articles": 3}}                     # strong negative news: excluded
    t = pf.compute_targets(st, frames, news, COLS, cfg_v2(), regime)
    assert t["TSLA"]["weight"] == 0 and "uptrend" in t["TSLA"]["why"]
    assert t["GOOGL"]["weight"] == 0 and t["GOOGL"]["why"] == "news veto"
    held = [s for s, v in t.items() if v["weight"] > 0]
    assert 0 < len(held) <= 9
    sat = sum(v["weight"] for s, v in t.items() if st.category(s) == "satellite")
    core = sum(v["weight"] for s, v in t.items() if st.category(s) == "core")
    assert sat <= 0.95 * 0.40 + 1e-6 and core <= 0.95 * 0.60 + 1e-6
    assert sat + core > 0.80                                              # most of the capital put to work
    assert all(v["weight"] <= 0.15 + 1e-9 for v in t.values())


# ------------------------------------------------------------------ intraday v2 decision
BAR = {"close": 100.0, "vwap": 100.2, "i_ema9": 100.0, "i_ema21": 99.9, "i_ema21_slope": 0.01, "i_rsi": 55.0,
       "lower_lows6": 1}
MOM = {"atr": 2.0, "atr_pct": 0.02, "ema50": 92.0, "r20": 0.05, "r60": 0.1, "r120": 0.2}
TARGET = {"target_value": 12000.0, "weight": 0.12, "rank": 1, "why": "target 12.0% (core, rank 1, regime bull)",
          "momentum": MOM}
OK = {"known": True, "change_pct": 0.1, "below_vwap": False}


def v2(bar=None, target=None, plan=None, hold=None, daily_close=101.0, market=None):
    return pf.decide_v2("X", cfg_v2(), target or TARGET, daily_close, {**BAR, **(bar or {})}, plan or PositionPlan(),
                        hold or pf.Holding(), market or OK)


def test_buys_the_whole_gap_at_or_below_vwap_and_half_above():
    d = v2()
    assert d.action == "BUY" and d.evidence["buy_value"] == 12000.0 and d.stop == pytest.approx(93.0)
    above = v2(bar={"close": 100.6})
    assert above.action == "BUY" and above.evidence["buy_value"] == 6000.0


def test_no_buy_when_at_target_or_target_is_zero():
    assert v2(hold=pf.Holding(value=11000.0)).action == "HOLD"                      # within the 25% band
    assert v2(target={**TARGET, "target_value": 0.0, "why": "momentum not positive"}).action == "HOLD"


@pytest.mark.parametrize("bar,market,why", [
    ({"close": 99.0, "vwap": 100.5, "i_ema9": 99.2, "i_ema21": 99.8, "i_ema21_slope": -0.2, "lower_lows6": 5}, None,
     "falling hard"),
    ({"close": 102.0}, None, "stretched"),
    ({"i_rsi": 85.0}, None, "stretched"),
    (None, {"known": True, "change_pct": -1.5, "below_vwap": True}, "weak market"),
])
def test_buy_postponed_when_timing_is_bad(bar, market, why):
    d = v2(bar=bar, market=market)
    assert d.action == "HOLD" and why in d.reasons[0]


def test_trend_following_exits_not_tight_stops():
    plan = PositionPlan(qty=100, avg_cost=95.0, anchor_stop=88.0, entries=1)
    # a normal pullback inside the chandelier is NOT a sell
    assert v2(plan=plan, hold=pf.Holding(value=10000, highest_close=104.0), daily_close=99.0).action != "SELL"
    # close below highest close - 3 x ATR -> SELL request
    s = v2(plan=plan, hold=pf.Holding(value=10000, highest_close=110.0), daily_close=103.0)
    assert s.action == "SELL" and "chandelier" in s.reasons[0]
    # trend exit below EMA50 - 0.5 x ATR
    assert v2(plan=plan, hold=pf.Holding(value=10000, highest_close=100.0), daily_close=90.5).action == "SELL"
    # emergency intraday stop
    assert v2(plan=plan, bar={"close": 87.0}, hold=pf.Holding(value=8700, highest_close=100.0)).action == "SELL"
    # momentum gone
    gone = {**TARGET, "target_value": 0.0, "weight": 0.0, "why": "momentum not positive"}
    assert v2(target=gone, plan=plan, hold=pf.Holding(value=10000, highest_close=100.0)).action == "SELL"
    # waiting approval: no repeat
    waiting = PositionPlan(**{**plan.__dict__, "sell_pending": True})
    assert v2(target=gone, plan=waiting, hold=pf.Holding(value=10000, highest_close=100.0)).action == "HOLD"


def test_lower_rank_alone_never_sells():
    plan = PositionPlan(qty=100, avg_cost=95.0, anchor_stop=88.0, entries=1)
    dropped = {**TARGET, "target_value": 0.0, "weight": 0.0, "why": "eligible but ranked 11 (holds the best 9)"}
    assert v2(target=dropped, plan=plan, hold=pf.Holding(value=10000, highest_close=101.0)).action == "HOLD"


# ------------------------------------------------------------------ risk checks against the target
def _ctx(**kw):
    st = load_students().get("S2")
    base = dict(student=st, cfg=cfg_v2(), symbol="KO", category="core", signal_action="BUY", data_quality="ok",
                data_fresh=True, atr=2.0, orders_allowed=True, asset={"status": "active", "tradable": True},
                account={"cash": 100000, "equity": 100000, "last_equity": 100000}, positions={}, open_orders=[],
                latest_price=100.0, price_time=NOW, now=NOW, market_open=True, already_bought_today=True,
                buys_this_run=0, intraday=True, entry_kind="initial", stop=93.0, target_value=12000.0,
                buy_value=12000.0)
    base.update(kw)
    return BuyContext(**base)


def test_v2_risk_check_sizes_to_the_target_not_the_fixed_allocation():
    r = check_buy(_ctx())
    assert r.passed and 115 <= r.qty <= 120                         # ~$12,000 at $100 incl. costs (> $7,500 core allocation)
    half = check_buy(_ctx(buy_value=6000.0))
    assert 55 <= half.qty <= 60


def test_v2_risk_check_keeps_the_hard_limits():
    assert "15% cap" in check_buy(_ctx(target_value=30000.0, buy_value=30000.0,
                                       positions={"KO": {"qty": 150, "avg_entry": 100, "market_value": 15000}})).summary()
    assert not check_buy(_ctx(account={"cash": 50, "equity": 100000, "last_equity": 100000})).passed
    assert "daily loss halt" in check_buy(_ctx(account={"cash": 90000, "equity": 94000, "last_equity": 100000})).summary()
    risky = check_buy(_ctx(stop=60.0))                              # very wide stop: risk budget limits size
    assert risky.qty <= int(2000 // 40)


# ------------------------------------------------------------------ end to end (simulated data, MCP server)
def test_v2_cycle_end_to_end(monkeypatch, tmp_path):
    import json

    from test_intraday import write_strategy

    from agents import trading_crew as tc
    from aaba import db, market_data
    write_strategy(monkeypatch, tmp_path, engine="v2")
    monkeypatch.setenv("AABA_SYNTHETIC_NOW", "2026-09-30T15:12:00+00:00")
    market_data.set_provider(None)
    res = tc.run_workflow("paper", "intraday", use_llm=False)
    assert res["status"] == "DONE", res["error"]
    sigs = db.q("SELECT * FROM signals WHERE run_id=?", (res["run_id"],))
    assert len(sigs) == 36 and all(s["outcome"] for s in sigs)
    ev = json.loads(sigs[0]["indicators"])
    assert ev["v2"]["engine"] == "v2" and ev["regime"]["label"] in ("bull", "neutral", "bear")
    assert db.q1("SELECT * FROM events WHERE kind='regime' AND run_id=?", (res["run_id"],))
    for o in db.q("SELECT * FROM orders WHERE run_id=?", (res["run_id"],)):
        assert o["client_order_id"].startswith("aaba-sig-") and o["side"] == "buy"


# ------------------------------------------------------------------ news
def test_news_phrases_and_recency():
    assert score_text("Company raises guidance, beats estimates") == 1.0
    assert score_text("Analyst cuts price target after the company lowers guidance") == -1.0
    assert recency_weight(0) == 1.0 and recency_weight(24) == 0.5 and recency_weight(48) == 0.25
