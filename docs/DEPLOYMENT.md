# Deployment

## Where each part can run

| Part | Needs | Streamlit Community Cloud? |
|---|---|---|
| Dashboard (`app/streamlit_app.py`) | the SQLite database in `data/` | Starts, but sees an **empty, temporary** database |
| Trading workflow (`main.py run`) | Python, the MCP server as a child process, LLM keys, Alpaca keys, `data/` | Possible to start, but its records are lost on every restart |
| Scheduler (`main.py scheduler`) | a process that runs all day | **No**. Cloud apps sleep and restart. |
| Ollama at a LAN address (e.g. `172.x.x.x`) | the local network | **No**. Not reachable from the cloud. |

The MCP server is not a blocker: it is started as a child process (stdio) inside the same machine
or container as the workflow.

The blocker is state. Every signal, order, SELL approval and audit event lives in one SQLite file.
Streamlit Cloud has no persistent disk, so that file is wiped whenever the app restarts, sleeps or is
redeployed. The cloud dashboard would also be a **different database** from the laptop where the
scheduler trades, so a student approving a SELL in the cloud would approve nothing the laptop can see.

Orders stay safe even then: Alpaca rejects a repeated `client_order_id`, and the code checks the
broker before sending. But the audit trail and pending approvals would be lost.

## Supported setup (no code changes)

Run everything on one always-on machine: the laptop, or a small VM.

```powershell
python main.py scheduler            # window 1: trading at 09:45 New York, Mon-Fri
python main.py dashboard            # window 2: http://localhost:8501
powershell -File scripts\start_ngrok.ps1 -Password <strong password>   # optional: phone access
```

The Ollama fallback only works where its address is reachable. Either point `OLLAMA_BASE_URL` at
an OpenAI-compatible Ollama endpoint that machine can reach (check Ollama's documentation for its
hosted API and set `OLLAMA_API_KEY`), or leave Ollama unset: the chain then runs Groq, then Gemini.

## Streamlit Community Cloud

The hosted app is a **read-only demo**, started from `app/demo_app.py`, not the trading dashboard.

What the demo does:
- It switches on demo mode (`AABA_DEMO_MODE=1`) before anything else loads. It ignores `.env`,
  removes any key, PIN, token or account-number variable from its environment, and forces
  simulated prices and the simulated broker.
- It builds a sample database once, in the server's temp folder, using the normal rules engine on
  one simulated day. It never opens the real `data/aaba.db`.
- It shows students as "Student 1/2/3" (no names or roll numbers) and `FAKE-S1/2/3` accounts.
- It refuses every change on the server side (`aaba/demo.py`), whatever the page sends: orders,
  SELL approvals and decisions, settings changes, workflow runs, the scheduler, account checks,
  reconciliation, symbol validation and backtests. The real Alpaca client cannot be created.
- On Streamlit Cloud it installs only `app/requirements.txt`, which has no `crewai`, `mcp` or
  `alpaca-py`, so the hosted demo has no broker or LLM code at all.

Steps:

1. Push the repository to GitHub.
2. On share.streamlit.io: **Create app**, then **Deploy a public app from GitHub**. Choose the repository,
   branch `main`, main file path **`app/demo_app.py`**. Under Advanced settings, set the Python
   version to **3.11** and leave **Secrets empty**. The demo needs no keys, so never paste real ones.
3. Deploy, then check on the live URL:
   - Overview shows the blue READ-ONLY DEMO banner and "Student 1/2/3 (demo)"
   - there are no Run, Approve, Reject, Save or Check-account buttons on any page
   - System says the checks are disabled in the read-only demo

Test the demo locally first: `streamlit run app/demo_app.py` (it never reads your `.env`).

## Controlled paper-order test (run once, by hand)

Do this only after `scripts\predeploy_check.ps1 -WithAnalysisRun` passes, and after
`python scripts/paper_preflight.py` reports three distinct, ACTIVE paper accounts. Two separate steps:

1. **Broker round-trip, no position change.** This places one 1-share LIMIT BUY at half the market
   price, which cannot fill, and cancels it straight away:

   ```powershell
   $env:AABA_E2E = "1"; $env:AABA_E2E_SUBMIT = "1"
   foreach ($s in "S1","S2","S3") { $env:AABA_E2E_STUDENT = $s; python -m pytest tests/test_e2e_paper.py -v -s }
   Remove-Item Env:AABA_E2E, Env:AABA_E2E_SUBMIT, Env:AABA_E2E_STUDENT
   ```

   Check in each Alpaca paper dashboard that the order shows as canceled.
2. **One real paper session.** Set `ENABLE_PAPER_ORDERS=true` in `.env`, run
   `python main.py run --mode paper` while the market is open, then check:
   - each submitted BUY appears only in its own student's Alpaca paper account
   - `orders.client_order_id` matches Alpaca's client order id
   - the run report lists every BUY/SELL with its outcome
   - SELLs appear as PENDING in the Approval Centre, and nothing was sold

   Set `ENABLE_PAPER_ORDERS=false` again if you are not continuing.
