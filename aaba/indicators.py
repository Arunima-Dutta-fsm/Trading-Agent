"""Technical indicators written in plain pandas so every number can be checked.

Column names follow the old project's indicator CSVs (EMA20, EMA50, EMA200, RSI14,
MACD, MACD_SIGNAL, MACD_HIST, ATR14, BB_LOWER, BB_MIDDLE, BB_UPPER, OBV, OBV_AVG20).
If a length is changed in strategy.json, the column name changes with it
(for example EMA30), so a name always matches what was calculated.

Methods: EMA = exponential average (adjust=False). RSI and ATR use Wilder's
smoothing. Bollinger Bands use the population standard deviation. Values that
need more history than is available stay empty (NaN) and are never filled in.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .settings import IndicatorCfg

BAR_COLS = ["open", "high", "low", "close", "volume"]


def colnames(cfg: IndicatorCfg) -> dict[str, str]:
    return {
        "ema_fast": f"EMA{cfg.ema_fast}",
        "ema_mid": f"EMA{cfg.ema_mid}",
        "ema_slow": f"EMA{cfg.ema_slow}",
        "rsi": f"RSI{cfg.rsi_length}",
        "macd": "MACD",
        "macd_signal": "MACD_SIGNAL",
        "macd_hist": "MACD_HIST",
        "atr": f"ATR{cfg.atr_length}",
        "bb_lower": "BB_LOWER",
        "bb_middle": "BB_MIDDLE",
        "bb_upper": "BB_UPPER",
        "obv": "OBV",
        "obv_avg": f"OBV_AVG{cfg.obv_avg_length}",
    }


def clean_bars(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Drop rows that cannot be real prices. Returns (clean frame, list of problems)."""
    problems: list[str] = []
    d = df.copy()
    for c in BAR_COLS:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.sort_values("timestamp")
    dup = d.duplicated(subset="timestamp", keep="last")
    if dup.any():
        problems.append(f"{int(dup.sum())} duplicate bars removed")
        d = d[~dup]
    bad = (d[["open", "high", "low", "close"]].isna().any(axis=1)
           | (d[["open", "high", "low", "close"]] <= 0).any(axis=1)
           | (d["high"] < d["low"])
           | d["volume"].isna() | (d["volume"] < 0)
           | ~np.isfinite(d[["open", "high", "low", "close"]]).all(axis=1))
    if bad.any():
        problems.append(f"{int(bad.sum())} invalid bars removed")
        d = d[~bad]
    return d.reset_index(drop=True), problems


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss
    out = 100 - 100 / (1 + rs)
    out = out.where(avg_loss != 0, 100.0)          # only gains: RSI is 100
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)  # flat prices
    return out.where(avg_gain.notna())


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff()).fillna(0)
    return (direction * volume).cumsum()


def add_indicators(bars: pd.DataFrame, cfg: IndicatorCfg) -> pd.DataFrame:
    """bars: one symbol, columns timestamp/open/high/low/close/volume (others kept)."""
    c = colnames(cfg)
    d = bars.sort_values("timestamp").reset_index(drop=True).copy()
    close = d["close"]
    d[c["ema_fast"]] = ema(close, cfg.ema_fast)
    d[c["ema_mid"]] = ema(close, cfg.ema_mid)
    d[c["ema_slow"]] = ema(close, cfg.ema_slow)
    d[c["rsi"]] = rsi(close, cfg.rsi_length)
    fast = close.ewm(span=cfg.macd_fast, adjust=False, min_periods=cfg.macd_fast).mean()
    slow = close.ewm(span=cfg.macd_slow, adjust=False, min_periods=cfg.macd_slow).mean()
    d[c["macd"]] = fast - slow
    d[c["macd_signal"]] = d[c["macd"]].ewm(span=cfg.macd_signal, adjust=False,
                                           min_periods=cfg.macd_signal).mean()
    d[c["macd_hist"]] = d[c["macd"]] - d[c["macd_signal"]]
    d[c["atr"]] = atr(d["high"], d["low"], close, cfg.atr_length)
    mid = close.rolling(cfg.bb_length, min_periods=cfg.bb_length).mean()
    sd = close.rolling(cfg.bb_length, min_periods=cfg.bb_length).std(ddof=0)
    d[c["bb_middle"]] = mid
    d[c["bb_upper"]] = mid + cfg.bb_std * sd
    d[c["bb_lower"]] = mid - cfg.bb_std * sd
    d[c["obv"]] = obv(close, d["volume"])
    d[c["obv_avg"]] = d[c["obv"]].rolling(cfg.obv_avg_length, min_periods=cfg.obv_avg_length).mean()
    return d


def add_indicators_multi(bars: pd.DataFrame, cfg: IndicatorCfg) -> pd.DataFrame:
    """bars for many symbols in one long table (column 'symbol')."""
    parts = [add_indicators(g, cfg) for _, g in bars.groupby("symbol", sort=True)]
    return pd.concat(parts, ignore_index=True) if parts else bars.copy()


def required_columns(cfg: IndicatorCfg) -> list[str]:
    return list(colnames(cfg).values())
