"""AABA trading dashboard (Streamlit).

    streamlit run app/streamlit_app.py      (or: python main.py dashboard)

Everything shown comes from the database and the paper accounts. Nothing is
hard-coded. Secrets are never displayed: the page only says whether a key is set.
Protected actions (order runs, config changes) need DASHBOARD_ADMIN_PIN from .env.
SELL approvals need the owning student's own PIN and are checked on the server side.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from aaba import approvals, clock, controller, db, paths  # noqa: E402
from aaba import broker as brokers  # noqa: E402
from aaba.demo import guard, is_demo, is_read_only  # noqa: E402
from aaba.runlock import current_holder  # noqa: E402
from aaba.settings import (StrategyConfig, is_simulated, load_strategy, load_students,  # noqa: E402
                           paper_orders_enabled, save_strategy, set_student_enabled)
from app import dashboard_data as dd  # noqa: E402

st.set_page_config(page_title="AABA Trading Agent", page_icon="📈", layout="wide")
paths.ensure_dirs()
db.init_db()
DEMO = is_demo()     # read-only public demo: controls are hidden here AND refused server-side (aaba/demo.py)
READ_ONLY = is_read_only()   # demo OR the private live dashboard (AABA_READ_ONLY=1): no action controls
DEMO_NOTE = "Disabled in the read-only demo." if DEMO else "Disabled on the read-only dashboard."
# live dashboard: reload all data every N seconds (0 = off; the static demo never needs it)
REFRESH_SECONDS = 0 if DEMO else int(os.getenv("AABA_DASHBOARD_REFRESH_SECONDS", "60" if READ_ONLY else "0") or 0)


def money(x):
    return "-" if x is None else f"${x:,.2f}"


def admin_ok(pin: str) -> bool:
    return approvals.verify_admin_pin(pin)


def sim_banner():
    if DEMO:
        st.info("READ-ONLY DEMO: simulated prices, simulated broker and anonymized students. No real account, "
                "order or approval is involved, and nothing on this site can trade or change settings.")
    elif is_simulated():
        st.warning("SIMULATED MODE: prices and/or broker are fake (AABA_DATA_PROVIDER / AABA_BROKER). "
                   "Nothing here is real market data or a real order.")
    if not DEMO:
        freshness_banner()


def freshness_banner():
    f = dd.freshness()
    tz = load_strategy().reports.timezone
    snap = (f"{clock.fmt_local(f['snapshot_at'], tz)} ({f['snapshot_age_min']} min ago)"
            if f["snapshot_at"] else "none yet")
    run = f["last_run"]
    run_txt = (f"{run['mode']} run {run['status']} at {clock.fmt_local(clock.parse_iso(run['started_at']), tz)}"
               if run else "none yet")
    st.caption(f"Last broker data: {snap} · Last workflow run: {run_txt} · Page loaded "
               f"{clock.fmt_local(f['now'], tz)}" + (f" · refreshes every {REFRESH_SECONDS} s" if REFRESH_SECONDS
                                                       else "") + (" · READ-ONLY" if READ_ONLY else ""))
    for w in f["warnings"]:
        st.warning(w)


def start_run(mode: str, no_llm: bool = False) -> str:
    guard("starting a workflow run")
    log =paths.LOG_DIR / f"dashboard_run_{clock.now_utc().strftime('%Y%m%d-%H%M%S')}.log"
    cmd = [sys.executable, str(ROOT / "main.py"), "run", "--mode", mode] + (["--no-llm"] if no_llm else [])
    with open(log, "w", encoding="utf-8") as f:
        subprocess.Popen(cmd, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT)
    return log.name


def run_controls(holder):
    a, b = st.columns(2)
    with a:
        if st.button("Run Analysis Now (no orders)", type="primary"):
            if holder:
                st.error("A run is already in progress.")
            else:
                st.success(f"Started an analysis run. Output: logs/{start_run('analysis')}")
    with b:
        with st.popover("Run with orders"):
            mode = st.radio("Mode", ["dry_run", "paper"], horizontal=True,
                            help="dry_run: full workflow, nothing sent. paper: sends paper orders "
                                 "(needs ENABLE_PAPER_ORDERS=true).")
            pin = st.text_input("Admin PIN", type="password", key="run_pin")
            if st.button("Start"):
                if not admin_ok(pin):
                    st.error("Wrong or missing admin PIN (DASHBOARD_ADMIN_PIN in .env).")
                elif holder:
                    st.error("A run is already in progress.")
                else:
                    st.success(f"Started a {mode} run. Output: logs/{start_run(mode)}")


# =========================================================================== pages
def page_overview():
    st.title("AABA Autonomous Multi-Portfolio Trading Agent")
    sim_banner()
    cfg = load_strategy()
    run = dd.last_run()
    snap = dd.last_snapshot_meta()
    from aaba.scheduler import next_runs
    c1, c2, c3 = st.columns(3)
    c1.metric("Last run", run["status"] if run else "never",
              help=f"{run['run_id']} ({run['mode']}, {run['engine']})" if run else None)
    c1.caption(f"{clock.fmt_local(clock.parse_iso(run['started_at']), cfg.reports.timezone)} - mode {run['mode']}"
               if run else "")
    c2.metric("Market data", ("fresh" if snap["fresh"] else "STALE") if snap else "none",
              help=f"latest bar {snap['latest_bar_date']}, expected {snap['expected_bar_date']}" if snap else None)
    c2.caption(f"latest bar {snap['latest_bar_date']}" if snap else "")
    nxt = next_runs()
    if DEMO:
        c3.metric("Scheduler", "not running")
        c3.caption("The demo never trades; paper orders OFF")
    else:
        c3.metric("Next scheduled run", " ".join(nxt[0].split()[1:4]) if nxt else "-")
        c3.caption(f"{nxt[0] if nxt else ''} - mode '{cfg.schedule.mode}', paper orders "
                   f"{'ON' if paper_orders_enabled() else 'OFF'}")
    holder = current_holder()
    if holder:
        st.info(f"A run is in progress: {holder.get('label')}")

    g = dd.group_summary()
    st.subheader("Combined portfolio (sum of the three separate accounts)")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Starting capital", money(g["starting_capital"]))
    cur = g["current_value"]
    c2.metric("Value now", money(cur), delta=f"{(cur / g['starting_capital'] - 1) * 100:+.2f}%" if cur else None)
    c3.metric("Total P&L", money(g["total_pnl"]))
    c4.metric("Today's P&L", money(g["day_pnl"]))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cash", money(g["cash"]))
    c2.metric("Unrealized P&L", money(g["unrealized_pnl"]))
    c3.metric("Realized P&L", money(g["realized_pnl"]))
    c4.metric("SELLs waiting for approval", g["pending_approvals"])
    st.caption("Values come from the latest broker snapshot of each account (saved after every run and every "
               "reconcile). A total shows '-' until all three accounts have a snapshot.")

    rows = []
    for r in g["students"]:
        rows.append({"Student": r["student"], "Account": r["account"] or "-", "Verified": r["account_ok"],
                     "Cash": r["cash"], "Invested": r["invested"], "Equity": r["equity"],
                     "Today's P&L": r["day_pnl"], "Total P&L": r["total_pnl"],
                     "Unrealized P&L": r["unrealized_pnl"], "Realized P&L": r["realized_pnl"],
                     "Return %": r["return_pct"], "BUY": r["buy"], "SELL": r["sell"], "HOLD": r["hold"],
                     "Open orders": r["open_orders"], "Pending approvals": r["pending_approvals"],
                     "Errors": r["errors"], "Snapshot": r["snapshot_at"]})
    st.subheader("Students")
    money_cols = ["Cash", "Invested", "Equity", "Today's P&L", "Total P&L", "Unrealized P&L", "Realized P&L"]
    st.dataframe(pd.DataFrame(rows), hide_index=True,
                 column_config={c: st.column_config.NumberColumn(c, format="$%.2f") for c in money_cols})

    st.subheader("All holdings (each row belongs to one student's own account)")
    gh = dd.group_holdings()
    if gh.empty:
        st.caption("No positions in any account.")
    else:
        st.dataframe(gh.round(2), hide_index=True)

    st.subheader("Return vs SPY")
    series = {}
    gp = dd.group_performance()
    if not gp.empty:
        series["Combined"] = gp["return_pct"]
    for r in g["students"]:
        p = dd.performance(r["student_id"])
        if not p.empty:
            series[r["student"].split(" (")[0]] = p["return_pct"]
            if "spy_return_pct" in p and "SPY" not in series:
                series["SPY"] = p["spy_return_pct"]
    if series:
        st.line_chart(pd.DataFrame(series).sort_index().ffill(), y_label="% since tracking began")
        st.caption("Each line starts at its own first snapshot. SPY is price return only (no dividends); the "
                   "portfolios include their uninvested cash.")
    else:
        st.caption("No portfolio snapshots yet. They are saved after each run.")

    st.subheader("Run now")
    if READ_ONLY:
        st.caption(DEMO_NOTE)
    else:
        run_controls(holder)

    s = dd.trading_sessions()
    st.subheader("Paper-trading period")
    st.write(f"Trading days with a completed **paper** run: **{s['sessions']}**"
             + (f" ({s['first']} to {s['last']})" if s["first"] else "")
             + ". The brief asks for 15-20 trading days; this count is shown as it is.")

    st.subheader("Recent events")
    st.dataframe(pd.DataFrame(controller.audit_log(None, 25)), hide_index=True)


def make_student_page(student_id: str):
    def page():
        stc = load_students().get(student_id)
        st.title(f"{stc.name}")
        st.caption(f"Roll no {stc.roll_no} - module {student_id} - "
                   f"{'enabled' if stc.enabled else 'DISABLED'}")
        sim_banner()
        r = dd.student_summary(student_id)
        c1, c2, c3 = st.columns(3)
        c1.metric("Equity", money(r["equity"]), delta=f"{r['return_pct']:+.2f}%" if r["return_pct"] is not None else None)
        c2.metric("Cash", money(r["cash"]))
        c3.metric("Invested", money(r["invested"]))
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Today's P&L", money(r["day_pnl"]))
        c2.metric("Total P&L", money(r["total_pnl"]))
        c3.metric("Unrealized P&L", money(r["unrealized_pnl"]))
        c4.metric("Realized P&L", money(r["realized_pnl"]))
        st.caption(f"Paper account {r['account'] or '-'} ({'verified' if r['account_ok'] else 'not verified'}); "
                   f"snapshot {r['snapshot_at'] or 'none yet'}")
        st.write(f"**Growth Satellites:** {', '.join(stc.satellites)}  \n**Diversified Core:** {', '.join(stc.core)}")
        if stc.replacements:
            st.caption("Replacements (logged in configs/students.json)")
            st.dataframe(pd.DataFrame([x.model_dump() for x in stc.replacements])[["date", "old", "new", "reason"]],
                         hide_index=True)

        st.subheader("Holdings")
        h = dd.holdings(student_id).round(2)
        if h.empty:
            st.caption("No positions.")
        else:
            st.dataframe(h, hide_index=True)
        st.subheader("Latest signals")
        s = dd.latest_signals(student_id)
        if s.empty:
            st.caption("No signals yet.")
        else:
            st.dataframe(s, hide_index=True)
        st.subheader("SELL approvals")
        ap = dd.approvals_view(student_id)
        if ap.empty:
            st.caption("None.")
        else:
            st.dataframe(ap, hide_index=True)
            st.caption("proposed_price is the signal's close, not a fill. fill_qty / fill_price / realized_pnl "
                       "appear only after the broker reports the SELL as filled. Approve or reject in the "
                       "Approval Centre (needs your own PIN).")
        st.subheader("Orders")
        o = dd.orders_view(student_id)
        if o.empty:
            st.caption("No orders.")
        else:
            st.dataframe(o, hide_index=True)
            st.caption("proposed_price is the price used to size the order; fill_price is the broker-confirmed "
                       "average fill and stays empty until shares are filled (refreshed by every reconcile).")
        st.subheader("Blocked, failed, rejected or cancelled")
        pr = dd.problems_view(student_id)
        if pr.empty:
            st.caption("None.")
        else:
            st.dataframe(pr, hide_index=True)
        st.subheader("Performance")
        p = dd.performance(student_id)
        if p.empty:
            st.caption("No snapshots yet.")
        else:
            st.line_chart(p[["equity"]], y_label="equity $")
            cols = ["return_pct"] + (["spy_return_pct"] if "spy_return_pct" in p else [])
            st.line_chart(p[cols], y_label="% return")
            st.line_chart(p[["drawdown_pct"]], y_label="drawdown %")
        if not READ_ONLY and st.button("Refresh from Alpaca", key=f"rec_{student_id}"):
            try:
                st.write(controller.reconcile_student(student_id))
            except Exception as e:  # noqa: BLE001
                st.error(f"{type(e).__name__}: {e}")
    return page


def page_approvals():
    st.title("Approval Centre")
    sim_banner()
    st.caption("Each SELL needs the owning student's own approval. Enter your student ID and personal PIN. "
               "The server checks both; another student's PIN is refused.")
    approvals.expire_stale()
    group = load_students()
    rows = db.q("SELECT * FROM approvals WHERE status IN ('PENDING','APPROVED') ORDER BY created_at")
    if not rows:
        st.info("No SELL requests waiting.")
    for ap in rows:
        stc = group.get(ap["student_id"])
        with st.container(border=True):
            st.markdown(f"**SELL {ap['qty']:g} {ap['symbol']}** for **{stc.name}** ({stc.roll_no}, "
                        f"{stc.student_id}) - status **{ap['status']}**")
            tz = load_strategy().reports.timezone
            st.caption(f"Account {ap['account_number']} - signal price {money(ap['signal_price'])} - created "
                       f"{clock.fmt_local(clock.parse_iso(ap['created_at']), tz)} - expires "
                       f"{clock.fmt_local(clock.parse_iso(ap['expires_at']), tz)}")
            st.write(ap["reason"])
            ev = json.loads(ap["evidence"] or "{}")
            with st.expander("Indicator evidence"):
                st.json({k: v for k, v in ev.items() if k not in ("news",)})
            if READ_ONLY:
                st.caption(f"Approve / reject: {DEMO_NOTE}" + ("" if DEMO else
                           " Approve with: sudo aaba approve <approval id> --student <S1/S2/S3> on the server."))
                continue
            if ap["status"] == "APPROVED":
                st.info(ap.get("execution_detail") or "Approved; will be sent after the re-check.")
                if st.button("Try to execute now", key=f"x_{ap['approval_id']}"):
                    st.write(controller.execute_approved_sell(ap["approval_id"], actor="dashboard"))
                continue
            with st.form(f"f_{ap['approval_id']}"):
                ids = [s.student_id for s in group.students]
                sid = st.selectbox("Your student ID", ids, index=ids.index(ap["student_id"]),
                                   format_func=lambda x: f"{x} - {group.get(x).name}")
                pin = st.text_input("Your approval PIN", type="password")
                note = st.text_input("Note (optional)")
                c1, c2 = st.columns(2)
                yes = c1.form_submit_button("Approve SELL", type="primary")
                no = c2.form_submit_button("Reject")
            if yes or no:
                try:
                    res = approvals.decide(ap["approval_id"], sid, pin, "approve" if yes else "reject", note)
                    st.success(f"{res['status']} by {res['decided_by']}")
                    if res["status"] == "APPROVED":
                        st.write(controller.execute_approved_sell(ap["approval_id"], actor="dashboard"))
                except approvals.ApprovalError as e:
                    st.error(f"Refused: {e}")
    st.subheader("Decided requests")
    done = pd.DataFrame(db.q("SELECT approval_id, student_id, symbol, qty, status, decided_by, decided_at, "
                             "execution_detail FROM approvals WHERE status NOT IN ('PENDING','APPROVED') "
                             "ORDER BY created_at DESC LIMIT 200"))
    if done.empty:
        st.caption("None yet.")
    else:
        st.dataframe(done, hide_index=True)


def page_config():
    st.title("Strategy and configuration")
    cfg = load_strategy()
    if READ_ONLY:
        sim_banner()
        st.caption(f"Settings shown read-only. Changing them: {DEMO_NOTE}")
        st.json(cfg.model_dump())
        return
    st.caption("Changes are validated before saving and a dated backup is kept in configs/history/. "
               "Saving needs the admin PIN.")
    st.write(f"Paper orders: **{'ON' if paper_orders_enabled() else 'OFF'}** (set ENABLE_PAPER_ORDERS in .env; "
             "the dashboard cannot switch it on). Trading endpoint: paper only.")
    from aaba.llm import describe
    prov, model = describe()
    st.write(f"LLM: **{prov} / {model}** (set in .env). Keys are never shown here.")

    st.subheader("Student modules")
    group = load_students()
    for s in group.students:
        k, sec = s.alpaca_keys()
        st.write(f"- {s.label} [{s.student_id}]: {'enabled' if s.enabled else 'disabled'}, Alpaca keys "
                 f"{'set' if k and sec else 'missing'}, approval PIN {'set' if len(s.approval_pin()) >= 6 else 'missing'}")

    st.subheader("Settings")
    raw = cfg.model_dump()
    edited = {}
    for section, values in raw.items():
        if not isinstance(values, dict):
            edited[section] = st.selectbox(section, ["rules", "composite"], index=["rules", "composite"].index(values)) \
                if section == "signal_mode" else values
            continue
        with st.expander(section):
            out = {}
            for k, v in values.items():
                key = f"cfg_{section}_{k}"
                if isinstance(v, bool):
                    out[k] = st.checkbox(k, v, key=key)
                elif isinstance(v, int):
                    out[k] = int(st.number_input(k, value=v, step=1, key=key))
                elif isinstance(v, float):
                    out[k] = float(st.number_input(k, value=v, key=key, format="%.4f"))
                elif isinstance(v, list):
                    out[k] = [x.strip() for x in st.text_input(k, ", ".join(map(str, v)), key=key).split(",") if x.strip()]
                elif section == "schedule" and k == "mode":
                    out[k] = st.selectbox(k, ["analysis", "dry_run", "paper"],
                                          index=["analysis", "dry_run", "paper"].index(v), key=key)
                else:
                    out[k] = st.text_input(k, str(v), key=key)
            edited[section] = out
    enabled = {s.student_id: st.checkbox(f"Enable {s.label}", s.enabled, key=f"en_{s.student_id}")
               for s in group.students}
    pin = st.text_input("Admin PIN", type="password", key="cfg_pin")
    if st.button("Validate and save", type="primary"):
        try:
            StrategyConfig(**edited)
        except Exception as e:  # noqa: BLE001
            st.error(f"Not saved, invalid settings: {e}")
        else:
            if not admin_ok(pin):
                st.error("Wrong or missing admin PIN.")
            else:
                save_strategy(edited, "dashboard")
                for sid, en in enabled.items():
                    if group.get(sid).enabled != en:
                        set_student_enabled(sid, en)
                db.log_event("INFO", "config", "strategy/students config changed from dashboard")
                st.success("Saved. The next run uses the new settings. (Restart the scheduler to change run times.)")


def page_history():
    st.title("Trade history and reports")
    sim_banner()
    group = load_students()
    c1, c2, c3 = st.columns(3)
    kind = c1.selectbox("Records", ["signals", "orders", "approvals", "events", "snapshots"])
    who = c2.selectbox("Student", ["all"] + [s.student_id for s in group.students])
    text = c3.text_input("Search text")
    d1, d2 = st.columns(2)
    start = d1.date_input("From", date.today() - timedelta(days=30))
    end = d2.date_input("To", date.today())
    df = dd.history(kind, None if who == "all" else who, text, start, end)
    st.caption(f"{len(df)} rows")
    st.dataframe(df, hide_index=True)
    if not df.empty:
        st.download_button("Download CSV", df.to_csv(index=False), f"{kind}.csv", "text/csv")

    st.subheader("Agent reports (model-run/)")
    files = dd.model_run_files()
    if files:
        pick = st.selectbox("File", files, format_func=lambda p: p.name)
        st.markdown(Path(pick).read_text(encoding="utf-8"))
    else:
        st.caption("No reports yet.")

    st.subheader("Backtests")
    bts = sorted(paths.BACKTEST_DIR.glob("*.md"), reverse=True)
    if bts:
        pick = st.selectbox("Backtest", bts, format_func=lambda p: p.name)
        st.markdown(Path(pick).read_text(encoding="utf-8"))
    else:
        st.caption("No backtests yet: python main.py backtest")


def page_system():
    st.title("System checks")
    sim_banner()
    if READ_ONLY:
        st.caption(f"Account checks, MCP self-test, symbol validation and run logs: {DEMO_NOTE}")
        return
    st.write(f"Project folder: `{ROOT}`  \nDatabase: `{paths.db_path()}`")
    if st.button("Check paper accounts"):
        st.write(brokers.verify_accounts())
    if st.button("MCP self-test (start server, list tools, call one)"):
        r = subprocess.run([sys.executable, str(ROOT / "mcp_server" / "mcp_client_test.py")], capture_output=True,
                           text=True, timeout=180, cwd=str(ROOT))
        st.code(r.stdout[-4000:] + r.stderr[-1500:])
    if st.button("Validate symbols (tradable, history, beta, correlation)"):
        from aaba.validation import validate_symbols
        with st.spinner("Checking..."):
            st.write(validate_symbols())
    st.subheader("Run logs")
    logs = sorted(paths.LOG_DIR.glob("dashboard_run_*.log"), reverse=True)[:10]
    for lg in logs:
        with st.expander(lg.name):
            st.code(lg.read_text(encoding="utf-8", errors="replace")[-5000:])


# =========================================================================== navigation
group = load_students()
# AABA_DASHBOARD_PAGE picks the start page (used by the tests to render every page)
start = os.getenv("AABA_DASHBOARD_PAGE", "overview")
pages = {
    "Dashboard": [st.Page(page_overview, title="Overview", icon="📊", url_path="overview",
                          default=start == "overview")],
    "Students": [st.Page(make_student_page(s.student_id), title=s.name, icon="👤", url_path=f"student-{s.student_id}",
                         default=start == f"student-{s.student_id}") for s in group.students],
    "Actions": [st.Page(page_approvals, title="Approval Centre", icon="✅", url_path="approvals",
                        default=start == "approvals"),
                st.Page(page_config, title="Strategy & Config", icon="⚙️", url_path="config",
                        default=start == "config")],
    "Records": [st.Page(page_history, title="History & Reports", icon="🗂️", url_path="history",
                        default=start == "history"),
                st.Page(page_system, title="System", icon="🛠️", url_path="system", default=start == "system")],
}
if REFRESH_SECONDS:
    @st.fragment(run_every=REFRESH_SECONDS)
    def _auto_refresh():
        # first call (inside a full page run) arms the timer; the timed call reruns the whole page
        if st.session_state.get("_refresh_armed"):
            st.session_state["_refresh_armed"] = False
            st.rerun()
        st.session_state["_refresh_armed"] = True

    _auto_refresh()
st.navigation(pages).run()
