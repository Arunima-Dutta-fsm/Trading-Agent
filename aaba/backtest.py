"""Walk-forward backtest, kept completely separate from order execution.

- About two years of daily bars (plus 200+ extra bars so EMA200 is ready), fetched for
  the student's 12 stocks and SPY in one batch request.
- First 18 months: try a small grid of settings and keep the one with the best Sharpe.
- Last 6 months: test the chosen settings (and the default settings) on data the
  tuning never saw.
- Signals use strategy.evaluate_symbol(), the same code as live trading. A signal at
  day t's close is filled at day t+1's open, with 0.05% slippage each way.
- Differences from live trading, stated plainly: no news or earnings filters (no
  reliable history), SELL signals are assumed approved, whole shares only.
- Compared with buy-and-hold of the same 12 stocks (same dollar allocations) and SPY.

Results are written to reports/backtests/. Nothing here can place an order.
"""
from __future__ import annotations

import itertools
import json
import math
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from . import clock, paths
from .indicators import add_indicators, clean_bars, colnames
from .market_data import drop_incomplete_bars, get_provider
from .risk import allocation_for
from .settings import DataCfg, StrategyConfig, StudentConfig, load_strategy, load_students
from .strategy import Position, evaluate_symbol

SLIPPAGE = 0.0005
GRID = {"satellite.rsi_min": [45, 50, 55], "core.rsi_oversold": [30, 35, 40], "risk.stop_atr_mult": [1.5, 2.0, 3.0]}


@dataclass
class Metrics:
    total_return_pct: float
    max_drawdown_pct: float
    sharpe: float
    win_rate_pct: float | None
    trades: int
    buys: int

    def as_dict(self):
        return self.__dict__.copy()


def _metrics(equity: pd.Series, closed: list[float], buys: int) -> Metrics:
    if equity.empty:
        return Metrics(0, 0, 0, None, 0, 0)
    rets = equity.pct_change().dropna()
    dd = (equity / equity.cummax() - 1).min() * 100
    sharpe = float(rets.mean() / rets.std() * math.sqrt(252)) if rets.std() > 0 else 0.0
    wins = [p for p in closed if p > 0]
    return Metrics(round(float(equity.iloc[-1] / equity.iloc[0] - 1) * 100, 2), round(float(dd), 2), round(sharpe, 2),
                   round(len(wins) / len(closed) * 100, 1) if closed else None, len(closed), buys)


def _with_params(cfg: StrategyConfig, params: dict) -> StrategyConfig:
    d = cfg.model_dump()
    for k, v in params.items():
        sec, field = k.split(".")
        d[sec][field] = v
    return StrategyConfig(**d)


def simulate(frames: dict[str, pd.DataFrame], student: StudentConfig, cfg: StrategyConfig,
             dates: list[pd.Timestamp]) -> tuple[pd.Series, list[float], int, list[dict]]:
    """frames: symbol -> indicator frame indexed 0..n with 'timestamp'. dates: the trading days to simulate."""
    cash = student.starting_capital
    pos: dict[str, dict] = {}
    equity, closed, buys, log = [], [], 0, []
    idx = {s: {ts: i for i, ts in enumerate(f["timestamp"])} for s, f in frames.items()}
    pending: list[tuple[str, str, int]] = []   # (symbol, side, qty) to fill at next open
    cap = student.starting_capital * cfg.risk.max_position_pct / 100
    for day in dates:
        # 1) fill yesterday's decisions at today's open
        for sym, side, qty in pending:
            i = idx[sym].get(day)
            if i is None:
                continue
            o = float(frames[sym]["open"].iloc[i])
            if side == "buy":
                px = o * (1 + SLIPPAGE)
                if qty * px > cash:
                    qty = int(cash // px)
                if qty <= 0:
                    continue
                p = pos.setdefault(sym, {"qty": 0, "avg": 0.0, "hi": 0.0})
                p["avg"] = (p["qty"] * p["avg"] + qty * px) / (p["qty"] + qty)
                p["qty"] += qty
                p["hi"] = max(p["hi"], px)
                cash -= qty * px
                buys += 1
                log.append({"date": str(day.date()), "symbol": sym, "side": "buy", "qty": qty, "price": round(px, 2)})
            elif sym in pos and pos[sym]["qty"] > 0:
                px = o * (1 - SLIPPAGE)
                p = pos.pop(sym)
                cash += p["qty"] * px
                closed.append((px - p["avg"]) * p["qty"])
                log.append({"date": str(day.date()), "symbol": sym, "side": "sell", "qty": p["qty"],
                            "price": round(px, 2), "pnl": round((px - p["avg"]) * p["qty"], 2)})
        pending = []
        # 2) decide at today's close
        value = cash
        for sym in student.symbols:
            i = idx.get(sym, {}).get(day)
            if i is None:
                continue
            f = frames[sym]
            close = float(f["close"].iloc[i])
            p = pos.get(sym)
            if p:
                p["hi"] = max(p["hi"], close)
                value += p["qty"] * close
            mv = p["qty"] * close if p else 0.0
            alloc = allocation_for(student.category(sym), cfg)
            position = Position(qty=p["qty"] if p else 0, avg_entry=p["avg"] if p else None, market_value=mv,
                                highest_close_since_entry=p["hi"] if p else None, room_value=min(alloc, cap) - mv)
            sig = evaluate_symbol(f.iloc[: i + 1], sym, student.category(sym), cfg, position)
            if sig.action == "SELL":
                pending.append((sym, "sell", 0))
            elif sig.action == "BUY":
                atr = float(f[colnames(cfg.indicators)["atr"]].iloc[i])
                room = min(position.room_value, cash)
                q_room = int(room // close) if room > 0 else 0
                q_risk = int(student.starting_capital * cfg.risk.risk_per_trade_pct / 100 // (cfg.risk.stop_atr_mult * atr)) if atr > 0 else 0
                qty = min(q_room, q_risk)
                if qty >= 1 and qty * close >= cfg.risk.min_order_value:
                    pending.append((sym, "buy", qty))
        equity.append((day, value))
    eq = pd.Series([v for _, v in equity], index=[d for d, _ in equity])
    return eq, closed, buys, log


def buy_and_hold(frames, student: StudentConfig, cfg: StrategyConfig, dates) -> pd.Series:
    cash = student.starting_capital
    shares = {}
    first = dates[0]
    for sym in student.symbols:
        f = frames.get(sym)
        if f is None:
            continue
        row = f[f["timestamp"] == first]
        if row.empty:
            continue
        px = float(row["open"].iloc[0]) * (1 + SLIPPAGE)
        q = int(allocation_for(student.category(sym), cfg) // px)
        shares[sym] = q
        cash -= q * px
    vals = []
    for d in dates:
        v = cash
        for sym, q in shares.items():
            f = frames[sym]
            row = f[f["timestamp"] == d]
            if not row.empty:
                v += q * float(row["close"].iloc[0])
        vals.append(v)
    return pd.Series(vals, index=dates)


def run_backtest(student_id: str, tune: bool = True) -> dict:
    from .demo import guard
    guard("backtests")
    cfg = load_strategy()
    student = load_students().get(student_id)
    provider = get_provider()
    data = provider.daily_bars(student.symbols + ["SPY"], DataCfg(history_days=1150, feed=cfg.data.feed))
    mclock = provider.market_clock()
    sessions = provider.sessions(mclock.timestamp.date() - timedelta(days=10), mclock.timestamp.date())
    bars, _ = drop_incomplete_bars(data.bars, sessions, mclock.timestamp)   # never use today's unfinished bar
    frames = {}
    for sym, g in bars.groupby("symbol"):
        clean, _ = clean_bars(g)
        frames[sym] = add_indicators(clean, cfg.indicators).reset_index(drop=True)
    spy = frames.get("SPY")
    if spy is None:
        raise RuntimeError("SPY data missing")
    # common trading days where EMA200 exists for SPY; keep the last ~2 years
    ready = spy[spy[colnames(cfg.indicators)["ema_slow"]].notna()]["timestamp"].tolist()
    window = ready[-504:]
    if len(window) < 300:
        raise RuntimeError(f"only {len(window)} usable days; need about 504 for an 18+6 month split")
    split = len(window) - 126
    tune_days, test_days = window[:split], window[split:]

    results = {"student_id": student_id, "student": student.label, "data_source": data.source,
               "simulated_data": provider.simulated, "batch_requests": data.batch_calls,
               "tuning_period": [str(tune_days[0].date()), str(tune_days[-1].date())],
               "test_period": [str(test_days[0].date()), str(test_days[-1].date())],
               "slippage_each_way": SLIPPAGE, "notes": [
                   "No news or earnings filters in the backtest (no reliable history).",
                   "SELL signals are assumed approved; live trading needs the student's approval.",
                   "Signals at day t close, filled at day t+1 open."]}
    best_params, grid_rows = {}, []
    if tune:
        best = None
        keys = list(GRID)
        for combo in itertools.product(*[GRID[k] for k in keys]):
            params = dict(zip(keys, combo))
            eq, closed, buys, _ = simulate(frames, student, _with_params(cfg, params), tune_days)
            m = _metrics(eq, closed, buys)
            grid_rows.append({**params, **m.as_dict()})
            key = (m.sharpe, m.total_return_pct)
            if best is None or key > best[0]:
                best = (key, params)
        best_params = best[1]
    results["tuning_grid"] = grid_rows
    results["chosen_params"] = best_params

    out = {}
    for label, c in (("default_settings", cfg), ("tuned_settings", _with_params(cfg, best_params))):
        if label == "tuned_settings" and not best_params:
            continue
        eq, closed, buys, log = simulate(frames, student, c, test_days)
        out[label] = {"metrics": _metrics(eq, closed, buys).as_dict(), "trades": log[-60:],
                      "equity": [round(v, 2) for v in eq.tolist()]}
    bh = buy_and_hold(frames, student, cfg, test_days)
    spy_eq = spy.set_index("timestamp").loc[test_days, "close"]
    spy_eq = spy_eq / spy_eq.iloc[0] * student.starting_capital
    out["buy_and_hold"] = {"metrics": _metrics(bh, [], 0).as_dict(), "equity": [round(v, 2) for v in bh.tolist()]}
    out["spy"] = {"metrics": _metrics(spy_eq, [], 0).as_dict(), "equity": [round(v, 2) for v in spy_eq.tolist()]}
    results["test_results"] = out
    results["test_dates"] = [str(d.date()) for d in test_days]

    paths.BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    stamp = clock.now_utc().strftime("%Y%m%d-%H%M")
    jpath = paths.BACKTEST_DIR / f"backtest_{student_id}_{stamp}.json"
    jpath.write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    md = [f"# Backtest - {student.label}", ""]
    if provider.simulated:
        md += ["> **SIMULATED DATA**: these numbers come from made-up prices and mean nothing.", ""]
    md += [f"- Tuning: {results['tuning_period'][0]} to {results['tuning_period'][1]}; "
           f"test: {results['test_period'][0]} to {results['test_period'][1]}",
           f"- Chosen settings (best Sharpe in tuning): {best_params or 'tuning skipped'}", "",
           "| Test period (last 6 months) | Return % | Max drawdown % | Sharpe | Win rate % | Closed trades | Buys |",
           "|---|---|---|---|---|---|---|"]
    for k, v in out.items():
        m = v["metrics"]
        md.append(f"| {k} | {m['total_return_pct']} | {m['max_drawdown_pct']} | {m['sharpe']} | "
                  f"{m['win_rate_pct'] if m['win_rate_pct'] is not None else '-'} | {m['trades']} | {m['buys']} |")
    md += ["", "Notes:"] + [f"- {n}" for n in results["notes"]]
    md.append("- A good backtest does not prove the strategy will make money in the next 15-20 days.")
    (paths.BACKTEST_DIR / f"backtest_{student_id}_{stamp}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    results["files"] = [str(jpath), str(jpath.with_suffix(".md"))]
    return results


if __name__ == "__main__":  # pragma: no cover
    import sys
    print(json.dumps(run_backtest(sys.argv[1] if len(sys.argv) > 1 else "S2"), indent=1, default=str)[:3000])
