"""Local MCP server (stdio) for the AABA trading agent.

This replaces the old single-tool server of the same name. It still offers
get_latest_indicators(student_id, symbol) and adds the tools the CrewAI agent
needs: batch market data, account and tradability checks, portfolio reads,
signal evaluation, risk preview, order and approval actions, and audit reads.

Run alone:  python mcp_server/technical_indicators_server.py
It then waits silently for an MCP client on stdin/stdout. That is normal. Test it
with:  python mcp_server/mcp_client_test.py

Nothing is ever printed to stdout here except MCP messages; logs go to logs/mcp_server.log.
All safety rules live in aaba/controller.py and aaba/risk.py, so a client cannot
skip them by calling tools in an unexpected order.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from aaba import broker as brokers  # noqa: E402
from aaba import controller, db, paths  # noqa: E402
from aaba.settings import load_students  # noqa: E402

paths.ensure_dirs()
db.init_db()
logging.basicConfig(filename=paths.LOG_DIR / "mcp_server.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("aaba.mcp")

mcp = FastMCP("AABA Trading Tools", log_level="WARNING")


def _safe(fn, *args, **kwargs):
    """Never let a Python error crash the server; return it as data instead."""
    try:
        return fn(*args, **kwargs)
    except KeyError as e:
        return {"error": f"not found: {e}"}
    except Exception as e:  # noqa: BLE001
        log.exception("tool error")
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}


@mcp.tool()
def prepare_market_data(run_id: str) -> dict:
    """Load daily bars for all students' stocks plus SPY in ONE batch request, then latest prices,
    news and earnings, and calculate indicators. Call once at the start of a run."""
    return _safe(controller.prepare_market_data, run_id)


@mcp.tool()
def check_accounts() -> dict:
    """Verify each student's Alpaca PAPER account (paper endpoint, account number, not shared)."""
    def run():
        res = brokers.verify_accounts()
        return {sid: {"ok": r["ok"], "account_number": r["account_number"], "problems": r["problems"],
                      "warnings": r["warnings"]} for sid, r in res.items()}
    return _safe(run)


@mcp.tool()
def check_tradability(student_id: str) -> dict:
    """Check that each of the student's stocks is active and tradable on Alpaca."""
    def run():
        st = load_students().get(student_id)
        b = brokers.get_broker(st)
        out = {}
        for sym in st.symbols:
            try:
                a = b.asset(sym)
                out[sym] = {"ok": a["status"].lower() == "active" and a["tradable"], **a}
            except Exception as e:  # noqa: BLE001
                out[sym] = {"ok": False, "error": type(e).__name__}
        return {"student_id": student_id, "assets": out}
    return _safe(run)


@mcp.tool()
def get_latest_indicators(student_id: str, symbol: str) -> dict:
    """Latest indicator row (EMA, RSI, MACD, ATR, Bollinger, OBV) for one of the student's stocks."""
    return _safe(controller.get_latest_indicators, student_id, symbol)


@mcp.tool()
def get_portfolio(student_id: str) -> dict:
    """Cash, equity and positions in this student's own paper account."""
    return _safe(controller.get_portfolio, student_id)


@mcp.tool()
def evaluate_student(run_id: str, student_id: str) -> dict:
    """Calculate BUY/SELL/HOLD for each of the student's stocks with the rules engine. HOLDs are
    recorded automatically. Returns signal_id values for the BUY and SELL actions."""
    return _safe(controller.evaluate_student, run_id, student_id)


@mcp.tool()
def preview_buy_risk(signal_id: str) -> dict:
    """Read-only: run the BUY risk checks and sizing for a signal without sending an order."""
    return _safe(controller.preview_buy, signal_id)


@mcp.tool()
def submit_buy(signal_id: str, reason: str) -> dict:
    """Act on a BUY signal. Code re-runs every risk check and sends the order to the right
    student's paper account only if all pass (and only in paper mode)."""
    return _safe(controller.submit_buy, signal_id, reason, "agent")


@mcp.tool()
def create_sell_approval(signal_id: str, reason: str) -> dict:
    """Act on a SELL signal: create an approval request for the owning student. Never sells directly."""
    return _safe(controller.create_sell_approval, signal_id, reason, "agent")


@mcp.tool()
def record_hold(signal_id: str, reason: str) -> dict:
    """Decline a BUY signal (keep it as HOLD) with a reason based on the evidence. Not allowed for SELL."""
    return _safe(controller.record_hold, signal_id, reason, "agent")


@mcp.tool()
def process_approved_sells(student_id: str, run_id: str) -> dict:
    """Re-check and send SELL orders that this student has already approved (not in analysis runs)."""
    return _safe(controller.process_approved_sells, student_id, run_id)


@mcp.tool()
def reconcile_student(student_id: str, run_id: str) -> dict:
    """Update order statuses from the broker and save a portfolio snapshot for the student."""
    return _safe(controller.reconcile_student, student_id, run_id)


@mcp.tool()
def get_execution_status(student_id: str) -> dict:
    """Recent orders and approvals for one student."""
    return _safe(controller.execution_status, student_id)


@mcp.tool()
def get_audit_log(student_id: str, limit: int = 20) -> list | dict:
    """Recent audit events for one student."""
    return _safe(controller.audit_log, student_id, max(1, min(int(limit), 100)))


if __name__ == "__main__":
    mcp.run()   # stdio transport
