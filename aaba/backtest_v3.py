"""Tune engine v3 on the OLD window, freeze it, then evaluate ONCE on the RECENT window.

    python -m aaba.backtest_v3            (or: python main.py backtest-v3)

1. Tuning window 15 Oct 2025 - 16 Apr 2026: every combination in GRID is simulated for all three
   students; the combination with the best combined Sharpe ratio (ties: higher return) is chosen.
2. The chosen settings are written to configs/v3_frozen.json together with the whole tuning table.
3. Evaluation window 17 Apr - 15 Oct 2026: the frozen settings run once, with v2, the daily strategy,
   buy-and-hold of the same stocks and SPY on the same days and the same cost assumptions. Nothing
   from this window is used to choose anything.
All runs use the intraday simulator in aaba/backtest_intraday.py (completed candles only, fills at the
next candle's open + 5 bps, SELLs after a 30-minute approval delay, SEC/FINRA sell fees).
"""
from __future__ import annotations

import itertools
import json
from datetime import date

import pandas as pd

from . import backtest as daily_bt
from . import clock, paths
from .backtest_intraday import cached_data, simulate_intraday, window_days
from .settings import load_strategy, load_students

TUNE = ("tuning", date(2025, 10, 15), date(2026, 4, 16))
EVAL = ("evaluation", date(2026, 4, 17), date(2026, 10, 15))
GRID = {"confirm_days": [2, 3], "stop_scale": [1.0, 1.5], "loss_cap_atr": [5.0, 8.0], "cooldown_days": [5, 10],
        "max_tilt": [0.25, 0.5]}
FROZEN = paths.CONFIG_DIR / "v3_frozen.json"
SLIP = daily_bt.SLIPPAGE


def _cfg(engine: str, adaptive: dict | None = None):
    base = load_strategy()
    upd = {"intraday": base.intraday.model_copy(update={"enabled": True, "engine": engine})}
    if adaptive:
        upd["adaptive"] = base.adaptive.model_copy(update=adaptive)
    return base.model_copy(update=upd)


def _m(eq: pd.Series, closed=None, buys=0) -> dict:
    return daily_bt._metrics(eq.reset_index(drop=True), closed or [], buys).as_dict()


def _combined(series: list[pd.Series]) -> pd.Series:
    return sum(s.reset_index(drop=True) for s in series)


def run(approval_delay_min: int = 30, grid: dict | None = None, frozen_path=None, tune: tuple = TUNE,
        evaluate: tuple = EVAL) -> dict:
    from .demo import guard
    guard("backtests")
    grid = grid or GRID
    frozen_file = frozen_path or FROZEN
    group = load_students()
    symbols = sorted(set(group.all_symbols) | {"SPY"})
    cfg3 = _cfg("v3")
    daily, intra, meta = cached_data(cfg3, symbols, tune[1], evaluate[2])
    tune_days, _ = window_days(daily, intra, tune[1], tune[2])
    eval_days, eval_ts = window_days(daily, intra, evaluate[1], evaluate[2])
    if not tune_days or not eval_days:
        raise RuntimeError("no intraday data for the tuning or evaluation window")

    # ---------------------------------------------------------------- 1. tune on the OLD window only
    table = []
    keys = list(grid)
    for combo in itertools.product(*[grid[k] for k in keys]):
        params = dict(zip(keys, combo))
        c = _cfg("v3", params)
        eqs = [simulate_intraday(daily, intra, st, c, tune_days, approval_delay_min, engine="v3")["equity"]
               for st in group.students]
        m = _m(_combined(eqs))
        table.append({**params, "sharpe": m["sharpe"], "return_pct": m["total_return_pct"],
                      "max_drawdown_pct": m["max_drawdown_pct"]})
    best = max(table, key=lambda r: (r["sharpe"], r["return_pct"]))
    chosen = {k: best[k] for k in keys}

    # ---------------------------------------------------------------- 2. freeze
    frozen = {"frozen_at": clock.iso(), "tuning_window": [str(tune[1]), str(tune[2])],
              "evaluation_window": [str(evaluate[1]), str(evaluate[2])], "selection": "best combined Sharpe on the tuning "
              "window (ties: higher return)", "chosen": chosen, "tuning_score": best, "grid": grid, "table": table}
    frozen_file.write_text(json.dumps(frozen, indent=1), encoding="utf-8")

    # ---------------------------------------------------------------- 3. evaluate ONCE on the RECENT window
    c3, c2 = _cfg("v3", json.loads(frozen_file.read_text(encoding="utf-8"))["chosen"]), _cfg("v2")
    base = load_strategy()
    out = {"meta": meta | {"approval_delay_min": approval_delay_min, "cost_bps_each_way": c3.intraday.cost_bps},
           "frozen": frozen, "students": {}}
    sums: dict[str, list] = {}
    for st in group.students:
        r3 = simulate_intraday(daily, intra, st, c3, eval_days, approval_delay_min, engine="v3")
        r2 = simulate_intraday(daily, intra, st, c2, eval_days, approval_delay_min, engine="v2")
        frames = {s: daily[s] for s in st.symbols if s in daily}
        eq_d, closed_d, buys_d, log_d = daily_bt.simulate(frames, st, base, eval_ts)
        bh = daily_bt.buy_and_hold(frames, st, base, eval_ts)
        spy = daily["SPY"].set_index("timestamp").loc[eval_ts, "close"]
        spy = spy / spy.iloc[0] * st.starting_capital
        daily_cost = sum(t["qty"] * t["price"] for t in log_d) * SLIP
        bh_cost = st.starting_capital * SLIP                         # one entry, no exit
        row = {
            "v3_frozen": {**r3["metrics"], **{k: r3["stats"][k] for k in (
                "buys", "adds", "sells", "fees", "slippage", "costs_total", "avg_invested_pct")}},
            "v2": {**r2["metrics"], **{k: r2["stats"][k] for k in (
                "buys", "adds", "sells", "fees", "slippage", "costs_total", "avg_invested_pct")}},
            "daily_strategy": {**_m(eq_d), "costs_total": round(daily_cost, 2), "trades": len(log_d)},
            "buy_and_hold": {**_m(bh), "costs_total": round(bh_cost, 2)},
            "spy": {**_m(spy), "costs_total": round(st.starting_capital * SLIP, 2)},
            "v3_trades_sample": r3["trades"][:30]}
        out["students"][st.student_id] = row
        for k, eq in (("v3_frozen", r3["equity"]), ("v2", r2["equity"]), ("daily_strategy", eq_d),
                      ("buy_and_hold", bh), ("spy", spy)):
            sums.setdefault(k, []).append(eq)
    out["combined"] = {k: _m(_combined(v)) for k, v in sums.items()}
    for k in ("v3_frozen", "v2", "daily_strategy", "buy_and_hold", "spy"):
        out["combined"][k]["costs_total"] = round(sum(out["students"][s][k]["costs_total"]
                                                      for s in out["students"]), 2)
    paths.BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    f = paths.BACKTEST_DIR / f"backtest_v3_{clock.now_utc().strftime('%Y%m%d-%H%M')}.json"
    f.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    out["file"] = str(f)
    return out


if __name__ == "__main__":  # pragma: no cover
    r = run()
    fz = r["frozen"]
    print("chosen on the tuning window:", fz["chosen"], "| tuning Sharpe", fz["tuning_score"]["sharpe"],
          "return", fz["tuning_score"]["return_pct"])
    print(f"EVALUATION {fz['evaluation_window']} (run once with the frozen settings)")
    for k, m in r["combined"].items():
        print(f"  combined {k:<15} return {m['total_return_pct']:>7}%  maxDD {m['max_drawdown_pct']:>7}%  "
              f"Sharpe {m['sharpe']:>5}  costs ${m['costs_total']:,.0f}")
    print("saved:", r["file"], "| frozen:", FROZEN)
