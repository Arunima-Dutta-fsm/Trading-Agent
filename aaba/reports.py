"""Markdown reports in model-run/, one file per agent output.

File name format: <agentName><DDMMYYYY>-<HH>-<MM>.md, for example
Orchestrator_S2_Arunima13102026-19-15.md. The brief asks for DDMMYYYY-HH:MM, but
Windows does not allow ':' in file names, so '-' is used. The time is in the report
timezone from strategy.json (default IST, Asia/Kolkata); the New York time is also
written inside each report.

The facts in every report (signals, checks, orders, approvals) come from the
database, not from the LLM. The LLM's own words are shown in a separate, labelled
section.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import clock, db, paths
from .settings import is_simulated, load_strategy, load_students, redact


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    i = 2
    while True:
        p = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not p.exists():
            return p
        i += 1


def report_path(agent_name: str) -> Path:
    tz = load_strategy().reports.timezone
    stamp = clock.report_stamp(clock.now_utc(), tz)
    paths.MODEL_RUN_DIR.mkdir(parents=True, exist_ok=True)
    return _unique(paths.MODEL_RUN_DIR / f"{agent_name}{stamp}.md")


def agent_name_for(student_id: str) -> str:
    st = load_students().get(student_id)
    return f"Orchestrator_{student_id}_{st.name.split()[0]}"


def _header(run: dict, title: str) -> list[str]:
    tz = load_strategy().reports.timezone
    snap = db.q1("SELECT * FROM market_snapshots WHERE run_id=?", (run["run_id"],)) or {}
    started = clock.parse_iso(run["started_at"])
    lines = [f"# {title}", ""]
    if run.get("simulated") or is_simulated():
        lines += ["> **SIMULATED RUN**: made-up prices and/or a fake broker. Not real market data, not real orders.", ""]
    lines += [
        "| Item | Value |", "|---|---|",
        f"| Run ID | `{run['run_id']}` |",
        f"| Started | {clock.fmt_local(started, tz)} / {clock.fmt_local(started, 'America/New_York')} |",
        f"| Mode | **{run['mode']}** |",
        f"| Engine | {run.get('engine')} |",
        f"| LLM provider / model | {run.get('llm_provider') or 'none'} / {run.get('llm_model') or 'none'} |",
        f"| Data source | {snap.get('source', '?')} / feed {snap.get('feed', '?')}, "
        f"{snap.get('batch_calls', '?')} batch request(s) for bars |",
        f"| Latest bar / expected | {snap.get('latest_bar_date')} / {snap.get('expected_bar_date')} |",
        f"| Data fresh | {'yes' if snap.get('fresh') else 'NO - no new trades'} |",
        "",
    ]
    return lines


def student_report(run_id: str, student_id: str, agent_text: str | None, agent_error: str | None = None) -> Path:
    run = db.get_run(run_id)
    st = load_students().get(student_id)
    sigs = db.student_signals(student_id, run_id=run_id)
    lines = _header(run, f"{st.name} ({st.roll_no}) - {student_id} - trading run")
    acct = db.account_row(student_id) or {}
    detail = json.loads(acct.get("detail") or "{}")
    lines += ["## Account", f"- Paper account: `{acct.get('account_number')}` - "
              f"{'verified' if acct.get('paper_verified') else 'NOT verified, orders blocked'}"]
    for p in detail.get("problems", []):
        lines.append(f"- Problem: {p}")
    for w in detail.get("warnings", []):
        lines.append(f"- Warning: {w}")
    lines += ["", f"## Signals ({len(sigs)} stocks)", "",
              "| Symbol | Type | Signal | Close | Score | Held | Outcome |", "|---|---|---|---|---|---|---|"]
    for s in sigs:
        px = f"{s['price']:.2f}" if s["price"] else ""
        lines.append(f"| {s['symbol']} | {s['category']} | **{s['action']}** | {px} | "
                     f"{s['score'] if s['score'] is not None else ''} | {s['held_qty']:g} | {s['outcome']} |")
    lines += ["", "## Why (from the rules engine)", ""]
    for s in sigs:
        reasons = json.loads(s["reasons"] or "[]")
        lines.append(f"- **{s['symbol']} {s['action']}**: {'; '.join(reasons)}")
        if s["outcome_detail"] and s["action"] != "HOLD":
            lines.append(f"  - Outcome detail: {s['outcome_detail'][:900]}")
    lines += ["", "## Indicator values used", ""]
    for s in sigs:
        ev = json.loads(s["indicators"] or "{}")
        keep = {k: v for k, v in ev.items() if k not in ("news", "earnings", "buy_checks", "composite")}
        lines.append(f"- {s['symbol']}: " + ", ".join(f"{k}={v}" for k, v in keep.items()))
        if ev.get("buy_checks"):
            lines.append("  - entry checks: " + ", ".join(f"{k}: {'yes' if v else 'no'}"
                                                         for k, v in ev["buy_checks"].items()))
        n = ev.get("news") or {}
        e = ev.get("earnings") or {}
        lines.append(f"  - news: {n.get('status')} score={n.get('score')} articles={n.get('articles')}; "
                     f"earnings: {e.get('next_earnings') or e.get('status')}")
    orders = [o for o in db.student_orders(student_id) if o["run_id"] == run_id]
    lines += ["", "## Orders from this run", ""]
    if orders:
        lines += ["| Time (UTC) | Side | Symbol | Qty | Status | Client order ID |", "|---|---|---|---|---|---|"]
        for o in orders:
            lines.append(f"| {o['submitted_at']} | {o['side']} | {o['symbol']} | {o['qty']:g} | {o['status']} | "
                         f"`{o['client_order_id']}` |")
    else:
        lines.append("No orders were sent in this run.")
    aps = [a for a in db.student_approvals(student_id) if a["run_id"] == run_id or a["status"] in ("PENDING", "APPROVED")]
    lines += ["", "## SELL approvals", ""]
    if aps:
        for a in aps:
            lines.append(f"- `{a['approval_id']}` SELL {a['qty']:g} {a['symbol']}: **{a['status']}**"
                         f"{' by ' + a['decided_by'] if a['decided_by'] else ''} (expires {a['expires_at']})")
    else:
        lines.append("None.")
    snap = db.latest_snapshot(student_id)
    if snap:
        base = acct.get("baseline_equity")
        ret = (snap["equity"] / base - 1) * 100 if base else None
        spy_ret = (snap["spy_price"] / acct["baseline_spy"] - 1) * 100 if (acct.get("baseline_spy") and snap["spy_price"]) else None
        lines += ["", "## Portfolio after the run", "",
                  f"- Equity ${snap['equity']:,.2f}, cash ${snap['cash']:,.2f}, invested ${snap['long_value']:,.2f}",
                  f"- Unrealized P&L ${snap['unrealized_pnl']:,.2f}; realized P&L ${snap['realized_pnl'] or 0:,.2f}",
                  f"- Return since tracking began: {ret:+.2f}%" if ret is not None else "- Return: baseline not set yet",
                  f"- SPY over the same period: {spy_ret:+.2f}%" if spy_ret is not None else "- SPY comparison: not available yet"]
    errs = db.q("SELECT at, level, message FROM events WHERE run_id=? AND student_id=? AND level IN ('WARNING','ERROR')",
                (run_id, student_id))
    lines += ["", "## Errors and warnings", ""]
    lines += [f"- {e['at']} {e['level']}: {e['message']}" for e in errs] or ["None."]
    lines += ["", "## Agent explanation (LLM output, checked against the tables above)", ""]
    if agent_error:
        lines.append(f"_The LLM step did not finish: {agent_error}. The deterministic controller completed the run._")
    lines.append(agent_text.strip() if agent_text else "_No LLM text for this student in this run._")
    path = report_path(agent_name_for(student_id))
    path.write_text(redact("\n".join(lines)) + "\n", encoding="utf-8")
    return path


def group_report(run_id: str, agent_text: str | None, safety_net: list[dict], agent_error: str | None) -> Path:
    run = db.get_run(run_id)
    group = load_students()
    lines = _header(db.get_run(run_id) or run, f"{group.group_name} - group summary")
    lines += ["## Per student", "", "| Student | BUY | SELL | HOLD | Orders sent | Pending approvals | Equity |",
              "|---|---|---|---|---|---|---|"]
    for st in group.students:
        sigs = db.student_signals(st.student_id, run_id=run_id)
        cnt = {a: sum(s["action"] == a for s in sigs) for a in ("BUY", "SELL", "HOLD")}
        sent = len([o for o in db.student_orders(st.student_id) if o["run_id"] == run_id])
        pend = len(db.student_approvals(st.student_id, ("PENDING",)))
        snap = db.latest_snapshot(st.student_id)
        eq = f"${snap['equity']:,.0f}" if snap else "-"
        lines.append(f"| {st.label} | {cnt['BUY']} | {cnt['SELL']} | {cnt['HOLD']} | {sent} | {pend} | {eq} |")
    lines += ["", "## Actions completed by the deterministic safety net", ""]
    if safety_net:
        lines += [f"- {x.get('student_id', '')} {x.get('symbol', '')}: {x.get('result')} ({x.get('why', '')})"
                  for x in safety_net]
    else:
        lines.append("None: the agent handled every BUY and SELL signal itself.")
    lines += ["", "## Agent summary (LLM output)", ""]
    if agent_error:
        lines.append(f"_LLM step error: {agent_error}_")
    lines.append(agent_text.strip() if agent_text else "_No LLM summary in this run._")
    path = report_path("Orchestrator_Group")
    path.write_text(redact("\n".join(lines)) + "\n", encoding="utf-8")
    return path
