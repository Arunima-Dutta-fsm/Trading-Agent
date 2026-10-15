"""END-TO-END test against a REAL Alpaca PAPER account. Skipped unless you ask for it.

    $env:AABA_E2E = "1"                 # read-only checks (account, paper endpoint, batch bars)
    $env:AABA_E2E_SUBMIT = "1"          # ALSO place and cancel one far-below-market limit order
    $env:AABA_E2E_STUDENT = "S2"        # which student's account (default S2)
    python -m pytest tests/test_e2e_paper.py -v -s

It reads keys straight from your .env file (the normal tests replace them with fakes).
The order test sends a 1-share LIMIT BUY at half the current price, which cannot fill,
then cancels it at once, so holdings do not change. It refuses to run if the client is
not on the paper endpoint.
"""
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(os.getenv("AABA_E2E") != "1", reason="set AABA_E2E=1 to run against a real paper account")


@pytest.fixture(scope="module")
def creds():
    vals = dotenv_values(ROOT / ".env")
    sid = os.getenv("AABA_E2E_STUDENT", "S2")
    prefix = {"S1": "STUDENT1", "S2": "STUDENT2", "S3": "STUDENT3"}[sid]
    key, sec = vals.get(f"{prefix}_ALPACA_API_KEY"), vals.get(f"{prefix}_ALPACA_SECRET_KEY")
    if not key or not sec:
        pytest.skip(f"{prefix} keys not in .env")
    return sid, key, sec


def test_paper_endpoint_and_account(creds):
    from alpaca.trading.client import TradingClient
    _, key, sec = creds
    c = TradingClient(key, sec, paper=True)
    base = getattr(c._base_url, "value", c._base_url)
    assert base == "https://paper-api.alpaca.markets"
    a = c.get_account()
    assert str(getattr(a.status, "value", a.status)).upper() == "ACTIVE"
    print("paper account", a.account_number, "cash", a.cash)


def test_batch_bars_for_the_students_12_symbols(creds):
    import json
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    sid, key, sec = creds
    students = json.loads((ROOT / "configs" / "students.json").read_text())["students"]
    st = next(s for s in students if s["student_id"] == sid)
    syms = st["satellites"] + st["core"]
    req = StockBarsRequest(symbol_or_symbols=syms, timeframe=TimeFrame.Day,
                           start=datetime.now(timezone.utc) - timedelta(days=30),
                           end=datetime.now(timezone.utc) - timedelta(minutes=16))
    df = StockHistoricalDataClient(key, sec).get_stock_bars(req).df.reset_index()
    assert set(df["symbol"]) == set(syms)


@pytest.mark.skipif(os.getenv("AABA_E2E_SUBMIT") != "1", reason="set AABA_E2E_SUBMIT=1 to place+cancel a test order")
def test_place_and_cancel_unfillable_limit_order(creds):
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest
    import json
    sid, key, sec = creds
    c = TradingClient(key, sec, paper=True)
    assert getattr(c._base_url, "value", c._base_url) == "https://paper-api.alpaca.markets"
    students = json.loads((ROOT / "configs" / "students.json").read_text())["students"]
    sym = next(s for s in students if s["student_id"] == sid)["core"][0]   # one of this student's own stocks
    px = StockHistoricalDataClient(key, sec).get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=sym, feed=DataFeed.IEX))[sym].price
    limit = round(float(px) * 0.5, 2)
    cid = f"aaba-e2e-{int(time.time())}"
    o = c.submit_order(LimitOrderRequest(symbol=sym, qty=1, side=OrderSide.BUY, limit_price=limit,
                                         time_in_force=TimeInForce.DAY, client_order_id=cid))
    try:
        got = c.get_order_by_client_id(cid)
        assert got.client_order_id == cid and float(got.filled_qty or 0) == 0
    finally:
        c.cancel_order_by_id(o.id)
    time.sleep(1)
    status = str(getattr(c.get_order_by_id(o.id).status, "value", ""))
    assert status in ("canceled", "pending_cancel")
