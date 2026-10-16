"""AABA trading agent - command line entry point.

    python main.py init                      create folders and the database
    python main.py check                     check .env, packages, accounts, LLM settings and the MCP server
    python main.py validate                  pre-trade symbol checks (tradable, history, beta, correlation)
    python main.py run --mode analysis       one workflow run, no orders (safe)
    python main.py run --mode dry_run        full workflow incl. approvals, but nothing is sent
    python main.py run --mode paper          real PAPER orders (also needs ENABLE_PAPER_ORDERS=true)
    python main.py run --mode analysis --no-llm   run without the LLM (rules engine only)
    python main.py scheduler                 start the daily scheduler
    python main.py approvals                 list pending SELL approvals
    python main.py approve <id> --student S2 [--reject]   decide a SELL (asks for the PIN)
    python main.py backtest --student S2     walk-forward backtest (all students if omitted)
    python main.py backtest-intraday         10-minute intraday engine vs daily strategy, buy-and-hold, SPY
    python main.py reconcile                 refresh order statuses and save snapshots
    python main.py mcp-test                  start the MCP server, list tools and call one
    python main.py llm-test                  check the LLM answers and can call tools (needed by the agent)
    python main.py dashboard                 start the Streamlit web app
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from aaba import db, paths  # noqa: E402
from aaba.settings import is_simulated, load_strategy, load_students, paper_orders_enabled  # noqa: E402


def cmd_init(_):
    paths.ensure_dirs()
    db.init_db()
    if not (ROOT / ".env").exists():
        print("No .env file yet: copy .env.example to .env and fill in your keys.")
    print(f"Ready. Database: {paths.db_path()}")


def cmd_check(_):
    import platform
    from importlib.metadata import PackageNotFoundError, version
    paths.ensure_dirs()
    db.init_db()
    ok = True
    print(f"Python {platform.python_version()} ({sys.executable})")
    if sys.version_info[:2] not in ((3, 11), (3, 12)):
        print("  ! Python 3.11 or 3.12 is recommended (CrewAI 1.15 supports 3.10-3.13).")
    for pkg in ("crewai", "crewai-tools", "mcp", "alpaca-py", "pandas", "streamlit", "APScheduler"):
        try:
            print(f"  {pkg} {version(pkg)}")
        except PackageNotFoundError:
            ok = False
            print(f"  ! {pkg} missing: pip install -r requirements.txt")
    print(f".env present: {(ROOT / '.env').exists()}")
    group = load_students()
    cfg = load_strategy()
    print(f"Students: {', '.join(s.label + ('' if s.enabled else ' [disabled]') for s in group.students)}")
    from aaba.approvals import pin_problem
    for s in group.students:
        k, sec = s.alpaca_keys()
        problem = pin_problem(s.approval_pin())
        ok = ok and not problem
        print(f"  {s.student_id}: keys {'set' if k and sec else 'MISSING'}, approval PIN "
              f"{'set' if not problem else 'NOT USABLE: ' + problem}")
    admin_problem = pin_problem(os.getenv("DASHBOARD_ADMIN_PIN", ""))
    print(f"  dashboard admin PIN {'set' if not admin_problem else 'NOT USABLE: ' + admin_problem}")
    from aaba.llm import describe
    prov, model = describe()
    key_env = {"groq": "GROQ_API_KEY", "openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_API_KEY"}.get(prov)
    llm_key_ok = not key_env or bool(os.getenv(key_env))
    print(f"LLM: {prov} / {model}; key {'set' if llm_key_ok else 'MISSING ' + key_env}")
    if cfg.agent.use_llm and not llm_key_ok:
        ok = False
        print("  ! Without the LLM key, runs fall back to the rules engine only (no CrewAI reasoning).")
    from aaba.llm import looks_small
    if looks_small(model):
        print(f"  ! '{model}' is a very small model. It usually cannot call tools reliably, so the safety net\n"
              f"    will end up doing the agent's work. Run 'python main.py llm-test' and see README step 5.")
    for s in group.enabled:
        if not all(s.alpaca_keys()) and not is_simulated():
            ok = False
    print(f"Schedule: {cfg.schedule.run_times} {cfg.schedule.timezone}, mode {cfg.schedule.mode}; "
          f"ENABLE_PAPER_ORDERS={paper_orders_enabled()}")
    print("\nPaper accounts:")
    from aaba.broker import verify_accounts
    for sid, r in verify_accounts().items():
        state = "OK" if r["ok"] else "BLOCKED"
        print(f"  {sid}: {state} account={r['account_number']} {'; '.join(r['problems'] + r['warnings'])}")
        ok = ok and r["ok"]
    print("\nMarket clock:")
    try:
        from aaba.market_data import get_provider
        c = get_provider().market_clock()
        print(f"  New York {c.timestamp:%Y-%m-%d %H:%M}, open={c.is_open}, next open {c.next_open:%Y-%m-%d %H:%M}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  ! {type(e).__name__}: {e}")
    print("\nMCP server:")
    rc = subprocess.call([sys.executable, str(ROOT / "mcp_server" / "mcp_client_test.py"), "--timeout", "60"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("  start/list/call: " + ("OK" if rc == 0 else "FAILED (run: python main.py mcp-test)"))
    ok = ok and rc == 0
    print("\nAll checks passed." if ok else "\nSome checks need attention (see above).")
    return 0 if ok else 1


def cmd_validate(_):
    from aaba.validation import validate_symbols
    r = validate_symbols()
    for sid, s in r["students"].items():
        print(f"{s['student']}: {'all usable' if s['all_ok'] else 'PROBLEMS'}")
        for f in s["flags"]:
            print(f"   - {f}")
    print("Reserves:", r["reserves"])
    print("Saved:", *r["files"], sep="\n  ")


def cmd_run(a):
    from agents.trading_crew import run_workflow
    if a.mode == "paper" and not paper_orders_enabled():
        print("Note: ENABLE_PAPER_ORDERS is not true in .env, so no order will be sent in this run.")
    res = run_workflow(a.mode, "cli", use_llm=not a.no_llm)
    print(json.dumps({k: v for k, v in res.items() if k != "outcomes"}, indent=2, default=str))
    for o in res["outcomes"]:
        print(f"  {o['student_id']} {o['action']:<4} {o['outcome']}: {o['n']}")
    return 0 if res["status"] == "DONE" and not res.get("agent_error") else 1


def cmd_scheduler(_):
    from aaba.scheduler import start
    start()


def cmd_approvals(_):
    from aaba import approvals
    db.init_db()
    approvals.expire_stale()
    rows = db.q("SELECT * FROM approvals WHERE status IN ('PENDING','APPROVED') ORDER BY created_at")
    if not rows:
        print("No open SELL approvals.")
    for r in rows:
        print(f"{r['approval_id']}  {r['student_id']}  SELL {r['qty']:g} {r['symbol']}  {r['status']}  "
              f"expires {r['expires_at']}\n    {r['reason'][:200]}")


def cmd_approve(a):
    from aaba import approvals, controller
    pin = getpass.getpass(f"Approval PIN for {a.student}: ")
    try:
        ap = approvals.decide(a.approval_id, a.student, pin, "reject" if a.reject else "approve", a.note or "")
    except approvals.ApprovalError as e:
        print(f"Refused: {e}")
        return 1
    print(f"{ap['status']} by {ap['decided_by']}")
    if ap["status"] == "APPROVED":
        print(json.dumps(controller.execute_approved_sell(a.approval_id, actor="cli"), indent=2, default=str))
    return 0


def cmd_backtest(a):
    from aaba.backtest import run_backtest
    ids = [a.student] if a.student else [s.student_id for s in load_students().enabled]
    for sid in ids:
        print(f"Backtesting {sid} (this can take a few minutes)...")
        r = run_backtest(sid, tune=not a.no_tune)
        print(f"  tuning {r['tuning_period']}, test {r['test_period']}, chosen {r['chosen_params']}")
        for k, v in r["test_results"].items():
            print(f"  {k:<17} {v['metrics']}")
        print("  saved:", *r["files"])


def cmd_backtest_intraday(a):
    from aaba.backtest_intraday import run_comparison
    print("Intraday backtest on real 5-minute history (several minutes; sends no orders)...")
    r = run_comparison(approval_delay_min=a.approval_delay)
    for label, w in r["windows"].items():
        print(f"\n{label}: {w['period'][0]} to {w['period'][1]} ({w['sessions']} sessions), all three students combined")
        for k, m in w["combined"].items():
            print(f"  {k:<18} return {m['total_return_pct']:>7}%   max drawdown {m['max_drawdown_pct']:>7}%   "
                  f"Sharpe {m['sharpe']:>6}")
    print("\nsaved:", r["file"])


def cmd_backtest_v3(_):
    from aaba.backtest_v3 import run
    print("Engine v3: tune on 15 Oct 2025-16 Apr 2026, freeze, evaluate once on 17 Apr-15 Oct 2026 (sends no orders)")
    r = run()
    fz = r["frozen"]
    print("chosen on the tuning window:", fz["chosen"])
    for k, m in r["combined"].items():
        print(f"  {k:<15} return {m['total_return_pct']:>7}%  max drawdown {m['max_drawdown_pct']:>7}%  "
              f"Sharpe {m['sharpe']:>5}  costs ${m['costs_total']:,.0f}")
    print("saved:", r["file"])


def cmd_reconcile(_):
    from aaba import controller
    db.init_db()
    for s in load_students().enabled:
        try:
            print(controller.reconcile_student(s.student_id))
        except Exception as e:  # noqa: BLE001
            print(f"{s.student_id}: {type(e).__name__}: {e}")


def cmd_llm_test(_):
    """Check that the configured LLM answers and can call a tool (what the agent needs)."""
    from aaba.llm import LLMConfigError, build, describe, looks_small, tool_call_probe
    prov, model = describe()
    print(f"LLM: {prov} / {model}")
    try:
        llm = build()
    except LLMConfigError as e:
        print(f"FAILED to set up: {e}")
        return 1
    results = [tool_call_probe(llm) for _ in range(3)]
    for i, r in enumerate(results, 1):
        print(f"  try {i}: {'OK  ' if r['ok'] else 'FAIL'} {r['seconds']:>6}s  {r['detail']}")
    good = sum(r["ok"] for r in results)
    slow = max(r["seconds"] for r in results)
    if good == 3 and slow < 60:
        print("Result: this model can drive the agent.")
        return 0
    if good == 3:
        print(f"Result: tool calls work but are slow ({slow}s). Raise LLM_TIMEOUT and "
              f"agent.crew_time_budget_seconds, or use a faster model.")
        return 0
    print(f"Result: {good}/3 tool calls worked. The agent will often fail and the safety net will do the work."
          + (" This is expected for 1B-3B models." if looks_small(model) else ""))
    print("Fix: use a model that supports tool calling, e.g. Groq 'openai/gpt-oss-120b' (free key) or a "
          "local 7B+ tool model such as 'qwen3:8b' or 'llama3.1:8b' in Ollama.")
    return 1


def cmd_mcp_test(a):
    return subprocess.call([sys.executable, str(ROOT / "mcp_server" / "mcp_client_test.py")]
                           + (["--prepare"] if a.prepare else []))


def cmd_dashboard(a):
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(ROOT / "app" / "streamlit_app.py"),
                            "--server.port", str(a.port), "--server.headless", "true"], cwd=str(ROOT))


def main():
    ap = argparse.ArgumentParser(description="AABA multi-portfolio trading agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("validate").set_defaults(fn=cmd_validate)
    p = sub.add_parser("run")
    p.add_argument("--mode", choices=["analysis", "dry_run", "paper"], default="analysis")
    p.add_argument("--no-llm", action="store_true")
    p.set_defaults(fn=cmd_run)
    sub.add_parser("scheduler").set_defaults(fn=cmd_scheduler)
    sub.add_parser("approvals").set_defaults(fn=cmd_approvals)
    p = sub.add_parser("approve")
    p.add_argument("approval_id")
    p.add_argument("--student", required=True)
    p.add_argument("--reject", action="store_true")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("backtest")
    p.add_argument("--student")
    p.add_argument("--no-tune", action="store_true")
    p.set_defaults(fn=cmd_backtest)
    p = sub.add_parser("backtest-intraday")
    p.add_argument("--approval-delay", type=int, default=30, help="minutes until a SELL request is approved")
    p.set_defaults(fn=cmd_backtest_intraday)
    sub.add_parser("backtest-v3").set_defaults(fn=cmd_backtest_v3)
    sub.add_parser("reconcile").set_defaults(fn=cmd_reconcile)
    sub.add_parser("llm-test").set_defaults(fn=cmd_llm_test)
    p = sub.add_parser("mcp-test")
    p.add_argument("--prepare", action="store_true")
    p.set_defaults(fn=cmd_mcp_test)
    p = sub.add_parser("dashboard")
    p.add_argument("--port", type=int, default=8501)
    p.set_defaults(fn=cmd_dashboard)
    a = ap.parse_args()
    paths.ensure_dirs()
    db.init_db()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
