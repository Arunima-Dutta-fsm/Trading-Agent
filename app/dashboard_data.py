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
        "buy": sum(s["action"] == "BUY" for s in sigs), "sell": sum(s["action"] == "SELL" for s in sigs),
        "hold": sum(s["action"] == "HOLD" for s in sigs),
        "pending_approvals": len(db.student_approvals(student_id, ("PENDING",))),
        "open_orders": open_orders, "errors": fails, "simulated": bool(snap and snap["simulated"]),
        "snapshot_at": snap["at"] if snap else None,
    }


def group_summary() -> dict:
    rows = [student_summary(s.student_id) for s in load_students().students]
    start = sum(r["starting_capital"] for r in rows)
    eq = [r["equity"] for r in rows if r["equity"] is not None]
    return {"students": rows, "starting_capital": start,
            "current_value": round(sum(eq), 2) if len(eq) == len(rows) else None,
            "pending_approvals": sum(r["pending_approvals"] for r in rows)}


def holdings(student_id: str) -> pd.DataFrame:
    snap = db.latest_snapshot(student_id)
    if not snap or not snap["positions"]:
        return pd.DataFrame(columns=["symbol", "qty", "avg_entry", "current_price", "market_value", "unrealized_pl"])
    pos = json.loads(snap["positions"])
    df = pd.DataFrame([{"symbol": k, **v} for k, v in pos.items()])
    keep = [c for c in ("symbol", "qty", "avg_entry", "current_price", "market_value", "unrealized_pl") if c in df]
    return df[keep].sort_values("symbol") if not df.empty else df


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
