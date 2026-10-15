"""News sentiment and earnings dates.

News: Alpaca's news API (Benzinga headlines), fetched for many symbols in one
request with the same paper keys. Sentiment is a simple, transparent word-list
score from -1 (very negative) to +1 (very positive) over headline + summary. It is
crude on purpose: easy to explain and never invents facts. A strongly negative
score can only block a new BUY; it never creates a trade by itself.

Earnings: optional. If FINNHUB_API_KEY is set, the Finnhub earnings calendar is
used to avoid buying just before results. If it is not set, the check is marked
'unknown' and (by default) does not block.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from datetime import date, timedelta

from . import clock
from .market_data import _retry, data_keys
from .settings import EarningsCfg, NewsCfg, data_provider

log = logging.getLogger("aaba.news")

POSITIVE = {
    "beat", "beats", "surge", "surges", "soar", "soars", "jump", "jumps", "rally", "rallies", "record",
    "upgrade", "upgraded", "outperform", "raises", "raised", "strong", "growth", "profit", "gain",
    "gains", "bullish", "buyback", "approval", "approved", "wins", "win", "partnership", "expands",
    "tops", "exceeds", "higher", "boost", "boosts", "optimistic", "breakthrough",
}
NEGATIVE = {
    "miss", "misses", "plunge", "plunges", "drop", "drops", "fall", "falls", "slump", "slumps",
    "downgrade", "downgraded", "underperform", "cuts", "cut", "weak", "loss", "losses", "bearish",
    "lawsuit", "probe", "investigation", "recall", "fraud", "halt", "halted", "bankruptcy", "layoffs",
    "warning", "warns", "lower", "decline", "declines", "tumble", "tumbles", "sank", "sinks", "crash",
    "subpoena", "fined", "delay", "delays", "resigns", "dilution",
}
WORD = re.compile(r"[a-z]+")


def score_text(text: str) -> float:
    words = WORD.findall((text or "").lower())
    pos = sum(w in POSITIVE for w in words)
    neg = sum(w in NEGATIVE for w in words)
    if pos + neg == 0:
        return 0.0
    return round((pos - neg) / (pos + neg), 3)


def news_sentiment(symbols: list[str], cfg: NewsCfg) -> dict[str, dict]:
    """Returns {symbol: {"score": float|None, "articles": int, "headlines": [...], "status": str}}."""
    out = {s: {"score": None, "articles": 0, "headlines": [], "status": "not checked"} for s in symbols}
    if not cfg.enabled:
        for s in out.values():
            s["status"] = "disabled"
        return out
    if data_provider() == "synthetic":
        for s in out.values():
            s["status"] = "no news (simulated mode)"
        return out
    key, secret = data_keys()
    if not key:
        for s in out.values():
            s["status"] = "no API key"
        return out
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest
        client = NewsClient(key, secret)
        start = clock.now_utc() - timedelta(hours=cfg.lookback_hours)
        req = NewsRequest(symbols=",".join(sorted(symbols)), start=start, limit=50, exclude_contentless=False)
        res = _retry(lambda: client.get_news(req), "news request")
        items = res.data.get("news", []) if hasattr(res, "data") else []
        per: dict[str, list[float]] = {s: [] for s in symbols}
        for n in items:
            headline = getattr(n, "headline", "") or ""
            summary = getattr(n, "summary", "") or ""
            sc = score_text(f"{headline} {summary}")
            for sym in getattr(n, "symbols", []) or []:
                if sym in per:
                    per[sym].append(sc)
                    if len(out[sym]["headlines"]) < 3:
                        out[sym]["headlines"].append(headline[:140])
        for sym, scores in per.items():
            out[sym]["articles"] = len(scores)
            out[sym]["status"] = "ok"
            out[sym]["score"] = round(sum(scores) / len(scores), 3) if scores else None
    except Exception as e:
        log.warning("news unavailable: %s", type(e).__name__)
        for s in out.values():
            s["status"] = f"news unavailable ({type(e).__name__})"
    return out


def earnings_dates(symbols: list[str], cfg: EarningsCfg, today: date) -> dict[str, dict]:
    """Returns {symbol: {"next_earnings": 'YYYY-MM-DD'|None, "status": str}}."""
    out = {s: {"next_earnings": None, "status": "unknown"} for s in symbols}
    if not cfg.enabled:
        for v in out.values():
            v["status"] = "disabled"
        return out
    token = os.getenv("FINNHUB_API_KEY", "").strip()
    if not token or data_provider() == "synthetic":
        for v in out.values():
            v["status"] = "unknown (no FINNHUB_API_KEY)" if not token else "unknown (simulated mode)"
        return out
    try:
        params = urllib.parse.urlencode({"from": today.isoformat(),
                                         "to": (today + timedelta(days=21)).isoformat(), "token": token})
        url = f"https://finnhub.io/api/v1/calendar/earnings?{params}"

        def call():
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.loads(r.read().decode())
        data = _retry(call, "earnings calendar")
        wanted = set(symbols)
        for row in data.get("earningsCalendar", []):
            sym = row.get("symbol")
            if sym in wanted:
                d = row.get("date")
                cur = out[sym]["next_earnings"]
                if d and (cur is None or d < cur):
                    out[sym]["next_earnings"] = d
        for v in out.values():
            v["status"] = "ok"
    except Exception as e:
        log.warning("earnings calendar unavailable: %s", type(e).__name__)
        for v in out.values():
            v["status"] = f"unknown ({type(e).__name__})"
    return out
