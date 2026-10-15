# Strategy, scoring and risk rules

All numbers below are the defaults in `configs/strategy.json`. They can be changed on the Strategy &
Config page (validated, admin PIN, dated backup kept) or in the file. The code is in `aaba/strategy.py`
and `aaba/risk.py`.

## Data quality first

A stock gets HOLD, never BUY or SELL, when:

- it has fewer than 200 clean daily bars (EMA200 needs them), or any indicator on the latest bar is empty;
- its latest bar is not the last completed trading session;
- the account's positions could not be read in this run.

Bars with missing prices, zero or negative prices, high below low, or negative volume are removed
before indicators are calculated, and the count is logged.

## Growth Satellites: trend-following momentum

BUY when all five are true on the latest completed bar:

| Check | Default |
|---|---|
| Close above EMA200 | regime filter |
| EMA20 above EMA50 | trend |
| MACD histogram turned from <= 0 to > 0 within the last N bars, and is still positive | N = 3 |
| RSI14 between the two limits | 50 to 70 |
| OBV above its 20-day average | can be switched off |

SELL, only if the stock is held, when any is true: close below EMA20; close more than 3 x ATR14 below
the highest close since the position was opened (trailing stop); close more than 2 x ATR14 below the
average entry price (hard stop).

The proposal's rule says the MACD histogram "crosses above zero". A cross on exactly the decision
day is rare, and with only 10 to 20 sessions that would mean very few trades. The 3-bar window is our
change; set `macd_cross_lookback` to 1 for the strict version.

## Diversified Core: mean reversion inside an uptrend

BUY when all four are true:

| Check | Default |
|---|---|
| Close above EMA200 | regime filter |
| RSI14 below 35, or close below the lower Bollinger Band (20, 2), on any of the last N bars | N = 3 |
| Today's close back inside the band (above the lower band) | |
| RSI rising (today above yesterday) | |

SELL, only if held, when any is true: RSI14 above 65; close at or above the upper band; the 2 x ATR
hard stop.

## Conflicts and filters

- Held stock meets BUY and SELL conditions: SELL (protect capital first).
- Stock not held meets SELL conditions: HOLD. There is no short selling.
- Position already at its allocation: a BUY setup becomes HOLD.
- News: average word-list sentiment of Alpaca news from the last 48 hours. At or below -0.4 (with at
  least 1 article), a BUY becomes HOLD. News never creates a BUY or SELL.
- Earnings: with a Finnhub key, a BUY becomes HOLD when results are due within 2 days. Without the
  key the date is "unknown" and does not block (change `block_if_unknown` to block).
- The LLM may turn a BUY into a HOLD with a written reason. It cannot create a BUY, cancel a SELL, or
  change a quantity.

## Composite score (our own indicator)

Every signal also gets a score from 0 to 1, shown in reports and the dashboard:

| Part | Weight | How it is scored |
|---|---|---|
| Trend | 0.30 | share of: close > EMA200, EMA20 > EMA50, close > EMA50 |
| Momentum | 0.30 | average of: MACD histogram > 0; RSI scaled 30 -> 0, 70 -> 1 (0.5 if above 75); RSI rising |
| Volume | 0.20 | 1 if OBV above its average, else 0 |
| News | 0.20 | (sentiment + 1) / 2, or 0.5 with no news |

With `"signal_mode": "composite"` the score drives decisions instead: BUY at 0.65 or more (close must
still be above EMA200), SELL a held stock at 0.40 or less, stops still apply. Weights must add up to 1.

## Position sizing and BUY limits

- Allocation per stock: $10,000 for a satellite, $7,500 for a core stock (the proposal's split).
- No position above 15% of the student's $100,000 ($15,000).
- Risk per trade 1% of capital: shares = $1,000 / (2 x ATR14). The order is the smaller of this and
  what the allocation room, the 15% cap, the remaining $100,000 budget and cash allow. Whole shares.
- Minimum order $200. At most 6 new BUYs per student per run. One BUY per stock per day.
- No new BUYs for a student who is down 5% or more on the day.
- Market must be open; latest price under 15 minutes old.

## Why some of these numbers

The allocation, 15% cap, ATR stops and indicator set come straight from the proposal. The 1% risk
rule is from the proposal's backtest section ("risk about 1% of capital per trade"). A worked example:
NVDA closed at $229.28 on 9 Oct. If its ATR14 were $8 (illustration only), 1% risk would allow 62
shares and the $10,000 allocation 43, so the allocation would set the size. For a very volatile name
the ATR rule sets it instead.
The run limit of 6 BUYs and the 5% daily halt are our own guard rails for an unattended system.
