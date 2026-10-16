"""Read-only safety check of the three Alpaca PAPER accounts. Sends no orders.

    python scripts/paper_preflight.py            # human-readable, account numbers masked
    python scripts/paper_preflight.py --json F   # also save a state snapshot (for before/after compare)

Checks per student: keys present, client on the paper endpoint, account ACTIVE and not blocked,
account number matches STUDENTn_ALPACA_ACCOUNT_NUMBER when that is set (reported as NOT PINNED
otherwise), and that no two students resolve to the same account. Exit code 1 if any check fails.
Keys and PINs are never printed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
PAPER_URL = "https://paper-api.alpaca.markets"
PREFIX = {"S1": "STUDENT1", "S2": "STUDENT2", "S3": "STUDENT3"}


def mask(acct: str | None) -> str:
    return f"{acct[:4]}****{acct[-2:]}" if acct and len(acct) > 6 else "****"


def check() -> tuple[bool, dict]:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    import os
    env = dotenv_values(os.getenv("AABA_ENV_FILE") or (ROOT / ".env"))   # server: /etc/aaba/engine.env
    out, ok = {}, True
    for sid, pre in PREFIX.items():
        r = {"problems": [], "pinned": False}
        key, sec = (env.get(f"{pre}_ALPACA_API_KEY") or "").strip(), (env.get(f"{pre}_ALPACA_SECRET_KEY") or "").strip()
        if not key or not sec:
            r["problems"].append("keys missing")
            out[sid] = r
            continue
        try:
            c = TradingClient(key, sec, paper=True)
            base = str(getattr(c._base_url, "value", c._base_url)).rstrip("/")
            if base != PAPER_URL:
                r["problems"].append(f"endpoint is not paper: {base}")
                out[sid] = r
                continue
            a = c.get_account()
            r["account_number"] = a.account_number
            r["status"] = str(getattr(a.status, "value", a.status))
            if r["status"].upper() != "ACTIVE":
                r["problems"].append(f"status {r['status']}")
            if a.trading_blocked or a.account_blocked:
                r["problems"].append("account blocked")
            expected = (env.get(f"{pre}_ALPACA_ACCOUNT_NUMBER") or "").strip()
            if expected:
                r["pinned"] = True
                if expected != a.account_number:
                    r["problems"].append("account number does not match the pinned value")
            r["cash"] = float(a.cash)
            r["equity"] = float(a.equity)
            r["positions"] = {p.symbol: float(p.qty) for p in c.get_all_positions()}
            r["open_orders"] = [o.client_order_id for o in
                                c.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))]
        except Exception as e:  # noqa: BLE001
            r["problems"].append(f"{type(e).__name__}: {str(e)[:120]}")
        out[sid] = r
    seen: dict[str, list[str]] = {}
    for sid, r in out.items():
        if r.get("account_number"):
            seen.setdefault(r["account_number"], []).append(sid)
    for sids in seen.values():
        if len(sids) > 1:
            for sid in sids:
                out[sid]["problems"].append(f"same account as {', '.join(s for s in sids if s != sid)}")
    for r in out.values():
        ok = ok and not r["problems"]
    return ok, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="save the state snapshot (account numbers included) to this file")
    a = ap.parse_args()
    ok, out = check()
    for sid, r in out.items():
        print(f"{sid}: account {mask(r.get('account_number'))} status={r.get('status', '-')} "
              f"pinned={'yes' if r['pinned'] else 'NOT PINNED'} equity={r.get('equity', '-')} "
              f"positions={len(r.get('positions', {}))} open_orders={len(r.get('open_orders', []))} "
              f"-> {'OK' if not r['problems'] else 'FAIL: ' + '; '.join(r['problems'])}")
    print(f"distinct paper accounts: {'yes' if ok else 'NO'}; endpoint {PAPER_URL}")
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
