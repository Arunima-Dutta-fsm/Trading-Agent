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

## Intraday decisions (optional, `intraday.enabled`)

When `intraday.enabled` is true in `configs/strategy.json`, every stock is re-decided every
`cycle_minutes` (default 10) from `first_cycle` 09:50 to `last_cycle` 15:40 New York time, using
**completed** 5-minute candles (Alpaca IEX feed, real time) plus the completed daily bars. The code
is `aaba/intraday.py`; the live cycle and the intraday backtest call the same `decide()`.

| Evidence | How it is used |
|---|---|
| Daily trend: close vs EMA200, EMA50 rising over 5 sessions, EMA20 vs EMA50 | "healthy trend" is required for dip entries and adds |
| Daily rules engine (the sections above) | a daily BUY can be entered when the intraday trend confirms it; a daily SELL raises a SELL request |
| VWAP, intraday EMA9/EMA21 and slope, intraday RSI14 | momentum confirmation; intraday downtrend detection |
| Relative volume, bullish candle, RSI recovering from <= 45 | a "bounce" that confirms a dip is being bought |
| Support (prior-day low, 30-min opening range low, daily EMA20/EMA50, lower band) | stop placement for dip entries |
| Resistance (prior-day high, session high, upper band) | target for reward-to-risk |
| SPY change vs its prior close and VWAP | weak market blocks new entries |

**No buying into sustained weakness.** A BUY is refused when price is below VWAP with EMA9 < EMA21
and EMA21 falling, when 4+ of the last 6 candles made lower lows, when price is 0.25 x ATR below the
prior day's low or 0.5 x ATR below the daily EMA50, when SPY is down `market_weak_drop_pct` and
below its VWAP, or when news/earnings veto it.

**Scaling in.** The one-BUY-per-day rule is replaced by: the first entry uses at most
`initial_entry_fraction` (50%) of the stock's allocation; an add needs a confirmed bounce at least
`min_add_drop_atr` (0.25) x ATR below the last entry, at least `min_minutes_between_entries` (30)
after it, at most `max_entries_per_position` (3) entries, and the whole position's risk to the
**anchored stop of the first entry (never lowered)** within `max_position_risk_pct` (1.5%) of
capital. Allocation, 15% cap, cash, minimum order, daily loss halt and the per-run and per-day BUY
limits still apply. Every order has its own client order ID (`aaba-<signal id>-buy`).

**Reward-to-risk.** Every BUY needs (target - entry) / (entry - stop) >= `min_reward_risk` (1.5)
after `cost_bps` (5 bps) each way. Momentum entries stop just below VWAP / EMA21; dip entries just
below support; never closer than 0.5 x ATR or wider than 2 x ATR.

**Exits** are SELL approval requests (the student's PIN is still required): price below the anchored
stop, a daily SELL rule, an intraday trend break for satellites, or price above the upper band with
intraday RSI >= `profit_take_rsi`. While a request is waiting, no new request and no new BUY are
made for that stock.

**LLM use.** In intraday cycles the agent is called only when a cycle has a BUY or SELL; quiet
cycles cost no LLM calls and write no report files.

## Engine v2: regime-scaled momentum portfolio (`intraday.engine: "v2"`)

Code: `aaba/portfolio.py`; settings: the `portfolio` section of `configs/strategy.json`.

1. **Market regime** (once per run, completed daily bars): bull = SPY above EMA50 and EMA200 and at
   least 50% of our 36 stocks above their EMA50; bear = SPY below EMA200 and under 40% above EMA50;
   otherwise neutral. Invested share of each student's capital: bull 95%, neutral 60%, bear 25%.
2. **Targets per student**: eligible = uptrend (close > EMA50 and EMA200, EMA20 > EMA50) with positive
   blended momentum (0.5 x 20-session + 0.3 x 60-session + 0.2 x 120-session return) and no strong
   negative news. The best 9 by risk-adjusted momentum (momentum / ATR%) are held. The proposal's
   40% satellites / 60% core split is kept; inside each, weights are inverse to volatility and tilted
   by news (x (1 + 0.3 x sentiment)), each capped at 15%.
3. **Every 10 minutes**: BUY toward the target when 25%+ below it (the whole gap at or below VWAP,
   half above), unless the stock is falling hard intraday, the market is weak, or price is stretched
   (> 0.5 x ATR above VWAP or intraday RSI >= 80).
4. **Exits (SELL approval requests)**: daily close below the chandelier stop (highest close since
   entry - 3 x ATR), below EMA50 - 0.5 x ATR, momentum turned negative or news veto, or the intraday
   emergency stop (entry - 3.5 x ATR). A lower rank alone never sells.
5. **Risk limits kept**: 15% per stock, no margin, minimum order, 5% daily loss halt, BUYs per run and
   per day, position risk to its stop <= 2% of capital, SELLs only with the student's PIN.

## News sources

Alpaca news (Benzinga, real time, free with the paper keys) is read for all 36 stocks in ONE request
(up to 500 articles, pages followed). Scoring: word list plus analyst/earnings phrases (x3), weighted by
recency (half per day). Stocktwits' API answered 403 (partner access needed) and NewsAPI's free plan
delays articles 24 hours and is for development only, so neither is used for live decisions.

## Trading hours and Alpaca's extended-hours rules

The agent trades the **regular session only** (09:30-16:00 ET, risk check "market open"). Alpaca also
offers pre-market (04:00-09:30), after-hours (16:00-20:00) and overnight (20:00-04:00, Sun-Fri) trading,
but there: only LIMIT orders with `extended_hours=true` and time-in-force `day`/`gtc` are accepted;
market, stop, bracket orders are rejected and trailing stops do not trigger; overnight needs an
`overnight_tradable` asset and a separate data feed and does not run before market holidays; liquidity
is thin and spreads wide. The agent's orders are market orders, so it does not trade off-hours.