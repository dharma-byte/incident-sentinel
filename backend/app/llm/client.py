"""LLM access: Groq with a local Ollama fallback (Section 2 of the build spec).

Local development defaults to Ollama so no API key is needed; deployment sets
``LLM_PROVIDER=groq`` and ``GROQ_API_KEY``. Both providers are asked for strict
JSON, and every call is retried with exponential backoff -- Groq's free tier
caps requests per minute, and a cold Ollama model can time out on first use.

Tests use :class:`StubLLMClient`, so the agent suite never needs a live model.
"""

from __future__ import annotations

import json
import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Any, Callable

import httpx

from app.config import settings

logger = logging.getLogger("incident_sentinel.llm")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
OLLAMA_MODEL = "llama3.2:3b"

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.5
REQUEST_TIMEOUT = 180.0


class LLMError(RuntimeError):
    """Raised when a provider cannot produce a usable response."""


def _retry(call: Callable[[], str], *, what: str) -> str:
    """Run ``call`` with exponential backoff and jitter."""
    last: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return call()
        except (httpx.HTTPError, LLMError) as exc:
            last = exc
            if attempt == MAX_ATTEMPTS:
                break
            delay = BACKOFF_BASE_SECONDS**attempt + random.uniform(0, 0.4)
            logger.warning("%s failed (attempt %d/%d): %s -- retrying in %.1fs",
                           what, attempt, MAX_ATTEMPTS, exc, delay)
            time.sleep(delay)
    raise LLMError(f"{what} failed after {MAX_ATTEMPTS} attempts: {last}") from last


def _extract_json(raw: str) -> dict[str, Any]:
    """Parse a JSON object out of a model response.

    Small models like to wrap JSON in prose or fences, so fall back to the
    outermost braces before giving up.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :] if "{" in text else text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"model did not return JSON: {raw[:200]}") from exc
    raise LLMError(f"model did not return JSON: {raw[:200]}")


class LLMClient(ABC):
    """Minimal interface the agents depend on."""

    name: str = "llm"
    model: str = ""

    @abstractmethod
    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str:
        """Return the raw text completion."""

    def complete_json(self, *, system: str, user: str, temperature: float = 0.2) -> dict[str, Any]:
        """Return the completion parsed as a JSON object."""
        return _extract_json(self.complete(system=system, user=user, temperature=temperature))

    def available(self) -> bool:
        """Cheap readiness probe used by /health and the triage endpoint."""
        return True


class OllamaClient(LLMClient):
    """Local Ollama server (no API key, no rate limit)."""

    name = "ollama"

    def __init__(self, base_url: str | None = None, model: str = OLLAMA_MODEL) -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model

    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str:
        def call() -> str:
            response = httpx.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": temperature},
                },
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            content = response.json().get("message", {}).get("content", "")
            if not content:
                raise LLMError("ollama returned an empty message")
            return content

        return _retry(call, what=f"ollama:{self.model}")

    def available(self) -> bool:
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=5.0)
            response.raise_for_status()
            models = [m.get("name", "") for m in response.json().get("models", [])]
            return any(m.startswith(self.model.split(":")[0]) for m in models)
        except httpx.HTTPError:
            return False


class GroqClient(LLMClient):
    """Groq free tier (OpenAI-compatible chat completions)."""

    name = "groq"

    def __init__(self, api_key: str | None = None, model: str = GROQ_MODEL) -> None:
        self.api_key = api_key if api_key is not None else settings.groq_api_key
        self.model = model

    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str:
        if not self.api_key:
            raise LLMError("GROQ_API_KEY is not set")

        def call() -> str:
            response = httpx.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temperature,
                    "response_format": {"type": "json_object"},
                },
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 429:  # rate limited -- let the backoff handle it
                raise LLMError("groq rate limit hit (429)")
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]

        return _retry(call, what=f"groq:{self.model}")

    def available(self) -> bool:
        return bool(self.api_key)


class StubLLMClient(LLMClient):
    """Deterministic stand-in so the agent tests need no model.

    Responses are keyed by agent name; anything unknown gets a generic object.
    """

    name = "stub"
    model = "stub"

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[dict[str, str]] = []

    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str:
        self.calls.append({"system": system, "user": user})
        # Longest match wins: prompts mention each other's agents by name, so a
        # first-match rule would hand back the wrong canned response.
        matches = [key for key in self.responses if key in system or key in user]
        if matches:
            return json.dumps(self.responses[max(matches, key=len)])
        return json.dumps({"summary": "stub response", "reasoning": "stub reasoning"})


def get_llm_client(provider: str | None = None) -> LLMClient:
    """Build the configured client, falling back to Ollama when Groq has no key."""
    choice = (provider or settings.llm_provider).lower()
    if choice == "groq":
        client = GroqClient()
        if not client.available():
            logger.warning("LLM_PROVIDER=groq but GROQ_API_KEY is empty; falling back to ollama")
            return OllamaClient()
        return client
    if choice == "stub":
        return StubLLMClient()
    return OllamaClient()
