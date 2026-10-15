"""Standalone MCP client test.

Starts the local MCP server with this same Python, connects over stdio, lists the
tools, calls get_latest_indicators for a known student and symbol, prints the
result and closes cleanly.

    python mcp_server/mcp_client_test.py                 # uses existing indicator files
    python mcp_server/mcp_client_test.py --prepare       # first loads fresh data (analysis run)
    python mcp_server/mcp_client_test.py --student S2 --symbol AMD
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp_server" / "technical_indicators_server.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _text(result) -> str:
    parts = [getattr(c, "text", "") for c in (result.content or [])]
    return "\n".join(p for p in parts if p)


async def main(student: str, symbol: str, prepare: bool, timeout: float) -> int:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        print("The 'mcp' package is missing. Run: pip install -r requirements.txt")
        return 2
    if not SERVER.exists():
        print(f"Server file not found: {SERVER}")
        return 2

    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=dict(os.environ), cwd=str(ROOT))
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                info = await asyncio.wait_for(session.initialize(), timeout)
                print(f"Connected to MCP server: {info.serverInfo.name} (protocol {info.protocolVersion})")
                tools = await asyncio.wait_for(session.list_tools(), timeout)
                print(f"\n{len(tools.tools)} tools:")
                for t in tools.tools:
                    print(f"  - {t.name}: {(t.description or '').splitlines()[0][:90]}")

                if prepare:
                    from aaba import db
                    db.init_db()
                    run_id = db.create_run("analysis", "mcp_client_test", "mcp-client-test", None, None, False)
                    print(f"\nprepare_market_data(run_id={run_id}) ...")
                    res = await asyncio.wait_for(session.call_tool("prepare_market_data", {"run_id": run_id}), 300)
                    print(_text(res)[:1500])
                    db.finish_run(run_id, "DONE", notes="created by mcp_client_test --prepare")

                print(f"\nget_latest_indicators(student_id={student}, symbol={symbol}):")
                res = await asyncio.wait_for(
                    session.call_tool("get_latest_indicators", {"student_id": student, "symbol": symbol}), timeout)
                body = _text(res)
                try:
                    print(json.dumps(json.loads(body), indent=2))
                except ValueError:
                    print(body)
                if "no indicator file" in body:
                    print("\n(No data yet: run with --prepare, or run an analysis first.)")

                print("\nInvalid input check: get_latest_indicators(S2, 'ZZZZ')")
                res = await asyncio.wait_for(
                    session.call_tool("get_latest_indicators", {"student_id": student, "symbol": "ZZZZ"}), timeout)
                print(_text(res))
        print("\nSession closed cleanly.")
        return 0
    except asyncio.TimeoutError:
        print(f"Timed out after {timeout}s waiting for the server.")
        return 1
    except Exception as e:  # startup failure, broken pipe, etc.
        print(f"MCP test failed: {type(e).__name__}: {e}")
        print("Check logs/mcp_server.log and that dependencies are installed.")
        return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--student", default="S2")
    ap.add_argument("--symbol", default="AMD")
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--timeout", type=float, default=60)
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.student, a.symbol.upper(), a.prepare, a.timeout)))
