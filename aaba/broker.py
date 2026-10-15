"""Broker access for each student's own Alpaca PAPER account.

Safety rules in this file:
- The Alpaca client is always created with paper=True, and the base URL is checked
  to be https://paper-api.alpaca.markets before anything else happens. There is no
  setting that switches to live trading.
- Each student uses keys from their own env prefix (STUDENT1_..., STUDENT2_...).
- verify_accounts() records each account number and blocks orders for any student
  whose account cannot be verified or is shared with another student.

FakeBroker keeps a small JSON file per student. It exists only for automated tests
and offline demos and is labelled SIMULATED wherever it is used.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass

from . import clock, db, demo, paths
from .settings import StudentConfig, broker_backend, load_students

log = logging.getLogger("aaba.broker")
PAPER_URL = "https://paper-api.alpaca.markets"
FINAL_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced", "done_for_day", "stopped",
                  "suspended"}


class BrokerError(RuntimeError):
    pass


@dataclass
class OrderResult:
    broker_order_id: str | None
    client_order_id: str
    status: str
    filled_qty: float = 0.0
    filled_avg_price: float | None = None


def _num(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# =========================================================================== Alpaca
class AlpacaPaperBroker:
    simulated = False

    def __init__(self, student: StudentConfig):
        demo.guard("connecting to Alpaca")
        key, secret = student.alpaca_keys()
        if not key or not secret:
            raise BrokerError(f"{student.student_id}: missing {student.env_prefix}_ALPACA_API_KEY / "
                              f"{student.env_prefix}_ALPACA_SECRET_KEY in .env")
        from alpaca.trading.client import TradingClient
        self.student = student
        self.client = TradingClient(key, secret, paper=True)
        base = getattr(self.client, "_base_url", None)
        base = getattr(base, "value", base)
        if str(base).rstrip("/") != PAPER_URL:
            raise BrokerError(f"refusing to continue: trading endpoint is {base}, not the paper endpoint")
        self.base_url = str(base)
        self.key_prefix_ok = key.upper().startswith("PK")   # Alpaca paper key IDs start with PK

    def account(self) -> dict:
        a = self.client.get_account()
        return {"account_number": a.account_number, "status": str(getattr(a.status, "value", a.status)),
                "cash": _num(a.cash), "equity": _num(a.equity), "last_equity": _num(a.last_equity),
                "buying_power": _num(a.buying_power),
                "non_marginable_buying_power": _num(a.non_marginable_buying_power),
                "long_market_value": _num(a.long_market_value), "trading_blocked": bool(a.trading_blocked),
                "account_blocked": bool(a.account_blocked), "currency": a.currency}

    def positions(self) -> dict[str, dict]:
        out = {}
        for p in self.client.get_all_positions():
            out[p.symbol] = {"qty": _num(p.qty), "avg_entry": _num(p.avg_entry_price),
                             "market_value": _num(p.market_value), "unrealized_pl": _num(p.unrealized_pl),
                             "current_price": _num(p.current_price), "side": str(getattr(p.side, "value", p.side))}
        return out

    def open_orders(self) -> list[dict]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        return [{"symbol": o.symbol, "side": str(getattr(o.side, "value", o.side)), "qty": _num(o.qty),
                 "client_order_id": o.client_order_id, "status": str(getattr(o.status, "value", o.status)),
                 "notional": _num(o.notional)} for o in orders]

    def asset(self, symbol: str) -> dict:
        a = self.client.get_asset(symbol)
        return {"symbol": a.symbol, "status": str(getattr(a.status, "value", a.status)),
                "tradable": bool(a.tradable), "fractionable": bool(a.fractionable),
                "exchange": str(getattr(a.exchange, "value", a.exchange))}

    def submit_market_order(self, symbol: str, qty: float, side: str, client_order_id: str,
                            est_price: float | None = None) -> OrderResult:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                                 time_in_force=TimeInForce.DAY, client_order_id=client_order_id)
        o = self.client.submit_order(req)
        return self._result(o)

    def order_by_client_id(self, client_order_id: str) -> OrderResult | None:
        try:
            o = self.client.get_order_by_client_id(client_order_id)
        except Exception as e:
            if getattr(e, "status_code", None) == 404 or "not found" in str(e).lower():
                return None
            raise
        return self._result(o)

    @staticmethod
    def _result(o) -> OrderResult:
        return OrderResult(str(o.id), o.client_order_id, str(getattr(o.status, "value", o.status)),
                           _num(o.filled_qty), _num(o.filled_avg_price, None) if o.filled_avg_price else None)


# =========================================================================== Fake
_fake_lock = threading.Lock()


class FakeBroker:
    """SIMULATED paper account stored in data/fake_broker/<student>.json."""
    simulated = True
    base_url = "simulated://fake-broker"
    key_prefix_ok = True

    def __init__(self, student: StudentConfig, account_number: str | None = None):
        self.student = student
        self.file = paths.FAKE_BROKER_DIR / f"{student.student_id}.json"
        self.file.parent.mkdir(parents=True, exist_ok=True)
        if not self.file.exists():
            self._save({"account_number": account_number or f"FAKE-{student.student_id}",
                        "cash": float(student.starting_capital), "positions": {}, "orders": {},
                        "assets_untradable": [], "fail_submit": False})
        self.submitted: list[dict] = []

    def _load(self) -> dict:
        return json.loads(self.file.read_text(encoding="utf-8"))

    def _save(self, st: dict) -> None:
        self.file.write_text(json.dumps(st, indent=1), encoding="utf-8")

    def set_state(self, **kw) -> None:
        with _fake_lock:
            st = self._load()
            st.update(kw)
            self._save(st)

    def account(self) -> dict:
        st = self._load()
        lv = sum(p["qty"] * p["last_price"] for p in st["positions"].values())
        return {"account_number": st["account_number"], "status": "ACTIVE", "cash": st["cash"],
                "equity": st["cash"] + lv, "last_equity": st.get("last_equity", st["cash"] + lv),
                "buying_power": st["cash"], "non_marginable_buying_power": st["cash"],
                "long_market_value": lv, "trading_blocked": False, "account_blocked": False, "currency": "USD"}

    def positions(self) -> dict[str, dict]:
        st = self._load()
        return {s: {"qty": p["qty"], "avg_entry": p["avg_entry"], "market_value": p["qty"] * p["last_price"],
                    "unrealized_pl": p["qty"] * (p["last_price"] - p["avg_entry"]),
                    "current_price": p["last_price"], "side": "long"}
                for s, p in st["positions"].items() if p["qty"] > 0}

    def open_orders(self) -> list[dict]:
        return [o for o in self._load()["orders"].values() if o["status"] in ("new", "accepted")]

    def asset(self, symbol: str) -> dict:
        bad = symbol in self._load().get("assets_untradable", [])
        return {"symbol": symbol, "status": "inactive" if bad else "active", "tradable": not bad,
                "fractionable": True, "exchange": "SIMULATED"}

    def submit_market_order(self, symbol: str, qty: float, side: str, client_order_id: str,
                            est_price: float | None = None) -> OrderResult:
        demo.guard("order submission")
        with _fake_lock:
            st = self._load()
            if st.get("fail_submit"):
                raise BrokerError("simulated broker outage")
            if client_order_id in st["orders"]:
                raise BrokerError("client_order_id must be unique")
            price = float(est_price or 0)
            pos = st["positions"].setdefault(symbol, {"qty": 0.0, "avg_entry": 0.0, "last_price": price})
            if side == "buy":
                cost = qty * price
                if cost > st["cash"] + 1e-6:
                    raise BrokerError("insufficient buying power")
                new_qty = pos["qty"] + qty
                pos["avg_entry"] = (pos["qty"] * pos["avg_entry"] + cost) / new_qty
                pos["qty"] = new_qty
                st["cash"] -= cost
            else:
                if qty > pos["qty"] + 1e-9:
                    raise BrokerError("insufficient qty available")
                pos["qty"] -= qty
                st["cash"] += qty * price
            pos["last_price"] = price
            oid = f"fake-{len(st['orders']) + 1}"
            st["orders"][client_order_id] = {"id": oid, "symbol": symbol, "side": side, "qty": qty,
                                             "client_order_id": client_order_id, "status": "filled",
                                             "filled_qty": qty, "filled_avg_price": price}
            self._save(st)
        self.submitted.append({"symbol": symbol, "qty": qty, "side": side, "client_order_id": client_order_id})
        return OrderResult(oid, client_order_id, "filled", qty, price)

    def order_by_client_id(self, client_order_id: str) -> OrderResult | None:
        o = self._load()["orders"].get(client_order_id)
        if not o:
            return None
        return OrderResult(o["id"], client_order_id, o["status"], o["filled_qty"], o["filled_avg_price"])

    def mark_prices(self, prices: dict[str, float]) -> None:
        with _fake_lock:
            st = self._load()
            for s, p in st["positions"].items():
                if s in prices:
                    p["last_price"] = float(prices[s])
            self._save(st)


# =========================================================================== factory
_override: dict[str, object] = {}


def set_broker(student_id: str, broker) -> None:
    """Tests inject brokers here."""
    _override[student_id] = broker


def clear_brokers() -> None:
    _override.clear()


def get_broker(student: StudentConfig):
    if demo.is_demo():
        return FakeBroker(student)      # never a real account in the demo
    if student.student_id in _override:
        return _override[student.student_id]
    if broker_backend() == "fake":
        return FakeBroker(student)
    return AlpacaPaperBroker(student)


# =========================================================================== account checks
def verify_accounts(run_id: str | None = None) -> dict[str, dict]:
    """Check every enabled student's account. Result per student:
    {"ok": bool, "account_number": str|None, "orders_allowed": bool, "problems": [...], "warnings": [...]}"""
    demo.guard("checking broker accounts")
    group = load_students()
    results: dict[str, dict] = {}
    for st in group.students:
        r = {"ok": False, "account_number": None, "orders_allowed": False, "problems": [], "warnings": [],
             "simulated": False}
        if not st.enabled:
            r["problems"].append("student module disabled in configs/students.json")
            results[st.student_id] = r
            continue
        try:
            b = get_broker(st)
            r["simulated"] = bool(getattr(b, "simulated", False))
            if not getattr(b, "key_prefix_ok", True):
                r["warnings"].append("API key does not start with 'PK' (Alpaca paper keys usually do)")
            a = b.account()
            r["account_number"] = a["account_number"]
            exp = st.expected_account_number()
            if exp and exp != a["account_number"]:
                r["problems"].append(f"account number {a['account_number']} does not match "
                                     f"{st.env_prefix}_ALPACA_ACCOUNT_NUMBER")
            if a["status"].upper() != "ACTIVE":
                r["problems"].append(f"account status is {a['status']}")
            if a["trading_blocked"] or a["account_blocked"]:
                r["problems"].append("account is blocked for trading")
        except Exception as e:
            r["problems"].append(f"could not read account: {type(e).__name__}: {str(e)[:200]}")
        results[st.student_id] = r

    # the same paper account must never serve two students
    seen: dict[str, list[str]] = {}
    for sid, r in results.items():
        if r["account_number"]:
            seen.setdefault(r["account_number"], []).append(sid)
    for acct, sids in seen.items():
        if len(sids) > 1:
            for sid in sids:
                results[sid]["problems"].append(
                    f"account {acct} is shared by {', '.join(sids)}; orders blocked to prevent cross-student trades")

    for sid, r in results.items():
        r["ok"] = not r["problems"]
        r["orders_allowed"] = r["ok"]
        prev = db.account_row(sid)
        db.ex("INSERT INTO accounts(student_id, account_number, verified_at, paper_verified, status, detail)"
              " VALUES (?,?,?,?,?,?) ON CONFLICT(student_id) DO UPDATE SET account_number=excluded.account_number,"
              " verified_at=excluded.verified_at, paper_verified=excluded.paper_verified, status=excluded.status,"
              " detail=excluded.detail",
              (sid, r["account_number"], clock.iso(), int(r["ok"]), "OK" if r["ok"] else "BLOCKED",
               db.j({"problems": r["problems"], "warnings": r["warnings"], "simulated": r["simulated"]})))
        if prev and prev.get("account_number") and r["account_number"] and prev["account_number"] != r["account_number"]:
            db.log_event("WARNING", "account_changed",
                         f"{sid} account changed from {prev['account_number']} to {r['account_number']}",
                         run_id, sid)
        if r["problems"]:
            db.log_event("WARNING", "account_check", "; ".join(r["problems"]), run_id, sid)
    return results
