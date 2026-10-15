"""End-to-end checks on simulated data: MCP server/client, the CrewAI workflow,
the safety net, reports, secrets, dashboard data, scheduler and backtest.
No network and no real orders."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT, SECRETS, give_position

from aaba import db, market_data, paths
from aaba import broker as brokers
from aaba.runlock import RunLocked, run_lock
from aaba.settings import load_students

BUSY_DAY = "2026-09-30T14:30:00+00:00"     # simulated data has several BUY setups on this day


def env_for_subprocess():
    return dict(os.environ)


# ------------------------------------------------------------------ MCP
def test_mcp_client_starts_server_lists_tools_and_calls_one():
    r = subprocess.run([sys.executable, str(ROOT / "mcp_server" / "mcp_client_test.py"), "--prepare"],
                       capture_output=True, text=True, timeout=300, env=env_for_subprocess(), cwd=str(ROOT))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "14 tools" in r.stdout
    for name in ("prepare_market_data", "evaluate_student", "submit_buy", "create_sell_approval",
                 "get_latest_indicators", "process_approved_sells"):
        assert name in r.stdout
    assert '"symbol": "AMD"' in r.stdout and "EMA200" in r.stdout
    assert "is not in S2's portfolio" in r.stdout          # invalid input handled
    assert "Session closed cleanly" in r.stdout


def test_crewai_mcp_adapter_exposes_tools():
    from crewai_tools import MCPServerAdapter
    from agents.trading_crew import _server_params
    with MCPServerAdapter(_server_params(), connect_timeout=60) as tools:
        names = {t.name for t in tools}
        assert {"submit_buy", "create_sell_approval", "record_hold", "evaluate_student"} <= names
        out = json.loads(next(t for t in tools if t.name == "get_portfolio").run(student_id="S3"))
        assert out["student_id"] == "S3" and out["cash"] == 100000


# ------------------------------------------------------------------ CrewAI workflow
def _run(monkeypatch, llm=None, mode="paper", use_llm=True):
    from agents import trading_crew as tc
    monkeypatch.setenv("AABA_SYNTHETIC_NOW", BUSY_DAY)
    if llm is not None:
        monkeypatch.setattr(tc, "_build_llm", lambda: (llm, "scripted", "scripted-test", None))
    return tc.run_workflow(mode, "test", use_llm=use_llm)


def test_cmd_run_returns_failure_for_llm_error(monkeypatch, capsys):
    import argparse
    import main

    monkeypatch.setattr(
        "agents.trading_crew.run_workflow",
        lambda *args, **kwargs: {
            "status": "DONE_WITH_LLM_ERROR",
            "agent_error": "CrewAI timed out",
            "outcomes": [],
        },
    )

    result = main.cmd_run(argparse.Namespace(mode="analysis", no_llm=False))

    assert result == 1


def test_crewai_agent_drives_the_workflow_through_mcp(monkeypatch):
    from scripted_llm import ScriptedLLM
    llm = ScriptedLLM(model="scripted-test")
    res = _run(monkeypatch, llm)
    assert res["engine"] == "crewai+mcp" and res["status"] == "DONE", res
    assert llm.calls >= 10                          # the real CrewAI loop called the model repeatedly
    assert res["safety_net_actions"] == 0           # the agent handled every BUY/SELL itself
    sigs = db.q("SELECT * FROM signals WHERE run_id=?", (res["run_id"],))
    assert len(sigs) == 36
    buys = [s for s in sigs if s["action"] == "BUY"]
    assert buys and all(s["outcome_by"] == "agent" for s in buys)
    assert all(s["outcome"] for s in sigs)          # nothing left unhandled
    for s in buys:
        if s["outcome"] == "BUY_SUBMITTED":
            o = db.q1("SELECT * FROM orders WHERE signal_id=?", (s["signal_id"],))
            assert o["student_id"] == s["student_id"] and o["account_number"] == f"FAKE-{s['student_id']}"
    names = sorted(Path(f).name for f in res["reports"])
    assert len(names) == 4
    pat = re.compile(r"^Orchestrator_(S\d_[A-Za-z]+|Group)\d{8}-\d{2}-\d{2}(_\d+)?\.md$")
    assert all(pat.match(n) for n in names), names


def test_safety_net_completes_signals_the_agent_skipped(monkeypatch):
    from scripted_llm import ScriptedLLM
    all_syms = load_students().all_symbols
    llm = ScriptedLLM(model="scripted-test", skip_symbols=all_syms)
    res = _run(monkeypatch, llm)
    sigs = db.q("SELECT * FROM signals WHERE run_id=? AND action IN ('BUY','SELL')", (res["run_id"],))
    assert sigs and res["safety_net_actions"] == len(sigs)
    assert all(s["outcome_by"] == "agent" or "safety net" in (s["outcome_detail"] or "") or s["outcome"] for s in sigs)
    group = [f for f in res["reports"] if "Group" in f][0]
    assert "safety net" in Path(group).read_text(encoding="utf-8").lower()


def test_agent_veto_is_respected(monkeypatch):
    from scripted_llm import ScriptedLLM
    llm = ScriptedLLM(model="scripted-test", veto_symbols=["AMD"])
    res = _run(monkeypatch, llm)
    amd = db.q1("SELECT * FROM signals WHERE run_id=? AND symbol='AMD'", (res["run_id"],))
    assert amd["action"] == "BUY" and amd["outcome"] == "AGENT_VETO_HOLD"
    assert db.q1("SELECT * FROM orders WHERE symbol='AMD'") is None


def test_slow_model_cannot_hold_up_the_run(monkeypatch):
    import time
    from scripted_llm import ScriptedLLM
    from aaba import settings
    real = settings.load_strategy

    def short_budget():
        cfg = real()
        return cfg.model_copy(update={"agent": cfg.agent.model_copy(update={"crew_time_budget_seconds": 30})})
    import agents.trading_crew as tc
    monkeypatch.setattr(tc, "load_strategy", short_budget)
    t0 = time.perf_counter()
    res = _run(monkeypatch, ScriptedLLM(model="scripted-test", delay_seconds=20))
    assert time.perf_counter() - t0 < 120
    assert "did not finish within 30 seconds" in (res["agent_error"] or "")
    sigs = db.q("SELECT * FROM signals WHERE run_id=?", (res["run_id"],))
    assert len(sigs) == 36 and all(s["outcome"] for s in sigs)
    assert res["safety_net_actions"] >= 1


def test_agent_veto_can_be_switched_off(monkeypatch):
    from conftest import add_signal, make_run
    from aaba import controller, settings
    real = settings.load_strategy

    def no_veto():
        cfg = real()
        return cfg.model_copy(update={"agent": cfg.agent.model_copy(update={"allow_agent_veto": False})})
    monkeypatch.setattr(controller, "load_strategy", no_veto)
    run = make_run("paper", prices={"AMD": 150.0})
    sig = add_signal(run, "S2", "AMD", "BUY", 150.0)
    assert controller.record_hold(sig["signal_id"], "small model says no")["result"] == "refused"


def test_sell_signal_in_workflow_creates_approval_not_order(monkeypatch):
    from scripted_llm import ScriptedLLM
    # S3 holds every stock at a much higher price, so the 2xATR stop fires: SELL signals
    b = brokers.FakeBroker(load_students().get("S3"))
    st = b._load()
    for sym in load_students().get("S3").symbols:
        st["positions"][sym] = {"qty": 3, "avg_entry": 1_000_000.0, "last_price": 1.0}   # far above any price
    b._save(st)
    res = _run(monkeypatch, ScriptedLLM(model="scripted-test"))
    sells = db.q("SELECT * FROM signals WHERE run_id=? AND student_id='S3' AND action='SELL'", (res["run_id"],))
    assert len(sells) == 12
    assert all(s["outcome"] == "SELL_PENDING_APPROVAL" for s in sells)
    assert len(db.student_approvals("S3", ("PENDING",))) == 12
    assert db.q("SELECT * FROM orders WHERE side='sell'") == []


def test_analysis_mode_sends_nothing(monkeypatch):
    from scripted_llm import ScriptedLLM
    res = _run(monkeypatch, ScriptedLLM(model="scripted-test"), mode="analysis")
    assert db.q("SELECT * FROM orders") == [] and db.q("SELECT * FROM approvals") == []
    outs = {s["outcome"] for s in db.q("SELECT * FROM signals WHERE run_id=? AND action='BUY'", (res["run_id"],))}
    assert outs <= {"ANALYSIS_ONLY", "BUY_BLOCKED"}


def test_missing_llm_key_falls_back_to_rules_only(monkeypatch):
    from aaba import llm as llm_factory
    # test the missing-key case for Groq whatever provider the real .env names
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-120b")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(llm_factory.LLMConfigError, match="GROQ_API_KEY is missing"):
        llm_factory.build()
    res = _run(monkeypatch, None)
    assert res["engine"].startswith("rules-only") and res["status"] == "DONE"
    sigs = db.q("SELECT * FROM signals WHERE run_id=?", (res["run_id"],))
    assert len(sigs) == 36 and all(s["outcome"] for s in sigs)
    text = Path([f for f in res["reports"] if "Group" in f][0]).read_text(encoding="utf-8")
    assert "GROQ_API_KEY is missing" in text


def test_no_secrets_in_reports_logs_or_database(monkeypatch, isolated):
    from scripted_llm import ScriptedLLM
    _run(monkeypatch, ScriptedLLM(model="scripted-test"))
    blobs = []
    for folder in (paths.MODEL_RUN_DIR, paths.LOG_DIR, paths.DATA_DIR):
        for p in Path(folder).rglob("*"):
            if p.is_file():
                blobs.append((p, p.read_bytes()))
    assert blobs
    for p, data in blobs:
        for secret in SECRETS:
            # numeric PINs are checked as whole tokens (a PIN's digits can occur inside a long decimal)
            pat = rb"(?<![0-9A-Za-z.])" + re.escape(secret.encode()) + rb"(?![0-9A-Za-z])"
            assert not re.search(pat, data), f"secret found in {p}"


def test_redaction_removes_secrets_but_keeps_numbers():
    from aaba.settings import redact
    text = "key PKTESTSECRETKEYONE111 pin 481516 price 0.4815162 and 1481516"
    out = redact(text)
    assert "PKTESTSECRETKEYONE111" not in out and " 481516 " not in out
    assert "0.4815162" in out and "1481516" in out


def test_repository_contains_no_credentials():
    gi = (ROOT / ".gitignore").read_text()
    assert re.search(r"^\.env$", gi, re.M) and "data/" in gi
    key_like = re.compile(rb"\b(PK|AK)[A-Z0-9]{18}\b|gsk_[A-Za-z0-9]{20,}")
    skip = {"conftest.py"}                      # holds clearly fake test values only
    for p in ROOT.rglob("*"):
        if (p.is_file() and p.suffix in {".py", ".md", ".json", ".toml", ".txt", ".ps1", ".example", ".yml"}
                and ".venv" not in p.parts and p.name not in skip):
            assert not key_like.search(p.read_bytes()), f"possible key in {p}"
    assert not (ROOT / ".env").exists() or ".env" in gi


# ------------------------------------------------------------------ other pieces
def test_run_lock_blocks_overlapping_runs():
    with run_lock("first"):
        with pytest.raises(RunLocked):
            with run_lock("second"):
                pass
    paths.LOCK_FILE.write_text(json.dumps({"pid": 999999, "t": 0, "label": "crashed"}))
    with run_lock("after crash"):          # a stale lock from a dead process is cleared
        pass


def test_dashboard_data_reflects_database():
    from app import dashboard_data as dd
    db.ex("INSERT INTO accounts(student_id, account_number, paper_verified, baseline_equity, baseline_spy)"
          " VALUES ('S1','PA1',1,100000,500)")
    db.ex("INSERT INTO snapshots(at, student_id, account_number, equity, cash, long_value, unrealized_pnl,"
          " realized_pnl, positions, spy_price) VALUES ('2026-10-13T15:00:00+00:00','S1','PA1',103000,50000,53000,"
          " 2500, 500, ?, 510)", (json.dumps({"NVDA": {"qty": 10, "avg_entry": 200, "market_value": 2300,
                                                      "unrealized_pl": 300, "current_price": 230}}),))
    s = dd.student_summary("S1")
    assert s["equity"] == 103000 and s["return_pct"] == 3.0 and s["account"] == "PA1"
    assert dd.holdings("S1")["symbol"].tolist() == ["NVDA"]
    assert dd.student_summary("S2")["equity"] is None          # S1 data never shows up for S2
    db.ex("INSERT INTO runs(run_id, started_at, mode, trigger, status, simulated) VALUES "
          "('r1','2026-10-14T14:00:00+00:00','paper','t','DONE',0),"
          "('r2','2026-10-14T18:00:00+00:00','paper','t','DONE',0),"
          "('r3','2026-10-15T14:00:00+00:00','analysis','t','DONE',0),"
          "('r4','2026-10-16T14:00:00+00:00','paper','t','DONE',1)")
    assert dd.trading_sessions()["sessions"] == 1             # analysis and simulated runs do not count


def test_dashboard_renders_without_errors(monkeypatch):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=60).run()
    assert not at.exception, at.exception
    text = " ".join(m.value for m in at.markdown) + " ".join(str(t.value) for t in at.title)
    assert "AABA" in text


def test_approved_sell_job_never_overlaps_a_workflow_run(monkeypatch):
    from conftest import add_signal, make_run
    from aaba import approvals, controller, scheduler
    run = make_run("paper", prices={"AMD": 150.0})
    give_position("S2", "AMD", 20, 140.0, 150.0)
    aid = controller.create_sell_approval(add_signal(run, "S2", "AMD", "SELL", 150.0)["signal_id"])["approval_id"]
    approvals.decide(aid, "S2", "234223", "approve")
    with run_lock("workflow"):
        scheduler.approved_sell_job()                     # a run holds the lock: nothing happens
    assert db.q1("SELECT status FROM approvals WHERE approval_id=?", (aid,))["status"] == "APPROVED"
    assert "skipped" in db.q1("SELECT message FROM events WHERE kind='scheduler' ORDER BY id DESC")["message"]
    scheduler.approved_sell_job()                         # lock free: the approved SELL is executed once
    assert db.q1("SELECT status FROM approvals WHERE approval_id=?", (aid,))["status"] == "EXECUTED"
    assert len(db.q("SELECT * FROM orders WHERE side='sell'")) == 1


def test_scheduler_jobs_are_single_instance_at_new_york_times(monkeypatch):
    from apscheduler.schedulers.blocking import BlockingScheduler
    from aaba import scheduler
    captured = {}
    monkeypatch.setattr(BlockingScheduler, "start", lambda self: captured.update(
        {j.id: (j.max_instances, str(j.trigger)) for j in self.get_jobs()}))
    scheduler.start()
    assert captured["daily-09:45"][0] == 1 and "hour='9', minute='45'" in captured["daily-09:45"][1]
    assert "day_of_week='mon-fri'" in captured["daily-09:45"][1]
    assert captured["approved-sells"][0] == 1


def test_scheduler_skips_non_trading_days(monkeypatch):
    from aaba import scheduler
    from aaba.clock import parse_iso
    market_data.set_provider(market_data.SyntheticMarketData(now=parse_iso("2026-10-17T14:00:00+00:00")))  # Saturday
    scheduler.daily_job()
    ev = db.q1("SELECT * FROM events WHERE kind='scheduler' ORDER BY id DESC")
    assert ev and "skipped" in ev["message"]
    assert db.q("SELECT * FROM runs") == []
    assert len(scheduler.next_runs()) == 3


def test_symbol_validation_report():
    from aaba.validation import validate_symbols
    r = validate_symbols()
    assert set(r["students"]) == {"S1", "S2", "S3"} and r["simulated"] is True
    assert all(len(s["symbols"]) == 12 for s in r["students"].values())
    assert set(r["reserves"]) == {"MRVL", "LRCX", "ANET"}
    assert all(Path(f).exists() for f in r["files"])


def test_backtest_runs_separately_from_orders():
    from aaba.backtest import run_backtest
    r = run_backtest("S1", tune=False)
    assert r["simulated_data"] is True and r["batch_requests"] == 1
    assert set(r["test_results"]) >= {"default_settings", "buy_and_hold", "spy"}
    assert len(r["test_dates"]) == 126
    assert db.q("SELECT * FROM orders") == []
    assert all(o == {} for o in (brokers.FakeBroker(s)._load()["orders"] for s in load_students().students))


# ------------------------------------------------------------------ real OpenAI-compatible HTTP path
def _point_llm_at(monkeypatch, url):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_MODEL", "llama3.2:1b")
    monkeypatch.setenv("LLM_BASE_URL", url)
    monkeypatch.setenv("LLM_API_KEY", "not-needed")
    monkeypatch.setenv("LLM_TIMEOUT", "30")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")


def test_openai_compatible_endpoint_drives_the_agent(monkeypatch):
    """Same settings shape as an Ollama setup, but against a local fake server: proves the real
    CrewAI HTTP client, model naming and multi-step tool calling work end to end."""
    from agents import trading_crew as tc
    from fake_openai_server import FakeOpenAIServer
    with FakeOpenAIServer() as srv:
        _point_llm_at(monkeypatch, srv.url)
        monkeypatch.setenv("AABA_SYNTHETIC_NOW", BUSY_DAY)
        res = tc.run_workflow("paper", "test", use_llm=True)
    assert res["engine"] == "crewai+mcp" and res["status"] == "DONE", res
    assert len(srv.requests) >= 10
    assert all(r["model"] == "llama3.2:1b" for r in srv.requests)   # model name sent unchanged
    assert any(r.get("tools") for r in srv.requests)
    assert res["safety_net_actions"] == 0
    sigs = db.q("SELECT * FROM signals WHERE run_id=? AND action='BUY'", (res["run_id"],))
    assert sigs and all(s["outcome"] for s in sigs)


def test_llm_probe_detects_models_that_cannot_call_tools(monkeypatch):
    from aaba import llm as llm_factory
    from fake_openai_server import FakeOpenAIServer
    with FakeOpenAIServer() as good:
        _point_llm_at(monkeypatch, good.url)
        assert llm_factory.tool_call_probe(llm_factory.build())["ok"] is True
    with FakeOpenAIServer(text_only=True) as bad:
        _point_llm_at(monkeypatch, bad.url)
        r = llm_factory.tool_call_probe(llm_factory.build())
        assert r["ok"] is False and "no tool call" in r["detail"]
    assert llm_factory.looks_small("llama3.2:1b") and not llm_factory.looks_small("openai/gpt-oss-120b")


def test_text_only_model_still_completes_safely(monkeypatch):
    """A model that never calls tools: the agent achieves nothing, the safety net does it all."""
    from agents import trading_crew as tc
    from fake_openai_server import FakeOpenAIServer
    with FakeOpenAIServer(text_only=True) as srv:
        _point_llm_at(monkeypatch, srv.url)
        monkeypatch.setenv("AABA_SYNTHETIC_NOW", BUSY_DAY)
        res = tc.run_workflow("paper", "test", use_llm=True)
    sigs = db.q("SELECT * FROM signals WHERE run_id=? AND action IN ('BUY','SELL')", (res["run_id"],))
    assert sigs and res["safety_net_actions"] == len(sigs)
    assert all(s["outcome"] for s in db.q("SELECT * FROM signals WHERE run_id=?", (res["run_id"],)))
