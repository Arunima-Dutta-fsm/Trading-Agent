"""Signal rules from the proposal, on hand-made indicator values."""
from datetime import date

from conftest import indicator_frame

from aaba.settings import load_strategy
from aaba.strategy import Position, evaluate_symbol

SAT_BUY = dict(close=100.0, EMA20=99.0, EMA50=97.0, EMA200=90.0, RSI14=60.0, MACD_HIST=[-0.2, 0.3],
               OBV=2e7, OBV_AVG20=1e7)


def frame(**kw):
    df = indicator_frame()
    for k, v in kw.items():
        vals = v if isinstance(v, list) else [v]
        df.loc[df.index[-len(vals):], k] = vals
    return df


def test_satellite_momentum_buy():
    s = evaluate_symbol(frame(**SAT_BUY), "NVDA", "satellite", load_strategy())
    assert s.action == "BUY" and "momentum entry" in s.reasons[0]


def test_satellite_no_buy_when_rsi_too_high():
    kw = dict(SAT_BUY, RSI14=75.0)
    s = evaluate_symbol(frame(**kw), "NVDA", "satellite", load_strategy())
    assert s.action == "HOLD" and "RSI" in s.reasons[0]


def test_satellite_sell_when_held_and_below_ema20():
    kw = dict(SAT_BUY, close=98.0, EMA20=99.0)
    s = evaluate_symbol(frame(**kw), "NVDA", "satellite", load_strategy(), Position(qty=10, avg_entry=95.0))
    assert s.action == "SELL" and "EMA20" in s.reasons[0]


def test_never_sell_what_is_not_held():
    cfg = load_strategy()
    # exit conditions everywhere (below EMA20, far below a would-be stop) but nothing is held
    kw = dict(SAT_BUY, close=80.0, EMA20=99.0, RSI14=40.0)
    s = evaluate_symbol(frame(**kw), "NVDA", "satellite", cfg, Position(qty=0))
    assert s.action == "HOLD"
    s2 = evaluate_symbol(frame(close=120.0, BB_UPPER=105.0, RSI14=80.0), "KO", "core", cfg, Position(qty=0))
    assert s2.action == "HOLD"


def test_hard_stop_and_trailing_stop():
    cfg = load_strategy()
    s = evaluate_symbol(frame(close=90.0, EMA20=89.0, ATR14=2.0), "NVDA", "satellite", cfg,
                        Position(qty=5, avg_entry=100.0, highest_close_since_entry=100.0))
    assert s.action == "SELL" and any("stop" in r for r in s.reasons)


def test_core_mean_reversion_buy_and_exit():
    cfg = load_strategy()
    buy = frame(close=[94.0, 96.5], BB_LOWER=[95.0, 95.0], RSI14=[33.0, 38.0], EMA200=90.0)
    s = evaluate_symbol(buy, "KO", "core", cfg)
    assert s.action == "BUY" and "mean-reversion" in s.reasons[0]
    exit_ = frame(close=106.0, BB_UPPER=105.0, RSI14=60.0, EMA200=90.0)
    s2 = evaluate_symbol(exit_, "KO", "core", cfg, Position(qty=10, avg_entry=100.0))
    assert s2.action == "SELL" and "upper band" in s2.reasons[0]


def test_sell_wins_over_buy_when_held():
    cfg = load_strategy()
    df = frame(close=[94.0, 96.5], BB_LOWER=[95.0, 95.0], RSI14=[33.0, 38.0], EMA200=90.0, ATR14=2.0)
    s = evaluate_symbol(df, "KO", "core", cfg, Position(qty=5, avg_entry=110.0))   # 2xATR stop at 106
    assert s.action == "SELL"


def test_news_veto_and_earnings_blackout_turn_buy_into_hold():
    cfg = load_strategy()
    df = frame(**SAT_BUY)
    s = evaluate_symbol(df, "NVDA", "satellite", cfg, news={"score": -0.8, "articles": 3})
    assert s.action == "HOLD" and "news veto" in s.reasons[0]
    s2 = evaluate_symbol(df, "NVDA", "satellite", cfg, earnings={"next_earnings": "2026-10-14"},
                         today=date(2026, 10, 13))
    assert s2.action == "HOLD" and "earnings" in s2.reasons[0]
    s3 = evaluate_symbol(df, "NVDA", "satellite", cfg, news={"score": 0.9, "articles": 2})
    assert s3.action == "BUY"


def test_full_position_gives_hold():
    s = evaluate_symbol(frame(**SAT_BUY), "NVDA", "satellite", load_strategy(), Position(qty=0, room_value=0))
    assert s.action == "HOLD" and "allocation" in s.reasons[0]


def test_composite_mode():
    cfg = load_strategy().model_copy(update={"signal_mode": "composite"})
    s = evaluate_symbol(frame(**SAT_BUY), "NVDA", "satellite", cfg)
    assert s.score is not None and s.action in ("BUY", "HOLD")
    assert s.evidence["composite"]["score"] == s.score
