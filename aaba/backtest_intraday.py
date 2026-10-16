"""Intraday backtest: the 10-minute decision engine (aaba/intraday.decide) on real historical
5-minute candles, compared with the existing daily strategy, buy-and-hold and SPY.

Realism, stated plainly:
- Decisions every `cycle_minutes` from `first_cycle` to `last_cycle` New York time, using only
  candles that had CLOSED by the decision time, and daily indicators up to the previous session.
- BUYs fill at the NEXT candle's open + `cost_bps` (slippage/spread). SELLs are approval requests:
  they fill `approval_delay_min` later (default 30 min) at that candle's open - `cost_bps`, or at the
  next session's open if that falls after the close. US sell-side regulatory fees are charged
  (SEC fee 27.80 USD per million, FINRA TAF 0.000166 USD/share up to 8.30 USD). Alpaca charges no
  commission.
- Sizing and limits follow the live risk checks: initial-entry fraction of the allocation, the
  allocation and 15% cap, cash (no margin), minimum order, anchored-stop risk budget, entries per
  position, spacing, per-cycle and per-day BUY limits, the 5% daily loss halt.
- Not modelled: news and earnings vetoes (no reliable history), partial fills, an approval that
  the student rejects or that is invalidated by the 5% price-move check.
- No parameter is fitted to the test windows: the settings are the documented defaults.
Nothing here can place an order.
"""
from __future__ import annotations

import bisect
import json
import math
from datetime import datetime, time as dtime, timedelta

import pandas as pd

from . import backtest as daily_bt
from . import clock, paths
from .indicators import add_indicators, clean_bars, colnames
from .intraday import PositionPlan, add_intraday_features, daily_context, decide, market_context, risk_qty
from .market_data import drop_incomplete_bars, get_provider
from .risk import allocation_for
from .settings import DataCfg, StrategyConfig, StudentConfig, load_strategy, load_students
from .strategy import Position, evaluate_symbol

SEC_FEE = 27.80 / 1_000_000
TAF_PER_SHARE, TAF_MAX = 0.000166, 8.30


def _sell_fees(qty: float, px: float) -> float:
    return qty * px * SEC_FEE + min(qty * TAF_PER_SHARE, TAF_MAX)


def load_data(cfg: StrategyConfig, symbols: list[str], start_day, end_day, provider=None):
    """Daily frames with indicators (enough history for EMA200) and intraday features, in two
    batch requests."""
    provider = provider or get_provider()
    data = provider.daily_bars(symbols, DataCfg(history_days=1150, feed=cfg.data.feed))
    mclock = provider.market_clock()
    sessions = provider.sessions(mclock.timestamp.date() - timedelta(days=10), mclock.timestamp.date())
    bars, _ = drop_incomplete_bars(data.bars, sessions, mclock.timestamp)
    daily = {}
    for sym, g in bars.groupby("symbol"):
        clean, _ = clean_bars(g)
        daily[sym] = add_indicators(clean, cfg.indicators).reset_index(drop=True)
    ic = cfg.intraday
    s = datetime.combine(start_day - timedelta(days=10), dtime(0), tzinfo=clock.NY)
    e = datetime.combine(end_day + timedelta(days=1), dtime(0), tzinfo=clock.NY)
    feed = "sip" if not getattr(provider, "simulated", False) else "synthetic"
    ib = provider.intraday_bars(symbols, ic.bar_minutes, 30, feed, start=s, end=min(e, clock.now_utc()))
    intra = {}
    for sym, g in ib.bars.groupby("symbol"):
        f = add_intraday_features(g, ic.bar_minutes)
        by_day = {}
        for day, gd in f.groupby("session"):
            by_day[day] = (list(gd["timestamp"]), gd.to_dict("records"))
        intra[sym] = by_day
    return daily, intra, {"daily_source": data.source, "intraday_feed": ib.feed, "intraday_rows": len(ib.bars),
                          "daily_batch_requests": data.batch_calls, "intraday_batch_requests": ib.batch_calls}


def _last_closed(day_data, t_utc, bar_minutes):
    """Index of the newest candle that had closed by t_utc (or None)."""
    ts, _ = day_data
    cutoff = pd.Timestamp(t_utc) - pd.Timedelta(minutes=bar_minutes)
    i = bisect.bisect_right(ts, cutoff) - 1
    return i if i >= 0 else None


def _bar_at_or_after(day_data, t_utc):
    ts, rows = day_data
    i = bisect.bisect_left(ts, pd.Timestamp(t_utc))
    return rows[i] if i < len(rows) else None


def simulate_intraday(daily: dict, intra: dict, student: StudentConfig, cfg: StrategyConfig, days: list,
                      approval_delay_min: int = 30, engine: str = "v1") -> dict:
    from . import portfolio as pf
    ic, rk, pc = cfg.intraday, cfg.risk, cfg.portfolio
    universe = load_students().all_symbols
    cols = colnames(cfg.indicators)
    cost = ic.cost_bps / 10_000
    cap_val = student.starting_capital * rk.max_position_pct / 100
    cash = student.starting_capital
    pos: dict[str, dict] = {}
    pending_sell: dict[str, tuple] = {}        # sym -> (execute_at_utc, reason)
    equity, closed, log, invested = [], [], [], []
    stats = {"buys": 0, "adds": 0, "sell_requests": 0, "sells": 0, "fees": 0.0}
    didx = {s: {ts.date() if hasattr(ts, "date") else ts: i for i, ts in
                enumerate(f["timestamp"].dt.tz_convert("America/New_York"))} for s, f in daily.items()}
    spy_daily = daily.get("SPY")
    last_px: dict[str, float] = {}

    def mark():
        return cash + sum(p["qty"] * last_px.get(s, p["avg"]) for s, p in pos.items())

    def fill_sell(sym, row, why):
        nonlocal cash
        p = pos.pop(sym)
        px = float(row["open"]) * (1 - cost)
        fee = _sell_fees(p["qty"], px)
        cash += p["qty"] * px - fee
        stats["fees"] += fee
        stats["sells"] += 1
        pnl = (px - p["avg"]) * p["qty"] - fee
        closed.append(pnl)
        log.append({"t": str(row["timestamp"]), "symbol": sym, "side": "sell", "qty": p["qty"], "price": round(px, 2),
                    "pnl": round(pnl, 2), "why": why[:120]})

    for day in days:
        d = day.date() if hasattr(day, "date") else day
        # SELLs approved overnight fill at today's first candle
        for sym, (at, why) in list(pending_sell.items()):
            dd = intra.get(sym, {}).get(d)
            if dd and pd.Timestamp(at) <= pd.Timestamp(dd[0][0]) and sym in pos:
                fill_sell(sym, dd[1][0], why)
                pending_sell.pop(sym)
        # daily context and daily signal from bars up to the PREVIOUS session
        ctxs, dsig = {}, {}
        for sym in student.symbols:
            f, i = daily.get(sym), didx.get(sym, {}).get(d)
            if f is None or i is None or i < 2:
                continue
            hist = f.iloc[:i]
            p = pos.get(sym)
            mv = p["qty"] * float(hist["close"].iloc[-1]) if p else 0.0
            alloc = allocation_for(student.category(sym), cfg)
            position = Position(qty=p["qty"] if p else 0, avg_entry=p["avg"] if p else None, market_value=mv,
                                highest_close_since_entry=p.get("hi") if p else None, room_value=min(alloc, cap_val) - mv)
            dsig[sym] = evaluate_symbol(hist, sym, student.category(sym), cfg, position)
            ctxs[sym] = daily_context(hist, cols)
        targets = {}
        if engine == "v2":
            hist_all = {s: daily[s].iloc[:didx[s][d]] for s in set(universe) | {"SPY"}
                        if s in daily and didx[s].get(d)}
            regime = pf.market_regime(hist_all, universe, cols, cfg)
            targets = pf.compute_targets(student, hist_all, {}, cols, cfg, regime)
            stats.setdefault("regimes", {})
            stats["regimes"][regime["label"]] = stats["regimes"].get(regime["label"], 0) + 1
        spy_prev = None
        if spy_daily is not None and didx["SPY"].get(d):
            spy_prev = float(spy_daily["close"].iloc[didx["SPY"][d] - 1])
        start_equity, buys_today = mark(), 0
        t = datetime.combine(d, dtime(*map(int, ic.first_cycle.split(":"))), tzinfo=clock.NY)
        t_end = datetime.combine(d, dtime(*map(int, ic.last_cycle.split(":"))), tzinfo=clock.NY)
        while t <= t_end:
            tu = t.astimezone(clock.UTC)
            # approved SELLs due now
            for sym, (at, why) in list(pending_sell.items()):
                dd = intra.get(sym, {}).get(d)
                if dd and pd.Timestamp(at) <= pd.Timestamp(tu) and sym in pos:
                    row = _bar_at_or_after(dd, tu)
                    if row is not None:
                        fill_sell(sym, row, why)
                        pending_sell.pop(sym)
            spy_dd = intra.get("SPY", {}).get(d)
            si = _last_closed(spy_dd, tu, ic.bar_minutes) if spy_dd else None
            market = market_context(spy_dd[1][si] if si is not None else None, spy_prev)
            buys_cycle = 0
            halted = (mark() / start_equity - 1) * 100 <= -rk.daily_loss_halt_pct if start_equity else False
            for sym in student.symbols:
                dd = intra.get(sym, {}).get(d)
                if not dd or sym not in ctxs:
                    continue
                k = _last_closed(dd, tu, ic.bar_minutes)
                if k is None:
                    continue
                bar = dd[1][k]
                last_px[sym] = float(bar["close"])
                p = pos.get(sym)
                if p and engine == "v1":
                    p["hi"] = max(p.get("hi", 0), float(bar["close"]))     # v2 trails DAILY closes (end of day)
                plan = PositionPlan(qty=p["qty"] if p else 0, avg_cost=p["avg"] if p else None,
                                    anchor_stop=p["stop"] if p else None, entries=p["entries"] if p else 0,
                                    last_entry_price=p["last"] if p else None,
                                    minutes_since_last_entry=(tu - p["last_t"]).total_seconds() / 60 if p else None,
                                    buys_today_student=buys_today, sell_pending=sym in pending_sell)
                sig = dsig.get(sym)
                if engine == "v2":
                    daily_close = float(daily[sym]["close"].iloc[didx[sym][d] - 1])
                    hold = pf.Holding(value=(p["qty"] * float(bar["close"])) if p else 0.0,
                                      highest_close=p.get("hi") if p else None)
                    dec = pf.decide_v2(sym, cfg, targets.get(sym, {}), daily_close, bar, plan, hold, market)
                else:
                    dec = decide(sym, student.category(sym), cfg, ctxs[sym], bar, sig.action if sig else "HOLD",
                                 list(sig.reasons) if sig else [], [], market, plan)
                if dec.action == "SELL" and p and sym not in pending_sell:
                    pending_sell[sym] = (tu + timedelta(minutes=approval_delay_min), "; ".join(dec.reasons))
                    stats["sell_requests"] += 1
                elif dec.action == "BUY" and not halted and buys_cycle < rk.max_new_buys_per_run:
                    row = _bar_at_or_after(dd, tu)                 # the candle that opens at decision time
                    if row is None:
                        continue
                    px = float(row["open"]) * (1 + cost)
                    held_val = p["qty"] * px if p else 0.0
                    if engine == "v2":
                        tgt = targets.get(sym, {}).get("target_value", 0.0)
                        room = min(tgt - held_val, dec.evidence.get("buy_value") or 0.0)
                    else:
                        alloc = allocation_for(student.category(sym), cfg)
                        room = (alloc * ic.initial_entry_fraction if dec.kind == "initial" else alloc) - held_val
                    room = min(room, cap_val - held_val, cash)
                    q_room = int(room // px) if room > 0 else 0
                    q_risk = risk_qty(cfg, student.starting_capital, px, dec.stop, p["qty"] if p else 0,
                                      p["avg"] if p else None, "add" if engine == "v2" else dec.kind,
                                      budget_pct=pc.max_position_risk_pct if engine == "v2" else None)
                    qty = min(q_room, q_risk)
                    if qty < 1 or qty * px < rk.min_order_value or px <= dec.stop:
                        continue
                    if p:
                        p["avg"] = (p["qty"] * p["avg"] + qty * px) / (p["qty"] + qty)
                        p["qty"] += qty
                        p.update(entries=p["entries"] + 1, last=px, last_t=tu)
                        stats["adds"] += 1
                    else:
                        pos[sym] = {"qty": qty, "avg": px, "stop": dec.stop, "entries": 1, "last": px, "last_t": tu,
                                    "hi": px}
                        stats["buys"] += 1
                    cash -= qty * px
                    buys_today += 1
                    buys_cycle += 1
                    log.append({"t": str(row["timestamp"]), "symbol": sym, "side": "buy", "kind": dec.kind,
                                "qty": qty, "price": round(px, 2), "stop": round(dec.stop, 2),
                                "rr": round(dec.reward_risk or 0, 2), "why": dec.reasons[0][:120]})
            t += timedelta(minutes=ic.cycle_minutes)
        # end of day: mark at the last candle's close (v2: the trailing high uses daily closes)
        for sym in list(pos):
            dd = intra.get(sym, {}).get(d)
            if dd:
                last_px[sym] = float(dd[1][-1]["close"])
                if engine == "v2":
                    pos[sym]["hi"] = max(pos[sym].get("hi") or 0.0, last_px[sym])
        v = mark()
        invested.append(1 - cash / v if v else 0.0)
        equity.append((pd.Timestamp(d), v))
    eq = pd.Series([v for _, v in equity], index=[d for d, _ in equity])
    m = daily_bt._metrics(eq, closed, stats["buys"] + stats["adds"])
    return {"metrics": m.as_dict(), "stats": {**stats, "fees": round(stats["fees"], 2),
                                               "open_positions_at_end": len(pos),
                                               "avg_invested_pct": round(100 * sum(invested) / len(invested), 1)
                                               if invested else 0.0},
            "equity": eq, "trades": log}


def _series_metrics(eq: pd.Series) -> dict:
    return daily_bt._metrics(eq, [], 0).as_dict()


def run_comparison(windows: list[tuple] | None = None, approval_delay_min: int = 30) -> dict:
    """windows: [(label, first_day, last_day)] as dates; default = the last 126 sessions and the
    126 sessions before them."""
    from .demo import guard
    guard("backtests")
    base = load_strategy()
    cfg = base.model_copy(update={"intraday": base.intraday.model_copy(update={"enabled": True})})
    group = load_students()
    symbols = sorted(set(group.all_symbols) | {"SPY"})
    provider = get_provider()
    daily_spy =provider.daily_bars(["SPY"], DataCfg(history_days=400, feed=cfg.data.feed)).bars
    mclock = provider.market_clock()
    sessions = provider.sessions(mclock.timestamp.date() - timedelta(days=10), mclock.timestamp.date())
    daily_spy, _ = drop_incomplete_bars(daily_spy, sessions, mclock.timestamp)
    all_days = list(daily_spy["timestamp"].dt.tz_convert("America/New_York").dt.normalize())
    if windows is None:
        a, b = all_days[-126:], all_days[-252:-126]
        windows = [("recent 6 months", a[0].date(), a[-1].date()), ("previous 6 months", b[0].date(), b[-1].date())]
    first = min(w[1] for w in windows)
    last = max(w[2] for w in windows)
    import os
    import pickle
    cache = os.getenv("AABA_BACKTEST_CACHE", "").strip()       # optional local file: skip the long download
    key = (tuple(symbols), str(first), str(last), cfg.intraday.bar_minutes, bool(getattr(provider, "simulated", 0)))
    loaded = None
    if cache and os.path.exists(cache):
        with open(cache, "rb") as fh:
            c = pickle.load(fh)
        loaded = c["data"] if c.get("key") == key else None
    if loaded is None:
        loaded = load_data(cfg, symbols, first, last, provider)
        if cache:
            with open(cache, "wb") as fh:
                pickle.dump({"key": key, "data": loaded}, fh)
    daily, intra, meta = loaded
    out = {"meta": meta | {"simulated": bool(getattr(provider, "simulated", False)),
                           "approval_delay_min": approval_delay_min, "cost_bps_each_way": cfg.intraday.cost_bps,
                           "settings": cfg.intraday.model_dump()}, "windows": {}}
    for label, w0, w1 in windows:
        days = [d for d in daily["SPY"]["timestamp"].dt.tz_convert("America/New_York").dt.normalize()
                if w0 <= d.date() <= w1 and all(d.date() in intra.get(s, {}) for s in ("SPY",))]
        res = {"period": [str(days[0].date()), str(days[-1].date())], "sessions": len(days), "students": {}}
        sums = {}
        cfg_v2 = cfg.model_copy(update={"intraday": cfg.intraday.model_copy(update={"engine": "v2"})})
        for st in group.students:
            r_i = simulate_intraday(daily, intra, st, cfg, days, approval_delay_min, engine="v1")
            r_2 = simulate_intraday(daily, intra, st, cfg_v2, days, approval_delay_min, engine="v2")
            day_ts = [daily["SPY"]["timestamp"].iloc[i] for i, d in enumerate(daily["SPY"]["timestamp"].dt.tz_convert(
                "America/New_York").dt.normalize()) if d in days]
            frames = {s: daily[s] for s in st.symbols if s in daily}
            eq_d, closed_d, buys_d, _ = daily_bt.simulate(frames, st, base, day_ts)
            bh = daily_bt.buy_and_hold(frames, st, base, day_ts)
            spy = daily["SPY"].set_index("timestamp").loc[day_ts, "close"]
            spy = spy / spy.iloc[0] * st.starting_capital
            res["students"][st.student_id] = {
                "new_engine_v2": {**r_2["metrics"], **r_2["stats"]},
                "v2_trades_sample": r_2["trades"][:25],
                "intraday_strategy": {**r_i["metrics"], **r_i["stats"]},
                "daily_strategy": daily_bt._metrics(eq_d, closed_d, buys_d).as_dict(),
                "buy_and_hold": _series_metrics(bh), "spy": _series_metrics(spy),
                "trades_sample": r_i["trades"][:25]}
            for k, s in (("new_engine_v2", r_2["equity"].values), ("intraday_strategy", r_i["equity"].values),
                         ("daily_strategy", eq_d.values),
                         ("buy_and_hold", bh.values), ("spy", spy.values)):
                sums[k] = s if k not in sums else sums[k] + s
        idx = pd.Index(range(len(days)))
        res["combined"] = {k: _series_metrics(pd.Series(v, index=idx)) for k, v in sums.items()}
        out["windows"][label] = res
    paths.BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    stamp = clock.now_utc().strftime("%Y%m%d-%H%M")
    f = paths.BACKTEST_DIR / f"backtest_intraday_{stamp}.json"
    f.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    out["file"] = str(f)
    return out


if __name__ == "__main__":  # pragma: no cover
    r = run_comparison()
    for label, w in r["windows"].items():
        print(f"== {label}: {w['period'][0]} .. {w['period'][1]} ({w['sessions']} sessions)")
        for k, m in w["combined"].items():
            print(f"   combined {k:<18} return {m['total_return_pct']:>7}%  maxDD {m['max_drawdown_pct']:>7}%  "
                  f"Sharpe {m['sharpe']:>6}")
    print("saved:", r["file"])
