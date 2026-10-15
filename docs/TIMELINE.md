# Timeline and trading-day count

Written 13 Oct 2026 (Tuesday). Intended last trading day: Tue 27 Oct 2026.

## The arithmetic

NYSE has no holidays between 14 Oct and 10 Nov 2026 (Columbus Day and Election Day are normal
trading days). Counting from Wed 14 Oct:

| Paper-trading sessions | Last session |
|---|---|
| 10 | Tue 27 Oct |
| 15 | Tue 3 Nov |
| 20 | Tue 10 Nov |

So ending on 27 Oct gives at most 10 sessions. The brief asks for 15 to 20. That gap cannot be closed
by code. The group has three honest options:

1. Keep 27 Oct, trade 10 sessions, and state the shortfall in the report.
2. Ask the faculty whether trading can run to 3 Nov (15 sessions).
3. Trade to 3 Nov or 10 Nov and submit then, if the deadline allows.

The dashboard's Overview page counts the New York trading days on which a paper-mode run actually
completed (simulated and analysis runs do not count). Use that number in the report, not a planned one.

## Suggested plan

| When | What |
|---|---|
| Tue 13 Oct | Clone or copy the project to the new laptop, run `scripts\setup.ps1`, fill in `.env` |
| Tue 13 Oct night / Wed 14 Oct day (IST) | `python main.py check`, `python main.py validate`, `python main.py backtest`, a few `--mode analysis` runs |
| Wed 14 Oct 19:15 IST (09:45 New York) | One `dry_run` during market hours; read the reports; then `ENABLE_PAPER_ORDERS=true` and `--mode paper` |
| From Thu 15 Oct | Scheduler running in `paper` mode every weekday; students check the Approval Centre each evening |
| Sun 1 Nov | US clocks go back. The scheduler uses New York time, so the run moves to 20:15 IST by itself |
| Last day | Final reconcile (`python main.py reconcile`), export History & Reports, write up |

If setup slips past Wednesday evening, the first paper session becomes Thu 15 Oct and the count to
27 Oct drops to 9.

## What the laptop needs

The scheduler only runs while the laptop is on, awake and online with the scheduler window open.
Missing a day means one fewer session. Set Windows sleep to "Never" while plugged in during the
trading period. In India the US session is 19:00 to 01:30 IST (until 31 Oct) and 20:00 to 02:30 IST
(from 2 Nov). The default single run at 09:45 New York sits early in that window, so students can
approve SELLs the same evening.
