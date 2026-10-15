"""A scripted stand-in LLM for automated tests ONLY.

It plugs into CrewAI exactly like a real model (native tool calling), so the tests
prove that the real CrewAI agent loop calls the real MCP tools. It does not think:
it follows the task steps literally. It is never used outside tests.

Options:
  skip_symbols  BUY/SELL symbols it "forgets" to act on (to test the safety net)
  veto_symbols  BUY symbols it declines with record_hold
  delay_seconds pause before every answer (to test the time budget)
"""
from __future__ import annotations

import json
import re
from typing import Any

from crewai.llms.base_llm import BaseLLM


class ScriptedLLM(BaseLLM):
    skip_symbols: list[str] = []
    veto_symbols: list[str] = []
    delay_seconds: float = 0.0
    calls: int = 0

    def supports_function_calling(self) -> bool:
        return True

    def get_context_window_size(self) -> int:
        return 128000

    @staticmethod
    def _content(m) -> str:
        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        if isinstance(c, list):
            return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        return c or ""

    def call(self, messages, tools=None, callbacks=None, available_functions=None, from_task=None,
             from_agent=None, response_model=None) -> Any:
        self.calls += 1
        if self.delay_seconds:
            import time
            time.sleep(self.delay_seconds)
        msgs = messages if isinstance(messages, list) else [{"role": "user", "content": messages}]
        names = {t["function"]["name"] for t in (tools or []) if isinstance(t, dict) and "function" in t}
        task_text = " ".join(self._content(m) for m in msgs if (m.get("role") if isinstance(m, dict) else "") == "user")
        called = []
        for m in msgs:
            if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
                called += [tc["function"]["name"] for tc in m["tool_calls"]]
        tool_results = [self._content(m) for m in msgs if isinstance(m, dict) and m.get("role") == "tool"]
        run_id = (re.search(r"run-\d{8}-\d{6}-[0-9a-f]{4}", task_text) or [None])[0]

        def tc(i, name, args):
            return {"id": f"call_{self.calls}_{i}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}

        if "get_signal_count" in names:          # the llm-test probe
            if not tool_results:
                return [tc(0, "get_signal_count", {"student_id": "S2"})]
            return "S2 has the number of BUY signals returned by the tool."

        if "prepare_market_data" in names:
            if "prepare_market_data" not in called:
                return [tc(0, "prepare_market_data", {"run_id": run_id})]
            return "Market data loaded and checked (scripted test model)."

        if "evaluate_student" in names:
            sid = (re.search(r'student_id="(S\d+)"', task_text) or [None, None])[1]
            if "process_approved_sells" not in called:
                return [tc(0, "process_approved_sells", {"student_id": sid, "run_id": run_id})]
            if "evaluate_student" not in called:
                return [tc(0, "evaluate_student", {"run_id": run_id, "student_id": sid})]
            if not any(n in called for n in ("submit_buy", "create_sell_approval", "record_hold")):
                ev = None
                for r in tool_results:
                    try:
                        d = json.loads(r)
                    except ValueError:
                        continue
                    if isinstance(d, dict) and "signals" in d:
                        ev = d
                calls = []
                for s in (ev or {}).get("signals", []):
                    if s["symbol"] in self.skip_symbols:
                        continue
                    if s["action"] == "BUY":
                        if s["symbol"] in self.veto_symbols:
                            calls.append(tc(len(calls), "record_hold",
                                            {"signal_id": s["signal_id"], "reason": "test veto"}))
                        else:
                            calls.append(tc(len(calls), "submit_buy",
                                            {"signal_id": s["signal_id"], "reason": "rules engine BUY"}))
                    elif s["action"] == "SELL":
                        calls.append(tc(len(calls), "create_sell_approval",
                                        {"signal_id": s["signal_id"], "reason": "rules engine SELL"}))
                if calls:
                    return calls
            return f"| Symbol | Signal | Result |\n|---|---|---|\n(scripted test summary for {sid})"
        return "Group summary written by the scripted test model."
