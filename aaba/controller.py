"""Decision and execution controller: the one place where signals become actions.

The MCP server exposes these functions as tools for the CrewAI agent. The
dashboard calls the same functions for approvals. There is no second copy of
this logic anywhere else.

Modes (stored on each run, so the LLM cannot change them):
  analysis  read-only: signals and risk checks are recorded, no orders, no approvals
  dry_run   full workflow, approvals are created, but no order is ever sent
  paper     BUYs are sent to the student's paper account after every check passes,
            SELLs are sent only after approval. Also needs ENABLE_PAPER_ORDERS=true.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from . import approvals, broker as brokers, clock, db, demo, paths
from .indicators import add_indicators, clean_bars, colnames
from .market_data import (MarketClock, Session, bar_session_date, drop_incomplete_bars, get_provider,
                          last_completed_session)
from .news import earnings_dates, news_sentiment
from .risk import BuyContext, SellContext, allocation_for, check_buy, check_sell
from .settings import (StrategyConfig, StudentConfig, load_strategy, load_students, paper_orders_enabled,
                       redact)
from .strategy import Position, evaluate_symbol

log = logging.getLogger("aaba.controller")
BENCHMARK = "SPY"


@dataclass
class RunState:
    run_id: str
    mode: str
    cfg: StrategyConfig
    clock: MarketClock
    sessions: list[Session]
    frames: dict[str, pd.DataFrame]
    fresh: bool
    expected_bar_date: str | None
    prices: dict = field(default_factory=dict)
    news: dict = field(default_factory=dict)
    earnings: dict = field(default_factory=dict)
    accounts: dict = field(default_factory=dict)
    symbol_problems: dict = field(default_factory=dict)
    provider: object = None
    # intraday mode: features of completed 5-minute candles per symbol, SPY's prior close
    intraday: dict = field(default_factory=dict)
    spy_prev_close: float | None = None
    intraday_fresh: bool = True
    intraday_note: str = ""
    regime: dict | None = None                # engine v2: market regime for this run (computed once)


_RUNS: dict[str, RunState] = {}


def _run_row(run_id: str) -> dict:
    r = db.get_run(run_id)
    if not r:
        raise ValueError(f"unknown run_id {run_id}")
    return r


# =========================================================================== market data
def prepare_market_data(run_id: str) -> dict:
    """Fetch daily bars for every student's stocks plus SPY in ONE batch request, then
    latest prices (one request), news (one request) and earnings (one request).
    Calculates indicators and checks freshness. Safe to call twice: the second call
    reuses what is already loaded for this run."""
    demo.guard("loading market data")
    if run_id in _RUNS:
        return _snapshot_summary(_RUNS[run_id], cached=True)
    run = _run_row(run_id)
    cfg = load_strategy()
    group = load_students()
    provider = get_provider()
    symbols = sorted(set(group.all_symbols) | {BENCHMARK})

    mclock = provider.market_clock()
    now_ny = mclock.timestamp
    sessions = provider.sessions(now_ny.date() - timedelta(days=20), now_ny.date())
    last_done = last_completed_session(sessions, now_ny)

    result = provider.daily_bars(symbols, cfg.data)
    bars = result.bars
    bars, dropped = drop_incomplete_bars(bars, sessions, now_ny)
    notes = list(result.notes)
    if dropped:
        notes.append(f"dropped {dropped} unfinished bar(s) from today's open session")

    frames: dict[str, pd.DataFrame] = {}
    problems: dict[str, str] = {}
    expected = None
    if last_done:
        done = [s for s in sessions if s.close <= now_ny]
        idx = max(0, len(done) - 1 - cfg.data.max_bar_age_sessions)
        expected = done[idx].date
    for sym in symbols:
        g = bars[bars["symbol"] == sym] if not bars.empty else bars
        if g.empty:
            problems[sym] = "no bars returned"
            continue
        clean, probs = clean_bars(g)
        if probs:
            notes.append(f"{sym}: {', '.join(probs)}")
        if clean.empty:
            problems[sym] = "all bars invalid"
            continue
        frames[sym] = add_indicators(clean, cfg.indicators)
        latest = bar_session_date(clean["timestamp"].iloc[-1])
        if expected and latest < expected:
            problems[sym] = f"stale: latest bar {latest}, expected {expected}"

    fresh = bool(expected) and not any(s in problems for s in group.all_symbols if s in symbols)
    prices = {}
    try:
        prices = provider.latest_prices([s for s in symbols])
    except Exception as e:
        notes.append(f"latest prices unavailable: {type(e).__name__}")
    news = news_sentiment(group.all_symbols, cfg.news)
    earn = earnings_dates(group.all_symbols, cfg.earnings, now_ny.date())
    accounts = brokers.verify_accounts(run_id)

    state = RunState(run_id, run["mode"], cfg, mclock, sessions, frames, fresh,
                     str(expected) if expected else None, prices, news, earn, accounts, problems, provider)
    if cfg.intraday.enabled:
        _load_intraday(state, symbols, notes)
    _RUNS[run_id] = state

    # save indicator CSVs (same columns as the old project) for audit and for get_latest_indicators
    out_dir = paths.INDICATOR_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    all_df = pd.concat(frames.values(), ignore_index=True) if frames else pd.DataFrame()
    csv_all = out_dir / "indicators_all.csv"
    if not all_df.empty:
        all_df.to_csv(csv_all, index=False)
        for st in group.students:
            part = all_df[all_df["symbol"].isin(st.symbols)]
            part.to_csv(out_dir / f"{st.student_id}_indicators.csv", index=False)
    latest_dates = [str(bar_session_date(f["timestamp"].iloc[-1])) for f in frames.values()]
    db.ex("INSERT OR REPLACE INTO market_snapshots(run_id, fetched_at, source, feed, latest_bar_date,"
          " expected_bar_date, fresh, symbols_ok, symbols_failed, batch_calls, csv_path, detail)"
          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
          (run_id, clock.iso(), result.source, result.feed, max(latest_dates) if latest_dates else None,
           state.expected_bar_date, int(fresh), len(frames), db.j(problems), result.batch_calls,
           str(csv_all.relative_to(paths.ROOT)) if csv_all.is_relative_to(paths.ROOT) else str(csv_all),
           db.j({"notes": notes, "market_open": mclock.is_open})))
    db.ex("UPDATE runs SET data_source=?, data_asof=? WHERE run_id=?",
          (f"{result.source}/{result.feed}", max(latest_dates) if latest_dates else None, run_id))
    db.log_event("INFO" if fresh else "WARNING", "market_data",
                 f"{len(frames)} symbols loaded in {result.batch_calls} batch request(s); fresh={fresh}",
                 run_id, None, {"problems": problems, "notes": notes})
    return _snapshot_summary(state, cached=False, notes=notes, batch_calls=result.batch_calls)


def _load_intraday(state: RunState, symbols: list[str], notes: list[str]) -> None:
    """One batch request for completed 5-minute candles of every symbol, then the features."""
    from .intraday import add_intraday_features
    ic = state.cfg.intraday
    try:
        res = state.provider.intraday_bars(symbols, ic.bar_minutes, ic.lookback_days, ic.feed)
    except Exception as e:  # noqa: BLE001
        state.intraday_fresh, state.intraday_note = False, f"intraday bars unavailable: {type(e).__name__}"
        notes.append(state.intraday_note)
        return
    bars = res.bars
    newest = None
    for sym in symbols:
        g = bars[bars["symbol"] == sym] if not bars.empty else bars
        if not g.empty:
            state.intraday[sym] = add_intraday_features(g, ic.bar_minutes)
            last = g["timestamp"].max() + pd.Timedelta(minutes=ic.bar_minutes)
            newest = last if newest is None else max(newest, last)
    spy = state.frames.get(BENCHMARK)
    state.spy_prev_close = float(spy["close"].iloc[-1]) if spy is not None and not spy.empty else None
    if state.clock.is_open:
        age = (pd.Timestamp(state.provider.now()) - newest).total_seconds() / 60 if newest is not None else None
        state.intraday_fresh = age is not None and age <= ic.max_bar_age_minutes
        state.intraday_note = (f"newest completed {ic.bar_minutes}-min candle closed {age:.0f} min ago"
                               if age is not None else "no intraday candles")
    else:
        state.intraday_fresh, state.intraday_note = False, "market closed: intraday decisions paused"
    notes.append(f"intraday ({res.feed}, {res.batch_calls} batch request): {state.intraday_note}")


def position_plan(student_id: str, symbol: str, live_qty: float, live_avg: float | None, now_utc) -> "PositionPlan":
    """Entries of the CURRENT position (BUY orders since the last filled SELL), its anchored stop and
    timing, read from the order history so it survives restarts."""
    import json
    from .intraday import PositionPlan
    live_bad = ("SUBMIT_ERROR", "PRECHECK_ERROR", "rejected", "canceled", "expired")
    last_sell = db.q1("SELECT MAX(submitted_at) AS t FROM orders WHERE student_id=? AND symbol=? AND side='sell'"
                      " AND filled_qty>0", (student_id, symbol))
    since = (last_sell or {}).get("t") or "1970-01-01"
    marks = ",".join("?" * len(live_bad))
    buys = db.q(f"SELECT o.*, s.indicators AS sig_ind FROM orders o LEFT JOIN signals s ON s.signal_id=o.signal_id"
                f" WHERE o.student_id=? AND o.symbol=? AND o.side='buy' AND o.submitted_at>? AND o.status NOT IN ({marks})"
                f" ORDER BY o.submitted_at", (student_id, symbol, since, *live_bad))
    plan = PositionPlan(qty=live_qty, avg_cost=live_avg, entries=len(buys) if live_qty > 0 else 0)
    if buys and live_qty > 0:
        first, last = buys[0], buys[-1]
        try:
            plan.anchor_stop = json.loads(first["sig_ind"] or "{}").get("stop")
        except ValueError:
            plan.anchor_stop = None
        plan.last_entry_price = last["filled_avg_price"] or last["est_price"]
        plan.minutes_since_last_entry = (now_utc - clock.parse_iso(last["submitted_at"])).total_seconds() / 60
    ny = clock.to_ny(now_utc)
    start = clock.iso(datetime(ny.year, ny.month, ny.day, tzinfo=clock.NY).astimezone(clock.UTC))
    plan.buys_today_student = (db.q1(f"SELECT COUNT(*) AS n FROM orders WHERE student_id=? AND side='buy' AND "
                                     f"submitted_at>=? AND status NOT IN ({marks})", (student_id, start, *live_bad))
                               or {}).get("n", 0)
    plan.sell_pending = bool(db.q1("SELECT approval_id FROM approvals WHERE student_id=? AND symbol=? AND status IN "
                                   "('PENDING','APPROVED','EXECUTING')", (student_id, symbol)))
    return plan


def _intraday_signal(s: RunState, student: StudentConfig, sym: str, cat: str, daily_sig, position, live) -> tuple:
    """(action, reasons, evidence) for one stock this cycle."""
    from . import intraday as intra
    cols = colnames(s.cfg.indicators)
    frame = s.frames.get(sym)
    ctx = intra.daily_context(frame, cols) if frame is not None else None
    feats = s.intraday.get(sym)
    bar = feats.iloc[-1] if feats is not None and not feats.empty else None
    if bar is not None and bar["session"] != s.clock.timestamp.date():
        bar = None                                                   # no candle from today yet
    spy = s.intraday.get(BENCHMARK)
    spy_bar = spy.iloc[-1] if spy is not None and not spy.empty and spy.iloc[-1]["session"] == s.clock.timestamp.date() \
        else None
    p = live.get(sym, {})
    plan = position_plan(student.student_id, sym, float(p.get("qty") or 0),
                         float(p["avg_entry"]) if p.get("avg_entry") else None, clock.now_utc())
    d = intra.decide(sym, cat, s.cfg, ctx, bar, daily_sig.action, list(daily_sig.reasons), list(daily_sig.blocked_by),
                     intra.market_context(spy_bar, s.spy_prev_close), plan)
    ev = dict(daily_sig.evidence)
    ev["intraday"] = d.evidence
    ev.update({"entry_kind": d.kind, "stop": d.stop, "target": d.target, "reward_risk": d.reward_risk,
               "intraday_price": d.price})
    return d.action, d.reasons, ev, d.price


def _v2_targets(s: RunState, student: StudentConfig, held: set[str] | None = None) -> dict:
    """Engines v2/v3: regime (once per run) and this student's targets."""
    from . import adaptive, portfolio
    cols = colnames(s.cfg.indicators)
    if s.regime is None:
        s.regime = portfolio.market_regime(s.frames, load_students().all_symbols, cols, s.cfg)
        db.log_event("INFO", "regime", f"market regime {s.regime['label']}: {s.regime['why']}", s.run_id)
    if s.cfg.intraday.engine == "v3":
        return adaptive.compute_targets(student, s.frames, s.news, cols, s.cfg, s.regime, held=held or set())
    return portfolio.compute_targets(student, s.frames, s.news, cols, s.cfg, s.regime)


def _sessions_since_exit(student_id: str, symbol: str, today) -> int | None:
    """Trading days (weekdays) since this stock was last sold, from the order history."""
    import numpy as np
    row = db.q1("SELECT MAX(submitted_at) AS t FROM orders WHERE student_id=? AND symbol=? AND side='sell' "
                "AND filled_qty>0", (student_id, symbol))
    if not row or not row.get("t"):
        return None
    sold = clock.to_ny(clock.parse_iso(row["t"])).date()
    return int(np.busday_count(sold, today))


def _v2_signal(s: RunState, student: StudentConfig, sym: str, daily_sig, position, live, targets) -> tuple:
    from . import intraday as intra
    from . import portfolio
    feats = s.intraday.get(sym)
    bar = feats.iloc[-1] if feats is not None and not feats.empty else None
    if bar is not None and bar["session"] != s.clock.timestamp.date():
        bar = None
    spy = s.intraday.get(BENCHMARK)
    spy_bar = spy.iloc[-1] if spy is not None and not spy.empty and spy.iloc[-1]["session"] == s.clock.timestamp.date() \
        else None
    p = live.get(sym, {})
    qty = float(p.get("qty") or 0)
    plan = position_plan(student.student_id, sym, qty, float(p["avg_entry"]) if p.get("avg_entry") else None,
                         clock.now_utc())
    frame = s.frames.get(sym)
    daily_close = float(frame["close"].iloc[-1]) if frame is not None and not frame.empty else 0.0
    price = float(bar["close"]) if bar is not None else daily_close
    hold = portfolio.Holding(value=qty * price, highest_close=position.highest_close_since_entry)
    market = intra.market_context(spy_bar, s.spy_prev_close)
    if s.cfg.intraday.engine == "v3":
        from . import adaptive
        d = adaptive.decide_v3(sym, s.cfg, targets.get(sym, {}), bar, plan, hold, market,
                               _sessions_since_exit(student.student_id, sym, s.clock.timestamp.date()))
    else:
        d = portfolio.decide_v2(sym, s.cfg, targets.get(sym, {}), daily_close, bar, plan, hold, market)
    ev = dict(daily_sig.evidence)
    ev["v2"] = d.evidence
    ev["regime"] = s.regime
    ev.update({"entry_kind": d.kind, "stop": d.stop, "target_value": (targets.get(sym) or {}).get("target_value"),
               "buy_value": d.evidence.get("buy_value"), "intraday_price": d.price})
    return d.action, d.reasons, ev, d.price


def _snapshot_summary(s: RunState, cached: bool, notes: list | None = None, batch_calls: int | None = None) -> dict:
    return {"run_id": s.run_id, "mode": s.mode, "market_open": s.clock.is_open,
            "market_time_ny": s.clock.timestamp.strftime("%Y-%m-%d %H:%M"),
            "expected_bar_date": s.expected_bar_date, "data_fresh": s.fresh,
            "symbols_loaded": len(s.frames), "symbol_problems": s.symbol_problems,
            "batch_requests_for_bars": batch_calls, "reused_cached_data": cached,
            "accounts": {k: {"ok": v["ok"], "problems": v["problems"]} for k, v in s.accounts.items()},
            "notes": (notes or [])[:8]}


def _state(run_id: str) -> RunState:
    if run_id not in _RUNS:
        prepare_market_data(run_id)
    return _RUNS[run_id]


# =========================================================================== positions
def _entry_info(student_id: str, symbol: str, frame: pd.DataFrame | None, avg_entry: float | None) -> float | None:
    """Highest close since the current position was opened (for the trailing stop)."""
    last_sell = db.q1("SELECT MAX(updated_at) AS t FROM orders WHERE student_id=? AND symbol=? AND side='sell'"
                      " AND filled_qty>0", (student_id, symbol))
    since = (last_sell or {}).get("t") or "1970-01-01"
    first_buy = db.q1("SELECT MIN(submitted_at) AS t FROM orders WHERE student_id=? AND symbol=? AND side='buy'"
                      " AND filled_qty>0 AND submitted_at>?", (student_id, symbol, since))
    if frame is None or frame.empty:
        return avg_entry
    if first_buy and first_buy.get("t"):
        start = pd.Timestamp(clock.parse_iso(first_buy["t"])).tz_convert("UTC").normalize()
        part = frame[frame["timestamp"] >= start]
        if not part.empty:
            return float(max(part["close"].max(), avg_entry or 0))
    return avg_entry


def build_positions(student: StudentConfig, cfg: StrategyConfig, live_positions: dict,
                    frames: dict[str, pd.DataFrame]) -> dict[str, Position]:
    out = {}
    for sym in student.symbols:
        p = live_positions.get(sym, {})
        qty = float(p.get("qty") or 0)
        mv = float(p.get("market_value") or 0)
        avg = float(p["avg_entry"]) if p.get("avg_entry") else None
        alloc = allocation_for(student.category(sym), cfg)
        cap = student.starting_capital * cfg.risk.max_position_pct / 100
        out[sym] = Position(qty=qty, avg_entry=avg, market_value=mv,
                            highest_close_since_entry=_entry_info(student.student_id, sym, frames.get(sym), avg)
                            if qty > 0 else None,
                            room_value=min(alloc, cap) - mv)
    return out


# =========================================================================== evaluate
def evaluate_student(run_id: str, student_id: str) -> dict:
    """Work out BUY/SELL/HOLD for each of the student's 12 stocks and store them.
    HOLDs are recorded straight away (no order). BUY and SELL wait for an action."""
    demo.guard("evaluating signals")
    s = _state(run_id)
    student = load_students().get(student_id)
    if not student.enabled:
        return {"student_id": student_id, "error": "student module is disabled"}
    existing = db.student_signals(student_id, run_id=run_id)
    if existing:
        return _signals_summary(student, existing, reused=True)

    acct = s.accounts.get(student_id, {})
    live_positions, read_error = {}, None
    try:
        live_positions = brokers.get_broker(student).positions()
    except Exception as e:
        read_error = f"could not read positions: {type(e).__name__}"
        db.log_event("ERROR", "positions", read_error, run_id, student_id)
    positions = build_positions(student, s.cfg, live_positions, s.frames)
    today = s.clock.timestamp.date()
    created = clock.iso()
    # engine v2: regime and targets BEFORE the write transaction below (they log an event, which
    # needs its own connection and would wait forever for this transaction's write lock)
    v2_targets = (_v2_targets(s, student, {k for k, v in live_positions.items() if float(v.get("qty") or 0) > 0})
                  if s.cfg.intraday.enabled and s.cfg.intraday.engine in ("v2", "v3") and s.intraday_fresh else None)
    with db.tx() as con:
        for sym in student.symbols:
            cat = student.category(sym)
            frame = s.frames.get(sym)
            sig = evaluate_symbol(frame, sym, cat, s.cfg, positions[sym], s.news.get(sym), s.earnings.get(sym), today)
            action, reasons = sig.action, list(sig.reasons)
            sig_price, ev_override = sig.price, None
            if s.cfg.intraday.enabled:
                if not s.intraday_fresh:
                    action, reasons = "HOLD", [f"intraday decision paused: {s.intraday_note}"] + reasons
                elif s.cfg.intraday.engine in ("v2", "v3"):
                    if v2_targets is None:
                        v2_targets = _v2_targets(s, student)
                    action, reasons, ev_override, ip = _v2_signal(s, student, sym, sig, positions[sym],
                                                                  live_positions, v2_targets)
                    sig_price = ip or sig.price
                else:
                    action, reasons, ev_override, ip = _intraday_signal(s, student, sym, cat, sig, positions[sym],
                                                                        live_positions)
                    sig_price = ip or sig.price
            if action != "HOLD" and (not s.fresh or sym in s.symbol_problems):
                reasons = [f"{action} setup ignored: market data not fresh "
                           f"({s.symbol_problems.get(sym, 'snapshot stale')})"] + reasons
                action = "HOLD"
            if action != "HOLD" and read_error:
                reasons = [f"{action} setup ignored: {read_error}"] + reasons
                action = "HOLD"
            outcome = "HOLD_RECORDED" if action == "HOLD" else None
            ev = dict(ev_override if ev_override is not None else sig.evidence)
            ev["news"] = s.news.get(sym)
            ev["earnings"] = s.earnings.get(sym)
            ev["account_ok"] = acct.get("ok")
            con.execute(
                "INSERT INTO signals(signal_id, run_id, student_id, symbol, category, action, score, reasons,"
                " indicators, price, bar_date, data_quality, news, held_qty, created_at, outcome, outcome_detail,"
                " outcome_by, outcome_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (db.new_id("sig"), run_id, student_id, sym, cat, action, sig.score, db.j(reasons), db.j(ev),
                 sig_price, sig.bar_date, sig.data_quality, db.j(s.news.get(sym)), positions[sym].qty, created,
                 outcome, "no order placed" if outcome else None, "code" if outcome else None,
                 created if outcome else None))
    rows = db.student_signals(student_id, run_id=run_id)
    db.log_event("INFO", "signals", f"{student_id}: " + ", ".join(
        f"{a}={sum(r['action'] == a for r in rows)}" for a in ("BUY", "SELL", "HOLD")), run_id, student_id)
    return _signals_summary(student, rows, reused=False)


def _signals_summary(student: StudentConfig, rows: list[dict], reused: bool) -> dict:
    import json
    items = []
    for r in rows:
        reasons = json.loads(r["reasons"] or "[]")
        items.append({"signal_id": r["signal_id"], "symbol": r["symbol"], "category": r["category"],
                      "action": r["action"], "close": r["price"], "score": r["score"],
                      "held_qty": r["held_qty"], "reason": (reasons[0] if reasons else "")[:220],
                      "outcome": r["outcome"]})
    return {"student_id": student.student_id, "student": student.label, "reused_existing": reused,
            "counts": {a: sum(i["action"] == a for i in items) for a in ("BUY", "SELL", "HOLD")},
            "signals": items}


# =========================================================================== orders
def _claim_signal(signal_id: str, expected_action: str) -> tuple[dict | None, str | None]:
    sig = db.q1("SELECT * FROM signals WHERE signal_id=?", (signal_id,))
    if not sig:
        return None, "unknown signal_id"
    if sig["action"] != expected_action:
        return None, f"signal {signal_id} is {sig['action']}, not {expected_action}"
    run = db.get_run(sig["run_id"])
    if not run or run["status"] != "RUNNING":
        # a signal is only valid inside its own run; never act on one from a finished or crashed run
        return None, f"run {sig['run_id']} is {run['status'] if run else 'unknown'}; signal no longer valid"
    n = db.ex("UPDATE signals SET outcome='PROCESSING' WHERE signal_id=? AND outcome IS NULL", (signal_id,))
    if n != 1:
        sig = db.q1("SELECT * FROM signals WHERE signal_id=?", (signal_id,))
        return None, f"already handled: {sig['outcome']}"
    return sig, None


def _set_outcome(signal_id: str, outcome: str, detail: str, actor: str) -> None:
    db.ex("UPDATE signals SET outcome=?, outcome_detail=?, outcome_by=?, outcome_at=? WHERE signal_id=?",
          (outcome, redact(detail)[:3000], actor, clock.iso(), signal_id))


def place_order(student: StudentConfig, broker, *, symbol: str, qty: float, side: str, est_price: float | None,
                client_order_id: str, run_id: str | None, mode: str, signal_id: str | None = None,
                approval_id: str | None = None, avg_entry: float | None = None,
                account_number: str | None = None) -> dict:
    """Send one market order with duplicate protection.
    1) the order row is written first with a UNIQUE client_order_id, so the same
       order cannot be inserted twice, 2) the broker is asked whether that id already
       exists (covers a crash after sending), 3) only then is the order sent."""
    now = clock.iso()
    try:
        db.ex("INSERT INTO orders(client_order_id, student_id, account_number, symbol, side, qty, est_price, notional,"
              " signal_id, approval_id, run_id, mode, status, avg_entry_at_submit, submitted_at, updated_at)"
              " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (client_order_id, student.student_id, account_number, symbol, side, qty, est_price,
               round(qty * (est_price or 0), 2), signal_id, approval_id, run_id, mode, "PENDING_SUBMIT",
               avg_entry, now, now))
    except sqlite3.IntegrityError:
        row = db.q1("SELECT * FROM orders WHERE client_order_id=?", (client_order_id,))
        return {"ok": False, "duplicate": True, "status": row["status"] if row else "unknown",
                "detail": "duplicate order blocked (same client_order_id already recorded)"}

    def update(res: brokers.OrderResult | None, status: str | None = None, error: str | None = None):
        db.ex("UPDATE orders SET status=?, broker_order_id=?, filled_qty=?, filled_avg_price=?, updated_at=?, error=?"
              " WHERE client_order_id=?",
              (status or (res.status if res else "UNKNOWN"), res.broker_order_id if res else None,
               res.filled_qty if res else 0, res.filled_avg_price if res else None, clock.iso(),
               redact(error)[:1000] if error else None, client_order_id))

    try:
        existing = broker.order_by_client_id(client_order_id)
        if existing:
            update(existing)
            return {"ok": True, "duplicate": True, "status": existing.status,
                    "detail": "order already existed at broker; not sent again"}
    except Exception as e:
        update(None, "PRECHECK_ERROR", f"could not check for an existing order: {type(e).__name__}: {e}")
        return {"ok": False, "status": "PRECHECK_ERROR", "detail": f"broker check failed ({type(e).__name__}); not sent"}

    try:
        res = broker.submit_market_order(symbol, qty, side, client_order_id, est_price)
        update(res)
        db.log_event("INFO", "order_submitted", f"{side.upper()} {qty:g} {symbol} -> {res.status}", run_id,
                     student.student_id, {"client_order_id": client_order_id, "broker_order_id": res.broker_order_id})
        return {"ok": True, "status": res.status, "broker_order_id": res.broker_order_id,
                "filled_qty": res.filled_qty, "detail": f"sent; broker status '{res.status}' (a fill is confirmed "
                                                         f"only when status becomes 'filled')"}
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:300]}"
        # the request may still have reached the broker: look it up before giving up
        try:
            found = broker.order_by_client_id(client_order_id)
        except Exception:
            found = None
        if found:
            update(found, error=err)
            return {"ok": True, "status": found.status, "detail": "send reported an error but the order exists at the broker"}
        update(None, "SUBMIT_ERROR", err)
        db.log_event("ERROR", "order_failed", f"{side.upper()} {qty:g} {symbol} failed: {err}", run_id,
                     student.student_id)
        return {"ok": False, "status": "SUBMIT_ERROR", "detail": err}


def _bought_today(student_id: str, symbol: str, now_ny: datetime) -> bool:
    start = datetime(now_ny.year, now_ny.month, now_ny.day, tzinfo=clock.NY).astimezone(clock.UTC)
    row = db.q1("SELECT COUNT(*) AS n FROM orders WHERE student_id=? AND symbol=? AND side='buy' AND submitted_at>=?"
                " AND status NOT IN ('SUBMIT_ERROR','PRECHECK_ERROR','rejected','canceled')",
                (student_id, symbol, clock.iso(start)))
    return bool(row and row["n"])


def _buys_this_run(run_id: str, student_id: str) -> int:
    row = db.q1("SELECT COUNT(*) AS n FROM signals WHERE run_id=? AND student_id=? AND outcome IN "
                "('BUY_SUBMITTED','DRY_RUN_NOT_SENT')", (run_id, student_id))
    return row["n"] if row else 0


def _buy_check(sig: dict, s: RunState):
    """Gather live account facts and run check_buy(). Returns (result, student, broker, account)."""
    import json
    sid, sym = sig["student_id"], sig["symbol"]
    student = load_students().get(sid)
    b = brokers.get_broker(student)
    acct = s.accounts.get(sid, {})
    account = b.account()
    try:
        asset = b.asset(sym)
    except Exception as e:
        asset = {"status": f"error {type(e).__name__}", "tradable": False}
    lp = s.prices.get(sym)
    ev = json.loads(sig["indicators"] or "{}")
    positions = b.positions()
    extra = {}
    if s.cfg.intraday.enabled:
        p = positions.get(sym, {})
        plan = position_plan(sid, sym, float(p.get("qty") or 0), float(p["avg_entry"]) if p.get("avg_entry") else None,
                             clock.now_utc())          # order timestamps are wall-clock
        extra = dict(intraday=True, entry_kind=ev.get("entry_kind") or "initial", stop=ev.get("stop"),
                     entries=plan.entries, last_entry_price=plan.last_entry_price,
                     minutes_since_last_entry=plan.minutes_since_last_entry,
                     buys_today_student=plan.buys_today_student, sell_pending=plan.sell_pending)
        if s.cfg.intraday.engine in ("v2", "v3") and ev.get("target_value") is not None:
            extra.update(target_value=float(ev["target_value"]),
                         buy_value=float(ev["buy_value"]) if ev.get("buy_value") is not None else None)
    ctx = BuyContext(student=student, cfg=s.cfg, symbol=sym, category=sig["category"], signal_action=sig["action"],
                     data_quality=sig["data_quality"], data_fresh=s.fresh and sym not in s.symbol_problems,
                     atr=ev.get(colnames(s.cfg.indicators)["atr"]), orders_allowed=bool(acct.get("orders_allowed")),
                     asset=asset, account=account, positions=positions, open_orders=b.open_orders(),
                     latest_price=lp.price if lp else None, price_time=lp.timestamp if lp else None,
                     now=s.provider.now(), market_open=s.clock.is_open,
                     already_bought_today=_bought_today(sid, sym, s.clock.timestamp),
                     buys_this_run=_buys_this_run(sig["run_id"], sid), **extra)
    return check_buy(ctx), student, b, account


def preview_buy(signal_id: str) -> dict:
    """Read-only: show the BUY risk checks and share quantity without sending anything."""
    demo.guard("BUY risk previews")
    sig = db.q1("SELECT * FROM signals WHERE signal_id=?", (signal_id,))
    if not sig:
        return {"error": "unknown signal_id"}
    rr, *_ = _buy_check(sig, _state(sig["run_id"]))
    return {"signal_id": signal_id, "symbol": sig["symbol"], "would_pass": rr.passed, "qty": rr.qty,
            "est_price": rr.est_price, "checks": rr.as_list()}


def submit_buy(signal_id: str, agent_reason: str = "", actor: str = "agent") -> dict:
    """BUY: run every risk check, size the order and (in paper mode) send it automatically."""
    demo.guard("BUY orders")
    sig, err = _claim_signal(signal_id, "BUY")
    if err:
        return {"signal_id": signal_id, "result": "not processed", "detail": err}
    run_id, sid, sym = sig["run_id"], sig["student_id"], sig["symbol"]
    try:
        s = _state(run_id)
        rr, student, b, account = _buy_check(sig, s)
        checks_txt = "; ".join(f"{'PASS' if c.ok else 'FAIL'} {c.name} ({c.detail})" for c in rr.checks)
        note = f" | agent note: {agent_reason[:300]}" if agent_reason else ""
        if not rr.passed:
            _set_outcome(signal_id, "BUY_BLOCKED", f"blocked by risk checks: {rr.summary()}{note}", actor)
            db.log_event("INFO", "buy_blocked", f"BUY {sym} blocked: {rr.summary()}", run_id, sid)
            return {"signal_id": signal_id, "symbol": sym, "result": "BUY_BLOCKED", "detail": rr.summary()}
        if s.mode == "analysis":
            _set_outcome(signal_id, "ANALYSIS_ONLY", f"would BUY {rr.qty:g} @ ~{rr.est_price} (analysis run, "
                                                     f"nothing sent){note}", actor)
            return {"signal_id": signal_id, "symbol": sym, "result": "ANALYSIS_ONLY", "qty": rr.qty}
        if s.mode == "dry_run":
            _set_outcome(signal_id, "DRY_RUN_NOT_SENT", f"all checks passed; would BUY {rr.qty:g} @ ~{rr.est_price}"
                                                        f" (dry run){note}", actor)
            return {"signal_id": signal_id, "symbol": sym, "result": "DRY_RUN_NOT_SENT", "qty": rr.qty}
        if not paper_orders_enabled():
            _set_outcome(signal_id, "BLOCKED_ORDERS_DISABLED",
                         f"checks passed but ENABLE_PAPER_ORDERS is not true; nothing sent{note}", actor)
            return {"signal_id": signal_id, "symbol": sym, "result": "BLOCKED_ORDERS_DISABLED"}
        session = s.clock.timestamp.strftime("%Y%m%d")
        # daily mode: one BUY per stock per day; intraday mode: one order per decision (signal)
        cid = f"aaba-{signal_id}-buy" if s.cfg.intraday.enabled else f"aaba-{sid}-{sym}-{session}-buy"
        res = place_order(student, b, symbol=sym, qty=rr.qty, side="buy", est_price=rr.est_price,
                          client_order_id=cid, run_id=run_id, mode=s.mode, signal_id=signal_id,
                          account_number=account.get("account_number"))
        outcome = "BUY_SUBMITTED" if res["ok"] and not res.get("duplicate") else (
            "BUY_DUPLICATE_BLOCKED" if res.get("duplicate") else "BUY_FAILED")
        _set_outcome(signal_id, outcome, f"{res['detail']}; qty {rr.qty:g} @ ~{rr.est_price}; {checks_txt[:1500]}{note}",
                     actor)
        return {"signal_id": signal_id, "symbol": sym, "result": outcome, "qty": rr.qty, "status": res.get("status")}
    except Exception as e:
        _set_outcome(signal_id, "BUY_ERROR", f"{type(e).__name__}: {e}", actor)
        db.log_event("ERROR", "buy_error", f"BUY {sym} error: {type(e).__name__}: {e}", run_id, sid)
        return {"signal_id": signal_id, "symbol": sym, "result": "BUY_ERROR", "detail": type(e).__name__}


def create_sell_approval(signal_id: str, agent_reason: str = "", actor: str = "agent") -> dict:
    """SELL: never sent straight away. Creates a request for the owning student."""
    demo.guard("creating SELL approvals")
    sig, err = _claim_signal(signal_id, "SELL")
    if err:
        return {"signal_id": signal_id, "result": "not processed", "detail": err}
    run_id, sid, sym = sig["run_id"], sig["student_id"], sig["symbol"]
    try:
        s = _state(run_id)
        student = load_students().get(sid)
        if s.mode == "analysis":
            _set_outcome(signal_id, "ANALYSIS_ONLY", "SELL signal noted; analysis run creates no approval", actor)
            return {"signal_id": signal_id, "symbol": sym, "result": "ANALYSIS_ONLY"}
        b = brokers.get_broker(student)
        held = float(b.positions().get(sym, {}).get("qty") or 0)
        if held <= 0:
            _set_outcome(signal_id, "SELL_BLOCKED", "no shares held; nothing to sell", actor)
            return {"signal_id": signal_id, "symbol": sym, "result": "SELL_BLOCKED", "detail": "no shares held"}
        acct = s.accounts.get(sid, {})
        open_ap = db.q1("SELECT approval_id, qty FROM approvals WHERE student_id=? AND symbol=? AND status IN "
                        "('PENDING','APPROVED','EXECUTING') ORDER BY created_at DESC", (sid, sym))
        if s.cfg.intraday.enabled and open_ap and abs(float(open_ap["qty"]) - held) < 1e-9:
            # intraday cycles see the same exit again and again: keep the request the student already has
            _set_outcome(signal_id, "SELL_PENDING_APPROVAL",
                         f"SELL already waiting for {student.label}: approval {open_ap['approval_id']}", actor)
            return {"signal_id": signal_id, "symbol": sym, "result": "SELL_PENDING_APPROVAL",
                    "approval_id": open_ap["approval_id"], "qty": held, "approver": student.label, "existing": True}
        import json
        reasons = "; ".join(json.loads(sig["reasons"] or "[]"))
        reason = reasons + (f" | agent note: {agent_reason[:300]}" if agent_reason else "")
        ap = approvals.create_request(run_id, sig, sid, acct.get("account_number"), held, reason)
        _set_outcome(signal_id, "SELL_PENDING_APPROVAL",
                     f"approval {ap['approval_id']} for {held:g} shares sent to {student.label}", actor)
        return {"signal_id": signal_id, "symbol": sym, "result": "SELL_PENDING_APPROVAL",
                "approval_id": ap["approval_id"], "qty": held, "approver": student.label}
    except Exception as e:
        _set_outcome(signal_id, "SELL_ERROR", f"{type(e).__name__}: {e}", actor)
        return {"signal_id": signal_id, "symbol": sym, "result": "SELL_ERROR", "detail": type(e).__name__}


def record_hold(signal_id: str, note: str, actor: str = "agent") -> dict:
    """The agent may turn a BUY into a HOLD (more cautious) with a reason. It may not
    cancel a SELL: SELL decisions belong to the student."""
    demo.guard("changing signal outcomes")
    sig = db.q1("SELECT * FROM signals WHERE signal_id=?", (signal_id,))
    if not sig:
        return {"signal_id": signal_id, "result": "not processed", "detail": "unknown signal_id"}
    if sig["action"] == "SELL":
        return {"signal_id": signal_id, "result": "refused",
                "detail": "SELL signals always go to the student for approval; call create_sell_approval"}
    if sig["action"] == "HOLD":
        return {"signal_id": signal_id, "result": "HOLD_RECORDED", "detail": "already recorded"}
    if actor == "agent" and not load_strategy().agent.allow_agent_veto:
        return {"signal_id": signal_id, "result": "refused",
                "detail": "agent vetoes are switched off (agent.allow_agent_veto); call submit_buy"}
    claimed, err = _claim_signal(signal_id, "BUY")
    if err:
        return {"signal_id": signal_id, "result": "not processed", "detail": err}
    _set_outcome(signal_id, "AGENT_VETO_HOLD", f"agent chose not to buy: {note[:500]}", actor)
    db.log_event("INFO", "agent_veto", f"agent vetoed BUY {sig['symbol']}: {note[:200]}", sig["run_id"],
                 sig["student_id"])
    return {"signal_id": signal_id, "symbol": sig["symbol"], "result": "AGENT_VETO_HOLD"}


# =========================================================================== approved sells
def execute_approved_sell(approval_id: str, actor: str = "system") -> dict:
    """Re-check an APPROVED sell and send it. Uses client_order_id 'aaba-<approval_id>',
    so a retry can never create a second order."""
    demo.guard("SELL execution")
    approvals.expire_stale()
    n = db.ex("UPDATE approvals SET status='EXECUTING' WHERE approval_id=? AND status='APPROVED'", (approval_id,))
    ap = db.q1("SELECT * FROM approvals WHERE approval_id=?", (approval_id,))
    if not ap:
        return {"approval_id": approval_id, "result": "unknown approval"}
    if n != 1:
        return {"approval_id": approval_id, "result": "not executed", "detail": f"status is {ap['status']}"}
    sid, sym = ap["student_id"], ap["symbol"]
    run = db.get_run(ap["run_id"]) or {"mode": "paper"}
    cfg = load_strategy()
    student = load_students().get(sid)

    def back_to_approved(why):
        db.ex("UPDATE approvals SET status='APPROVED', execution_detail=? WHERE approval_id=?", (why, approval_id))
        return {"approval_id": approval_id, "symbol": sym, "result": "WAITING", "detail": why}

    try:
        b = brokers.get_broker(student)
        acc = brokers.verify_accounts().get(sid, {})
        provider = get_provider()
        mclock = provider.market_clock()
        prices = provider.latest_prices([sym])
        lp = prices.get(sym)
        ctx = SellContext(cfg=cfg, approval=ap, orders_allowed=bool(acc.get("orders_allowed")),
                          current_account_number=acc.get("account_number"), positions=b.positions(),
                          open_orders=b.open_orders(), latest_price=lp.price if lp else None,
                          price_time=lp.timestamp if lp else None, now=provider.now(),
                          market_open=mclock.is_open, expired=approvals.is_expired(ap))
        rr = check_sell(ctx)
        if not rr.passed:
            failed = {c.name for c in rr.failed()}
            if failed <= {"market open", "latest price fresh"}:
                return back_to_approved(f"waiting for the market to open ({rr.summary()})")
            db.ex("UPDATE approvals SET status='INVALIDATED', execution_detail=? WHERE approval_id=?",
                  (rr.summary()[:1500], approval_id))
            db.log_event("WARNING", "sell_invalidated", f"approved SELL {sym} failed re-check: {rr.summary()}",
                         ap["run_id"], sid)
            return {"approval_id": approval_id, "symbol": sym, "result": "INVALIDATED", "detail": rr.summary()}
        if run["mode"] == "dry_run":
            db.ex("UPDATE approvals SET status='EXECUTED_DRY_RUN', execution_detail=? WHERE approval_id=?",
                  (f"re-check passed; would SELL {rr.qty:g} @ ~{rr.est_price} (dry run, nothing sent)", approval_id))
            return {"approval_id": approval_id, "symbol": sym, "result": "EXECUTED_DRY_RUN"}
        if not paper_orders_enabled():
            return back_to_approved("ENABLE_PAPER_ORDERS is not true; order not sent")
        avg = float(ctx.positions.get(sym, {}).get("avg_entry") or 0) or None
        cid = f"aaba-{approval_id}"
        res = place_order(student, b, symbol=sym, qty=rr.qty, side="sell", est_price=rr.est_price,
                          client_order_id=cid, run_id=ap["run_id"], mode="paper", approval_id=approval_id,
                          signal_id=ap["signal_id"], avg_entry=avg, account_number=acc.get("account_number"))
        status = "EXECUTED" if res["ok"] else "FAILED"
        db.ex("UPDATE approvals SET status=?, client_order_id=?, execution_detail=? WHERE approval_id=?",
              (status, cid, res["detail"][:1500], approval_id))
        return {"approval_id": approval_id, "symbol": sym, "result": status, "detail": res["detail"]}
    except Exception as e:
        return back_to_approved(f"error during re-check ({type(e).__name__}); will retry later")


def process_approved_sells(student_id: str, run_id: str = "") -> dict:
    """Send SELLs this student has approved. Skipped inside an analysis-only run, which never sends orders."""
    demo.guard("SELL execution")
    approvals.expire_stale()
    run = db.get_run(run_id) if run_id else None
    if run and run["mode"] == "analysis":
        return {"student_id": student_id, "skipped": "analysis run: approved SELLs are not sent",
                "approved_waiting": len(db.student_approvals(student_id, ("APPROVED",)))}
    rows = db.student_approvals(student_id, ("APPROVED",))
    waiting = 0
    if run and run["mode"] == "dry_run":
        # a dry run never sends an order: approvals made in paper runs wait for a paper run
        paper_rows = [r for r in rows if (db.get_run(r["run_id"]) or {"mode": "paper"})["mode"] != "dry_run"]
        waiting = len(paper_rows)
        rows = [r for r in rows if r not in paper_rows]
    results = [execute_approved_sell(r["approval_id"], actor="workflow") for r in rows]
    pending = len(db.student_approvals(student_id, ("PENDING",)))
    out = {"student_id": student_id, "approved_processed": results, "still_pending_approval": pending}
    if waiting:
        out["approved_waiting_for_paper_run"] = waiting
    return out


# =========================================================================== reconcile
def reconcile_student(student_id: str, run_id: str | None = None) -> dict:
    """Update order statuses from the broker and store a portfolio snapshot."""
    demo.guard("reconciling accounts")
    student = load_students().get(student_id)
    b = brokers.get_broker(student)
    updated = 0
    for o in db.q("SELECT * FROM orders WHERE student_id=? AND status NOT IN "
                  "('filled','canceled','expired','rejected','SUBMIT_ERROR','PRECHECK_ERROR')", (student_id,)):
        try:
            res = b.order_by_client_id(o["client_order_id"])
        except Exception as e:
            db.log_event("WARNING", "reconcile", f"order {o['client_order_id']}: {type(e).__name__}", run_id, student_id)
            continue
        if res is None:
            if o["status"] in ("PENDING_SUBMIT", "UNKNOWN"):
                db.ex("UPDATE orders SET status='SUBMIT_ERROR', error='not found at broker', updated_at=? "
                      "WHERE order_pk=?", (clock.iso(), o["order_pk"]))
            continue
        realized = None
        if o["side"] == "sell" and res.filled_qty and res.filled_avg_price and o["avg_entry_at_submit"]:
            realized = round((res.filled_avg_price - o["avg_entry_at_submit"]) * res.filled_qty, 2)
        db.ex("UPDATE orders SET status=?, broker_order_id=?, filled_qty=?, filled_avg_price=?, realized_pnl=?,"
              " updated_at=? WHERE order_pk=?",
              (res.status, res.broker_order_id, res.filled_qty, res.filled_avg_price, realized, clock.iso(),
               o["order_pk"]))
        updated += 1
    # realized P&L for orders filled immediately at submit time
    for o in db.q("SELECT * FROM orders WHERE student_id=? AND side='sell' AND status='filled' AND realized_pnl IS NULL"
                  " AND avg_entry_at_submit IS NOT NULL AND filled_avg_price IS NOT NULL", (student_id,)):
        db.ex("UPDATE orders SET realized_pnl=? WHERE order_pk=?",
              (round((o["filled_avg_price"] - o["avg_entry_at_submit"]) * o["filled_qty"], 2), o["order_pk"]))

    acct = b.account()
    pos = b.positions()
    spy = None
    try:
        lp = get_provider().latest_prices([BENCHMARK]).get(BENCHMARK)
        spy = lp.price if lp else None
    except Exception:
        pass
    realized = (db.q1("SELECT COALESCE(SUM(realized_pnl),0) AS r FROM orders WHERE student_id=?", (student_id,))
                or {}).get("r", 0)
    unreal = sum(p.get("unrealized_pl", 0) for p in pos.values())
    mine = {k: v for k, v in pos.items()}
    db.ex("INSERT INTO snapshots(at, run_id, student_id, account_number, equity, cash, long_value, unrealized_pnl,"
          " realized_pnl, positions, spy_price, simulated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
          (clock.iso(), run_id, student_id, acct["account_number"], acct["equity"], acct["cash"],
           acct["long_market_value"], round(unreal, 2), realized, db.j(mine), spy, int(getattr(b, "simulated", False))))
    row = db.account_row(student_id)
    if row is not None and row.get("baseline_equity") is None:
        db.ex("UPDATE accounts SET baseline_equity=?, baseline_at=?, baseline_spy=? WHERE student_id=?",
              (acct["equity"], clock.iso(), spy, student_id))
    others = [s for s in pos if s not in student.symbols]
    if others:
        db.log_event("WARNING", "foreign_positions",
                     f"{student_id} account holds stocks outside the student's list: {others}", run_id, student_id)
    return {"student_id": student_id, "orders_updated": updated, "equity": acct["equity"], "cash": acct["cash"],
            "positions": len(pos)}


# =========================================================================== reads for tools
def get_portfolio(student_id: str) -> dict:
    student = load_students().get(student_id)
    b = brokers.get_broker(student)
    acct = b.account()
    pos = b.positions()
    return {"student_id": student_id, "student": student.label, "cash": round(acct["cash"], 2),
            "equity": round(acct["equity"], 2),
            "positions": {k: {"qty": v["qty"], "avg_entry": round(v["avg_entry"], 2),
                              "market_value": round(v["market_value"], 2)} for k, v in pos.items()},
            "pending_sell_approvals": len(db.student_approvals(student_id, ("PENDING",)))}


def get_latest_indicators(student_id: str, symbol: str) -> dict:
    """Kept from the old MCP server: read the latest indicator row for one of the student's stocks."""
    student = load_students().get(student_id)
    symbol = symbol.strip().upper()
    if symbol not in student.symbols:
        return {"error": f"{symbol} is not in {student_id}'s portfolio"}
    snaps = db.q("SELECT run_id FROM market_snapshots ORDER BY fetched_at DESC LIMIT 20")
    for snap in snaps:
        p = paths.INDICATOR_DIR / snap["run_id"] / f"{student_id}_indicators.csv"
        if p.exists():
            df = pd.read_csv(p)
            g = df[df["symbol"] == symbol]
            if g.empty:
                continue
            row = g.iloc[-1].to_dict()
            clean = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in row.items()
                     if not (isinstance(v, float) and pd.isna(v))}
            return {"student_id": student_id, "symbol": symbol, "run_id": snap["run_id"], "latest": clean}
    return {"error": "no indicator file yet; run prepare_market_data first"}


def execution_status(student_id: str, limit: int = 20) -> dict:
    return {"student_id": student_id,
            "orders": [{k: o[k] for k in ("submitted_at", "symbol", "side", "qty", "status", "filled_qty",
                                          "filled_avg_price", "mode")} for o in db.student_orders(student_id, limit)],
            "approvals": [{k: a[k] for k in ("approval_id", "symbol", "qty", "status", "created_at", "decided_by")}
                          for a in db.student_approvals(student_id)[:limit]]}


def audit_log(student_id: str | None = None, limit: int = 30) -> list[dict]:
    if student_id:
        return db.q("SELECT at, level, kind, message FROM events WHERE student_id=? ORDER BY id DESC LIMIT ?",
                    (student_id, limit))
    return db.q("SELECT at, student_id, level, kind, message FROM events ORDER BY id DESC LIMIT ?", (limit,))


def unhandled_signals(run_id: str) -> list[dict]:
    return db.q("SELECT * FROM signals WHERE run_id=? AND outcome IS NULL", (run_id,))


def reset_cache() -> None:
    _RUNS.clear()
