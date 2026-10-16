# AABA autonomous multi-portfolio trading agent

FORE School of Management, PGDM, AABA final project: "Trade Algorithmically using Trading Agents".
Group: Akshita Jain (065009), Arunima Dutta (065014), Navya Gupta (065015).

One CrewAI agent, running on one LLM, trades three separate $100,000 Alpaca paper accounts
(12 stocks each, 36 in total). It reaches data, accounts and orders only through a local
MCP server. Code decides the signals and enforces the risk rules; the LLM reads the results,
acts through the tools and explains what happened. BUYs are sent automatically when every
check passes. A SELL waits until the student who owns that portfolio approves it with their
own PIN. A HOLD never places an order.

```
scheduler / "Run now"
   -> CrewAI Portfolio Orchestrator (one agent, one LLM)
        -> local MCP server (stdio): market data, indicators, signals, risk checks, orders, approvals
             -> Alpaca PAPER account of Student 1 / 2 / 3
   -> safety net (code): any BUY/SELL the agent skipped is completed through the same MCP tools
   -> SQLite records + model-run/*.md reports -> Streamlit dashboard
```

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/STRATEGY.md](docs/STRATEGY.md),
[docs/TIMELINE.md](docs/TIMELINE.md), [docs/AUDIT.md](docs/AUDIT.md).

## What is in the folder

| Path | What it is |
|---|---|
| `main.py` | Command line entry point (init, check, validate, run, scheduler, approvals, backtest, dashboard) |
| `agents/trading_crew.py` | The CrewAI agent, its tasks, the MCP client (MCPServerAdapter) and the safety net |
| `mcp_server/technical_indicators_server.py` | The local MCP server (14 tools) |
| `mcp_server/mcp_client_test.py` | Stand-alone MCP client test |
| `aaba/` | The engine: config, database, market data, indicators, strategy, risk, broker, approvals, controller, reports, backtest, scheduler |
| `app/streamlit_app.py` | The web dashboard |
| `configs/students.json` | The three students and their 12 stocks (copied exactly from the proposal) |
| `configs/strategy.json` | Indicator settings, thresholds, risk limits, schedule |
| `tests/` | Automated tests (simulated data, fake broker, fake LLM server) and one opt-in real paper-account test |
| `scripts/*.ps1` | Windows PowerShell setup and run scripts |
| `model-run/` | One Markdown report per agent output, per run |

## 1. Install Python

Use Python 3.11 (3.12 also works). On Windows, either download 3.11 from
https://www.python.org/downloads/ and tick "Add python.exe to PATH", or run:

```powershell
winget install -e --id Python.Python.3.11
```

Also install Git for Windows (https://git-scm.com/download/win) if you will push to GitHub.

## 2. Create a fresh virtual environment

The setup script does steps 2 and 3 and creates `.env`. From the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

Or by hand:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
```

If PowerShell blocks `Activate.ps1`, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once,
or skip activation and call `.venv\Scripts\python.exe` directly (the scripts do this).

## 3. Install the packages

```powershell
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements.txt` pins the exact versions that were installed and tested together
(CrewAI 1.15.27, crewai-tools 1.15.27 with MCP, mcp 1.28.1, alpaca-py 0.44.0, Streamlit 1.65.0).
Nothing from the old laptop or its `.venv-crewai` folder is needed.

The rest of this README writes `python` for `.venv\Scripts\python.exe` (or `python` after activating).

## 4. Set up the three Alpaca paper accounts

Each student signs in to their own Alpaca account, opens Paper Trading, and:

1. Resets the paper account to $100,000 if it holds anything else (Account > Reset).
2. Creates API keys (paper keys usually start with `PK`).
3. Notes the paper account number shown on the dashboard (it starts with `PA`).

Put the keys in `.env` (copied from `.env.example`):

```
STUDENT1_ALPACA_API_KEY=...        # Akshita
STUDENT1_ALPACA_SECRET_KEY=...
STUDENT1_ALPACA_ACCOUNT_NUMBER=PA...
STUDENT2_...                       # Arunima
STUDENT3_...                       # Navya
STUDENT1_APPROVAL_PIN=...          # each student's private PIN, 6+ characters
DASHBOARD_ADMIN_PIN=...
ENABLE_PAPER_ORDERS=false
```

The program checks every account before trading. It always connects to
`https://paper-api.alpaca.markets` (there is no setting for live trading). Orders are blocked for a
student if their account cannot be read, does not match the account number you gave, is blocked, or is
the same account as another student's. If the group only has one Alpaca account, the check reports
it as shared and blocks orders instead of mixing three portfolios in one account.

Then check everything:

```powershell
python main.py check
python main.py validate     # tradable, enough history, 1-year beta and 60-day correlation vs SPY
```

`validate` writes a report to `data/validation/`. It never swaps a stock by itself. If a stock fails,
use a reserve from the proposal (MRVL, LRCX, ANET for satellites), edit `configs/students.json`, and add
a line to that student's `"replacements"` list (old, new, date, reason).

## 5. Set up the LLM

One model serves the whole system. Default: Groq, model `openai/gpt-oss-120b`.

```
LLM_PROVIDER=groq
LLM_MODEL=openai/gpt-oss-120b
GROQ_API_KEY=gsk_...
```

The workflow runs a fallback chain: Groq, then Gemini, then Ollama (`aaba/llm_fallback.py`).
`LLM_PROVIDER` (groq, gemini or ollama) picks which one is tried first. If a call fails (outage,
timeout, retired model), the same call goes to the next provider. Providers without settings are
skipped, and the run report lists them.

| Provider | Settings |
|---|---|
| Groq | `GROQ_API_KEY`, optional `GROQ_MODEL` (default `openai/gpt-oss-120b`) |
| Gemini | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), optional `GEMINI_MODEL` |
| Ollama | `OLLAMA_BASE_URL` (falls back to `LLM_BASE_URL`), optional `OLLAMA_MODEL`, `OLLAMA_API_KEY` |

`LLM_PROVIDER=openai_compatible` with `LLM_BASE_URL` skips the chain and uses that one endpoint.
Providers retire models, so if a run says the model is not found, check the provider's model list.

The agent has to call tools over many steps, so the model must support tool calling and be big
enough to follow instructions. Check any model before relying on it:

```powershell
python main.py llm-test
```

It asks the model three times to call a test tool and prints the result and the time each try took.

| Model | What to expect |
|---|---|
| Groq `openai/gpt-oss-120b` (free key from console.groq.com) | Recommended. Fast, calls tools well. |
| Ollama `qwen3:8b` or `llama3.1:8b` on your own PC | Works if the PC is fast enough. Raise `LLM_TIMEOUT` to 180-300 on CPU-only machines. |
| Ollama `llama3.2:1b`, `llama3.2:3b` and other 1B-3B models | Usually too small for multi-step tool calling. When they fail, the safety net ends up doing the agent's work. Run `llm-test` to see. |

For Ollama use `LLM_PROVIDER=openai_compatible`, `LLM_BASE_URL=http://localhost:11434/v1` (or the IP of
the machine running Ollama), `LLM_API_KEY=ollama`. An address like `172.x.x.x` usually belongs to WSL or
Hyper-V and can change after a restart; prefer `localhost` if Ollama runs on Windows itself.

Time limits: `LLM_TIMEOUT` (seconds per reply) and, in `configs/strategy.json`,
`agent.crew_time_budget_seconds` (default 900) for the whole agent phase. When the budget runs out,
the run moves on and the safety net completes any signal the agent has not handled. For a weak model
you can also set `agent.allow_agent_veto` to `false`, so the model cannot turn BUYs into HOLDs.

Do not move the safety net in front of the CrewAI crew to "make it faster". The agent acting on the
signals through MCP is the project requirement, and the tests check for it.

If the LLM fails or no key is set, the run still completes using the rules engine and the report
says so plainly.

## 6. Start and test the local MCP server and client

```powershell
python main.py mcp-test             # starts the server, lists the 14 tools, calls get_latest_indicators
python main.py mcp-test --prepare   # loads fresh data first, then calls the tool
```

Running `python mcp_server/technical_indicators_server.py` on its own just waits silently. That is
normal: it talks over stdin/stdout and waits for a client. The CrewAI run starts it for you.

## 7. Run CrewAI in analysis-only mode

```powershell
python main.py run --mode analysis
```

Analysis mode reads data and accounts, works out every signal and every risk check, and writes the
reports. It sends no orders and creates no approvals. Run it a few times and read `model-run/`.

Then try `--mode dry_run`: the full workflow including SELL approvals, but nothing is ever sent.

## 8. Start the dashboard

```powershell
python main.py dashboard            # http://localhost:8501
```

Pages: Overview (group status, data freshness, next run, capital, P&L, SPY comparison, Run now),
one page per student, Approval Centre, Strategy & Config, History & Reports, System checks.

To open it on a phone, install ngrok, then run
`powershell -File scripts\start_ngrok.ps1 -Password <a strong password>`. This adds a username and
password in front of the page. Do not share the ngrok link without that.

## 9. Start the scheduler

```powershell
python main.py scheduler
```

It runs the workflow at 09:45 New York time, Monday to Friday, skipping market holidays (it asks
Alpaca's calendar). In India that is 19:15 IST until 31 Oct and 20:15 IST from 2 Nov, after the US
clock change. Every 15 minutes during market hours it also sends SELLs that were approved since the
last run. The laptop must be on, awake and online, with this window open. The run mode comes from
`schedule.mode` in `configs/strategy.json` (default `analysis`).

## 10. Turn on automatic paper BUYs safely

Orders need two switches, so one mistake cannot start trading:

1. In `.env`: `ENABLE_PAPER_ORDERS=true`
2. Run mode `paper`: `python main.py run --mode paper`, or set `schedule.mode` to `paper` on the
   Strategy & Config page (admin PIN) and restart the scheduler.

Suggested order: `check` and `validate` pass, a few `analysis` runs look right, one `dry_run` looks
right, then switch on paper mode.

What happens to a BUY: the code re-checks that the account is verified and belongs to this
student, the stock is in this student's list and tradable, the daily bars are up to date, the market
is open, the latest price is less than 15 minutes old, there is no open order for that stock, no BUY
for it was already sent today, the run's limit of 6 new BUYs is not used up, the student is not down
5% today, the position stays within its allocation ($10,000 satellite / $7,500 core) and the 15% cap,
the student's $100,000 budget is not exceeded, and there is cash for it (no margin). The size is the
smaller of what the allocation allows and what risks 1% of capital with a 2 x ATR stop. If any check
fails, nothing is sent and the reason is saved.

## 11. Approve or reject SELL requests

A SELL signal never sells. It creates a request for the student who owns the stock. That student
opens the Approval Centre, picks their own student ID, enters their own PIN and clicks Approve or
Reject. From the command line:

```powershell
python main.py approvals
python main.py approve apv-1234abcd --student S2           # asks for the PIN
python main.py approve apv-1234abcd --student S2 --reject
```

The server refuses the decision if the student ID is not the owner, the PIN is wrong (5 wrong tries
lock that ID for 15 minutes), the request expired (24 hours), was already decided, or was replaced by
a newer SELL signal for the same stock. An approved SELL is checked again before sending: same
account, the holding still equals the approved quantity, no open order, market open, fresh price,
and the price within 5% of the signal price. If the market is closed it waits for the next check. If
anything else changed, the request is marked INVALIDATED and nothing is sent. Each request can
create at most one order (its client order ID is `aaba-<approval id>`).

Be aware of one trade-off: because SELLs wait for a person, the ATR stops are not automatic stop
orders. A stock can fall further while a request waits. Approve or reject promptly.

Access control is a PIN per student, checked on the server. It is not a full login system. It
suits a local classroom demo; keep the dashboard on localhost or behind ngrok's password.

## 12. Run the tests

Before switching on orders, run the full pre-deployment check:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\predeploy_check.ps1
powershell -ExecutionPolicy Bypass -File scripts\predeploy_check.ps1 -WithAnalysisRun   # also one full analysis run
```

It runs the tests, `check`, the MCP test with fresh data and `llm-test`, prints PASS/FAIL for each,
and exits with code 1 if anything failed. Only the tests:

```powershell
powershell -File scripts\run_tests.ps1      # or: python -m pytest -q tests
```

The tests use made-up prices and a fake broker, so they need no keys and send nothing. They cover:
the three portfolios, isolation between students, one batch request for bars, indicator edge cases,
the strategy rules, every BUY risk check, routing to the right account, SELL approval rules (wrong
student, wrong PIN, lockout, rejected, expired, re-check, no double send), HOLD, broker and credential
failures, restart safety, the MCP server and client, the CrewAI agent loop with a scripted model,
the safety net, a slow model hitting the time budget, a model that never calls tools, the real
OpenAI-compatible HTTP client (against a small local fake server), reports, secrets, the dashboard,
the scheduler, symbol validation and the backtest. The tests ignore your `.env`, so your real keys
and LLM settings never affect them.

A separate test checks a real paper account and is off by default:

```powershell
$env:AABA_E2E = "1"; python -m pytest tests/test_e2e_paper.py -v -s           # read-only
$env:AABA_E2E_SUBMIT = "1"; python -m pytest tests/test_e2e_paper.py -v -s    # also places and cancels an unfillable limit order
```

Backtest (about 2 minutes per student):

```powershell
python main.py backtest             # all students, or --student S2
```

It tunes a few settings on the first 18 months, tests on the last 6, and compares with buy-and-hold
of the same stocks and with SPY. Results go to `reports/backtests/`. It has no news filter and assumes
every SELL is approved, so it is a rough guide, not a promise.

## 13. Put it on GitHub from the new laptop

Create an empty repository on github.com (no README), then in the project folder:

```powershell
git init
git add .
git status                 # check that .env, data/ and .venv/ are NOT listed
git commit -m "AABA trading agent: CrewAI + local MCP + Streamlit"
git branch -M main
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

`.gitignore` keeps out `.env`, the database, `data/`, logs, `model-run/` and `reports/`. Reports
are excluded because they contain account numbers. If you want to submit them, remove the two
`model-run` lines from `.gitignore` and check the files first. Before every push, run
`git status` and make sure no key or PIN is in a tracked file.

To set up another laptop: `git clone <url>`, then steps 2 to 5.

## Intraday decisions (off by default)

`intraday.enabled: true` in `configs/strategy.json` makes the scheduler re-decide every stock every
10 minutes during the session (09:50-15:40 New York) from completed 5-minute candles, with
evidence-based dip buying, disciplined scaling in and an anchored stop. All rules are in
[docs/STRATEGY.md](docs/STRATEGY.md#intraday-decisions-optional-intradayenabled). Compare it with
the daily strategy first:

```powershell
python main.py backtest-intraday
```

## Modes at a glance

| Mode | Signals and checks | SELL approvals | Orders sent |
|---|---|---|---|
| `analysis` | yes | no | no |
| `dry_run` | yes | yes | no |
| `paper` with `ENABLE_PAPER_ORDERS=false` | yes | yes | no (recorded as blocked) |
| `paper` with `ENABLE_PAPER_ORDERS=true` | yes | yes | yes, paper only |

## Things to know

- The trading window: from 14 Oct to 27 Oct 2026 there are 10 trading sessions, short of the 15 to
  20 the brief asks for. See [docs/TIMELINE.md](docs/TIMELINE.md). The dashboard counts the real
  sessions with a completed paper run and shows that number as it is.
- Report file names use `DDMMYYYY-HH-MM` because Windows does not allow `:` in file names. The time
  is IST; the New York time is written inside each report.
- Free Alpaca data: daily bars come from the SIP feed ending 16 minutes ago; latest prices come from
  IEX. If SIP is refused, the run falls back to IEX and notes it.
- News sentiment is a simple word count over Alpaca news headlines. It can only block a BUY.
- Earnings dates need a free Finnhub key (`FINNHUB_API_KEY`). Without it the check says "unknown"
  and does not block.
- Paper fills are kinder than real ones. Results from 10 to 20 days cannot prove a strategy works.
