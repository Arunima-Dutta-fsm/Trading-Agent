"""Intraday decision engine: a fresh, evidence-based BUY / HOLD / SELL every cycle (10 minutes).

Used by the live workflow (controller.evaluate_student) and by the intraday backtest, so both
make exactly the same decisions from the same numbers.

Inputs per stock and cycle
- daily context from COMPLETED daily bars (yesterday and before): EMA20/50/200, RSI14, ATR14,
  Bollinger Bands, the daily rules-engine signal and its news/earnings vetoes;
- intraday features from COMPLETED 5-minute candles: VWAP, EMA9/EMA21 and its slope, RSI14,
  relative volume, session high/low, 30-minute opening range, staircase of lower lows;
- the market (SPY) today: change since the prior close and position versus its VWAP;
- the position: shares, average cost, the ANCHORED stop of the first entry, number of entries,
  last entry price and time.

Decisions
- BUY (initial): the daily rules engine says BUY and the intraday trend confirms it, or the stock
  is in a healthy long-term uptrend and has just bounced from a temporary dip with volume.
- BUY (add): only into an existing position, only on a confirmed bounce at least
  `min_add_drop_atr` x ATR below the last entry, never below the anchored stop, at most
  `max_entries_per_position` entries `min_minutes_between_entries` apart.
- No buying at all while there are signs of sustained weakness: an intraday downtrend below VWAP,
  a staircase of lower lows, a break of the prior day's low or of the daily EMA50, a weak market
  (SPY), a news or earnings veto.
- Every BUY needs reward-to-risk >= `min_reward_risk` after costs, using the nearest resistance as
  the target and the stop as the risk.
- SELL (approval request, never sent directly) when price is below the anchored stop, when the
  daily rules engine says SELL, when the trend breaks intraday (below EMA20 daily, VWAP and in an
  intraday downtrend), or when price is stretched above the upper band with intraday RSI >= limit.
- Otherwise HOLD, with the reason. A cycle that finds no evidence does nothing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .indicators import ema, rsi
from .settings import StrategyConfig

OPENING_RANGE_MINUTES = 30


@dataclass
class PositionPlan:
    qty: float = 0.0
    avg_cost: float | None = None
    anchor_stop: float | None = None        # set by the first entry, never lowered
    entries: int = 0                         # BUY fills since the position was opened
    last_entry_price: float | None = None
    minutes_since_last_entry: float | None = None
    buys_today_student: int = 0              # all BUYs this student made today (any stock)
    sell_pending: bool = False               # a SELL approval is open for this stock


@dataclass
class Decision:
    action: str                              # BUY / SELL / HOLD
    kind: str                                # initial / add / exit / none
    reasons: list[str]
    evidence: dict
    price: float | None
    stop: float | None = None
    target: float | None = None
    reward_risk: float | None = None
    blocked: list[str] = field(default_factory=list)


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _r(x, n=2):
    v = _f(x)
    return None if v is None else round(v, n)


# =========================================================================== features
def add_intraday_features(bars: pd.DataFrame, bar_minutes: int) -> pd.DataFrame:
    """One symbol's completed regular-hours candles (several sessions, oldest first) -> features.
    Every value at a candle uses only that candle and earlier ones (no look-ahead)."""
    d = bars.sort_values("timestamp").reset_index(drop=True).copy()
    if d.empty:
        return d
    ny = d["timestamp"].dt.tz_convert("America/New_York")
    d["session"] = ny.dt.date
    mins = (ny.dt.hour * 60 + ny.dt.minute) - (9 * 60 + 30)
    d["minute_of_day"] = mins
    close = d["close"]
    d["i_ema9"] = ema(close, 9)
    d["i_ema21"] = ema(close, 21)
    d["i_ema21_slope"] = d["i_ema21"] - d["i_ema21"].shift(3)
    d["i_rsi"] = rsi(close, 14)
    d["i_rsi_min3"] = d["i_rsi"].rolling(3, min_periods=1).min()
    d["vol_avg20"] = d["volume"].rolling(20, min_periods=5).mean()
    d["rel_vol"] = d["volume"] / d["vol_avg20"]
    typical = (d["high"] + d["low"] + d["close"]) / 3
    g = d.groupby("session", sort=False)
    pv = (typical * d["volume"]).groupby(d["session"]).cumsum()
    vv = d["volume"].groupby(d["session"]).cumsum()
    d["vwap"] = (pv / vv.replace(0, np.nan)).fillna(typical)
    d["sess_high"] = g["high"].cummax()
    d["sess_low"] = g["low"].cummin()
    d["day_open"] = g["open"].transform("first")
    in_or = mins < OPENING_RANGE_MINUTES
    d["or_low"] = d["low"].where(in_or).groupby(d["session"]).cummin()
    d["or_low"] = d["or_low"].groupby(d["session"]).ffill()
    d["or_high"] = d["high"].where(in_or).groupby(d["session"]).cummax()
    d["or_high"] = d["or_high"].groupby(d["session"]).ffill()
    lower = (d["low"] < d["low"].shift(1)) & (d["session"] == d["session"].shift(1))
    d["lower_lows6"] = lower.astype(int).rolling(6, min_periods=1).sum()
    d["up_bar"] = d["close"] > d["open"]
    d["bars_today"] = g.cumcount() + 1
    return d


def daily_context(daily: pd.DataFrame, cols: dict) -> dict | None:
    """Context from the latest COMPLETED daily bar (the frame must end before today)."""
    if daily is None or len(daily) < 6:
        return None
    row, back5 = daily.iloc[-1], daily.iloc[-6]
    ctx = {"close": _f(row["close"]), "high": _f(row["high"]), "low": _f(row["low"]),
           "ema_fast": _f(row[cols["ema_fast"]]), "ema_mid": _f(row[cols["ema_mid"]]),
           "ema_slow": _f(row[cols["ema_slow"]]), "ema_mid_5ago": _f(back5[cols["ema_mid"]]),
           "rsi": _f(row[cols["rsi"]]), "atr": _f(row[cols["atr"]]),
           "bb_upper": _f(row[cols["bb_upper"]]), "bb_lower": _f(row[cols["bb_lower"]])}
    return ctx if all(v is not None for v in ctx.values()) else None


def market_context(spy_bar: pd.Series | dict | None, spy_prev_close: float | None) -> dict:
    if spy_bar is None or spy_prev_close in (None, 0):
        return {"known": False}
    close = float(spy_bar["close"])
    return {"known": True, "change_pct": round((close / spy_prev_close - 1) * 100, 3),
            "below_vwap": close < float(spy_bar["vwap"])}


# =========================================================================== decision
def decide(symbol: str, category: str, cfg: StrategyConfig, daily: dict | None, bar: dict | pd.Series | None,
           daily_signal: str, daily_reasons: list[str], daily_blocked: list[str], market: dict,
           pos: PositionPlan) -> Decision:
    ic, risk = cfg.intraday, cfg.risk
    if daily is None or bar is None:
        return Decision("HOLD", "none", ["no complete daily or intraday data"], {}, None)
    b = {k: (bar[k] if k in bar else None) for k in (
        "close", "open", "high", "low", "vwap", "i_ema9", "i_ema21", "i_ema21_slope", "i_rsi", "i_rsi_min3",
        "rel_vol", "sess_high", "sess_low", "or_low", "or_high", "lower_lows6", "up_bar", "bars_today",
        "day_open")}
    price = _f(b["close"])
    atr = daily["atr"]
    needed = ("vwap", "i_ema9", "i_ema21", "i_rsi", "rel_vol")
    if price is None or not atr or any(_f(b[k]) is None for k in needed):
        return Decision("HOLD", "none", ["intraday indicators not ready yet (too few candles today)"], {}, price)
    held = pos.qty > 0
    cost = ic.cost_bps / 10_000
    ev = {"price": _r(price), "vwap": _r(b["vwap"]), "i_ema9": _r(b["i_ema9"]), "i_ema21": _r(b["i_ema21"]),
          "i_rsi": _r(b["i_rsi"], 1), "rel_vol": _r(b["rel_vol"]), "sess_low": _r(b["sess_low"]),
          "sess_high": _r(b["sess_high"]), "daily_atr": _r(atr), "daily_ema20": _r(daily["ema_fast"]),
          "daily_ema50": _r(daily["ema_mid"]), "daily_ema200": _r(daily["ema_slow"]),
          "prior_low": _r(daily["low"]), "prior_high": _r(daily["high"]), "market": market,
          "held_qty": pos.qty, "avg_cost": _r(pos.avg_cost), "anchor_stop": _r(pos.anchor_stop),
          "entries": pos.entries, "daily_signal": daily_signal}

    # ---------------------------------------------------------------- exits first (protect capital)
    if held:
        sells = []
        if pos.anchor_stop and price < pos.anchor_stop:
            sells.append(f"price {price:.2f} is below the position's stop {pos.anchor_stop:.2f}")
        if daily_signal == "SELL":
            sells.append("daily rules: " + "; ".join(daily_reasons)[:300])
        intraday_down = price < b["vwap"] and b["i_ema9"] < b["i_ema21"] and (_f(b["i_ema21_slope"]) or 0) < 0
        if category == "satellite" and price < daily["ema_fast"] - 0.25 * atr and intraday_down:
            sells.append(f"trend break: {price:.2f} below daily EMA20 {daily['ema_fast']:.2f}, below VWAP and "
                         f"falling intraday")
        if price >= daily["bb_upper"] and b["i_rsi"] >= ic.profit_take_rsi:
            sells.append(f"stretched: {price:.2f} above the upper band {daily['bb_upper']:.2f} with intraday "
                         f"RSI {b['i_rsi']:.0f} >= {ic.profit_take_rsi:g}")
        if sells:
            if pos.sell_pending:
                return Decision("HOLD", "exit", ["SELL conditions still met; a SELL approval is already waiting "
                                                 "for the student: " + sells[0]], ev, price)
            return Decision("SELL", "exit", sells, ev, price)

    # ---------------------------------------------------------------- is buying allowed at all?
    weakness = []
    if price < b["vwap"] and b["i_ema9"] < b["i_ema21"] and (_f(b["i_ema21_slope"]) or 0) < 0:
        weakness.append("intraday downtrend: below VWAP, EMA9 < EMA21 and EMA21 falling")
    if (_f(b["lower_lows6"]) or 0) >= 4 and not b["up_bar"]:
        weakness.append(f"staircase down: {int(b['lower_lows6'])} lower lows in the last 6 candles")
    if price < daily["low"] - 0.25 * atr:
        weakness.append(f"broke below the prior day's low {daily['low']:.2f}")
    if price < daily["ema_mid"] - 0.5 * atr:
        weakness.append(f"below daily EMA50 {daily['ema_mid']:.2f}")
    if market.get("known") and market["change_pct"] <= -ic.market_weak_drop_pct and market["below_vwap"]:
        weakness.append(f"weak market: SPY {market['change_pct']:+.2f}% and below its VWAP")
    healthy = (price > daily["ema_slow"] and daily["ema_mid"] >= daily["ema_mid_5ago"]
               and daily["ema_fast"] >= daily["ema_mid"] - 0.5 * atr)
    ev["healthy_trend"] = healthy
    ev["weakness"] = weakness

    # ---------------------------------------------------------------- what kind of BUY setup is there?
    momentum_ok = (price > b["vwap"] and b["i_ema9"] > b["i_ema21"] and 50 <= b["i_rsi"] <= 75
                   and price <= daily["close"] + 1.5 * atr)
    reference = pos.last_entry_price if held else b["sess_high"]
    dip = (reference or price) - price
    bounce = (bool(b["up_bar"]) and price > b["i_ema9"] and (_f(b["rel_vol"]) or 0) >= ic.min_relative_volume
              and (_f(b["i_rsi_min3"]) or 50) <= 45)
    supports = [v for v in (daily["low"], b["or_low"], daily["ema_fast"], daily["ema_mid"], daily["bb_lower"])
                if _f(v) is not None and v < price]
    support = max(supports) if supports else price - risk.stop_atr_mult * atr

    def target_for(kind_: str) -> float:
        # a dip aims back at the session high; a momentum entry is already AT the session high,
        # so its target is the next daily resistance (or 2 x ATR when there is none above)
        levels = (daily["high"], b["sess_high"], daily["bb_upper"]) if kind_ in ("dip", "add") \
            else (daily["high"], daily["bb_upper"])
        above = [v for v in levels if _f(v) is not None and v > price * 1.001]
        return min(above) if above else price + 2 * atr

    ev.update({"support": _r(support), "dip_from_reference": _r(dip),
               "bounce_confirmed": bounce, "momentum_confirmed": momentum_ok})

    kind, why, setup = None, [], None
    if not held:
        if daily_signal == "BUY" and momentum_ok:
            kind, setup, why = "initial", "momentum", [
                "daily rules engine BUY: " + "; ".join(daily_reasons)[:250],
                f"intraday confirms: above VWAP {b['vwap']:.2f}, EMA9 > EMA21, RSI {b['i_rsi']:.0f}"]
        elif healthy and bounce and dip >= ic.min_add_drop_atr * atr and daily["rsi"] < 65:
            kind, setup, why = "initial", "dip", [
                f"dip in a healthy uptrend: {dip:.2f} below the session high, above support {support:.2f}, "
                f"bounce with volume x{b['rel_vol']:.1f} and RSI recovering"]
    else:
        if pos.entries >= ic.max_entries_per_position:
            return Decision("HOLD", "add", [f"holding: already {pos.entries} entries (limit "
                                            f"{ic.max_entries_per_position})"], ev, price)
        if healthy and bounce and dip >= ic.min_add_drop_atr * atr:
            kind, setup, why = "add", "add", [f"add on a temporary dip: {dip:.2f} below the last entry "
                                f"{pos.last_entry_price:.2f} (>= {ic.min_add_drop_atr:g} x ATR), still above the "
                                f"stop, bounce confirmed with volume x{b['rel_vol']:.1f}"]
    if not kind:
        if held:
            reason = "holding: no new evidence to add or to exit"
            if dip > 0 and weakness:
                reason = "holding, not averaging down: " + "; ".join(weakness)
        elif weakness:
            reason = "no entry: " + "; ".join(weakness)
        else:
            reason = "no entry: no confirmed setup this cycle"
        return Decision("HOLD", "none", [reason], ev, price)
    if weakness:
        return Decision("HOLD", kind, ["BUY setup rejected, sustained weakness: " + "; ".join(weakness)], ev, price,
                        blocked=weakness)
    if daily_blocked:
        return Decision("HOLD", kind, ["BUY setup blocked: " + "; ".join(daily_blocked)], ev, price,
                        blocked=daily_blocked)
    if pos.sell_pending:
        return Decision("HOLD", kind, ["BUY setup ignored: a SELL approval is waiting for this stock"], ev, price)
    if held and pos.minutes_since_last_entry is not None and pos.minutes_since_last_entry < ic.min_minutes_between_entries:
        return Decision("HOLD", kind, [f"add setup, but the last entry was {pos.minutes_since_last_entry:.0f} min ago "
                                       f"(minimum {ic.min_minutes_between_entries})"], ev, price)
    if pos.buys_today_student >= ic.max_buys_per_student_per_day:
        return Decision("HOLD", kind, [f"daily BUY limit reached ({ic.max_buys_per_student_per_day})"], ev, price)

    # ---------------------------------------------------------------- stop, reward-to-risk
    if held and pos.anchor_stop:
        stop = pos.anchor_stop                                   # never lowered when adding
    elif setup == "momentum":
        # a momentum entry is wrong once it loses VWAP / the intraday EMA21
        stop = min(max(b["vwap"], b["i_ema21"]) - 0.25 * atr, price - 0.5 * atr)
        stop = max(stop, price - risk.stop_atr_mult * atr)
    else:
        stop = max(price - risk.stop_atr_mult * atr, support - 0.25 * atr)
        stop = min(stop, price - 0.5 * atr)                      # never closer than half an ATR
    if price <= stop:
        return Decision("HOLD", kind, [f"price {price:.2f} is at or below the stop {stop:.2f}"], ev, price)
    target = target_for(setup)
    ev["setup"] = setup
    entry_cost = price * (1 + cost)
    reward = target * (1 - cost) - entry_cost
    loss = entry_cost - stop * (1 - cost)
    rr = reward / loss if loss > 0 else 0.0
    ev.update({"stop": _r(stop), "target": _r(target), "reward_risk": _r(rr)})
    if rr < ic.min_reward_risk:
        return Decision("HOLD", kind, [f"BUY setup, but reward-to-risk {rr:.2f} < {ic.min_reward_risk:g} after costs "
                                       f"(target {target:.2f}, stop {stop:.2f})"], ev, price, stop, target, rr)
    why.append(f"reward-to-risk {rr:.2f} (target {target:.2f}, stop {stop:.2f}) after costs")
    return Decision("BUY", kind, why, ev, price, stop, target, rr)


def risk_qty(cfg: StrategyConfig, capital: float, price: float, stop: float, held_qty: float,
             held_avg: float | None, kind: str, budget_pct: float | None = None) -> int:
    """Shares allowed by the risk budget. Initial: risk_per_trade_pct of capital to the stop.
    Add: total risk of the whole position to the anchored stop <= max_position_risk_pct
    (or `budget_pct`, used by engine v2)."""
    per_share = price - stop
    if per_share <= 0:
        return 0
    if kind == "add":
        budget = capital * (budget_pct if budget_pct is not None else cfg.intraday.max_position_risk_pct) / 100
        held_risk = max(held_qty * ((held_avg or price) - stop), 0.0)
        return max(int((budget - held_risk) // per_share), 0)
    return max(int((capital * cfg.risk.risk_per_trade_pct / 100) // per_share), 0)
