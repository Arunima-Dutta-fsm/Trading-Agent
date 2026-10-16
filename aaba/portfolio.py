"""Engine v2: regime-scaled, momentum-ranked, volatility-weighted portfolio targets, with intraday
timing and trend-following exits. Used by the live cycle and by the intraday backtest.

1. Market regime (once per run, from completed daily bars)
   bull     SPY above EMA50 and EMA200, and >= breadth_bull of our 36 stocks above their EMA50
   bear     SPY below EMA200 and < breadth_bear of our stocks above their EMA50
   neutral  anything else
   The regime sets how much of each student's capital is invested: gross_bull / neutral / bear.

2. Targets per student (the proposal's 40% satellites / 60% core split is kept)
   A stock is eligible when it is in an uptrend (close > EMA50 > ..., EMA20 > EMA50, close > EMA200)
   with positive blended momentum (20/60/120-session returns) and no strong negative news.
   The best `max_positions` by risk-adjusted momentum (momentum / ATR%) are held. Inside each
   category, weights are inverse to volatility (ATR%) and tilted by news sentiment, capped at the
   15% per-stock limit. Cash that cannot be placed stays cash.

3. Each intraday cycle, per stock
   SELL request when the daily close fell below the chandelier stop (highest close since entry -
   chandelier_atr x ATR), below EMA50 - trend_exit_atr x ATR, when momentum turned negative or news
   is strongly negative, or (intraday emergency) when price is below the anchored entry stop.
   A lower rank alone never sells (no churn).
   BUY toward the target when the position is below it by at least `rebalance_band`, unless the
   stock is falling hard intraday, the market is weak, or price is stretched (> max_chase_atr x ATR
   above VWAP or intraday RSI >= 80). At or below VWAP the whole gap is bought; above it, half.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from .intraday import Decision, PositionPlan
from .settings import StrategyConfig, StudentConfig

BENCH = "SPY"


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _r(x, n=3):
    v = _f(x)
    return None if v is None else round(v, n)


# =========================================================================== momentum and regime
def momentum(frame: pd.DataFrame, cols: dict, weights: list[float]) -> dict | None:
    """Blended 20/60/120-session momentum from completed daily bars, risk-adjusted by ATR%."""
    if frame is None or len(frame) < 125:
        return None
    c = frame["close"].astype(float)
    row = frame.iloc[-1]
    close, atr = _f(row["close"]), _f(row[cols["atr"]])
    e20, e50, e200 = _f(row[cols["ema_fast"]]), _f(row[cols["ema_mid"]]), _f(row[cols["ema_slow"]])
    if None in (close, atr, e20, e50, e200) or close <= 0:
        return None
    rets = [close / float(c.iloc[-1 - n]) - 1 for n in (20, 60, 120)]
    raw = sum(w * r for w, r in zip(weights, rets))
    atr_pct = max(atr / close, 0.005)
    return {"r20": round(rets[0], 4), "r60": round(rets[1], 4), "r120": round(rets[2], 4), "raw": round(raw, 4),
            "atr_pct": round(atr_pct, 4), "score": round(raw / atr_pct, 3),
            "trend_ok": close > e50 and e20 > e50 and close > e200, "close": close, "atr": atr, "ema50": e50}


def market_regime(frames: dict, universe: list[str], cols: dict, cfg: StrategyConfig) -> dict:
    pc = cfg.portfolio
    spy = frames.get(BENCH)
    if spy is None or spy.empty:
        return {"label": "neutral", "gross": pc.gross_neutral, "why": "SPY data missing"}
    row = spy.iloc[-1]
    above50 = row["close"] > row[cols["ema_mid"]]
    above200 = row["close"] > row[cols["ema_slow"]]
    ups = [bool(f.iloc[-1]["close"] > f.iloc[-1][cols["ema_mid"]]) for s in universe
           if (f := frames.get(s)) is not None and not f.empty and _f(f.iloc[-1][cols["ema_mid"]]) is not None]
    breadth = sum(ups) / len(ups) if ups else 0.0
    if above50 and above200 and breadth >= pc.breadth_bull:
        label, gross = "bull", pc.gross_bull
    elif not above200 and breadth < pc.breadth_bear:
        label, gross = "bear", pc.gross_bear
    else:
        label, gross = "neutral", pc.gross_neutral
    return {"label": label, "gross": gross, "spy_above_ema50": bool(above50), "spy_above_ema200": bool(above200),
            "breadth": round(breadth, 3),
            "why": f"SPY {'above' if above50 else 'below'} EMA50, {'above' if above200 else 'below'} EMA200; "
                   f"{breadth:.0%} of our stocks above their EMA50"}


def _cap_weights(raw: dict[str, float], budget: float, cap: float) -> dict[str, float]:
    """Scale weights to `budget` with no weight above `cap`; excess goes to the others (iteratively)."""
    w = {k: 0.0 for k in raw}
    free = {k: v for k, v in raw.items() if v > 0}
    left = budget
    while free and left > 1e-9:
        tot = sum(free.values())
        share = {k: left * v / tot for k, v in free.items()}
        over = {k for k, v in share.items() if w[k] + v > cap}
        if not over:
            for k, v in share.items():
                w[k] += v
            break
        for k in over:
            left -= cap - w[k]
            w[k] = cap
            free.pop(k)
    return w


def compute_targets(student: StudentConfig, frames: dict, news: dict, cols: dict, cfg: StrategyConfig,
                    regime: dict) -> dict[str, dict]:
    """{symbol: {"weight", "target_value", "eligible", "rank", "why", "momentum"}} for one student."""
    pc = cfg.portfolio
    out, cand = {}, []
    for sym in student.symbols:
        m = momentum(frames.get(sym), cols, pc.momentum_weights)
        n = news.get(sym) or {}
        nscore = n.get("score") if n.get("articles", 0) >= cfg.news.min_articles else None
        veto = nscore is not None and nscore <= cfg.news.veto_below
        ok = bool(m and m["trend_ok"] and m["raw"] > 0 and not veto)
        why = ("no daily history" if not m else "news veto" if veto else
               "not in an uptrend (close/EMA20/EMA50/EMA200)" if not m["trend_ok"] else
               "momentum not positive" if m["raw"] <= 0 else "eligible")
        out[sym] = {"weight": 0.0, "target_value": 0.0, "eligible": ok, "rank": None, "why": why, "momentum": m,
                    "news": nscore}
        if ok:
            cand.append((m["score"], sym))
    cand.sort(reverse=True)
    chosen = [s for _, s in cand[:pc.max_positions]]
    for i, s in enumerate(s for _, s in cand):
        out[s]["rank"] = i + 1
        if s not in chosen:
            out[s]["why"] = f"eligible but ranked {i + 1} (holds the best {pc.max_positions})"
    cap = cfg.risk.max_position_pct / 100
    for cat, share in (("satellite", pc.satellite_share), ("core", 1 - pc.satellite_share)):
        members = [s for s in chosen if student.category(s) == cat]
        raw = {}
        for s in members:
            m, nscore = out[s]["momentum"], out[s]["news"]
            raw[s] = (1 + pc.news_tilt * (nscore or 0.0)) / m["atr_pct"]
        for s, w in _cap_weights(raw, regime["gross"] * share, cap).items():
            out[s]["weight"] = round(w, 4)
            out[s]["target_value"] = round(w * student.starting_capital, 2)
            out[s]["why"] = f"target {w:.1%} ({cat}, rank {out[s]['rank']}, regime {regime['label']})"
    return out


# =========================================================================== intraday decision
@dataclass
class Holding:
    value: float = 0.0                       # market value now
    highest_close: float | None = None       # highest DAILY close since entry


def decide_v2(symbol: str, cfg: StrategyConfig, target: dict, daily_close: float, bar, plan: PositionPlan,
              holding: Holding, market: dict) -> Decision:
    pc, ic = cfg.portfolio, cfg.intraday
    m = target.get("momentum") or {}
    atr = m.get("atr")
    price = _f(bar["close"]) if bar is not None else None
    if price is None or not atr:
        return Decision("HOLD", "none", ["no complete daily or intraday data"], {}, price)
    held = plan.qty > 0
    ev = {"engine": "v2", "price": _r(price, 2), "target_value": target.get("target_value"),
          "target_weight": target.get("weight"), "rank": target.get("rank"), "momentum": m,
          "held_value": round(holding.value, 2), "vwap": _r(bar["vwap"], 2), "i_rsi": _r(bar["i_rsi"], 1),
          "market": market, "target_why": target.get("why")}

    # ---------------------------------------------------------------- exits
    if held:
        sells = []
        if holding.highest_close:
            chand = holding.highest_close - pc.chandelier_atr * atr
            ev["chandelier_stop"] = round(chand, 2)
            if daily_close < chand:
                sells.append(f"daily close {daily_close:.2f} below the chandelier stop {chand:.2f} "
                             f"(highest close {holding.highest_close:.2f} - {pc.chandelier_atr:g} x ATR)")
        ema50 = m.get("ema50")
        if ema50 and daily_close < ema50 - pc.trend_exit_atr * atr:
            sells.append(f"trend exit: daily close {daily_close:.2f} below EMA50 {ema50:.2f} - "
                         f"{pc.trend_exit_atr:g} x ATR")
        if target.get("why") in ("momentum not positive", "news veto"):
            sells.append(f"no longer worth holding: {target['why']}")
        if plan.anchor_stop and price < plan.anchor_stop:
            sells.append(f"emergency: price {price:.2f} below the entry stop {plan.anchor_stop:.2f}")
        if sells:
            if plan.sell_pending:
                return Decision("HOLD", "exit", ["SELL already waiting for the student: " + sells[0]], ev, price)
            return Decision("SELL", "exit", sells, ev, price)

    # ---------------------------------------------------------------- buy toward the target
    tgt = target.get("target_value") or 0.0
    gap = tgt - holding.value
    if tgt <= 0 or gap < max(pc.rebalance_band * tgt, cfg.risk.min_order_value):
        why = target.get("why") if tgt <= 0 else f"at target (holding ${holding.value:,.0f} of ${tgt:,.0f})"
        return Decision("HOLD", "none", [f"{'holding' if held else 'no entry'}: {why}"], ev, price)
    blocks = []
    falling = (price < bar["vwap"] and bar["i_ema9"] < bar["i_ema21"] and (_f(bar["i_ema21_slope"]) or 0) < 0
               and (_f(bar["lower_lows6"]) or 0) >= 4)
    if falling:
        blocks.append("falling hard intraday (below VWAP, EMA9 < EMA21, 4+ lower lows)")
    if market.get("known") and market["change_pct"] <= -ic.market_weak_drop_pct and market["below_vwap"]:
        blocks.append(f"weak market: SPY {market['change_pct']:+.2f}% and below its VWAP")
    if price > bar["vwap"] + pc.max_chase_atr * atr or (_f(bar["i_rsi"]) or 0) >= 80:
        blocks.append(f"stretched: {price:.2f} vs VWAP {bar['vwap']:.2f}, intraday RSI {bar['i_rsi']:.0f}")
    if plan.sell_pending:
        blocks.append("a SELL approval is waiting for this stock")
    if plan.buys_today_student >= ic.max_buys_per_student_per_day:
        blocks.append(f"daily BUY limit reached ({ic.max_buys_per_student_per_day})")
    if held and plan.minutes_since_last_entry is not None and plan.minutes_since_last_entry < ic.min_minutes_between_entries:
        blocks.append(f"last entry {plan.minutes_since_last_entry:.0f} min ago (minimum {ic.min_minutes_between_entries})")
    if held and plan.entries >= ic.max_entries_per_position:
        blocks.append(f"{plan.entries} entries already (limit {ic.max_entries_per_position})")
    if blocks:
        return Decision("HOLD", "rebalance", ["BUY toward target postponed: " + "; ".join(blocks)], ev, price,
                        blocked=blocks)
    spend = gap if price <= bar["vwap"] else gap / 2
    # the emergency stop of this entry; it is anchored for the position and also the distance used
    # for the risk budget (wider than the chandelier, so risk is not understated)
    stop = plan.anchor_stop if held and plan.anchor_stop else price - pc.emergency_stop_atr * atr
    ev.update({"buy_value": round(spend, 2), "stop": round(stop, 2)})
    why = [f"{target.get('why')}: holding ${holding.value:,.0f}, buying ${spend:,.0f} "
           f"({'at/below' if price <= bar['vwap'] else 'above'} VWAP {bar['vwap']:.2f})",
           f"momentum 20/60/120: {m.get('r20', 0):+.1%} / {m.get('r60', 0):+.1%} / {m.get('r120', 0):+.1%}, "
           f"ATR {m.get('atr_pct', 0):.1%} of price"]
    d = Decision("BUY", "initial" if not held else "rebalance", why, ev, price, stop)
    d.evidence["buy_value"] = round(spend, 2)
    return d
