"""The intraday backtest runs end to end on simulated data and compares every strategy."""
from datetime import date

from aaba import market_data
from aaba.backtest_intraday import _sell_fees, run_comparison


def test_sell_fees_are_charged_and_capped():
    assert round(_sell_fees(100, 100.0), 4) == round(100 * 100 * 27.80 / 1e6 + 100 * 0.000166, 4)
    assert _sell_fees(1_000_000, 1.0) - 1_000_000 * 27.80 / 1e6 == 8.30          # TAF capped


def test_intraday_backtest_compares_strategies_on_simulated_data(monkeypatch):
    monkeypatch.setenv("AABA_SYNTHETIC_NOW", "2026-10-13T21:00:00+00:00")
    market_data.set_provider(None)
    r = run_comparison(windows=[("short", date(2026, 10, 5), date(2026, 10, 9))])
    w = r["windows"]["short"]
    assert w["sessions"] == 5 and set(w["students"]) == {"S1", "S2", "S3"}
    assert set(w["combined"]) == {"intraday_strategy", "daily_strategy", "buy_and_hold", "spy"}
    s = w["students"]["S2"]["intraday_strategy"]
    assert {"total_return_pct", "max_drawdown_pct", "sharpe", "buys", "adds", "sell_requests", "fees"} <= set(s)
    assert r["meta"]["simulated"] is True
    for t in w["students"]["S2"]["trades_sample"]:
        assert t["side"] in ("buy", "sell") and t["price"] > 0
