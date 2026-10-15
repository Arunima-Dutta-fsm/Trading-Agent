"""One shared LLM for the whole system, set in .env.

LLM_PROVIDER   groq (default) | gemini | openrouter | ollama | openai | openai_compatible
LLM_MODEL      the provider's model id, e.g. openai/gpt-oss-120b on Groq
LLM_BASE_URL   only for openai_compatible
Keys: GROQ_API_KEY, OPENROUTER_API_KEY, OPENAI_API_KEY, or LLM_API_KEY for openai_compatible.
Optional fallback: LLM_FALLBACK_PROVIDER / LLM_FALLBACK_MODEL (used only if the
primary cannot be set up). LLM_TIMEOUT (seconds per call, default 120) and LLM_MAX_RETRIES
(default 2) tune slow local models.

Model size matters: the agent has to call tools over several steps. Very small models
(1B-3B, e.g. llama3.2:1b) usually cannot do this reliably. Use `python main.py llm-test`
to check a model before relying on it.

Model names are not checked against the provider here. If a provider retires a
model, change LLM_MODEL in .env.
"""
from __future__ import annotations

import os

GROQ_BASE = "https://api.groq.com/openai/v1"
DEFAULTS = {
    "groq": "openai/gpt-oss-120b",
    "gemini": "gemini-2.5-flash",
    "openrouter": "openai/gpt-oss-120b:free",
    "ollama": "qwen3:8b",
    "openai": "gpt-4o-mini",
}


class LLMConfigError(RuntimeError):
    pass


def drop_empty_required(schema):
    """Remove every `"required": []` from a JSON schema, in place. Returns the schema.

    CrewAI's strict-mode sanitizer adds `"required": []` to tools that take no arguments
    (for example check_accounts). That is valid JSON Schema and OpenAI accepts it, but
    Groq's strict validator then reports "'required' present but 'properties' is missing"
    and rejects the whole request. An empty `required` list means the same as no list.
    """
    if isinstance(schema, dict):
        if schema.get("required") == []:
            del schema["required"]
        for value in schema.values():
            drop_empty_required(value)
    elif isinstance(schema, list):
        for item in schema:
            drop_empty_required(item)
    return schema


def openai_compatible_llm(model: str, base_url: str, api_key: str, **common):
    """CrewAI's native OpenAI client for a non-OpenAI endpoint (Groq, Ollama, LLM_BASE_URL).

    `model` is the id exactly as the endpoint expects it (e.g. 'openai/gpt-oss-120b' on Groq).
    Tool schemas are cleaned with drop_empty_required before they are sent."""
    from crewai.llms.providers.openai.completion import OpenAICompletion

    class OpenAICompatibleCompletion(OpenAICompletion):
        def _convert_tools_for_interference(self, tools):
            converted = super()._convert_tools_for_interference(tools)
            for tool in converted:
                drop_empty_required(tool.get("function", {}).get("parameters"))
            return converted

    return OpenAICompatibleCompletion(model=model, provider="openai", custom_openai=True,
                                      base_url=base_url, api_key=api_key, **common)


def describe(prefix: str = "LLM") -> tuple[str, str]:
    provider = os.getenv(f"{prefix}_PROVIDER", "groq" if prefix == "LLM" else "").strip().lower()
    model = os.getenv(f"{prefix}_MODEL", DEFAULTS.get(provider, "")).strip()
    return provider, model


def build(prefix: str = "LLM", temperature: float = 0.1):
    """Return a CrewAI LLM object, or raise LLMConfigError with a plain reason."""
    from crewai import LLM

    provider, model = describe(prefix)
    if not provider or not model:
        raise LLMConfigError(f"{prefix}_PROVIDER / {prefix}_MODEL not set")
    # LLM_TIMEOUT: seconds per model call. A local model on a laptop CPU can need 60-180 s per call.
    try:
        timeout = float(os.getenv("LLM_TIMEOUT", "120") or 120)
        retries = int(os.getenv("LLM_MAX_RETRIES", "2") or 2)
    except ValueError as e:
        raise LLMConfigError(f"LLM_TIMEOUT / LLM_MAX_RETRIES must be numbers ({e})")
    common = {"temperature": temperature, "timeout": timeout, "max_retries": retries}

    if provider == "groq":
        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key:
            raise LLMConfigError("GROQ_API_KEY is missing in .env")
        # Groq speaks the OpenAI API; the model id is sent unchanged (e.g. 'openai/gpt-oss-120b').
        return openai_compatible_llm(model, GROQ_BASE, key, **common)
    
    if provider == "gemini":
        key = (
            os.getenv("GEMINI_API_KEY", "").strip()
            or os.getenv("GOOGLE_API_KEY", "").strip()
        )
        if not key:
            raise LLMConfigError(
                "GEMINI_API_KEY or GOOGLE_API_KEY is missing in .env"
            )
        return LLM(
            model=f"gemini/{model}",
            api_key=key,
            **common,
        )
    
    if provider == "openrouter":
        key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise LLMConfigError("OPENROUTER_API_KEY is missing in .env")
        return LLM(model=f"openrouter/{model}", api_key=key, **common)
    
    if provider == "ollama":
        return LLM(model=f"ollama/{model}", **common)
    
    if provider == "openai":
        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            raise LLMConfigError("OPENAI_API_KEY is missing in .env")
        return LLM(model=model, api_key=key, **common)
    
    if provider == "openai_compatible":
        base = os.getenv("LLM_BASE_URL", "").strip()
        if not base:
            raise LLMConfigError("LLM_BASE_URL is required for openai_compatible")
        return openai_compatible_llm(model, base, os.getenv("LLM_API_KEY", "none"), **common)
    raise LLMConfigError(f"unsupported LLM_PROVIDER '{provider}'")


SMALL_MODEL_HINTS = (":1b", ":1.5b", ":2b", ":3b", "-1b", "-3b", "1b-", "3b-", "0.5b", "270m", "135m", "360m")


def looks_small(model: str) -> bool:
    m = (model or "").lower()
    return any(h in m for h in SMALL_MODEL_HINTS)


def tool_call_probe(llm) -> dict:
    """Ask the model to call one simple tool. Returns {"ok", "seconds", "detail"}.
    This is the same kind of call the agent makes on every step."""
    import json
    import time
    tool = {"type": "function", "function": {
        "name": "get_signal_count", "description": "Return how many BUY signals a student has.",
        "parameters": {"type": "object", "properties": {"student_id": {"type": "string"}},
                       "required": ["student_id"]}}}
    msgs = [{"role": "system", "content": "You must use the provided tool to answer."},
            {"role": "user", "content": "How many BUY signals does student S2 have? Call get_signal_count."}]
    t0 = time.perf_counter()
    try:
        out = llm.call(msgs, tools=[tool])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "seconds": round(time.perf_counter() - t0, 1),
                "detail": f"call failed: {type(e).__name__}: {str(e)[:300]}"}
    secs = round(time.perf_counter() - t0, 1)
    calls = out if isinstance(out, list) else []
    for c in calls:
        fn = c.get("function", {}) if isinstance(c, dict) else getattr(c, "function", None)
        if fn is None and getattr(c, "function_call", None) is not None:   # Gemini returns Part objects
            fn = {"name": c.function_call.name, "arguments": dict(c.function_call.args or {})}
        name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
        args = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
        if name == "get_signal_count":
            try:
                parsed = json.loads(args) if isinstance(args, str) else (args or {})
            except ValueError:
                parsed = {}
            if parsed.get("student_id") == "S2":
                return {"ok": True, "seconds": secs, "detail": "model called the tool with the right argument"}
            return {"ok": False, "seconds": secs, "detail": f"tool called with wrong arguments: {args}"}
    return {"ok": False, "seconds": secs, "detail": f"no tool call; model replied with text: {str(out)[:200]}"}
