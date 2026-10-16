"""Engine v3: stock-specific, adaptive strategy with confirmed exits and deliberate holding.

Every day each stock gets a PROFILE from its own trailing history (no look-ahead):
- normal pullback: the `pullback_quantile` of its drops from the 20-day high, in ATR units;
  its trailing stop is `stop_scale` x that depth, kept between stop_min_atr and stop_max_atr.
  A stock that routinely pulls back 5 ATR gets a wide stop; a calm one a tight stop.
- efficiency ratio (60 sessions): how cleanly it trends;  lag-1 autocorrelation (120 sessions):
  whether it tends to revert;  style = trend (ER >= er_trend), mean_revert (autocorrelation <=
  revert_autocorr) or steady.

Targets: the proposal's base allocation (10% satellite, 7.5% core) tilted +/- max_tilt by momentum
rank, scaled to the regime's invested share, each capped at 15%. New positions need an uptrend with
positive momentum; a HELD stock keeps its place while its long-term trend holds (above EMA200 or
positive 120-session momentum), even if its rank slips. Holding is the default.

Exits (SELL approval requests; never sold by code alone):
- normal pullback inside the stock's own range: HOLD;
- trend deterioration: `confirm_days` consecutive closes below the stock-specific trailing stop AND
  a weakening trend (EMA50 falling or 60-session momentum negative);
- weak stock: `confirm_days` closes below EMA200 with negative 120-session momentum;
- strong negative news while below EMA50;
- loss cap (always): a close below the entry's anchored stop (entry - loss_cap_atr x ATR), or an
  intraday price one ATR below it.
Entries: toward the target only when it is meaningfully below it (rebalance_band); after an exit no
re-entry for cooldown_days unless the stock makes a new 20-day high; mean-reverting stocks are only
bought at or below VWAP; nobody chases (> 0.5 ATR above VWAP or intraday RSI >= 80) or buys into a
hard intraday fall or a weak market.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .intraday import Decision, PositionPlan
from .portfolio import BENCH, Holding, _cap_weights, market_regime, momentum  # noqa: F401  (re-export)
from .risk import allocation_for
from .settings import StrategyConfig, StudentConfig


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def profile(frame: pd.DataFrame, cols: dict, cfg: StrategyConfig) -> dict | None:
    """Stock-specific behaviour from completed daily bars up to the previous session."""
    ac = cfg.adaptive
    if frame is None or len(frame) < 130:
        return None
    h = frame.iloc[-ac.profile_days:]
    close, atr = h["close"].astype(float), h[cols["atr"]].astype(float)
    roll_hi = close.rolling(20, min_periods=1).max()
    pull = ((roll_hi - close) / atr).replace([np.inf, -np.inf], np.nan).dropna()
    normal = float(pull.quantile(ac.pullback_quantile)) if len(pull) > 20 else 3.0
    stop_k = min(max(normal * ac.stop_scale, ac.stop_min_atr), ac.stop_max_atr)
    c60 = close.iloc[-61:]
    moves = c60.diff().abs().sum()
    er = float(abs(c60.iloc[-1] - c60.iloc[0]) / moves) if moves > 0 else 0.0
    rets = close.pct_change().iloc[-120:].dropna()
    ac1 = float(rets.autocorr(1)) if len(rets) > 30 else 0.0
    ac1 = 0.0 if not math.isfinite(ac1) else ac1
    style = "trend" if er >= ac.er_trend else "mean_revert" if ac1 <= ac.revert_autocorr else "steady"
    ema50 = frame[cols["ema_mid"]].astype(float)
    ema200 = _f(frame.iloc[-1][cols["ema_slow"]])
    return {"style": style, "normal_pullback_atr": round(normal, 2), "stop_atr": round(stop_k, 2),
            "efficiency": round(er, 3), "autocorr": round(ac1, 3),
            "ema50_slope": _f(ema50.iloc[-1] - ema50.iloc[-11]) if len(ema50) > 11 else 0.0,
            "ema200": ema200, "last_closes": [float(x) for x in close.iloc[-10:]],
            "high20": float(close.iloc[-20:].max())}


def compute_targets(student: StudentConfig, frames: dict, news: dict, cols: dict, cfg: StrategyConfig,
                    regime: dict, held: set[str] | None = None) -> dict[str, dict]:
    ac = cfg.adaptive
    held = held or set()
    out, ranked = {}, []
    for sym in student.symbols:
        f = frames.get(sym)
        m = momentum(f, cols, cfg.portfolio.momentum_weights)
        prof = profile(f, cols, cfg)
        n = news.get(sym) or {}
        nscore = n.get("score") if n.get("articles", 0) >= cfg.news.min_articles else None
        veto = nscore is not None and nscore <= cfg.news.veto_below
        entry_ok = bool(m and prof and m["trend_ok"] and m["raw"] > 0 and not veto)
        hold_ok = bool(m and prof and ((prof["ema200"] and m["close"] > prof["ema200"]) or m["r120"] > 0)
                       and not veto)
        ok = hold_ok if sym in held else entry_ok
        why = ("no daily history" if not (m and prof) else "news veto" if veto else
               ("long-term trend broken" if sym in held else "no uptrend with positive momentum") if not ok
               else "eligible")
        out[sym] = {"weight": 0.0, "target_value": 0.0, "eligible": ok, "rank": None, "why": why, "momentum": m,
                    "profile": prof, "news": nscore, "hold_ok": hold_ok, "veto": veto}
        if ok:
            ranked.append((m["score"], sym))
    ranked.sort(reverse=True)
    raw = {}
    for i, (_, s) in enumerate(ranked):
        z = 1 - 2 * i / (len(ranked) - 1) if len(ranked) > 1 else 0.0        # +1 best .. -1 worst
        base = allocation_for(student.category(s), cfg) / student.starting_capital
        raw[s] = base * (1 + ac.max_tilt * z)
        out[s]["rank"] = i + 1
    gross = {"bull": ac.gross_bull, "neutral": ac.gross_neutral, "bear": ac.gross_bear}[regime["label"]]
    # the invested share follows the regime, but never more than the eligible stocks' base allocations
    # (stocks without evidence leave their money in cash)
    budget = min(gross, sum(allocation_for(student.category(s), cfg) for s in raw) / student.starting_capital
                 * (1 + ac.max_tilt * 0.5)) if raw else 0.0
    for s, w in _cap_weights(raw, budget, cfg.risk.max_position_pct / 100).items():
        out[s]["weight"] = round(w, 4)
        out[s]["target_value"] = round(w * student.starting_capital, 2)
        out[s]["why"] = (f"target {w:.1%} ({out[s]['profile']['style']} stock, rank {out[s]['rank']}, "
                         f"regime {regime['label']})")
    return out


def decide_v3(symbol: str, cfg: StrategyConfig, target: dict, bar, plan: PositionPlan, holding: Holding,
              market: dict, days_since_exit: int | None) -> Decision:
    ac, ic = cfg.adaptive, cfg.intraday
    m, prof = target.get("momentum") or {}, target.get("profile") or {}
    atr = m.get("atr")
    price = _f(bar["close"]) if bar is not None else None
    if price is None or not atr or not prof:
        return Decision("HOLD", "none", ["no complete daily or intraday data"], {}, price)
    held = plan.qty > 0
    closes = prof["last_closes"]
    last_close = closes[-1]
    recent = closes[-ac.confirm_days:]
    ev = {"engine": "v3", "price": round(price, 2), "style": prof["style"], "stop_atr": prof["stop_atr"],
          "normal_pullback_atr": prof["normal_pullback_atr"], "efficiency": prof["efficiency"],
          "autocorr": prof["autocorr"], "target_value": target.get("target_value"), "rank": target.get("rank"),
          "held_value": round(holding.value, 2), "momentum": {k: m.get(k) for k in ("r20", "r60", "r120", "score")},
          "market": market, "target_why": target.get("why")}

    # ---------------------------------------------------------------- exits: only on evidence
    if held:
        sells = []
        if plan.anchor_stop and last_close < plan.anchor_stop:
            sells.append(f"loss cap: close {last_close:.2f} below the entry stop {plan.anchor_stop:.2f}")
        if plan.anchor_stop and price < plan.anchor_stop - atr:
            sells.append(f"loss cap (intraday): {price:.2f} more than 1 ATR below the entry stop {plan.anchor_stop:.2f}")
        if holding.highest_close:
            line = holding.highest_close - prof["stop_atr"] * atr
            ev["trailing_stop"] = round(line, 2)
            below = all(c < line for c in recent)
            weakening = (prof["ema50_slope"] or 0) < 0 or (m.get("r60") or 0) < 0
            if below and weakening:
                sells.append(f"trend deterioration: {ac.confirm_days} closes below the {prof['stop_atr']:.1f}-ATR "
                             f"trailing stop {line:.2f} and the trend is weakening")
        ema200 = prof.get("ema200")
        if ema200 and all(c < ema200 for c in recent) and (m.get("r120") or 0) < 0:
            sells.append(f"weak stock: {ac.confirm_days} closes below EMA200 {ema200:.2f} with negative "
                         f"120-session momentum")
        if target.get("veto") and m.get("ema50") and last_close < m["ema50"]:
            sells.append("strong negative news while below EMA50")
        if sells:
            if plan.sell_pending:
                return Decision("HOLD", "exit", ["SELL already waiting for the student: " + sells[0]], ev, price)
            return Decision("SELL", "exit", sells, ev, price)

    # ---------------------------------------------------------------- deliberate holding / entries
    tgt = target.get("target_value") or 0.0
    gap = tgt - holding.value
    if held and gap < max(ac.rebalance_band * tgt, cfg.risk.min_order_value):
        pull = ((holding.highest_close or price) - price) / atr
        return Decision("HOLD", "none", [f"holding deliberately: {prof['style']} stock, "
                                         f"{pull:.1f} ATR below its high (normal pullback up to "
                                         f"{prof['normal_pullback_atr']:.1f} ATR), trend intact"], ev, price)
    if tgt <= 0 or gap < max(ac.rebalance_band * tgt, cfg.risk.min_order_value):
        return Decision("HOLD", "none", [f"no entry: {target.get('why')}"], ev, price)
    blocks = []
    if not held and days_since_exit is not None and days_since_exit < ac.cooldown_days and last_close < prof["high20"]:
        blocks.append(f"cooldown: sold {days_since_exit} session(s) ago and no new 20-day high")
    falling = (price < bar["vwap"] and bar["i_ema9"] < bar["i_ema21"] and (_f(bar["i_ema21_slope"]) or 0) < 0
               and (_f(bar["lower_lows6"]) or 0) >= 4)
    if falling:
        blocks.append("falling hard intraday")
    if market.get("known") and market["change_pct"] <= -ic.market_weak_drop_pct and market["below_vwap"]:
        blocks.append(f"weak market: SPY {market['change_pct']:+.2f}% and below its VWAP")
    if price > bar["vwap"] + cfg.portfolio.max_chase_atr * atr or (_f(bar["i_rsi"]) or 0) >= 80:
        blocks.append("stretched above VWAP")
    if prof["style"] == "mean_revert" and price > bar["vwap"]:
        blocks.append("mean-reverting stock: buy only at or below VWAP")
    if plan.sell_pending:
        blocks.append("a SELL approval is waiting")
    if plan.buys_today_student >= ic.max_buys_per_student_per_day:
        blocks.append("daily BUY limit reached")
    if held and plan.minutes_since_last_entry is not None and plan.minutes_since_last_entry < ic.min_minutes_between_entries:
        blocks.append("last entry too recent")
    if blocks:
        return Decision("HOLD", "rebalance", ["BUY postponed: " + "; ".join(blocks)], ev, price, blocked=blocks)
    spend = gap if price <= bar["vwap"] else gap / 2
    stop = plan.anchor_stop if held and plan.anchor_stop else price - ac.loss_cap_atr * atr
    ev.update({"buy_value": round(spend, 2), "stop": round(stop, 2)})
    return Decision("BUY", "initial" if not held else "rebalance",
                    [f"{target.get('why')}: holding ${holding.value:,.0f}, buying ${spend:,.0f}",
                     f"momentum 20/60/120 {m.get('r20', 0):+.1%}/{m.get('r60', 0):+.1%}/{m.get('r120', 0):+.1%}; "
                     f"stock-specific stop {prof['stop_atr']:.1f} ATR, loss cap {ac.loss_cap_atr:g} ATR"],
                    ev, price, stop)
