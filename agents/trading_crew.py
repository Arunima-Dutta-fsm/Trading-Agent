"""CrewAI orchestration: ONE agent, ONE LLM, three student modules, tools over local MCP.

What happens in one run (see docs/ARCHITECTURE.md for the diagram):
 1. A run record is created with its mode (analysis / dry_run / paper). The LLM
    cannot change the mode.
 2. The local MCP server is started as a child process (stdio) through
    crewai_tools.MCPServerAdapter. Its tools are the only way the agent can touch
    data, accounts or orders.
 3. The Portfolio Orchestrator agent works through a sequential crew:
      task 1  load market data for all 36 stocks + SPY (one batch request) and check accounts
      task 2-4  one task per enabled student: process approved SELLs, evaluate the 12 stocks,
              submit BUYs, request SELL approvals (HOLDs are recorded automatically)
      task 5  short group summary
 4. A deterministic safety net then checks every BUY/SELL signal of this run. If the
    agent skipped one (LLMs sometimes do), the same MCP tool is called by code, and the
    report says so. If no LLM is configured, or it fails or runs out of time
    (agent.crew_time_budget_seconds), the safety net completes the workflow on its own and
    the report says that too.

    Do not move the safety net in front of the crew. The agent must be the one that acts
    on signals (the project requirement); the safety net is only the backstop.
 5. Orders are reconciled, snapshots saved, and one Markdown report per student plus
    a group report are written to model-run/.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

from aaba import db, paths, reports  # noqa: E402
from aaba import llm as llm_factory  # noqa: E402
from aaba.runlock import run_lock  # noqa: E402
from aaba.settings import (is_simulated, load_strategy, load_students, paper_orders_enabled,  # noqa: E402
                           redact)

log = logging.getLogger("aaba.crew")

PREP_TOOLS = ["prepare_market_data", "check_accounts"]
STUDENT_TOOLS = ["process_approved_sells", "evaluate_student", "preview_buy_risk", "submit_buy",
                 "create_sell_approval", "record_hold", "get_portfolio"]

BACKSTORY = """You coordinate paper trading for three MBA students. Each student has their own
$100,000 Alpaca paper account and 12 stocks. You never mix students.
Hard rules:
- You only act through the tools. You never invent prices, indicator values, quantities or signal ids.
- The rules engine (evaluate_student) decides BUY, SELL or HOLD. You do not create signals.
- For every BUY signal call submit_buy. Code runs the risk checks and sizes the order; you cannot
  override a failed check. You MAY call record_hold instead, but only if the tool evidence (for
  example strongly negative news in the reason) clearly argues against buying. Say why.
- For every SELL signal call create_sell_approval. A SELL is never sent without the student's
  approval, and you cannot cancel a SELL signal.
- HOLD signals are already recorded. Do nothing for them.
- Keep explanations short, factual and in plain English, using numbers from the tool results only."""


def _student_task_text(run_id: str, st) -> str:
    return f"""Run id: {run_id}
Student: {st.name}, roll no {st.roll_no}, student_id {st.student_id}.
Growth Satellites: {', '.join(st.satellites)}. Diversified Core: {', '.join(st.core)}.

Steps:
1. Call process_approved_sells with student_id="{st.student_id}" and run_id="{run_id}".
2. Call evaluate_student with run_id="{run_id}" and student_id="{st.student_id}".
3. For each signal with action BUY: call submit_buy(signal_id, reason) with a one-line reason,
   or record_hold(signal_id, reason) only if the evidence clearly argues against the buy.
4. For each signal with action SELL: call create_sell_approval(signal_id, reason).
5. Do nothing for HOLD signals.
Only use signal_id values returned by evaluate_student for student {st.student_id}."""


STUDENT_EXPECTED = ("A short Markdown note for this student: a table with symbol, signal and the result "
                    "returned by the tool for every BUY and SELL, then 3 to 5 plain sentences explaining the "
                    "decisions using only numbers from the tool results.")


def _tool_map(tools) -> dict:
    return {t.name: t for t in tools}


def _call(tool, **kwargs) -> dict:
    """Call an MCP tool from code (safety net). Returns parsed JSON when possible."""
    out = tool.run(**kwargs)
    if isinstance(out, (dict, list)):
        return out
    try:
        return json.loads(out)
    except (TypeError, ValueError):
        return {"raw": str(out)[:2000]}


def _server_params():
    from mcp import StdioServerParameters
    return StdioServerParameters(command=sys.executable, args=[str(paths.MCP_SERVER_SCRIPT)],
                                 env=dict(os.environ), cwd=str(ROOT))


def _build_llm():
    """Build the runtime LLM fallback chain."""
    try:
        from aaba.llm_fallback import build_fallback_llm

        llm, providers, setup_errors = build_fallback_llm()
        note = "; ".join(setup_errors) if setup_errors else None

        return llm, providers[0], " -> ".join(providers), note

    except Exception as e:  # noqa: BLE001
        provider, model = llm_factory.describe("LLM")
        return None, provider, model, f"LLM fallback chain unavailable: {e}"


def run_crew(run_id: str, tools: dict, llm, students) -> tuple[dict[str, str], str | None, str | None]:
    """Run the CrewAI crew. Returns ({student_id: text}, group_text, error)."""
    from crewai import Agent, Crew, Process, Task

    cfg = load_strategy()
    agent = Agent(
        role="AABA Portfolio Orchestrator",
        goal="Run today's trading workflow for each student portfolio through the MCP tools, following "
             "the rules engine and risk checks exactly, and explain the decisions in plain English.",
        backstory=BACKSTORY,
        llm=llm,
        tools=[],
        allow_delegation=False,
        max_iter=cfg.agent.max_iterations,
        max_rpm=cfg.agent.max_rpm,
        verbose=os.getenv("CREW_VERBOSE", "false").lower() == "true",
    )
    prep = Task(
        description=f"Run id: {run_id}. Call prepare_market_data with run_id=\"{run_id}\" once. Report whether "
                    f"the data is fresh, the expected bar date, any symbol problems and any account problems.",
        expected_output="Three or four plain sentences on data freshness and account status.",
        agent=agent, tools=[tools[n] for n in PREP_TOOLS if n in tools])
    st_tasks = []
    for st in students:
        st_tasks.append(Task(description=_student_task_text(run_id, st), expected_output=STUDENT_EXPECTED,
                             agent=agent, tools=[tools[n] for n in STUDENT_TOOLS if n in tools], context=[prep]))
    summary = Task(description="Write a group summary for the three students: what each student's portfolio did "
                               "in this run and anything that needs a human (pending SELL approvals, blocked "
                               "orders, data or account problems). Use only facts from the previous task outputs.",
                   expected_output="One short Markdown section, at most 150 words.", agent=agent, tools=[],
                   context=st_tasks)
    crew = Crew(agents=[agent], tasks=[prep, *st_tasks, summary], process=Process.sequential,
                verbose=agent.verbose, max_rpm=cfg.agent.max_rpm)
    try:
        crew.kickoff()
    except Exception as e:  # noqa: BLE001
        texts = {st.student_id: (t.output.raw if t.output else None) for st, t in zip(students, st_tasks)}
        return texts, None, redact(f"{type(e).__name__}: {str(e)[:400]}")
    texts = {st.student_id: (t.output.raw if t.output else None) for st, t in zip(students, st_tasks)}
    return texts, (summary.output.raw if summary.output else None), None


class AgentStopped(RuntimeError):
    """Raised inside the crew thread once its time budget is over."""


class AgentGate:
    """Every agent tool call and model call passes through here.

    close() waits for a tool call already in progress to finish, then refuses all later
    calls. So once the time budget is over, the abandoned crew thread can never call a
    tool at the same time as the safety net, or after it."""

    def __init__(self):
        self._lock = threading.Lock()
        self.closed = False

    def check(self) -> None:
        if self.closed:
            raise AgentStopped("agent time budget is over; this crew run was stopped")

    def run_tool(self, tool, kwargs: dict):
        with self._lock:
            if self.closed:
                return json.dumps({"error": "agent time budget is over; no action was taken"})
            return tool.run(**kwargs)

    def close(self, timeout: float) -> bool:
        """Returns False if a tool call was still running after `timeout` seconds."""
        if not self._lock.acquire(timeout=timeout):
            return False
        self.closed = True
        self._lock.release()
        return True


def _gated_tool(tool, gate: AgentGate):
    from crewai.tools import BaseTool

    class GatedTool(BaseTool):
        def _run(self, **kwargs):
            return gate.run_tool(tool, kwargs)

    return GatedTool(name=tool.name, description=tool.description, args_schema=tool.args_schema)


def _gated_llm(llm, gate: AgentGate):
    from crewai import BaseLLM
    from pydantic import PrivateAttr

    class GatedLLM(BaseLLM):
        _inner: object = PrivateAttr(default=None)

        def supports_function_calling(self) -> bool:
            return self._inner.supports_function_calling()

        def get_context_window_size(self) -> int:
            return self._inner.get_context_window_size()

        def call(self, messages, tools=None, callbacks=None, available_functions=None, from_task=None,
                 from_agent=None, response_model=None):
            gate.check()
            out = self._inner.call(messages, tools=tools, callbacks=callbacks,
                                   available_functions=available_functions, from_task=from_task,
                                   from_agent=from_agent, response_model=response_model)
            gate.check()      # the budget ran out during this call: do not act on its answer
            return out

    wrapped = GatedLLM(model=getattr(llm, "model", "gated"), temperature=getattr(llm, "temperature", None))
    wrapped._inner = llm
    return wrapped


TOOL_DRAIN_SECONDS = 300     # longest wait for an in-progress tool call once the budget is over


def _run_crew_with_budget(run_id: str, tools: dict, llm, students, budget: int):
    """Run the crew in a background thread and stop it after `budget` seconds.

    CrewAI's own max_execution_time waits for the stuck task to finish before it gives up,
    so it does not protect against a slow model. Here, when time is up, the gate first waits
    for any tool call in progress, then blocks every further tool and model call from the
    crew thread, so the safety net never acts on the run at the same time as the agent.

    Returns (texts, group_text, error, safe_to_continue, thread). safe_to_continue is False
    only if a tool call was still running after TOOL_DRAIN_SECONDS; then the safety net
    must not run."""
    box: dict = {}
    gate = AgentGate()
    gated_tools = {name: _gated_tool(t, gate) for name, t in tools.items()}
    gated_llm = _gated_llm(llm, gate)

    def work():
        try:
            box["result"] = run_crew(run_id, gated_tools, gated_llm, students)
        except Exception as e:  # noqa: BLE001
            box["error"] = redact(f"{type(e).__name__}: {str(e)[:400]}")

    t = threading.Thread(target=work, name=f"crew-{run_id}", daemon=True)
    t.start()
    t.join(budget)
    if t.is_alive():
        if not gate.close(TOOL_DRAIN_SECONDS):
            return {}, None, (f"the agent did not finish within {budget} seconds and a tool call was still "
                              f"running {TOOL_DRAIN_SECONDS} seconds later; the safety net was NOT run, "
                              f"check this run by hand"), False, t
        return {}, None, (f"the agent did not finish within {budget} seconds "
                          f"(agent.crew_time_budget_seconds); the agent was stopped and the safety net "
                          f"completed the run"), True, t
    gate.close(0)
    if "error" in box:
        return {}, None, box["error"], True, t
    return (*box["result"], True, t)


def safety_net(run_id: str, tools: dict, students, exec_buys: bool) -> list[dict]:
    """Make sure every BUY/SELL signal of this run has an outcome, using the same MCP tools."""
    actions: list[dict] = []
    _call(tools["prepare_market_data"], run_id=run_id)
    for st in students:
        _call(tools["process_approved_sells"], student_id=st.student_id, run_id=run_id)
        _call(tools["evaluate_student"], run_id=run_id, student_id=st.student_id)
        for sig in db.q("SELECT * FROM signals WHERE run_id=? AND student_id=? AND outcome IS NULL",
                        (run_id, st.student_id)):
            why = "agent did not act on this signal"
            if sig["action"] == "BUY":
                if exec_buys:
                    res = _call(tools["submit_buy"], signal_id=sig["signal_id"], reason=f"safety net: {why}")
                else:
                    res = _call(tools["record_hold"], signal_id=sig["signal_id"],
                                reason="safety net: agent did not act and safety_net_executes_buys is off")
            elif sig["action"] == "SELL":
                res = _call(tools["create_sell_approval"], signal_id=sig["signal_id"], reason=f"safety net: {why}")
            else:
                continue
            actions.append({"student_id": st.student_id, "symbol": sig["symbol"], "result": res.get("result"),
                            "why": why})
            db.log_event("WARNING", "safety_net", f"{sig['action']} {sig['symbol']} completed by safety net: "
                                                  f"{res.get('result')}", run_id, st.student_id)
        _call(tools["reconcile_student"], student_id=st.student_id, run_id=run_id)
    return actions


def run_workflow(mode: str = "analysis", trigger: str = "manual", use_llm: bool | None = None) -> dict:
    """Run one complete workflow. Returns a small summary dict (also printed by main.py)."""
    from aaba.demo import guard
    guard("running the trading workflow")
    if mode not in ("analysis", "dry_run", "paper"):
        raise ValueError("mode must be analysis, dry_run or paper")
    paths.ensure_dirs()
    db.init_db()
    cfg = load_strategy()
    group = load_students()
    students = group.enabled
    if not students:
        raise RuntimeError("no student module is enabled in configs/students.json")
    use_llm = cfg.agent.use_llm if use_llm is None else use_llm

    with run_lock(f"{mode}/{trigger}"):
        # we hold the run lock, so any run still marked RUNNING was left by a crashed process
        for old in db.close_abandoned_runs():
            db.log_event("WARNING", "recovery", f"run {old} was left unfinished by an earlier process; "
                                                f"marked ABANDONED, its signals can no longer be acted on", old)
        llm, provider, model, llm_note = (None, None, None, "LLM disabled for this run")
        if use_llm:
            llm, provider, model, llm_note = _build_llm()
        engine = "crewai+mcp" if llm is not None else "rules-only+mcp (no LLM)"
        run_id = db.create_run(mode, trigger, engine, provider if llm else None, model if llm else None,
                               is_simulated())
        if mode == "paper" and not paper_orders_enabled():
            db.log_event("WARNING", "orders_disabled",
                         "paper mode requested but ENABLE_PAPER_ORDERS is not true: no order will be sent", run_id)
        if llm_note:
            db.log_event("WARNING" if use_llm else "INFO", "llm", llm_note, run_id)

        texts: dict[str, str] = {}
        group_text, agent_error, actions, status, err = None, None, [], "DONE", None
        crew_thread = None
        try:
            from crewai_tools import MCPServerAdapter
            with MCPServerAdapter(_server_params(), connect_timeout=60) as mcp_tools:
                tools = _tool_map(mcp_tools)
                missing = [n for n in PREP_TOOLS + STUDENT_TOOLS + ["reconcile_student"] if n not in tools]
                if missing:
                    raise RuntimeError(f"MCP server is missing tools: {missing}")
                safe = True
                if llm is not None:
                    texts, group_text, agent_error, safe, crew_thread = _run_crew_with_budget(
                        run_id, tools, llm, students, cfg.agent.crew_time_budget_seconds)
                    if agent_error:
                        db.log_event("ERROR", "crew", f"CrewAI run stopped: {agent_error}", run_id)
                if not safe:
                    raise RuntimeError(agent_error)
                actions = safety_net(run_id, tools, students, cfg.agent.safety_net_executes_buys)
        except Exception as e:  # noqa: BLE001
            status, err = "FAILED", redact(f"{type(e).__name__}: {e}")
            db.log_event("ERROR", "workflow", err, run_id, data={"trace": traceback.format_exc()[-2000:]})
        finally:
            files = []
            for st in students:
                try:
                    files.append(str(reports.student_report(run_id, st.student_id, texts.get(st.student_id),
                                                            agent_error or (llm_note if llm is None else None))))
                except Exception as e:  # noqa: BLE001
                    db.log_event("ERROR", "report", f"report failed: {type(e).__name__}: {e}", run_id, st.student_id)
            try:
                files.append(str(reports.group_report(run_id, group_text, actions,
                                                      agent_error or (llm_note if llm is None else None))))
            except Exception as e:  # noqa: BLE001
                db.log_event("ERROR", "report", f"group report failed: {type(e).__name__}: {e}", run_id)
            if status == "DONE" and agent_error:
                status = "DONE_WITH_LLM_ERROR"
            db.finish_run(run_id, status, err, notes=json.dumps({"reports": [Path(f).name for f in files]}))
            if crew_thread is not None:
                # a stopped crew raises on its next model call; let it unwind before returning
                crew_thread.join(60)
                if crew_thread.is_alive():
                    db.log_event("WARNING", "crew", "stopped crew thread is still unwinding (it can no longer "
                                                    "call tools or the model)", run_id)
        counts = db.q("SELECT student_id, action, outcome, COUNT(*) AS n FROM signals WHERE run_id=? "
                      "GROUP BY student_id, action, outcome", (run_id,))
        return {"run_id": run_id, "mode": mode, "engine": engine, "status": status, "error": err,
                "agent_error": agent_error, "safety_net_actions": len(actions), "reports": files,
                "outcomes": counts}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Run one AABA trading workflow")
    ap.add_argument("--mode", default="analysis", choices=["analysis", "dry_run", "paper"])
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()
    print(json.dumps(run_workflow(a.mode, "cli", use_llm=not a.no_llm), indent=2, default=str))
