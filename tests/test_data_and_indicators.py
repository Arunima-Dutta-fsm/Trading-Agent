"""Batch market data, freshness and indicator handling."""
from datetime import datetime
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
from conftest import NOW, indicator_frame

from aaba import clock, controller, db, market_data
from aaba.indicators import add_indicators, clean_bars, ema, rsi
from aaba.market_data import Session, drop_incomplete_bars
from aaba.settings import DataCfg, IndicatorCfg, load_strategy, load_students
from aaba.strategy import evaluate_symbol


def test_alpaca_bars_use_one_batch_request_for_all_symbols(monkeypatch):
    import alpaca.data.historical as hist
    import alpaca.trading.client as tclient
    fake_data = MagicMock()
    idx = pd.MultiIndex.from_tuples([("AAA", pd.Timestamp("2026-10-12", tz="UTC")),
                                     ("BBB", pd.Timestamp("2026-10-12", tz="UTC"))], names=["symbol", "timestamp"])
    fake_data.get_stock_bars.return_value = MagicMock(df=pd.DataFrame(
        {"open": [1, 2], "high": [1, 2], "low": [1, 2], "close": [1, 2], "volume": [1, 2]}, index=idx))
    monkeypatch.setattr(hist, "StockHistoricalDataClient", lambda *a, **k: fake_data)
    monkeypatch.setattr(tclient, "TradingClient", lambda *a, **k: MagicMock())
    p = market_data.AlpacaMarketData()
    syms = load_students().all_symbols + ["SPY"]
    res = p.daily_bars(syms, DataCfg())
    assert fake_data.get_stock_bars.call_count == 1
    req = fake_data.get_stock_bars.call_args[0][0]
    assert sorted(req.symbol_or_symbols) == sorted(set(syms))       # all 37 symbols in one request
    assert res.batch_calls == 1 and set(res.bars["symbol"]) == {"AAA", "BBB"}


def test_sip_refusal_falls_back_to_iex(monkeypatch):
    import alpaca.data.historical as hist
    import alpaca.trading.client as tclient
    fake = MagicMock()
    ok = MagicMock(df=pd.DataFrame())
    fake.get_stock_bars.side_effect = [RuntimeError("subscription does not permit querying recent SIP data"), ok]
    monkeypatch.setattr(hist, "StockHistoricalDataClient", lambda *a, **k: fake)
    monkeypatch.setattr(tclient, "TradingClient", lambda *a, **k: MagicMock())
    res = market_data.AlpacaMarketData().daily_bars(["AAA"], DataCfg(feed="sip"))
    assert res.feed == "iex" and any("fell back" in n for n in res.notes)


def test_prepare_market_data_fetches_all_symbols_once():
    calls = []

    class Counting(market_data.SyntheticMarketData):
        def daily_bars(self, symbols, cfg, end=None):
            calls.append(list(symbols))
            return super().daily_bars(symbols, cfg, end)

    market_data.set_provider(Counting(now=NOW, market_open=True))
    run = db.create_run("analysis", "test", "test", None, None, True)
    out = controller.prepare_market_data(run)
    again = controller.prepare_market_data(run)
    assert len(calls) == 1 and len(calls[0]) == 37
    assert out["symbols_loaded"] == 37 and out["data_fresh"] is True
    assert again["reused_cached_data"] is True
    snap = db.q1("SELECT * FROM market_snapshots WHERE run_id=?", (run,))
    assert snap["batch_calls"] == 1 and snap["expected_bar_date"] == "2026-10-12"


def test_unfinished_bar_is_dropped_during_session():
    now_ny = clock.to_ny(NOW)
    sess = [Session(now_ny.date(), now_ny.replace(hour=9, minute=30), now_ny.replace(hour=16, minute=0))]
    bars = pd.DataFrame({"timestamp": [pd.Timestamp("2026-10-12", tz="America/New_York").tz_convert("UTC"),
                                       pd.Timestamp("2026-10-13", tz="America/New_York").tz_convert("UTC")]})
    out, n = drop_incomplete_bars(bars, sess, now_ny)
    assert n == 1 and len(out) == 1
    after_close = now_ny.replace(hour=17)
    out2, n2 = drop_incomplete_bars(bars, sess, after_close)
    assert n2 == 0 and len(out2) == 2


def test_stale_data_turns_every_action_into_hold():
    market_data.set_provider(market_data.SyntheticMarketData(now=NOW, market_open=True))
    run = db.create_run("paper", "test", "test", None, None, True)
    controller.prepare_market_data(run)
    controller._RUNS[run].fresh = False
    controller.evaluate_student(run, "S1")
    sigs = db.student_signals("S1", run_id=run)
    assert all(s["action"] == "HOLD" for s in sigs)
    assert db.q("SELECT * FROM orders") == []


def test_clean_bars_removes_invalid_rows():
    df = pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=5, tz="UTC"),
                       "open": [1, 2, np.nan, 4, 5], "high": [1, 2, 3, 4, 5], "low": [1, 2, 3, 5, -1],
                       "close": [1, 2, 3, 4, 5], "volume": [1, 1, 1, 1, 1]})
    clean, probs = clean_bars(df)
    assert len(clean) == 2      # NaN open, high<low and negative low removed
    assert probs


def test_indicators_match_hand_calculation_and_bounds():
    cfg = IndicatorCfg()
    rng = np.random.default_rng(1)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 300))))
    e = ema(close, 20)
    manual = close.iloc[:20].copy()
    alpha = 2 / 21
    val = close.iloc[0]
    for x in close.iloc[1:25]:
        val = alpha * x + (1 - alpha) * val
    assert abs(e.iloc[24] - val) < 1e-9 and e.iloc[:19].isna().all()
    r = rsi(close, 14).dropna()
    assert ((r >= 0) & (r <= 100)).all()
    bars = pd.DataFrame({"timestamp": pd.date_range("2025-01-01", periods=300, tz="UTC"), "open": close,
                         "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1e6})
    out = add_indicators(bars, cfg)
    for col in ["EMA20", "EMA50", "EMA200", "RSI14", "MACD", "MACD_SIGNAL", "MACD_HIST", "ATR14",
                "BB_LOWER", "BB_MIDDLE", "BB_UPPER", "OBV", "OBV_AVG20"]:
        assert col in out
    assert out["EMA200"].iloc[:199].isna().all() and pd.notna(out["EMA200"].iloc[-1])
    assert (out["BB_UPPER"].dropna() >= out["BB_LOWER"].dropna()).all()


def test_missing_indicator_value_gives_hold_not_buy():
    cfg = load_strategy()
    df = indicator_frame(RSI14=np.nan)
    sig = evaluate_symbol(df, "TEST", "satellite", cfg)
    assert sig.action == "HOLD" and sig.data_quality == "insufficient"
    short = indicator_frame(n=50)
    assert evaluate_symbol(short, "TEST", "core", cfg).data_quality == "insufficient"
    assert evaluate_symbol(pd.DataFrame(), "TEST", "core", cfg).data_quality == "invalid"
