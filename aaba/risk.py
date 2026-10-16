"""Deterministic risk checks and position sizing. The LLM cannot change any of this.

check_buy() runs every BUY rule and works out the share quantity.
check_sell() re-checks an approved SELL just before it is sent.
Both return every check with pass/fail and a plain-English detail, so the reason
for any blocked order is always recorded.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from .settings import StrategyConfig, StudentConfig


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class RiskResult:
    passed: bool
    qty: float = 0.0
    est_price: float | None = None
    notional: float = 0.0
    checks: list[Check] = field(default_factory=list)

    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def summary(self) -> str:
        bad = self.failed()
        if not bad:
            return "all checks passed"
        return "; ".join(f"{c.name}: {c.detail}" for c in bad)

    def as_list(self) -> list[dict]:
        return [{"check": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks]


@dataclass
class BuyContext:
    student: StudentConfig
    cfg: StrategyConfig
    symbol: str
    category: str
    signal_action: str
    data_quality: str
    data_fresh: bool
    atr: float | None
    orders_allowed: bool
    asset: dict | None
    account: dict
    positions: dict[str, dict]
    open_orders: list[dict]
    latest_price: float | None
    price_time: datetime | None
    now: datetime
    market_open: bool
    already_bought_today: bool
    buys_this_run: int
    # intraday mode (cfg.intraday.enabled): scaling in is allowed under these rules instead of
    # "one BUY per stock per day"
    intraday: bool = False
    entry_kind: str = "initial"              # initial / add
    stop: float | None = None                # the decision's stop (anchored stop for adds)
    entries: int = 0
    last_entry_price: float | None = None
    minutes_since_last_entry: float | None = None
    buys_today_student: int = 0
    sell_pending: bool = False
    # engine v2: the position's target value replaces the fixed allocation; buy_value is the
    # amount the decision wants to spend this cycle (the whole gap, or half of it above VWAP)
    target_value: float | None = None
    buy_value: float | None = None


def allocation_for(category: str, cfg: StrategyConfig) -> float:
    return cfg.risk.satellite_allocation if category == "satellite" else cfg.risk.core_allocation


def check_buy(c: BuyContext) -> RiskResult:
    r = c.cfg.risk
    checks: list[Check] = []

    def add(name, ok, detail):
        checks.append(Check(name, bool(ok), detail))

    add("signal is BUY", c.signal_action == "BUY", f"signal is {c.signal_action}")
    add("account verified for this student", c.orders_allowed,
        "paper account verified" if c.orders_allowed else "account not verified, shared or blocked")
    add("symbol belongs to this student", c.symbol in c.student.symbols,
        f"{c.symbol} in {c.student.student_id} universe" if c.symbol in c.student.symbols else "not in universe")
    a = c.asset or {}
    add("asset active and tradable", a.get("status", "").lower() == "active" and a.get("tradable", False),
        f"status={a.get('status')}, tradable={a.get('tradable')}")
    add("indicator data valid", c.data_quality == "ok", f"data quality: {c.data_quality}")
    add("daily bars up to date", c.data_fresh, "latest completed session present" if c.data_fresh else "stale bars")
    if r.require_market_open:
        add("market open", c.market_open, "regular session open" if c.market_open else "market closed")
    price_ok = c.latest_price is not None and c.latest_price > 0
    age_min = (c.now - c.price_time).total_seconds() / 60 if (price_ok and c.price_time) else None
    fresh = price_ok and age_min is not None and age_min <= r.max_price_age_minutes
    if not c.market_open and not r.require_market_open and price_ok:
        fresh = True  # outside hours the last trade is naturally older; only allowed if config says so
    add("latest price fresh", fresh,
        f"price {c.latest_price} age {age_min:.1f} min" if age_min is not None else "no latest price")
    open_same = [o for o in c.open_orders if o.get("symbol") == c.symbol]
    add("no open order for symbol", not open_same, f"{len(open_same)} open order(s)")
    if c.intraday:
        ic = c.cfg.intraday
        add("entries per position", c.entries < ic.max_entries_per_position,
            f"{c.entries} of {ic.max_entries_per_position} entries used")
        gap = c.minutes_since_last_entry
        add("time since last entry", gap is None or gap >= ic.min_minutes_between_entries,
            "first entry" if gap is None else f"{gap:.0f} min (minimum {ic.min_minutes_between_entries})")
        if c.entry_kind == "add" and c.target_value is None:          # v1 adds only; v2 buys toward its target
            need = (c.last_entry_price or 0) - ic.min_add_drop_atr * (c.atr or 0)
            ok = bool(c.latest_price and c.last_entry_price and c.atr and c.latest_price <= need)
            add("add only below the last entry", ok,
                f"price {c.latest_price} vs last entry {c.last_entry_price} - {ic.min_add_drop_atr:g} x ATR = "
                f"{need:.2f}" if c.last_entry_price and c.atr else "no last entry / ATR")
        add("stop below price", bool(c.stop and c.latest_price and c.stop < c.latest_price),
            f"stop {c.stop}, price {c.latest_price}")
        add("student BUYs today", c.buys_today_student < ic.max_buys_per_student_per_day,
            f"{c.buys_today_student} of {ic.max_buys_per_student_per_day}")
        add("no SELL waiting for this stock", not c.sell_pending,
            "a SELL approval is open" if c.sell_pending else "none")
    else:
        add("not already bought today", not c.already_bought_today,
            "already submitted a BUY today" if c.already_bought_today else "first BUY today")
    add("new-buy limit per run", c.buys_this_run < r.max_new_buys_per_run,
        f"{c.buys_this_run} of {r.max_new_buys_per_run} used")

    acct = c.account or {}
    eq, last_eq = float(acct.get("equity") or 0), float(acct.get("last_equity") or 0)
    day_change = (eq - last_eq) / last_eq * 100 if last_eq > 0 else 0.0
    add("daily loss halt", day_change > -r.daily_loss_halt_pct,
        f"today {day_change:+.2f}% (halt at -{r.daily_loss_halt_pct}%)")

    # ---- sizing
    price = c.latest_price if price_ok else None
    pos = c.positions.get(c.symbol, {})
    cur_value = float(pos.get("market_value") or 0)
    allocation = allocation_for(c.category, c.cfg)
    cap = c.student.starting_capital * r.max_position_pct / 100
    room_alloc = allocation - cur_value
    v2 = c.intraday and c.target_value is not None
    if v2:
        allocation = c.target_value                     # engine v2: momentum/volatility target
        room_alloc = c.target_value - cur_value
        if c.buy_value is not None:
            room_alloc = min(room_alloc, c.buy_value)
    elif c.intraday and c.entry_kind == "initial":
        # leave room to add on later, evidence-based dips (scaling in)
        room_alloc = allocation * c.cfg.intraday.initial_entry_fraction - cur_value
    room_cap = cap - cur_value
    cost_basis = sum(float(p.get("qty", 0)) * float(p.get("avg_entry") or 0) for p in c.positions.values())
    open_buy_value = sum(float(o.get("notional") or 0) or float(o.get("qty") or 0) * (price or 0)
                         for o in c.open_orders if o.get("side") == "buy")
    room_budget = c.student.starting_capital - cost_basis - open_buy_value
    cash = float(acct.get("non_marginable_buying_power") or acct.get("cash") or 0)
    dollars = min(room_alloc, room_cap, room_budget, cash)
    qty = 0
    cost = c.cfg.intraday.cost_bps / 10_000 if c.intraday else 0.0
    if price and dollars > 0:
        qty_room = math.floor(dollars / (price * (1 + cost)))
        if c.intraday and c.stop:
            from .intraday import risk_qty
            pos = c.positions.get(c.symbol, {})
            qty_risk = risk_qty(c.cfg, c.student.starting_capital, price, c.stop, float(pos.get("qty") or 0),
                                float(pos.get("avg_entry") or 0) or None, "add" if v2 else c.entry_kind,
                                budget_pct=c.cfg.portfolio.max_position_risk_pct if v2 else None)
        elif c.atr and c.atr > 0:
            risk_dollars = c.student.starting_capital * r.risk_per_trade_pct / 100
            qty_risk = math.floor(risk_dollars / (r.stop_atr_mult * c.atr))
        else:
            qty_risk = 0
        qty = max(0, min(qty_room, qty_risk))
        sizing = (f"room ${dollars:,.0f} (allocation ${room_alloc:,.0f}, 15% cap ${room_cap:,.0f}, budget "
                  f"${room_budget:,.0f}, cash ${cash:,.0f}) -> {qty_room} sh; ATR risk -> {qty_risk} sh")
    else:
        sizing = f"no room or no price (room ${dollars:,.0f})"
    notional = round(qty * (price or 0) * (1 + cost), 2)
    add("position within allocation and 15% cap", room_alloc > 0 and room_cap > 0,
        f"current ${cur_value:,.0f}, allocation ${allocation:,.0f}, cap ${cap:,.0f}")
    add("within student capital budget", room_budget > 0, f"used ${cost_basis + open_buy_value:,.0f} of "
        f"${c.student.starting_capital:,.0f}")
    add("enough cash (no margin)", cash >= max(notional, r.min_order_value),
        f"cash ${cash:,.0f}, order ${notional:,.0f}, minimum order ${r.min_order_value:,.0f}")
    add("order size", qty >= 1 and notional >= r.min_order_value, f"{qty} shares = ${notional:,.0f}; {sizing}")

    passed = all(ch.ok for ch in checks)
    return RiskResult(passed, float(qty) if passed else 0.0, price, notional if passed else 0.0, checks)


@dataclass
class SellContext:
    cfg: StrategyConfig
    approval: dict
    orders_allowed: bool
    current_account_number: str | None
    positions: dict[str, dict]
    open_orders: list[dict]
    latest_price: float | None
    price_time: datetime | None
    now: datetime
    market_open: bool
    expired: bool


def check_sell(c: SellContext) -> RiskResult:
    ap = c.approval
    sym, qty = ap["symbol"], float(ap["qty"])
    checks: list[Check] = []

    def add(name, ok, detail):
        checks.append(Check(name, bool(ok), detail))

    add("approval status APPROVED", ap["status"] in ("APPROVED", "EXECUTING"), f"status {ap['status']}")
    add("approval not expired", not c.expired, "expired" if c.expired else f"valid until {ap['expires_at']}")
    add("account verified", c.orders_allowed, "ok" if c.orders_allowed else "account not verified or shared")
    add("same account as the request", c.current_account_number == ap.get("account_number"),
        f"request {ap.get('account_number')}, now {c.current_account_number}")
    held = float(c.positions.get(sym, {}).get("qty") or 0)
    add("holding matches approved quantity", abs(held - qty) < 1e-9,
        f"approved {qty:g} shares, holding {held:g}")
    open_same = [o for o in c.open_orders if o.get("symbol") == sym]
    add("no open order for symbol", not open_same, f"{len(open_same)} open order(s)")
    if c.cfg.risk.require_market_open:
        add("market open", c.market_open, "open" if c.market_open else "market closed")
    price_ok = c.latest_price is not None and c.latest_price > 0
    age = (c.now - c.price_time).total_seconds() / 60 if (price_ok and c.price_time) else None
    add("latest price fresh", price_ok and age is not None and (age <= c.cfg.risk.max_price_age_minutes
                                                                 or not c.market_open),
        f"age {age:.1f} min" if age is not None else "no price")
    sp = float(ap.get("signal_price") or 0)
    move = abs(c.latest_price - sp) / sp * 100 if (price_ok and sp > 0) else None
    add("price close to the approved signal price",
        move is not None and move <= c.cfg.approvals.max_price_move_pct,
        f"moved {move:.2f}% (limit {c.cfg.approvals.max_price_move_pct}%)" if move is not None else "unknown")
    passed = all(ch.ok for ch in checks)
    return RiskResult(passed, qty if passed else 0.0, c.latest_price, round(qty * (c.latest_price or 0), 2), checks)
