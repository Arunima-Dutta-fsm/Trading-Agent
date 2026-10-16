"""Market data: daily bars for many symbols in ONE batch request, latest prices,
the market clock and the trading calendar.

Two providers:
- AlpacaMarketData: the real one (Alpaca market-data API).
- SyntheticMarketData: made-up prices for automated tests and offline demos only.
  Everything it produces is labelled SIMULATED and must never be presented as real.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from . import clock
from .settings import DataCfg, data_provider, load_students

log = logging.getLogger("aaba.data")


@dataclass
class Session:
    date: date
    open: datetime   # aware, New York
    close: datetime  # aware, New York


@dataclass
class MarketClock:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


@dataclass
class BarsResult:
    bars: pd.DataFrame          # long table: symbol, timestamp, open, high, low, close, volume, ...
    source: str
    feed: str
    batch_calls: int
    notes: list[str]


@dataclass
class LatestPrice:
    symbol: str
    price: float
    timestamp: datetime


def _retry(fn, what: str, attempts: int = 3):
    """Retry slow or rate-limited calls: waits 2s then 4s. Other errors fail at once."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # alpaca-py raises APIError / requests errors
            last = e
            msg = str(e).lower()
            code = getattr(e, "status_code", None)
            transient = (code in (429, 500, 502, 503, 504) or "timeout" in msg or "timed out" in msg
                         or "rate limit" in msg or "too many requests" in msg or "connection" in msg)
            if not transient or i == attempts - 1:
                raise
            wait = 2 ** (i + 1)
            log.warning("%s failed (%s); retrying in %ss", what, type(e).__name__, wait)
            time.sleep(wait)
    raise last  # pragma: no cover


def completed_regular_bars(df: pd.DataFrame, bar_minutes: int, now: datetime) -> pd.DataFrame:
    """Keep only intraday candles that lie inside regular hours (09:30-16:00 New York) and have
    already CLOSED by `now` (a candle stamped 10:05 with 5-minute bars closes at 10:10)."""
    if df.empty:
        return df
    ts_ny = df["timestamp"].dt.tz_convert("America/New_York")
    minutes = ts_ny.dt.hour * 60 + ts_ny.dt.minute
    inside = (minutes >= 9 * 60 + 30) & (minutes + bar_minutes <= 16 * 60)
    closed = df["timestamp"] + pd.Timedelta(minutes=bar_minutes) <= pd.Timestamp(now)
    return df[inside & closed].sort_values(["symbol", "timestamp"]).reset_index(drop=True)


def data_keys() -> tuple[str, str]:
    """Market data is not account specific. Use DATA_ALPACA_* if set, else the first
    student account that has keys."""
    k, s = os.getenv("DATA_ALPACA_API_KEY", "").strip(), os.getenv("DATA_ALPACA_SECRET_KEY", "").strip()
    if k and s:
        return k, s
    for st in load_students().students:
        k, s = st.alpaca_keys()
        if k and s:
            return k, s
    return "", ""


# =========================================================================== Alpaca
class AlpacaMarketData:
    name = "alpaca"
    simulated = False

    def __init__(self):
        key, secret = data_keys()
        if not key or not secret:
            raise RuntimeError("No Alpaca keys found for market data. Set DATA_ALPACA_API_KEY/"
                               "DATA_ALPACA_SECRET_KEY or STUDENT1_ALPACA_API_KEY/SECRET in .env")
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient
        self._data = StockHistoricalDataClient(key, secret)
        self._trading = TradingClient(key, secret, paper=True)   # clock/calendar only
        self._key, self._secret = key, secret

    def now(self) -> datetime:
        return clock.now_utc()

    def daily_bars(self, symbols: list[str], cfg: DataCfg, end: datetime | None = None) -> BarsResult:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        notes: list[str] = []
        now = clock.now_utc()
        start = now - timedelta(days=cfg.history_days)
        feed = cfg.feed

        def fetch(feed_name: str, end_dt: datetime | None):
            req = StockBarsRequest(symbol_or_symbols=sorted(set(symbols)), timeframe=TimeFrame.Day,
                                   start=start, end=end_dt, adjustment=Adjustment.SPLIT,
                                   feed=DataFeed.SIP if feed_name == "sip" else DataFeed.IEX)
            return _retry(lambda: self._data.get_stock_bars(req), "batch bars request")

        try:
            # Free plans cannot read the last 15 minutes of SIP data, so stop a little earlier.
            end_dt = end or (now - timedelta(minutes=cfg.sip_delay_minutes) if feed == "sip" else None)
            resp = fetch(feed, end_dt)
        except Exception as e:
            if feed != "sip":
                raise
            notes.append(f"SIP feed refused ({type(e).__name__}); fell back to IEX feed")
            feed = "iex"
            resp = fetch("iex", end)

        df = resp.df.reset_index() if hasattr(resp, "df") else pd.DataFrame()
        if df.empty:
            return BarsResult(pd.DataFrame(columns=["symbol", "timestamp"]), self.name, feed, 1, notes + ["no bars"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        # one request for all symbols (alpaca-py follows page tokens inside this one call)
        return BarsResult(df, self.name, feed, 1, notes)

    def intraday_bars(self, symbols: list[str], bar_minutes: int, lookback_days: int, feed: str = "iex",
                      start: datetime | None = None, end: datetime | None = None) -> BarsResult:
        """Completed regular-hours candles for all symbols in ONE batch request (pages followed)."""
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        now = clock.now_utc()
        start = start or now - timedelta(days=lookback_days * 2 + 4)       # weekends and holidays
        if end is None and feed == "sip":
            end = now - timedelta(minutes=16)                               # free SIP is 15 minutes late
        req = StockBarsRequest(symbol_or_symbols=sorted(set(symbols)), timeframe=TimeFrame(bar_minutes, TimeFrameUnit.Minute),
                               start=start, end=end, adjustment=Adjustment.SPLIT,
                               feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX)
        resp = _retry(lambda: self._data.get_stock_bars(req), "intraday batch bars")
        df = resp.df.reset_index() if hasattr(resp, "df") else pd.DataFrame()
        if df.empty:
            return BarsResult(pd.DataFrame(columns=["symbol", "timestamp"]), self.name, feed, 1, ["no intraday bars"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return BarsResult(completed_regular_bars(df, bar_minutes, end or now), self.name, feed, 1, [])

    def latest_prices(self, symbols: list[str]) -> dict[str, LatestPrice]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest
        req = StockLatestTradeRequest(symbol_or_symbols=sorted(set(symbols)), feed=DataFeed.IEX)
        res = _retry(lambda: self._data.get_stock_latest_trade(req), "latest trades")
        out = {}
        for sym, t in res.items():
            ts = t.timestamp if t.timestamp.tzinfo else t.timestamp.replace(tzinfo=clock.UTC)
            out[sym] = LatestPrice(sym, float(t.price), ts)
        return out

    def market_clock(self) -> MarketClock:
        c = _retry(self._trading.get_clock, "market clock")
        return MarketClock(clock.to_ny(c.timestamp), bool(c.is_open), clock.to_ny(c.next_open),
                           clock.to_ny(c.next_close))

    def sessions(self, start: date, end: date) -> list[Session]:
        from alpaca.trading.requests import GetCalendarRequest
        cal = _retry(lambda: self._trading.get_calendar(GetCalendarRequest(start=start, end=end)), "calendar")
        return [Session(c.date, clock.to_ny(c.open), clock.to_ny(c.close)) for c in cal]


# =========================================================================== Synthetic
class SyntheticMarketData:
    """SIMULATED prices. Deterministic per symbol so tests are repeatable."""
    name = "synthetic"
    simulated = True

    def __init__(self, now: datetime | None = None, market_open: bool | None = None):
        self._now = now
        self._market_open = market_open

    def now(self) -> datetime:
        return self._now or clock.now_utc()

    def sessions(self, start: date, end: date) -> list[Session]:
        out = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                o = datetime(d.year, d.month, d.day, 9, 30, tzinfo=clock.NY)
                c = datetime(d.year, d.month, d.day, 16, 0, tzinfo=clock.NY)
                out.append(Session(d, o, c))
            d += timedelta(days=1)
        return out

    def market_clock(self) -> MarketClock:
        now_ny = clock.to_ny(self.now())
        today = [s for s in self.sessions(now_ny.date(), now_ny.date())]
        is_open = bool(today and today[0].open <= now_ny < today[0].close)
        if self._market_open is not None:
            is_open = self._market_open
        upcoming = self.sessions(now_ny.date(), now_ny.date() + timedelta(days=7))
        nxt = next((s for s in upcoming if s.open > now_ny), upcoming[-1])
        cur_close = today[0].close if today and now_ny < today[0].close else nxt.close
        return MarketClock(now_ny, is_open, nxt.open, cur_close)

    @staticmethod
    def _seed(sym: str) -> int:
        return int(hashlib.sha256(sym.encode()).hexdigest()[:8], 16)

    EPOCH = date(2021, 1, 4)   # every simulated price path starts here, so any window is a slice of it

    def _series(self, sym: str, until: date) -> pd.DataFrame:
        days = [s.date for s in self.sessions(self.EPOCH, until)]
        rng = np.random.default_rng(self._seed(sym))
        n = len(days)
        drift = rng.uniform(-0.0003, 0.0012)
        vol = rng.uniform(0.012, 0.035)
        price0 = rng.uniform(40, 600)
        rets = rng.normal(drift, vol, 4000)[:n]          # fixed-length draws keep paths stable over time
        noise = rng.normal(0, 1, (4, 4000))[:, :n]
        volume = rng.integers(2_000_000, 40_000_000, 4000)[:n].astype(float)
        close = price0 * np.exp(np.cumsum(rets))
        openp = close * (1 + noise[0] * vol / 3)
        high = np.maximum(openp, close) * (1 + np.abs(noise[1] * vol / 2))
        low = np.minimum(openp, close) * (1 - np.abs(noise[2] * vol / 2))
        ts = [pd.Timestamp(datetime(d.year, d.month, d.day, tzinfo=clock.NY)).tz_convert("UTC") for d in days]
        return pd.DataFrame({"symbol": sym, "timestamp": ts, "open": openp, "high": high, "low": low,
                             "close": close, "volume": volume, "trade_count": volume / 100,
                             "vwap": (high + low + close) / 3})

    def daily_bars(self, symbols: list[str], cfg: DataCfg, end: datetime | None = None) -> BarsResult:
        now_ny = clock.to_ny(end or self.now())
        start = pd.Timestamp(datetime.combine(now_ny.date() - timedelta(days=cfg.history_days), datetime.min.time()),
                             tz="America/New_York").tz_convert("UTC")
        # includes today's (possibly unfinished) bar on purpose, so the
        # drop_incomplete_bars() safety step is exercised in tests
        frames = [self._series(s, now_ny.date()) for s in sorted(set(symbols))]
        df = pd.concat(frames, ignore_index=True)
        df = df[df["timestamp"] >= start].reset_index(drop=True)
        return BarsResult(df, self.name, "synthetic", 1, ["SIMULATED DATA - not real prices"])

    def intraday_bars(self, symbols: list[str], bar_minutes: int, lookback_days: int, feed: str = "synthetic",
                      start: datetime | None = None, end: datetime | None = None) -> BarsResult:
        """SIMULATED regular-hours candles: for each session a deterministic path from that day's
        simulated open to its close (a Brownian bridge), so daily and intraday data agree."""
        now = end or self.now()
        now_ny = clock.to_ny(now)
        days = [s for s in self.sessions(now_ny.date() - timedelta(days=lookback_days * 2 + 4), now_ny.date())
                if s.open <= now_ny][-lookback_days:]
        n = 390 // bar_minutes
        frames = []
        for sym in sorted(set(symbols)):
            daily = self._series(sym, now_ny.date()).set_index("timestamp")
            for s in days:
                key = pd.Timestamp(datetime(s.date.year, s.date.month, s.date.day, tzinfo=clock.NY)).tz_convert("UTC")
                if key not in daily.index:
                    continue
                d = daily.loc[key]
                rng = np.random.default_rng(self._seed(f"{sym}|{s.date}|{bar_minutes}"))
                steps = rng.normal(0, 1, n)
                walk = np.concatenate([[0.0], np.cumsum(steps)])
                t = np.linspace(0, 1, n + 1)
                bridge = walk - t * walk[-1]
                scale = max(abs(d["high"] - d["low"]), 0.01 * d["close"]) / (4 * max(np.abs(bridge).max(), 1e-9))
                path = d["open"] + (d["close"] - d["open"]) * t + bridge * scale
                o, c = path[:-1], path[1:]
                wig = np.abs(rng.normal(0, scale * 0.3, (2, n)))
                ts = [s.open + timedelta(minutes=bar_minutes * i) for i in range(n)]
                shape = 1.5 - np.sin(np.linspace(0, np.pi, n))           # more volume at the open and close
                frames.append(pd.DataFrame({
                    "symbol": sym, "timestamp": pd.to_datetime(ts).tz_convert("UTC"), "open": o,
                    "high": np.maximum(o, c) + wig[0], "low": np.maximum(np.minimum(o, c) - wig[1], 0.01),
                    "close": c, "volume": d["volume"] / n * shape / shape.mean()}))
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["symbol", "timestamp"])
        return BarsResult(completed_regular_bars(df, bar_minutes, now) if not df.empty else df, self.name,
                          "synthetic", 1, ["SIMULATED DATA - not real prices"])

    def latest_prices(self, symbols: list[str]) -> dict[str, LatestPrice]:
        """The last simulated close (today's bar if it exists) stamped with the simulated 'now'."""
        now = self.now()
        out = {}
        for sym in sorted(set(symbols)):
            g = self._series(sym, clock.to_ny(now).date())
            out[sym] = LatestPrice(sym, float(g["close"].iloc[-1]), now)
        return out


_provider_override = None


def set_provider(p) -> None:
    """Tests use this to inject a provider."""
    global _provider_override
    _provider_override = p


def get_provider():
    if _provider_override is not None:
        return _provider_override
    if data_provider() == "synthetic":
        # tests can pin the clock so results do not depend on when they run
        fixed = os.getenv("AABA_SYNTHETIC_NOW", "").strip()
        is_open = os.getenv("AABA_SYNTHETIC_MARKET_OPEN", "").strip().lower()
        return SyntheticMarketData(now=clock.parse_iso(fixed) if fixed else None,
                                   market_open={"true": True, "false": False}.get(is_open))
    return AlpacaMarketData()


# =========================================================================== freshness
def last_completed_session(sessions: list[Session], now_ny: datetime) -> Session | None:
    done = [s for s in sessions if s.close <= now_ny]
    return done[-1] if done else None


def bar_session_date(ts: pd.Timestamp) -> date:
    """Alpaca daily bars are stamped at midnight New York time of the session day."""
    return pd.Timestamp(ts).tz_convert("America/New_York").date()


def drop_incomplete_bars(bars: pd.DataFrame, sessions: list[Session], now_ny: datetime) -> tuple[pd.DataFrame, int]:
    """Remove today's daily bar while today's session is still running (no look-ahead,
    no half-finished candles). Returns (frame, rows dropped)."""
    if bars.empty:
        return bars, 0
    open_today = [s for s in sessions if s.date == now_ny.date() and now_ny < s.close]
    if not open_today:
        return bars, 0
    dates = bars["timestamp"].map(bar_session_date)
    mask = dates >= now_ny.date()
    return bars[~mask].copy(), int(mask.sum())
