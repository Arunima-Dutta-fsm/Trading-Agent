# Audit of the inputs, and what was built from them

## What was available

| Input | Status |
|---|---|
| Course brief PDF "Trade Algorithmically using Trading Agents" | Read in full |
| Submitted portfolio selection (Akshita, Arunima, Navya) | Read; stock lists copied exactly |
| Longer portfolio proposal (Sections 1-12, signal map, Alpaca checklist) | Read; used as the strategy spec |
| Architecture and workflow diagrams (3 images) | Followed box by box, see ARCHITECTURE.md |
| Engineering prompt (requirements for this build) | Followed |
| Old source files, CSVs, `students.json`, old MCP server, tests | Not provided. Searched past chats; not found |

Because the old code was not available, nothing from it could be inspected, reused or repaired. The
project was written new, keeping the old file and column names where the prompt described them, so
the history still makes sense.

## Old names and where that work lives now

| Old file (from the prompt) | Now |
|---|---|
| `main.py` | `main.py` (command line for every task) |
| `config.py`, `configs/students.json` | `aaba/settings.py` (validated), `configs/students.json`, `configs/strategy.json` |
| `fetch_data.py` | `aaba/market_data.py` (batch request, freshness, fallback feed) |
| `indicators.py` | `aaba/indicators.py` (same CSV column names: EMA20 ... OBV_AVG20) |
| `strategies/student_2.py` | `aaba/strategy.py`, one engine for all three students, settings per student module |
| `backtest.py`, `validate_backtest.py`, `diagnose_results.py` | `aaba/backtest.py` (walk-forward, benchmarks) |
| `paper_trade.py`, `risk_manager.py`, `check_positions.py` | `aaba/controller.py`, `aaba/risk.py`, `aaba/broker.py` |
| `agents/approval_manager.py`, `review_approvals.py`, `execute_approved_sell.py` | `aaba/approvals.py`, `controller.execute_approved_sell`, Approval Centre |
| `agents/trading_crew.py` (script runner for Student 2 only) | `agents/trading_crew.py`, now a real CrewAI agent using MCP tools, all three students |
| `mcp_server/technical_indicators_server.py` (one tool) | same path, 14 tools; `get_latest_indicators(student_id, symbol)` kept |
| planned MCP client test | `mcp_server/mcp_client_test.py` |
| `test_approval_flow.py`, `test_sell_approval.py` | `tests/test_orders_and_approvals.py` |
| `logs/` | `logs/`, plus SQLite `events` table |

## Choices made where the brief is silent

| Topic | Choice | Why |
|---|---|---|
| Indicator library | Own pandas code instead of pandas_ta | Every formula is visible and tested; avoids pandas_ta install problems with newer numpy/pandas |
| MCP client | `crewai_tools.MCPServerAdapter` (stdio) | The supported CrewAI route for local MCP servers; one server process per run |
| LLM provider | Groq through CrewAI's OpenAI-compatible route | No extra LiteLLM install needed; works the same for other OpenAI-compatible hosts |
| Agent count | One agent, one LLM | Required by the group's design and the prompt |
| Stops | SELL signals that need approval, not automatic stop orders | An automatic stop order would sell without the student's approval |
| Approval access control | Personal PIN per student, checked on the server, with lockout | No login system in a classroom Streamlit app; limitation stated in README |
| Report file names | `DDMMYYYY-HH-MM` | Windows forbids `:` in file names |
| Earnings data | Optional Finnhub key | Alpaca has no earnings calendar |
| News | Alpaca news (same keys), word-list sentiment | Free, one request for all stocks, easy to explain |

## Known limits

- No LLM provider and no Alpaca endpoint was reachable from the build environment. The agent loop,
  MCP server and all order logic were tested with simulated prices, a fake broker and a scripted
  stand-in model. The first real run on the laptop is the first test against the real services.
- The PowerShell scripts were written for Windows but could not be run here (no Windows machine).
- Backtests here ran only on simulated prices, so no performance numbers are reported in these docs.
- Ten sessions fit before 27 Oct; see TIMELINE.md.
