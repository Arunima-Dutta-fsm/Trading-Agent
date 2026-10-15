"""Time budget and run lifecycle: a timed-out agent can never act alongside or after the
safety net, signals expire with their run, and a crashed run is closed on restart."""
import threading
import time

import pytest
from conftest import add_signal, give_position, make_run
from pydantic import BaseModel

from aaba import approvals, controller, db
from aaba import broker as brokers
from aaba.settings import load_students

PINS = {"S2": "234223"}


class BuyArgs(BaseModel):
    signal_id: str
    reason: str


class SlowTool:
    """Stands in for an MCP tool: each call takes `seconds` and is logged with times."""
    name = "submit_buy"
    description = "Act on a BUY signal."
    args_schema = BuyArgs

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.log: list[tuple[float, float]] = []

    def run(self, **kwargs):
        start = time.monotonic()
        time.sleep(self.seconds)
        self.log.append((start, time.monotonic()))
        return '{"result": "ok"}'


def test_timed_out_agent_is_stopped_before_the_safety_net_runs(monkeypatch):
    from scripted_llm import ScriptedLLM

    from agents import trading_crew as tc

    tool = SlowTool(seconds=1.5)

    def endless_crew(run_id, tools, llm, students):
        # an agent that keeps calling the tool and the model until it is stopped
        while True:
            tools["submit_buy"].run(signal_id="sig-x", reason="r")
            llm.call([{"role": "user", "content": "go"}])

    monkeypatch.setattr(tc, "run_crew", endless_crew)
    texts, group, err, safe, thread = tc._run_crew_with_budget(
        "run-x", {"submit_buy": tool}, ScriptedLLM(model="scripted-test"), [], budget=2)
    closed_at = time.monotonic()

    assert safe is True and "did not finish within 2 seconds" in err
    assert tool.log, "the tool was never called"
    # the call that was running when time ran out finished BEFORE the safety net may start
    assert all(end <= closed_at for _, end in tool.log)
    n = len(tool.log)
    thread.join(10)
    assert not thread.is_alive(), "stopped crew thread kept running"
    time.sleep(2)
    assert len(tool.log) == n, "the abandoned agent called a tool after the budget ended"


def test_gate_refuses_tools_and_model_after_close():
    from scripted_llm import ScriptedLLM

    from agents import trading_crew as tc

    gate = tc.AgentGate()
    tool = SlowTool(seconds=0)
    gated = tc._gated_tool(tool, gate)
    llm = tc._gated_llm(ScriptedLLM(model="scripted-test"), gate)
    gated.run(signal_id="a", reason="b")
    assert len(tool.log) == 1
    assert gate.close(1) is True
    assert "time budget is over" in gated.run(signal_id="a", reason="b")
    assert len(tool.log) == 1
    with pytest.raises(tc.AgentStopped):
        llm.call([{"role": "user", "content": "hi"}])


def test_gate_close_reports_a_stuck_tool_call():
    from agents import trading_crew as tc

    gate = tc.AgentGate()
    tool = SlowTool(seconds=2)
    t = threading.Thread(target=lambda: tc._gated_tool(tool, gate).run(signal_id="a", reason="b"))
    t.start()
    time.sleep(0.3)
    assert gate.close(0.2) is False      # still running: caller must not start the safety net
    t.join()
    assert gate.close(1) is True


def test_gated_tools_send_the_same_schema_as_mcp_tools():
    from crewai.utilities.agent_utils import convert_tools_to_openai_schema
    from crewai_tools import MCPServerAdapter

    from agents import trading_crew as tc

    gate = tc.AgentGate()
    with MCPServerAdapter(tc._server_params(), connect_timeout=60) as tools:
        raw, _, _ = convert_tools_to_openai_schema(list(tools))
        gated, _, _ = convert_tools_to_openai_schema([tc._gated_tool(t, gate) for t in tools])
    assert raw == gated


# ------------------------------------------------------------------ run lifecycle
def test_signal_from_finished_run_cannot_be_acted_on():
    run = make_run("paper", prices={"AMD": 150.0})
    buy = add_signal(run, "S2", "AMD", "BUY", 150.0)
    give_position("S2", "TSLA", 5, 300.0)
    sell = add_signal(run, "S2", "TSLA", "SELL", 300.0)
    db.finish_run(run, "DONE")
    for res in (controller.submit_buy(buy["signal_id"]), controller.create_sell_approval(sell["signal_id"]),
                controller.record_hold(buy["signal_id"], "late")):
        assert res["result"] == "not processed" and "no longer valid" in res["detail"], res
    assert brokers.FakeBroker(load_students().get("S2"))._load()["orders"] == {}
    assert db.q("SELECT * FROM approvals") == []


def test_restart_marks_crashed_run_abandoned():
    crashed = make_run("paper", prices={"AMD": 150.0})
    sig = add_signal(crashed, "S2", "AMD", "BUY", 150.0)
    assert db.close_abandoned_runs() == [crashed]
    assert db.get_run(crashed)["status"] == "ABANDONED"
    assert controller.submit_buy(sig["signal_id"])["result"] == "not processed"
    assert db.close_abandoned_runs() == []


def test_dry_run_never_sends_an_approval_made_in_a_paper_run():
    paper = make_run("paper", prices={"AMD": 150.0})
    give_position("S2", "AMD", 20, 140.0, 150.0)
    sig = add_signal(paper, "S2", "AMD", "SELL", 150.0)
    aid = controller.create_sell_approval(sig["signal_id"], "exit")["approval_id"]
    approvals.decide(aid, "S2", PINS["S2"], "approve")
    dry = make_run("dry_run", prices={"AMD": 150.0})
    res = controller.process_approved_sells("S2", dry)
    assert res["approved_processed"] == [] and res["approved_waiting_for_paper_run"] == 1
    assert db.q1("SELECT status FROM approvals WHERE approval_id=?", (aid,))["status"] == "APPROVED"
    assert brokers.FakeBroker(load_students().get("S2"))._load()["orders"] == {}
    res = controller.process_approved_sells("S2", make_run("paper", prices={"AMD": 150.0}))
    assert res["approved_processed"][0]["result"] == "EXECUTED"
