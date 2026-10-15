# Arunima Dutta (065014) - S2 - trading run

> **SIMULATED RUN**: made-up prices and/or a fake broker. Not real market data, not real orders.

| Item | Value |
|---|---|
| Run ID | `run-20261013-175810-8c82` |
| Started | 13 Oct 2026 23:28 IST / 13 Oct 2026 13:58 EDT |
| Mode | **paper** |
| Engine | crewai+mcp |
| LLM provider / model | scripted / scripted-test (tests only) |
| Data source | synthetic / feed synthetic, 1 batch request(s) for bars |
| Latest bar / expected | 2026-09-29 / 2026-09-29 |
| Data fresh | yes |

## Account
- Paper account: `FAKE-S2` - verified

## Signals (12 stocks)

| Symbol | Type | Signal | Close | Score | Held | Outcome |
|---|---|---|---|---|---|---|
| AMD | satellite | **BUY** | 199.56 | 0.896 | 0 | BUY_SUBMITTED |
| AMZN | core | **BUY** | 1824.17 | 0.315 | 0 | BUY_SUBMITTED |
| GOOGL | core | **HOLD** | 1044.18 | 0.455 | 0 | HOLD_RECORDED |
| GS | core | **HOLD** | 1254.77 | 0.547 | 0 | HOLD_RECORDED |
| JNJ | core | **HOLD** | 247.33 | 0.242 | 0 | HOLD_RECORDED |
| KO | core | **HOLD** | 260.63 | 0.234 | 0 | HOLD_RECORDED |
| NEE | core | **HOLD** | 421.04 | 0.342 | 0 | HOLD_RECORDED |
| NEM | core | **HOLD** | 102.13 | 0.555 | 0 | HOLD_RECORDED |
| NET | satellite | **HOLD** | 1039.13 | 0.418 | 0 | HOLD_RECORDED |
| SHOP | satellite | **HOLD** | 392.46 | 0.328 | 0 | HOLD_RECORDED |
| TSLA | satellite | **BUY** | 2157.86 | 0.882 | 0 | BUY_SUBMITTED |
| UNP | core | **HOLD** | 64.62 | 0.112 | 0 | HOLD_RECORDED |

## Why (from the rules engine)

- **AMD BUY**: momentum entry: close > EMA200; EMA20 > EMA50; MACD histogram turned positive in last 3 bars; RSI in 50-70; OBV above its average
  - Outcome detail: sent; broker status 'filled' (a fill is confirmed only when status becomes 'filled'); qty 51 @ ~195.43099270928616; PASS signal is BUY (signal is BUY); PASS account verified for this student (paper account verified); PASS symbol belongs to this student (AMD in S2 universe); PASS asset active and tradable (status=active, tradable=True); PASS indicator data valid (data quality: ok); PASS daily bars up to date (latest completed session present); PASS market open (regular session open); PASS latest price fresh (price 195.43099270928616 age 0.0 min); PASS no open order for symbol (0 open order(s)); PASS not already bought today (first BUY today); PASS new-buy limit per run (0 of 6 used); PASS daily loss halt (today +0.00% (halt at -5.0%)); PASS position within allocation and 15% cap (current $0, allocation $10,000, cap $15,000); PASS within student capital budget (used $0 of $100,000); PASS e
- **AMZN BUY**: mean-reversion entry: close > EMA200; RSI < 35 or close below lower band in last 3 bars; close back inside the band; RSI turning up
  - Outcome detail: sent; broker status 'filled' (a fill is confirmed only when status becomes 'filled'); qty 4 @ ~1692.5224469439918; PASS signal is BUY (signal is BUY); PASS account verified for this student (paper account verified); PASS symbol belongs to this student (AMZN in S2 universe); PASS asset active and tradable (status=active, tradable=True); PASS indicator data valid (data quality: ok); PASS daily bars up to date (latest completed session present); PASS market open (regular session open); PASS latest price fresh (price 1692.5224469439918 age 0.0 min); PASS no open order for symbol (0 open order(s)); PASS not already bought today (first BUY today); PASS new-buy limit per run (1 of 6 used); PASS daily loss halt (today +0.00% (halt at -5.0%)); PASS position within allocation and 15% cap (current $0, allocation $7,500, cap $15,000); PASS within student capital budget (used $9,967 of $100,000); PAS
- **GOOGL HOLD**: no entry: not (close > EMA200); not (RSI < 35 or close below lower band in last 3 bars); not (RSI turning up)
- **GS HOLD**: no entry: not (close > EMA200); not (RSI < 35 or close below lower band in last 3 bars)
- **JNJ HOLD**: no entry: not (close > EMA200)
- **KO HOLD**: no entry: not (close > EMA200); not (RSI < 35 or close below lower band in last 3 bars)
- **NEE HOLD**: no entry: not (RSI < 35 or close below lower band in last 3 bars); not (RSI turning up)
- **NEM HOLD**: no entry: not (close > EMA200); not (RSI < 35 or close below lower band in last 3 bars)
- **NET HOLD**: no entry: not (close > EMA200); not (EMA20 > EMA50); not (MACD histogram turned positive in last 3 bars); not (RSI in 50-70)
- **SHOP HOLD**: no entry: not (MACD histogram turned positive in last 3 bars); not (RSI in 50-70); not (OBV above its average)
- **TSLA BUY**: momentum entry: close > EMA200; EMA20 > EMA50; MACD histogram turned positive in last 3 bars; RSI in 50-70; OBV above its average
  - Outcome detail: sent; broker status 'filled' (a fill is confirmed only when status becomes 'filled'); qty 4 @ ~2129.0121446469834; PASS signal is BUY (signal is BUY); PASS account verified for this student (paper account verified); PASS symbol belongs to this student (TSLA in S2 universe); PASS asset active and tradable (status=active, tradable=True); PASS indicator data valid (data quality: ok); PASS daily bars up to date (latest completed session present); PASS market open (regular session open); PASS latest price fresh (price 2129.0121446469834 age 0.0 min); PASS no open order for symbol (0 open order(s)); PASS not already bought today (first BUY today); PASS new-buy limit per run (2 of 6 used); PASS daily loss halt (today +0.00% (halt at -5.0%)); PASS position within allocation and 15% cap (current $0, allocation $10,000, cap $15,000); PASS within student capital budget (used $16,737 of $100,000); P
- **UNP HOLD**: no entry: not (close > EMA200); not (close back inside the band); not (RSI turning up)

## Indicator values used

- AMD: close=199.56, bar_date=2026-09-29, EMA20=179.92, EMA50=169.9, EMA200=174.19, RSI14=68.6, RSI_prev=62.3, MACD_HIST=0.566, ATR14=7.4, BB_LOWER=166.84, BB_UPPER=196.28, OBV_above_avg=True, held_qty=0.0, avg_entry=None, account_ok=True
  - entry checks: close > EMA200: yes, EMA20 > EMA50: yes, MACD histogram turned positive in last 3 bars: yes, RSI in 50-70: yes, OBV above its average: yes
  - news: no news (simulated mode) score=None articles=0; earnings: unknown (no FINNHUB_API_KEY)
- AMZN: close=1824.17, bar_date=2026-09-29, EMA20=1976.56, EMA50=2036.04, EMA200=1649.66, RSI14=35.9, RSI_prev=33.7, MACD_HIST=-23.583, ATR14=85.73, BB_LOWER=1633.48, BB_UPPER=2427.57, OBV_above_avg=False, held_qty=0.0, avg_entry=None, account_ok=True
  - entry checks: close > EMA200: yes, RSI < 35 or close below lower band in last 3 bars: yes, close back inside the band: yes, RSI turning up: yes
  - news: no news (simulated mode) score=None articles=0; earnings: unknown (no FINNHUB_API_KEY)
- GOOGL: close=1044.18, bar_date=2026-09-29, EMA20=1033.37, EMA50=1024.53, EMA200=1050.38, RSI14=52.0, RSI_prev=60.8, MACD_HIST=5.559, ATR14=48.24, BB_LOWER=952.89, BB_UPPER=1096.06, OBV_above_avg=False, held_qty=0.0, avg_entry=None, account_ok=True
  - entry checks: close > EMA200: no, RSI < 35 or close below lower band in last 3 bars: no, close back inside the band: yes, RSI turning up: no
  - news: no news (simulated mode) score=None articles=0; earnings: unknown (no FINNHUB_API_KEY)
- GS: close=1254.77, bar_date=2026-09-29, EMA20=1261.04, EMA50=1272.65, EMA200=1266.03, RSI14=48.7, RSI_prev=48.7, MACD_HIST=1.742, ATR14=47.77, BB_LOWER=1167.66, BB_UPPER=1334.23, OBV_above_avg=True, held_qty=0.0, avg_entry=None, account_ok=True
  - entry checks: close > EMA200: no, RSI < 35 or close below lower band in last 3 bars: no, close back inside the band: yes, RSI turning up: yes
  - news: no news (simulated mode) score=None articles=0; earnings: unknown (no FINNHUB_API_KEY)
- JNJ: close=247.33, bar_date=2026-09-29, EMA20=247.4, EMA50=254.42, EMA200=252.08, RSI14=46.8, RSI_prev=38.6, MACD_HIST=-0.056, ATR14=8.44, BB_LOWER=226.78, BB_UPPER=272.84, OBV_above_avg=False, held_qty=0.0, avg_entry=None, account_ok=True
  - entry checks: close > EMA200: no, RSI < 35 or close below lower band in last 3 bars: yes, close back inside the band: yes, RSI turning up: yes
  - news: no news (simulated mode) score=None articles=0; earnings: unknown (no FINNHUB_API_KEY)
- KO: close=260.63, bar_date=2026-09-29, EMA20=263.16, EMA50=269.12, EMA200=278.46, RSI14=43.8, RSI_prev=39.2, MACD_HIST=-0.153, ATR14=5.57, BB_LOWER=251.97, BB_UPPER=275.24, OBV_above_avg=False, held_qty=0.0, avg_entry=None, account_ok=True
  - entry checks: close > EMA200: no, RSI < 35 or close below lower band in last 3 bars: no, close back inside the band: yes, RSI turning up: yes
  - news: no news (simulated mode) score=None articles=0; earnings: unknown (no FINNHUB_API_KEY)
- NEE: close=421.04, bar_date=2026-09-29, EMA20=429.51, EMA50=423.46, EMA200=416.17, RSI14=46.6, RSI_prev=49.9, MACD_HIST=-1.585, ATR14=12.87, BB_LOWER=403.98, BB_UPPER=453.4, OBV_above_avg=False, held_qty=0.0, avg_entry=None, account_ok=True
  - entry checks: close > EMA200: yes, RSI < 35 or close below lower band in last 3 bars: no, close back inside the band: yes, RSI turning up: no
  - news: no news (simulated mode) score=None articles=0; earnings: unknown (no FINNHUB_API_KEY)
- NEM: close=102.13, bar_date=2026-09-29, EMA20=99.76, EMA50=109.48, EMA200=145.22, RSI14=51.9, RSI_prev=49.6, MACD_HIST=1.405, ATR14=4.14, BB_LOWER=93.4, BB_UPPER=103.35, OBV_above_avg=True, held_qty=0.0, avg_entry=None, account_ok=True
  - entry checks: close > EMA200: no, RSI < 35 or close below lower band in last 3 bars: no, close back inside the band: yes, RSI turning up: yes
  - news: no news (simulated mode) score=None articles=0; earnings: unknown (no FINNHUB_API_KEY)
- NET: close=1039.13, bar_date=2026-09-29, EMA20=1107.76, EMA50=1179.9, EMA200=1427.25, RSI14=37.4, RSI_prev=41.7, MACD_HIST=3.014, ATR14=49.27, BB_LOWER=1032.22, BB_UPPER=1138.88, OBV_above_avg=True, held_qty=0.0, avg_entry=None, account_ok=True
  - entry checks: close > EMA200: no, EMA20 > EMA50: no, MACD histogram turned positive in last 3 bars: no, RSI in 50-70: no, OBV above its average: yes
  - news: no news (simulated mode) score=None articles=0; earnings: unknown (no FINNHUB_API_KEY)
- SHOP: close=392.46, bar_date=2026-09-29, EMA20=411.96, EMA50=405.31, EMA200=392.37, RSI14=41.1, RSI_prev=45.3, MACD_HIST=-5.251, ATR14=14.46, BB_LOWER=384.49, BB_UPPER=456.97, OBV_above_avg=False, held_qty=0.0, avg_entry=None, account_ok=True
  - entry checks: close > EMA200: yes, EMA20 > EMA50: yes, MACD histogram turned positive in last 3 bars: no, RSI in 50-70: no, OBV above its average: no
  - news: no news (simulated mode) score=None articles=0; earnings: unknown (no FINNHUB_API_KEY)
- TSLA: close=2157.86, bar_date=2026-09-29, EMA20=2013.33, EMA50=1962.31, EMA200=1842.81, RSI14=62.7, RSI_prev=60.7, MACD_HIST=1.24, ATR14=78.06, BB_LOWER=1799.46, BB_UPPER=2332.95, OBV_above_avg=True, held_qty=0.0, avg_entry=None, account_ok=True
  - entry checks: close > EMA200: yes, EMA20 > EMA50: yes, MACD histogram turned positive in last 3 bars: yes, RSI in 50-70: yes, OBV above its average: yes
  - news: no news (simulated mode) score=None articles=0; earnings: unknown (no FINNHUB_API_KEY)
- UNP: close=64.62, bar_date=2026-09-29, EMA20=67.15, EMA50=67.58, EMA200=71.28, RSI14=34.8, RSI_prev=43.0, MACD_HIST=-0.211, ATR14=1.59, BB_LOWER=65.03, BB_UPPER=68.63, OBV_above_avg=False, held_qty=0.0, avg_entry=None, account_ok=True
  - entry checks: close > EMA200: no, RSI < 35 or close below lower band in last 3 bars: yes, close back inside the band: no, RSI turning up: no
  - news: no news (simulated mode) score=None articles=0; earnings: unknown (no FINNHUB_API_KEY)

## Orders from this run

| Time (UTC) | Side | Symbol | Qty | Status | Client order ID |
|---|---|---|---|---|---|
| 2026-10-13T17:58:15+00:00 | buy | TSLA | 4 | filled | `aaba-S2-TSLA-20260930-buy` |
| 2026-10-13T17:58:15+00:00 | buy | AMZN | 4 | filled | `aaba-S2-AMZN-20260930-buy` |
| 2026-10-13T17:58:15+00:00 | buy | AMD | 51 | filled | `aaba-S2-AMD-20260930-buy` |

## SELL approvals

None.

## Portfolio after the run

- Equity $100,000.00, cash $74,746.88, invested $25,253.12
- Unrealized P&L $0.00; realized P&L $0.00
- Return since tracking began: +0.00%
- SPY over the same period: +0.00%

## Errors and warnings

None.

## Agent explanation (LLM output, checked against the tables above)

| Symbol | Signal | Result |
|---|---|---|
(scripted test summary for S2)
