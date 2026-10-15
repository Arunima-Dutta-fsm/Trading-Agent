"""Data for the dashboard, read only from the database (the real backend state).

Kept separate from the Streamlit code so tests can check that what the dashboard
shows matches what is stored.
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd

from aaba import clock, db, paths
from aaba.settings import load_students


def last_run() -> dict | None:
    return db.q1("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")


def last_snapshot_meta() -> dict | None:
    return db.q1("SELECT * FROM market_snapshots ORDER BY fetched_at DESC LIMIT 1")


def student_summary(student_id: str) -> dict:
    st = load_students().get(student_id)
    snap = db.latest_snapshot(student_id)
    acct = db.account_row(student_id) or {}
    last = db.q1("SELECT run_id FROM signals WHERE student_id=? ORDER BY created_at DESC LIMIT 1", (student_id,))
    sigs = db.student_signals(student_id, run_id=last["run_id"]) if last else []
    fails = db.q1("SELECT COUNT(*) AS n FROM events WHERE student_id=? AND level='ERROR'", (student_id,))["n"]
    base = acct.get("baseline_equity") or st.starting_capital
    eq = snap["equity"] if snap else None
    open_orders = db.q1("SELECT COUNT(*) AS n FROM orders WHERE student_id=? AND status IN "
                        "('new','accepted','pending_new','partially_filled','PENDING_SUBMIT','UNKNOWN')",
                        (student_id,))["n"]
    return {
        "student_id": student_id, "student": st.label, "enabled": st.enabled,
        "account": acct.get("account_number"), "account_ok": bool(acct.get("paper_verified")),
        "starting_capital": st.starting_capital, "equity": eq, "cash": snap["cash"] if snap else None,
        "invested": snap["long_value"] if snap else None,
        "unrealized_pnl": snap["unrealized_pnl"] if snap else None,
        "realized_pnl": snap["realized_pnl"] if snap else None,
        "return_pct": round((eq / base - 1) * 100, 2) if (eq and base) else None,
        "total_pnl": round(eq - base, 2) if (eq is not None and base) else None,
        "day_pnl": _day_pnl(student_id, eq),
        "buy": sum(s["action"] == "BUY" for s in sigs), "sell": sum(s["action"] == "SELL" for s in sigs),
        "hold": sum(s["action"] == "HOLD" for s in sigs),
        "pending_approvals": len(db.student_approvals(student_id, ("PENDING",))),
        "open_orders": open_orders, "errors": fails, "simulated": bool(snap and snap["simulated"]),
        "snapshot_at": snap["at"] if snap else None,
    }


def _day_pnl(student_id: str, equity: float | None) -> float | None:
    """Equity now minus the last snapshot taken before today's New York session started."""
    if equity is None:
        return None
    now_ny = clock.to_ny(clock.now_utc())
    start = clock.iso(now_ny.replace(hour=0, minute=0, second=0, microsecond=0))
    prev = db.q1("SELECT equity FROM snapshots WHERE student_id=? AND at<? ORDER BY at DESC, id DESC LIMIT 1",
                 (student_id, start))
    return round(equity - prev["equity"], 2) if prev and prev["equity"] is not None else None


def group_summary() -> dict:
    rows = [student_summary(s.student_id) for s in load_students().students]
    start = sum(r["starting_capital"] for r in rows)
    complete = all(r["equity"] is not None for r in rows)

    def total(key):
        vals = [r[key] for r in rows]
        return round(sum(vals), 2) if complete and all(v is not None for v in vals) else None
    return {"students": rows, "starting_capital": start, "current_value": total("equity"),
            "cash": total("cash"), "invested": total("invested"), "unrealized_pnl": total("unrealized_pnl"),
            "realized_pnl": total("realized_pnl"), "total_pnl": total("total_pnl"), "day_pnl": total("day_pnl"),
            "pending_approvals": sum(r["pending_approvals"] for r in rows)}


HOLDING_COLS = ["symbol", "qty", "avg_entry", "current_price", "market_value", "unrealized_pl", "weight_pct"]


def holdings(student_id: str) -> pd.DataFrame:
    """Positions as last reported by the broker (saved in the latest snapshot)."""
    snap = db.latest_snapshot(student_id)
    if not snap or not snap["positions"]:
        return pd.DataFrame(columns=HOLDING_COLS)
    pos = json.loads(snap["positions"])
    df = pd.DataFrame([{"symbol": k, **v} for k, v in pos.items()])
    if df.empty:
        return pd.DataFrame(columns=HOLDING_COLS)
    if snap["equity"]:
        df["weight_pct"] = (df["market_value"] / snap["equity"] * 100).round(2)
    keep = [c for c in HOLDING_COLS if c in df]
    return df[keep].sort_values("symbol")


def group_holdings() -> pd.DataFrame:
    """All three students' positions in one table, one row per student and stock (never merged)."""
    parts = []
    for s in load_students().students:
        h = holdings(s.student_id)
        if not h.empty:
            parts.append(h.assign(student=s.label)[["student"] + list(h.columns)])
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["student"] + HOLDING_COLS)


def _first_reason(reasons_json: str | None) -> str:
    r = json.loads(reasons_json or "[]")
    return r[0][:200] if r else ""


def orders_view(student_id: str) -> pd.DataFrame:
    """Every order for one student. 'proposed_price' is the estimate used for sizing, NOT a fill;
    'fill_price' is only shown once the broker reports filled shares."""
    rows = db.q(
        "SELECT o.*, s.reasons AS sig_reasons, s.outcome_detail AS sig_detail, a.reason AS ap_reason,"
        " a.decided_by, a.decided_at FROM orders o LEFT JOIN signals s ON s.signal_id=o.signal_id"
        " LEFT JOIN approvals a ON a.approval_id=o.approval_id WHERE o.student_id=? ORDER BY o.submitted_at DESC, o.order_pk DESC",
        (student_id,))
    out = []
    for o in rows:
        filled = float(o["filled_qty"] or 0) > 0
        note = (o["sig_detail"] or "").split("agent note: ", 1)
        reason = (o["ap_reason"] or "")[:200] if o["side"] == "sell" else _first_reason(o["sig_reasons"])
        if o["side"] == "buy" and len(note) == 2:
            reason += f" | agent: {note[1][:120]}"
        out.append({"submitted_at": o["submitted_at"], "side": o["side"].upper(), "symbol": o["symbol"],
                    "qty": o["qty"], "proposed_price": o["est_price"],
                    "fill_qty": o["filled_qty"] if filled else 0,
                    "fill_price": o["filled_avg_price"] if filled else None,
                    "status": o["status"], "realized_pnl": o["realized_pnl"], "reason": reason,
                    "approved_by": o["decided_by"], "approved_at": o["decided_at"], "mode": o["mode"],
                    "error": o["error"], "client_order_id": o["client_order_id"]})
    return pd.DataFrame(out)


def approvals_view(student_id: str) -> pd.DataFrame:
    """SELL approvals with what happened after: the order, its broker-confirmed fill and realized P&L."""
    rows = db.q("SELECT a.*, o.status AS order_status, o.filled_qty, o.filled_avg_price, o.realized_pnl"
                " FROM approvals a LEFT JOIN orders o ON o.approval_id=a.approval_id"
                " WHERE a.student_id=? ORDER BY a.created_at DESC, a.rowid DESC", (student_id,))
    return pd.DataFrame([{
        "created_at": a["created_at"], "symbol": a["symbol"], "qty": a["qty"],
        "proposed_price": a["signal_price"], "reason": (a["reason"] or "")[:200], "status": a["status"],
        "expires_at": a["expires_at"], "decided_by": a["decided_by"], "decided_at": a["decided_at"],
        "order_status": a["order_status"],
        "fill_qty": a["filled_qty"] if float(a["filled_qty"] or 0) > 0 else None,
        "fill_price": a["filled_avg_price"] if float(a["filled_qty"] or 0) > 0 else None,
        "realized_pnl": a["realized_pnl"], "detail": a["execution_detail"] or a["decision_note"],
        "approval_id": a["approval_id"]} for a in rows])


PROBLEM_SIGNALS = ("BUY_BLOCKED", "BUY_FAILED", "BUY_ERROR", "BUY_DUPLICATE_BLOCKED", "BLOCKED_ORDERS_DISABLED",
                   "SELL_BLOCKED", "SELL_ERROR", "AGENT_VETO_HOLD")
PROBLEM_ORDERS = ("rejected", "canceled", "expired", "SUBMIT_ERROR", "PRECHECK_ERROR")
PROBLEM_APPROVALS = ("REJECTED", "EXPIRED", "INVALIDATED", "FAILED", "SUPERSEDED")


def problems_view(student_id: str) -> pd.DataFrame:
    """Blocked, vetoed, failed, rejected, cancelled or expired actions, with the recorded reason."""
    out = []
    marks = lambda t: ",".join("?" * len(t))  # noqa: E731
    for s in db.q(f"SELECT * FROM signals WHERE student_id=? AND outcome IN ({marks(PROBLEM_SIGNALS)})",
                  (student_id, *PROBLEM_SIGNALS)):
        out.append({"at": s["outcome_at"], "what": f"{s['action']} signal", "symbol": s["symbol"],
                    "result": s["outcome"], "reason": (s["outcome_detail"] or "")[:400]})
    for o in db.q(f"SELECT * FROM orders WHERE student_id=? AND status IN ({marks(PROBLEM_ORDERS)})",
                  (student_id, *PROBLEM_ORDERS)):
        out.append({"at": o["updated_at"], "what": f"{o['side'].upper()} order", "symbol": o["symbol"],
                    "result": o["status"], "reason": (o["error"] or "")[:400]})
    for a in db.q(f"SELECT * FROM approvals WHERE student_id=? AND status IN ({marks(PROBLEM_APPROVALS)})",
                  (student_id, *PROBLEM_APPROVALS)):
        out.append({"at": a["decided_at"] or a["created_at"], "what": "SELL approval", "symbol": a["symbol"],
                    "result": a["status"], "reason": (a["execution_detail"] or a["decision_note"] or "")[:400]})
    df = pd.DataFrame(out, columns=["at", "what", "symbol", "result", "reason"])
    return df.sort_values("at", ascending=False, na_position="last") if not df.empty else df


def group_performance() -> pd.DataFrame:
    """Combined equity of all three students, only at runs where every student has a snapshot."""
    n = len(load_students().students)
    rows = db.q("SELECT run_id, MIN(at) AS at, SUM(equity) AS equity, MAX(spy_price) AS spy_price, COUNT(*) AS k"
                " FROM snapshots WHERE run_id IS NOT NULL GROUP BY run_id HAVING COUNT(DISTINCT student_id)=?"
                " ORDER BY at", (n,))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["at"] = pd.to_datetime(df["at"], utc=True)
    df = df.set_index("at")
    df["return_pct"] = (df["equity"] / df["equity"].iloc[0] - 1) * 100
    if df["spy_price"].notna().any():
        df["spy_return_pct"] = (df["spy_price"] / df["spy_price"].dropna().iloc[0] - 1) * 100
    return df


def latest_signals(student_id: str) -> pd.DataFrame:
    last = db.q1("SELECT run_id FROM signals WHERE student_id=? ORDER BY created_at DESC LIMIT 1", (student_id,))
    if not last:
        return pd.DataFrame()
    rows = db.student_signals(student_id, run_id=last["run_id"])
    return pd.DataFrame([{"symbol": r["symbol"], "type": r["category"], "signal": r["action"],
                          "close": round(r["price"], 2) if r["price"] else None, "score": r["score"],
                          "outcome": r["outcome"], "why": "; ".join(json.loads(r["reasons"] or "[]"))[:300],
                          "bar_date": r["bar_date"], "run_id": r["run_id"]} for r in rows])


def performance(student_id: str) -> pd.DataFrame:
    """Equity, return, drawdown and SPY over time, from stored snapshots."""
    snaps = db.student_snapshots(student_id)
    if not snaps:
        return pd.DataFrame()
    df = pd.DataFrame(snaps)[["at", "equity", "spy_price"]]
    df["at"] = pd.to_datetime(df["at"], utc=True)
    df = df.set_index("at")
    base = df["equity"].iloc[0]
    df["return_pct"] = (df["equity"] / base - 1) * 100
    df["drawdown_pct"] = (df["equity"] / df["equity"].cummax() - 1) * 100
    if df["spy_price"].notna().any():
        first = df["spy_price"].dropna().iloc[0]
        df["spy_return_pct"] = (df["spy_price"] / first - 1) * 100
    return df


def history(kind: str, student_id: str | None = None, text: str = "", start: date | None = None,
            end: date | None = None, limit: int = 2000) -> pd.DataFrame:
    table = {"signals": ("signals", "created_at"), "orders": ("orders", "submitted_at"),
             "approvals": ("approvals", "created_at"), "events": ("events", "at"),
             "snapshots": ("snapshots", "at")}[kind]
    sql = f"SELECT * FROM {table[0]} WHERE 1=1"
    args: list = []
    if student_id:
        sql += " AND student_id=?"
        args.append(student_id)
    if start:
        sql += f" AND {table[1]}>=?"
        args.append(start.isoformat())
    if end:
        sql += f" AND {table[1]}<?"
        args.append((pd.Timestamp(end) + pd.Timedelta(days=1)).date().isoformat())
    sql += f" ORDER BY {table[1]} DESC LIMIT ?"
    args.append(limit)
    df = pd.DataFrame(db.q(sql, args))
    if text and not df.empty:
        mask = df.astype(str).apply(lambda col: col.str.contains(text, case=False, regex=False)).any(axis=1)
        df = df[mask]
    return df


def model_run_files() -> list:
    paths.MODEL_RUN_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(paths.MODEL_RUN_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)


def trading_sessions() -> dict:
    """Honest count of the New York trading days on which a PAPER-mode run completed."""
    rows = db.q("SELECT started_at FROM runs WHERE mode='paper' AND status LIKE 'DONE%' AND simulated=0")
    days = sorted({clock.to_ny(clock.parse_iso(r["started_at"])).date() for r in rows})
    return {"sessions": len(days), "first": str(days[0]) if days else None, "last": str(days[-1]) if days else None,
            "dates": [str(d) for d in days]}
