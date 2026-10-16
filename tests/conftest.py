"""Shared test setup.

Every test runs with:
- simulated data and a fake broker (no network, no real orders),
- its own temporary database, folders and fake-broker files,
- made-up keys and PINs (used to prove secrets never leak into outputs).
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

_BASE = Path(tempfile.mkdtemp(prefix="aaba-tests-"))


def _test_strategy_file() -> str:
    """The tests run on a copy of configs/strategy.json with intraday decisions OFF (the daily
    strategy), whatever the deployed config says; intraday tests switch it on explicitly."""
    import json
    raw = json.loads((ROOT / "configs" / "strategy.json").read_text(encoding="utf-8"))
    raw["intraday"] = {**raw.get("intraday", {}), "enabled": False, "engine": "v1"}
    f = _BASE / "strategy-test.json"
    f.write_text(json.dumps(raw), encoding="utf-8")
    return str(f)


TEST_ENV = {
    "AABA_STRATEGY_FILE": _test_strategy_file(),
    "AABA_SKIP_DOTENV": "1",
    "AABA_DATA_PROVIDER": "synthetic",
    "AABA_BROKER": "fake",
    "AABA_DATA_DIR": str(_BASE / "data"),
    "AABA_MODEL_RUN_DIR": str(_BASE / "model-run"),
    "AABA_LOG_DIR": str(_BASE / "logs"),
    "AABA_SYNTHETIC_NOW": "2026-10-13T14:30:00+00:00",     # Tue 13 Oct 2026 10:30 New York
    "AABA_SYNTHETIC_MARKET_OPEN": "true",
    "ENABLE_PAPER_ORDERS": "true",
    "STUDENT1_ALPACA_API_KEY": "PKTESTSECRETKEYONE111",
    "STUDENT1_ALPACA_SECRET_KEY": "test-secret-value-one-xyz",
    "STUDENT2_ALPACA_API_KEY": "PKTESTSECRETKEYTWO222",
    "STUDENT2_ALPACA_SECRET_KEY": "test-secret-value-two-xyz",
    "STUDENT3_ALPACA_API_KEY": "PKTESTSECRETKEYTHREE3",
    "STUDENT3_ALPACA_SECRET_KEY": "test-secret-value-three-xyz",
    "STUDENT1_APPROVAL_PIN": "481516",
    "STUDENT2_APPROVAL_PIN": "234223",
    "STUDENT3_APPROVAL_PIN": "908172",
    "DASHBOARD_ADMIN_PIN": "777333",
    "GROQ_API_KEY": "gsk_test_not_a_real_key_123456",
    "CREWAI_TRACING_ENABLED": "false",
    "CREWAI_DISABLE_TELEMETRY": "true",
    "OTEL_SDK_DISABLED": "true",
}
_CLEAR = ("LLM_PROVIDER", "LLM_MODEL", "LLM_FALLBACK_PROVIDER", "LLM_FALLBACK_MODEL", "STUDENT1_ALPACA_ACCOUNT_NUMBER", "STUDENT2_ALPACA_ACCOUNT_NUMBER", "STUDENT3_ALPACA_ACCOUNT_NUMBER",
          "DATA_ALPACA_API_KEY", "DATA_ALPACA_SECRET_KEY", "FINNHUB_API_KEY", "AABA_DB_PATH",
          "LLM_BASE_URL", "LLM_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
          "OPENAI_API_BASE","GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_TIMEOUT", "LLM_MAX_RETRIES", "CREW_VERBOSE")


def _sanitize_env():
    os.environ.update(TEST_ENV)
    for k in _CLEAR:
        os.environ.pop(k, None)


_sanitize_env()
# CrewAI runs load_dotenv() when it is imported, which would pull the project's real .env
# (LLM provider, base URL, keys) into the tests. Import it now, then clean the environment again.
import crewai.llm  # noqa: E402,F401
_sanitize_env()

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from aaba import broker as brokers  # noqa: E402
from aaba import clock, controller, db, market_data, paths  # noqa: E402
from aaba.indicators import colnames  # noqa: E402
from aaba.settings import load_strategy, load_students  # noqa: E402

SECRETS = [v for k, v in TEST_ENV.items() if k.endswith(("_KEY", "_PIN")) and len(v) >= 6]


@pytest.fixture(autouse=True)
def no_real_orders(monkeypatch):
    """ENABLE_PAPER_ORDERS=true above only ever reaches the FakeBroker. This tripwire makes
    sure no test can send an order through the real Alpaca client, whatever it configures.
    (tests/test_e2e_paper.py uses Alpaca's own client directly and is opt-in.)"""
    def refuse(*a, **k):
        raise AssertionError("a test tried to submit an order through AlpacaPaperBroker")
    monkeypatch.setattr(brokers.AlpacaPaperBroker, "submit_market_order", refuse)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Fresh database, folders and fake broker for every test."""
    data = tmp_path / "data"
    for k in _CLEAR:
        monkeypatch.delenv(k, raising=False)
    for k, v in TEST_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AABA_DATA_DIR", str(data))
    monkeypatch.setenv("AABA_DB_PATH", str(data / "aaba.db"))
    monkeypatch.setenv("AABA_MODEL_RUN_DIR", str(tmp_path / "model-run"))
    monkeypatch.setenv("AABA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(paths, "DATA_DIR", data)
    monkeypatch.setattr(paths, "INDICATOR_DIR", data / "indicators")
    monkeypatch.setattr(paths, "VALIDATION_DIR", data / "validation")
    monkeypatch.setattr(paths, "FAKE_BROKER_DIR", data / "fake_broker")
    monkeypatch.setattr(paths, "LOCK_FILE", data / "run.lock")
    monkeypatch.setattr(paths, "MODEL_RUN_DIR", tmp_path / "model-run")
    monkeypatch.setattr(paths, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(paths, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(paths, "BACKTEST_DIR", tmp_path / "reports" / "backtests")
    paths.ensure_dirs()
    db.init_db()
    controller.reset_cache()
    brokers.clear_brokers()
    market_data.set_provider(None)
    yield tmp_path
    controller.reset_cache()
    brokers.clear_brokers()
    market_data.set_provider(None)


NOW = clock.parse_iso(TEST_ENV["AABA_SYNTHETIC_NOW"])


def indicator_frame(n: int = 220, close: float = 100.0, **last) -> pd.DataFrame:
    """A frame with every indicator column filled with neutral values; override the
    last row(s) with keyword arguments, e.g. RSI14=55. Values can be lists to set the
    last len(list) rows."""
    cfg = load_strategy()
    cols = colnames(cfg.indicators)
    end = pd.Timestamp("2026-10-12", tz="America/New_York")
    ts = pd.bdate_range(end=end, periods=n, tz="America/New_York").tz_convert("UTC")
    df = pd.DataFrame({"symbol": "TEST", "timestamp": ts, "open": close, "high": close * 1.01,
                       "low": close * 0.99, "close": close, "volume": 1e6})
    neutral = {cols["ema_fast"]: close * 0.99, cols["ema_mid"]: close * 0.98, cols["ema_slow"]: close * 0.95,
               cols["rsi"]: 50.0, cols["macd"]: 0.1, cols["macd_signal"]: 0.1, cols["macd_hist"]: -0.1,
               cols["atr"]: 2.0, cols["bb_lower"]: close * 0.95, cols["bb_middle"]: close,
               cols["bb_upper"]: close * 1.05, cols["obv"]: 1e7, cols["obv_avg"]: 1e7}
    for k, v in neutral.items():
        df[k] = v
    for k, v in last.items():
        vals = v if isinstance(v, list) else [v]
        df.loc[df.index[-len(vals):], k] = vals
    return df


class StubProvider(market_data.SyntheticMarketData):
    """Simulated provider whose latest prices are set by the test."""

    def __init__(self, prices: dict, market_open: bool = True):
        super().__init__(now=NOW, market_open=market_open)
        self.fixed = dict(prices)

    def latest_prices(self, symbols):
        from aaba.market_data import LatestPrice
        return {s: LatestPrice(s, self.fixed[s], NOW) for s in symbols if s in self.fixed}


def make_run(mode: str = "paper", market_open: bool = True, fresh: bool = True, prices: dict | None = None,
             accounts_ok: bool = True) -> str:
    """Create a run and a matching in-memory run state without fetching any data.
    Also installs a StubProvider with the same prices, so later re-checks see them too."""
    from aaba.market_data import LatestPrice, MarketClock
    run_id = db.create_run(mode, "test", "test", None, None, True)
    cfg = load_strategy()
    now_ny = clock.to_ny(NOW)
    mclock = MarketClock(now_ny, market_open, now_ny + timedelta(days=1), now_ny + timedelta(hours=5))
    prov = StubProvider(prices or {}, market_open)
    market_data.set_provider(prov)
    accounts = brokers.verify_accounts(run_id) if accounts_ok else {
        s.student_id: {"ok": False, "orders_allowed": False, "account_number": None, "problems": ["test"],
                       "warnings": []} for s in load_students().students}
    lp = {s: LatestPrice(s, p, NOW) for s, p in (prices or {}).items()}
    controller._RUNS[run_id] = controller.RunState(run_id, mode, cfg, mclock, [], {}, fresh, "2026-10-12", lp,
                                                   {}, {}, accounts, {}, prov)
    return run_id


def add_signal(run_id: str, student_id: str, symbol: str, action: str, price: float = 100.0, atr: float = 2.0,
               category: str | None = None) -> dict:
    st = load_students().get(student_id)
    cat = category or st.category(symbol)
    sid = db.new_id("sig")
    import json
    db.ex("INSERT INTO signals(signal_id, run_id, student_id, symbol, category, action, score, reasons, indicators,"
          " price, bar_date, data_quality, news, held_qty, created_at, outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (sid, run_id, student_id, symbol, cat, action, 0.7, json.dumps([f"test {action}"]),
           json.dumps({"ATR14": atr, "close": price}), price, "2026-10-12", "ok", None, 0, clock.iso(),
           "HOLD_RECORDED" if action == "HOLD" else None))
    return db.q1("SELECT * FROM signals WHERE signal_id=?", (sid,))


def give_position(student_id: str, symbol: str, qty: float, avg: float, last: float | None = None):
    b = brokers.get_broker(load_students().get(student_id))
    st = b._load()
    st["positions"][symbol] = {"qty": qty, "avg_entry": avg, "last_price": last or avg}
    st["cash"] -= qty * avg
    b._save(st)
    return b
