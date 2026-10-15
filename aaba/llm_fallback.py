
"""Runtime LLM fallback for CrewAI 1.15.x.

Attempts providers in order. A model-call failure moves to the next provider;
this wrapper never directly executes or retries a trading tool.
"""
from __future__ import annotations

import os
import threading
from typing import Any

from crewai import BaseLLM, LLM
from crewai.utilities.exceptions.context_window_exceeding_exception import (
    LLMContextLengthExceededError,
)
from pydantic import PrivateAttr

from aaba.llm import LLMConfigError, openai_compatible_llm

GROQ_BASE = "https://api.groq.com/openai/v1"

DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-120b",
    "gemini": "gemini-3.6-flash",
    "ollama": "gpt-oss:120b-cloud",
}

PROVIDER_ORDER = ("groq", "gemini", "ollama")


def _build_provider(provider: str, temperature: float = 0.1):
    """Build a single CrewAI LLM client, or explain why it is unavailable."""
    try:
        timeout = float(os.getenv("LLM_TIMEOUT", "120") or 120)
        retries = int(os.getenv("LLM_MAX_RETRIES", "2") or 2)
    except ValueError as exc:
        raise LLMConfigError(
            f"LLM_TIMEOUT / LLM_MAX_RETRIES must be numbers ({exc})"
        ) from exc

    common = {
        "temperature": temperature,
        "timeout": timeout,
        "max_retries": retries,
    }

    if provider == "groq":
        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key:
            raise LLMConfigError("GROQ_API_KEY is missing")

        model = (
            os.getenv("GROQ_MODEL", "").strip()
            or DEFAULT_MODELS["groq"]
        )
        return openai_compatible_llm(model, GROQ_BASE, key, **common)

    if provider == "gemini":
        key = (
            os.getenv("GEMINI_API_KEY", "").strip()
            or os.getenv("GOOGLE_API_KEY", "").strip()
        )
        if not key:
            raise LLMConfigError(
                "GEMINI_API_KEY or GOOGLE_API_KEY is missing"
            )

        model = os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODELS["gemini"]
        return LLM(model=f"gemini/{model}", api_key=key, **common)

    if provider == "ollama":
        base_url = (
            os.getenv("OLLAMA_BASE_URL", "").strip()
            or os.getenv("LLM_BASE_URL", "").strip()
        )
        if not base_url:
            raise LLMConfigError(
                "OLLAMA_BASE_URL or LLM_BASE_URL is required for Ollama"
            )

        model = os.getenv("OLLAMA_MODEL", "").strip() or DEFAULT_MODELS["ollama"]
        return openai_compatible_llm(
            model, base_url, os.getenv("OLLAMA_API_KEY", "ollama").strip() or "ollama", **common
        )

    raise LLMConfigError(f"Unsupported fallback provider: {provider}")


class FallbackLLM(BaseLLM):
    """Delegate model calls to providers in order until one succeeds."""

    _clients: list[Any] = PrivateAttr(default_factory=list)
    _provider_names: list[str] = PrivateAttr(default_factory=list)
    _lock: Any = PrivateAttr(default_factory=threading.RLock)
    _active_index: int = PrivateAttr(default=0)

    def __init__(self, clients: list[Any], provider_names: list[str]):
        if not clients or len(clients) != len(provider_names):
            raise ValueError(
                "Provide matching, non-empty clients and provider_names"
            )

        super().__init__(
            model=f"fallback[{','.join(provider_names)}]",
            temperature=0.1,
        )
        self._clients = clients
        self._provider_names = provider_names

    def supports_function_calling(self) -> bool:
        return all(
            getattr(client, "supports_function_calling", lambda: True)()
            for client in self._clients
        )

    def get_context_window_size(self) -> int:
        return 128000

    def call(
        self,
        messages,
        tools=None,
        callbacks=None,
        available_functions=None,
        from_task=None,
        from_agent=None,
        response_model=None,
    ):
        kwargs = {
            "tools": tools,
            "callbacks": callbacks,
            "available_functions": available_functions,
            "from_task": from_task,
            "from_agent": from_agent,
            "response_model": response_model,
        }

        failures = []

        # Serialize calls so simultaneous requests cannot race provider selection.
        with self._lock:
            start = self._active_index
            order = (
                list(range(start, len(self._clients)))
                + list(range(0, start))
            )

            for index in order:
                provider = self._provider_names[index]

                try:
                    result = self._clients[index].call(messages, **kwargs)
                    self._active_index = index
                    return result

                except (KeyboardInterrupt, SystemExit):
                    raise

                except LLMContextLengthExceededError:
                    # CrewAI handles this itself (it summarizes and retries).
                    raise

                except Exception as exc:
                    if available_functions:
                        # The provider may already have run a tool inside this
                        # call; never repeat it on another provider.
                        raise
                    # Includes ValueError: CrewAI raises it for a retired or
                    # unknown model (HTTP 404), which another provider can serve.
                    # Each provider's error is kept in the final message.
                    failures.append(
                        f"{provider}: {type(exc).__name__}: {str(exc)[:180]}"
                    )

        raise RuntimeError(
            "All configured LLM providers failed: " + " | ".join(failures)
        )



def build_fallback_llm(temperature: float = 0.1):
    """Build the cloud fallback chain, or a single OpenAI-compatible LLM."""
    primary = os.getenv("LLM_PROVIDER", "groq").strip().lower()

    # Preserve local OpenAI-compatible endpoints and integration-test support.
    if primary == "openai_compatible":
        from aaba.llm import build as build_llm

        return build_llm(temperature=temperature), ["openai_compatible"], []

    if primary not in PROVIDER_ORDER:
        raise LLMConfigError(
            "Set LLM_PROVIDER to groq, gemini, ollama, or openai_compatible"
        )

    ordered = (primary,) + tuple(
        provider for provider in PROVIDER_ORDER if provider != primary
    )


    clients = []
    provider_names = []
    setup_errors = []

    for provider in ordered:
        try:
            clients.append(_build_provider(provider, temperature))
            provider_names.append(provider)
        except Exception as exc:  # noqa: BLE001
            # e.g. a missing provider SDK (ImportError) must not take down the other providers
            setup_errors.append(f"{provider}: {type(exc).__name__}: {str(exc)[:200]}")

    if not clients:
        raise LLMConfigError(
            "No LLM providers could be configured. " + " | ".join(setup_errors)
        )

    return (
        FallbackLLM(clients=clients, provider_names=provider_names),
        provider_names,
        setup_errors,
    )
