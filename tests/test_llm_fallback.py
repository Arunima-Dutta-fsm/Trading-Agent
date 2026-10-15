
import pytest

from aaba.llm_fallback import FallbackLLM


class FakeClient:
    """Simulates a provider without making network requests."""

    def __init__(self, name, outcome, calls):
        self.name = name
        self.outcome = outcome
        self.calls = calls

    def supports_function_calling(self):
        return True

    def call(self, messages, **kwargs):
        self.calls.append((self.name, messages, kwargs))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_fallback_order_groq_to_gemini_to_ollama():
    calls = []
    expected_result = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "test_tool",
                "arguments": "{}",
            },
        }
    ]

    llm = FallbackLLM(
        clients=[
            FakeClient("groq", TimeoutError("simulated timeout"), calls),
            FakeClient("gemini", ConnectionError("simulated outage"), calls),
            FakeClient("ollama", expected_result, calls),
        ],
        provider_names=["groq", "gemini", "ollama"],
    )

    messages = [{"role": "user", "content": "test"}]
    tools = [{"type": "function", "function": {"name": "test_tool"}}]

    result = llm.call(messages, tools=tools, from_task="task-marker")

    assert result == expected_result
    assert [entry[0] for entry in calls] == [
        "groq", "gemini", "ollama"
    ]
    assert calls[-1][1] is messages
    assert calls[-1][2]["tools"] is tools
    assert calls[-1][2]["from_task"] == "task-marker"


def test_all_providers_failed_reports_each_provider():
    calls = []
    llm = FallbackLLM(
        clients=[
            FakeClient("groq", TimeoutError("offline"), calls),
            FakeClient("gemini", RuntimeError("simulated failure"), calls),
            FakeClient("ollama", ConnectionError("server unavailable"), calls),
        ],
        provider_names=["groq", "gemini", "ollama"],
    )

    with pytest.raises(
        RuntimeError, match="All configured LLM providers failed"
    ) as exc:
        llm.call("test")

    message = str(exc.value)
    assert "groq: TimeoutError" in message
    assert "gemini: RuntimeError" in message
    assert "ollama: ConnectionError" in message
    assert [entry[0] for entry in calls] == [
        "groq", "gemini", "ollama"
    ]


def test_retired_model_falls_back_to_next_provider():
    # CrewAI turns an HTTP 404 "model not found" into ValueError.
    calls = []
    llm = FallbackLLM(
        clients=[
            FakeClient("groq", ValueError("Model 'old-model' not found"), calls),
            FakeClient("gemini", "answer from gemini", calls),
        ],
        provider_names=["groq", "gemini"],
    )

    assert llm.call("test") == "answer from gemini"
    assert [entry[0] for entry in calls] == ["groq", "gemini"]


def test_context_length_error_is_left_to_crewai():
    from crewai.utilities.exceptions.context_window_exceeding_exception import (
        LLMContextLengthExceededError,
    )

    calls = []
    llm = FallbackLLM(
        clients=[
            FakeClient("groq", LLMContextLengthExceededError("too long"), calls),
            FakeClient("gemini", "must not be used", calls),
        ],
        provider_names=["groq", "gemini"],
    )

    with pytest.raises(LLMContextLengthExceededError):
        llm.call("test")
    assert [entry[0] for entry in calls] == ["groq"]


def test_no_fallback_when_provider_may_have_run_a_tool():
    calls = []
    llm = FallbackLLM(
        clients=[
            FakeClient("groq", RuntimeError("failed after tool ran"), calls),
            FakeClient("gemini", "must not be used", calls),
        ],
        provider_names=["groq", "gemini"],
    )

    with pytest.raises(RuntimeError, match="failed after tool ran"):
        llm.call("test", available_functions={"submit_buy": lambda **_: None})
    assert [entry[0] for entry in calls] == ["groq"]


# ---------------------------------------------------------------- real HTTP, real MCP schemas
MSGS = [{"role": "user", "content": "Call check_accounts."}]


def _called(out) -> str:
    assert isinstance(out, list) and out, out
    return out[0].function.name


def _mcp_openai_tools():
    """The 14 MCP tools exactly as CrewAI's agent hands them to the LLM."""
    from crewai.utilities.agent_utils import convert_tools_to_openai_schema
    from crewai_tools import MCPServerAdapter

    from agents.trading_crew import _server_params

    with MCPServerAdapter(_server_params(), connect_timeout=60) as tools:
        openai_tools, _, _ = convert_tools_to_openai_schema(list(tools))
    assert len(openai_tools) == 14
    return openai_tools


def test_groq_receives_valid_schema_for_every_mcp_tool(monkeypatch):
    """Regression: CrewAI's strict sanitizer adds "required": [] to check_accounts, which Groq
    rejects with "'required' present but 'properties' is missing" (HTTP 400)."""
    from crewai import LLM
    from fake_openai_server import FakeOpenAIServer, groq_schema_error

    from aaba import llm_fallback

    tools = _mcp_openai_tools()
    with FakeOpenAIServer(groq_strict=True) as groq:
        # Negative control: CrewAI's stock client reproduces the production error.
        stock = LLM(model="openai/openai/gpt-oss-120b", custom_openai=True, base_url=groq.url,
                    api_key="x", max_retries=0)
        with pytest.raises(Exception, match="'required' present but 'properties' is missing"):
            stock.call(MSGS, tools=tools)

        monkeypatch.setattr(llm_fallback, "GROQ_BASE", groq.url)
        monkeypatch.setenv("LLM_MAX_RETRIES", "0")
        fixed = llm_fallback._build_provider("groq")
        out = fixed.call(MSGS, tools=tools)

    assert _called(out) in {t["function"]["name"] for t in tools}
    sent = groq.requests[-1]
    assert sent["model"] == "openai/gpt-oss-120b"
    assert groq_schema_error(sent["tools"]) is None
    by_name = {t["function"]["name"]: t["function"] for t in sent["tools"]}
    assert len(by_name) == 14
    assert by_name["check_accounts"]["parameters"] == {
        "type": "object", "properties": {}, "additionalProperties": False}
    for name, fn in by_name.items():
        params = fn["parameters"]
        assert params["type"] == "object" and isinstance(params["properties"], dict), name
        assert set(params.get("required", [])) <= set(params["properties"]), name
    assert by_name["submit_buy"]["parameters"]["required"] == ["signal_id", "reason"]


def test_missing_provider_sdk_does_not_disable_the_other_providers(monkeypatch):
    from aaba import llm_fallback

    real = llm_fallback._build_provider

    def no_gemini_sdk(provider, temperature=0.1):
        if provider == "gemini":
            raise ImportError("No module named 'google.genai'")
        return real(provider, temperature)

    monkeypatch.setattr(llm_fallback, "_build_provider", no_gemini_sdk)
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GEMINI_API_KEY", "x" * 20)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9/v1")
    llm, providers, setup_errors = llm_fallback.build_fallback_llm()
    assert providers == ["groq", "ollama"]
    assert setup_errors == ["gemini: ImportError: No module named 'google.genai'"]


def test_fallback_chain_over_http_when_groq_is_down(monkeypatch):
    """Groq outage -> Gemini not configured -> Ollama answers with the check_accounts call."""
    from fake_openai_server import FakeOpenAIServer

    from aaba import llm_fallback

    tools = _mcp_openai_tools()
    with FakeOpenAIServer(fail_status=503) as groq, FakeOpenAIServer(groq_strict=True) as ollama:
        monkeypatch.setattr(llm_fallback, "GROQ_BASE", groq.url)
        monkeypatch.setenv("LLM_PROVIDER", "groq")
        monkeypatch.setenv("LLM_MAX_RETRIES", "0")
        monkeypatch.setenv("OLLAMA_BASE_URL", ollama.url)
        monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
        llm, providers, setup_errors = llm_fallback.build_fallback_llm()
        out = llm.call(MSGS, tools=tools)

    assert providers == ["groq", "ollama"]
    assert any(e.startswith("gemini:") for e in setup_errors)
    assert len(groq.requests) == 1 and len(ollama.requests) == 1
    assert ollama.requests[0]["model"] == "qwen3:8b"
    assert _called(out) in {t["function"]["name"] for t in tools}
