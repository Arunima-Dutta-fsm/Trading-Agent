"""The signal engine: turns indicator values into BUY, SELL or HOLD with reasons.

This is the only place where signals are decided. The live workflow and the
backtest both call evaluate_symbol(), so they cannot drift apart.

Rules (from the submitted proposal, Section 9), decided on the latest COMPLETED daily bar:

Growth Satellites (trend-following momentum)
  BUY  when close > EMA200, EMA20 > EMA50, the MACD histogram turned positive within the
       last N bars and is still positive, RSI is between 50 and 70, and OBV is above its
       20-day average.
  SELL (only if held) when close < EMA20, or close falls 3 x ATR below the highest close
       since entry (trailing stop), or 2 x ATR below the entry price (hard stop).

Diversified Core (mean reversion inside an uptrend)
  BUY  when close > EMA200, RSI was below 35 or close was below the lower Bollinger Band
       within the last N bars, today's close is back inside the band, and RSI is rising.
  SELL (only if held) when RSI > 65, or close reaches the upper band, or the 2 x ATR stop.

Everything else is HOLD. When a held stock meets both BUY and SELL conditions, SELL wins
(protect capital first). A stock that is not held is never sold (no short selling).
News and earnings can only turn a BUY into a HOLD, never the other way round.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from .indicators import colnames
from .settings import StrategyConfig


@dataclass
class Position:
    qty: float = 0.0
    avg_entry: float | None = None
    market_value: float = 0.0
    highest_close_since_entry: float | None = None
    room_value: float | None = None   # how many more dollars the allocation allows


@dataclass
class Signal:
    symbol: str
    category: str
    action: str                 # BUY / SELL / HOLD
    reasons: list[str]
    evidence: dict
    score: float | None
    data_quality: str           # ok / insufficient / invalid
    bar_date: str | None
    price: float | None
    blocked_by: list[str] = field(default_factory=list)


def _f(x) -> float | None:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _r(x, n=2):
    v = _f(x)
    return round(v, n) if v is not None else None


def composite_parts(row: pd.Series, prev: pd.Series, cols: dict, news_score: float | None) -> dict:
    close = row["close"]
    trend = (int(close > row[cols["ema_slow"]]) + int(row[cols["ema_fast"]] > row[cols["ema_mid"]])
             + int(close > row[cols["ema_mid"]])) / 3
    rsi = row[cols["rsi"]]
    rsi_part = 0.5 if rsi > 75 else min(max((rsi - 30) / 40, 0.0), 1.0)
    momentum = (int(row[cols["macd_hist"]] > 0) + rsi_part + int(rsi > prev[cols["rsi"]])) / 3
    volume = float(row[cols["obv"]] > row[cols["obv_avg"]])
    news = 0.5 if news_score is None else (news_score + 1) / 2
    return {"trend": round(trend, 3), "momentum": round(momentum, 3), "volume": round(volume, 3),
            "news": round(news, 3)}


def evaluate_symbol(df: pd.DataFrame, symbol: str, category: str, cfg: StrategyConfig,
                    position: Position | None = None, news: dict | None = None,
                    earnings: dict | None = None, today: date | None = None) -> Signal:
    """df: one symbol's bars WITH indicators, oldest first, ending at the latest completed bar."""
    position = position or Position()
    cols = colnames(cfg.indicators)
    held = position.qty > 0

    if df is None or df.empty:
        return Signal(symbol, category, "HOLD", ["no price data"], {}, None, "invalid", None, None)

    row = df.iloc[-1]
    bar_date = str(pd.Timestamp(row["timestamp"]).tz_convert("America/New_York").date())
    price = _f(row["close"])
    need = list(cols.values())
    missing = [c for c in need if c not in df.columns or _f(row.get(c)) is None]
    if len(df) < cfg.indicators.ema_slow or missing or price is None or len(df) < 2:
        why = f"not enough clean history for indicators ({len(df)} bars; missing: {', '.join(missing) or 'none'})"
        return Signal(symbol, category, "HOLD", [why], {"bars": len(df)}, None, "insufficient", bar_date, price)

    prev = df.iloc[-2]
    ev = {
        "close": _r(price), "bar_date": bar_date,
        cols["ema_fast"]: _r(row[cols["ema_fast"]]), cols["ema_mid"]: _r(row[cols["ema_mid"]]),
        cols["ema_slow"]: _r(row[cols["ema_slow"]]), cols["rsi"]: _r(row[cols["rsi"]], 1),
        "RSI_prev": _r(prev[cols["rsi"]], 1), "MACD_HIST": _r(row[cols["macd_hist"]], 3),
        cols["atr"]: _r(row[cols["atr"]]), "BB_LOWER": _r(row[cols["bb_lower"]]),
        "BB_UPPER": _r(row[cols["bb_upper"]]), "OBV_above_avg": bool(row[cols["obv"]] > row[cols["obv_avg"]]),
        "held_qty": position.qty, "avg_entry": _r(position.avg_entry),
    }
    news_score = (news or {}).get("score")
    parts = composite_parts(row, prev, cols, news_score)
    w = cfg.composite
    score = round(parts["trend"] * w.weight_trend + parts["momentum"] * w.weight_momentum
                  + parts["volume"] * w.weight_volume + parts["news"] * w.weight_news, 3)
    ev["composite"] = parts | {"score": score}

    atr = float(row[cols["atr"]])
    close = float(price)
    reasons_buy: list[str] = []
    reasons_sell: list[str] = []
    regime = close > row[cols["ema_slow"]]

    # ---- stops (both categories)
    if held and position.avg_entry:
        hard_stop = position.avg_entry - cfg.risk.stop_atr_mult * atr
        ev["hard_stop"] = _r(hard_stop)
        if close < hard_stop:
            reasons_sell.append(f"close {close:.2f} is below the {cfg.risk.stop_atr_mult}xATR stop {hard_stop:.2f}")

    if cfg.signal_mode == "composite":
        if regime and score >= w.buy_above:
            reasons_buy.append(f"composite score {score} >= {w.buy_above} and close above {cols['ema_slow']}")
        if held and score <= w.sell_below:
            reasons_sell.append(f"composite score {score} <= {w.sell_below}")
    elif category == "satellite":
        s = cfg.satellite
        hist = df[cols["macd_hist"]].iloc[-(s.macd_cross_lookback + 1):].tolist()
        crossed = any(hist[i] > 0 and hist[i - 1] <= 0 for i in range(1, len(hist))) and hist[-1] > 0
        trend_up = row[cols["ema_fast"]] > row[cols["ema_mid"]]
        rsi_ok = s.rsi_min <= row[cols["rsi"]] <= s.rsi_max
        obv_ok = (row[cols["obv"]] > row[cols["obv_avg"]]) or not s.require_obv_confirm
        checks = {f"close > {cols['ema_slow']}": regime, f"{cols['ema_fast']} > {cols['ema_mid']}": trend_up,
                  f"MACD histogram turned positive in last {s.macd_cross_lookback} bars": crossed,
                  f"RSI in {s.rsi_min:g}-{s.rsi_max:g}": rsi_ok, "OBV above its average": obv_ok}
        ev["buy_checks"] = {k: bool(v) for k, v in checks.items()}
        if all(checks.values()):
            reasons_buy.append("momentum entry: " + "; ".join(checks))
        if held:
            if s.exit_below_ema_fast and close < row[cols["ema_fast"]]:
                reasons_sell.append(f"close {close:.2f} fell below {cols['ema_fast']} {row[cols['ema_fast']]:.2f}")
            hi = position.highest_close_since_entry or position.avg_entry
            if hi:
                trail = hi - s.trail_atr_mult * atr
                ev["trailing_stop"] = _r(trail)
                if close < trail:
                    reasons_sell.append(f"close {close:.2f} below {s.trail_atr_mult}xATR trailing stop {trail:.2f}")
    else:
        c = cfg.core
        win = df.iloc[-c.oversold_lookback:]
        was_oversold = bool(((win[cols["rsi"]] < c.rsi_oversold) | (win["close"] < win[cols["bb_lower"]])).any())
        back_inside = close > row[cols["bb_lower"]]
        rsi_up = row[cols["rsi"]] > prev[cols["rsi"]]
        checks = {f"close > {cols['ema_slow']}": regime,
                  f"RSI < {c.rsi_oversold:g} or close below lower band in last {c.oversold_lookback} bars": was_oversold,
                  "close back inside the band": back_inside, "RSI turning up": rsi_up}
        ev["buy_checks"] = {k: bool(v) for k, v in checks.items()}
        if all(checks.values()):
            reasons_buy.append("mean-reversion entry: " + "; ".join(checks))
        if held:
            if row[cols["rsi"]] > c.rsi_exit:
                reasons_sell.append(f"RSI {row[cols['rsi']]:.1f} > {c.rsi_exit:g}")
            if c.exit_at_upper_band and close >= row[cols["bb_upper"]]:
                reasons_sell.append(f"close {close:.2f} reached the upper band {row[cols['bb_upper']]:.2f}")

    blocked: list[str] = []
    if held and reasons_sell:
        return Signal(symbol, category, "SELL", reasons_sell, ev, score, "ok", bar_date, price)

    if reasons_buy:
        if position.room_value is not None and position.room_value < cfg.risk.min_order_value:
            blocked.append(f"position already at its allocation (room ${position.room_value:,.0f})")
        n = news or {}
        if n.get("score") is not None and n.get("articles", 0) >= cfg.news.min_articles \
                and n["score"] <= cfg.news.veto_below:
            blocked.append(f"news veto: sentiment {n['score']} over {n['articles']} article(s)")
        e = earnings or {}
        if e.get("next_earnings") and today:
            days = (date.fromisoformat(e["next_earnings"]) - today).days
            if 0 <= days <= cfg.earnings.blackout_days:
                blocked.append(f"earnings on {e['next_earnings']} (within {cfg.earnings.blackout_days} days)")
        elif cfg.earnings.enabled and cfg.earnings.block_if_unknown:
            blocked.append("earnings date unknown and block_if_unknown is on")
        if blocked:
            return Signal(symbol, category, "HOLD", ["BUY setup found but blocked: " + "; ".join(blocked)],
                          ev, score, "ok", bar_date, price, blocked)
        return Signal(symbol, category, "BUY", reasons_buy, ev, score, "ok", bar_date, price)

    if reasons_sell and not held:
        why = "sell conditions met but stock is not held (no short selling)"
    elif held:
        why = "holding: no exit rule triggered"
    else:
        failed = [k for k, v in ev.get("buy_checks", {}).items() if not v]
        why = "no entry: " + ("; ".join(f"not ({k})" for k in failed) if failed else "conditions not met")
    return Signal(symbol, category, "HOLD", [why], ev, score, "ok", bar_date, price)
