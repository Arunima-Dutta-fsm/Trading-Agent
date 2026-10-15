# Changes, 14 Oct 2026

Fixes after the first run on the group's Windows laptop (3 of the 73 tests that ran failed).

## Why the tests failed

| Failed test | Cause | Fix |
|---|---|---|
| `test_three_students_load_exactly_as_proposed` | NFLX was swapped for MRVL (logged in `replacements`, as the proposal allows). The test expected the original list. | The test now undoes logged replacements and compares with the proposal. Config loading now rejects a replacement that is not logged, not in the list, or (for satellites) not an approved reserve. |
| `test_crewai_agent_drives_the_workflow_through_mcp` | `agents/trading_crew.py` had been changed so code did all trading first and the LLM only wrote one summary. The agent no longer acted on any signal. | Restored the agent-driven workflow. The safety net runs after the crew again, as a backstop only. |
| `test_agent_veto_is_respected` | Same change: the agent never saw the BUY signals. | Same fix. |

The change to `trading_crew.py` was probably made because `llama3.2:1b` could not handle the
tool calls and was slow. That is a model problem, so the fix is on the model side (see README step 5),
plus the safeguards below.

## New safeguards for slow or weak models

- `agent.crew_time_budget_seconds` (default 900): the whole agent phase gets a time limit. When it runs
  out, the run moves on and the safety net completes the signals. CrewAI's own `max_execution_time`
  was not used because it waits for the stuck task to finish before giving up.
- `agent.allow_agent_veto` (default true): set to false so a weak model cannot turn BUYs into HOLDs.
- `LLM_TIMEOUT` and `LLM_MAX_RETRIES` in `.env` (defaults 120 s and 2) instead of fixed values.
- `python main.py llm-test`: checks that the configured model can call a tool, three times, with timings.
- `python main.py check` warns when the model name looks like a 1B-3B model.

## Other fixes

- Tests ignored `.env` only partly: CrewAI calls `load_dotenv()` when imported, which pulled the real
  LLM settings into the test run. The tests now clean the environment after that import and before
  every test. (Your edit to the missing-key test is kept.)
- `scripts/predeploy_check.ps1`: a correct version of the check script. The hand-written one printed
  "AUTOMATED CHECKS PASSED" after a failure because, in Windows PowerShell 5.1, a single result object
  has no `.Count`. The new script counts with `@(...)`, prints PASS/FAIL per check and exits with code 1
  on any failure. It was run under PowerShell 7 in both the passing and failing case.
- Dashboard: replacements show as a small table instead of raw JSON.
- `.gitignore`: added `.history/` (VS Code Local History can keep copies of `.env`).

## New tests (79 pass, 3 opt-in skipped)

- The real CrewAI OpenAI-compatible HTTP client against a local fake server, with the same settings
  shape as an Ollama setup.
- A model that never calls tools: the run still completes safely through the safety net.
- A slow model: the time budget stops waiting and the safety net completes the run.
- The `llm-test` probe tells a tool-calling model from a text-only one.
- Agent veto switch, replacement validation, and the whole suite with a realistic `.env` present.
