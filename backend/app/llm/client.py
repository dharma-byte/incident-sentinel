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
import re
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable

import httpx

from app.config import settings

logger = logging.getLogger("incident_sentinel.llm")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# The build spec named Llama 3.1/3.3, but Groq has since retired those ids;
# gpt-oss-120b is the strongest reasoning model its free tier now serves.
# Check https://api.groq.com/openai/v1/models if this 404s in future.
GROQ_MODEL = "openai/gpt-oss-120b"
OLLAMA_MODEL = "llama3.2:3b"

MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.5

# Ollama gets a long timeout because a cold model load on CPU can take minutes;
# Groq answers in seconds, so a long wait there only masks a problem.
OLLAMA_TIMEOUT = 300.0
GROQ_TIMEOUT = 60.0

# Cap generation: the agents want a summary and a few sentences of reasoning,
# and an unbounded local model will happily spend a minute writing more. Too
# low is its own failure though -- JSON truncated mid-object is rejected by
# Groq's validator -- so this is sized to fit the largest schema with room.
MAX_OUTPUT_TOKENS = 800


# Groq's free tier meters tokens per minute across the whole organisation. A
# four-agent triage costs roughly 7k, so firing the calls back to back starves
# the later ones -- and the root-cause agent, with the largest prompt, is the
# one that loses. Waiting for headroom beats retrying into a closed window.
GROQ_TOKENS_PER_MINUTE = 8000
TPM_SAFETY_MARGIN = 0.9


class LLMError(RuntimeError):
    """Raised when a provider cannot produce a usable response."""


class TokenBudget:
    """Sliding-window token accounting, so we wait rather than get refused.

    Only an estimate -- the server's count is authoritative and a 429 is still
    handled -- but it keeps a normal pipeline run inside the window.
    """

    def __init__(self, limit_per_minute: int, window_seconds: float = 60.0) -> None:
        self.limit = int(limit_per_minute * TPM_SAFETY_MARGIN)
        self.window = window_seconds
        self._spent: list[tuple[float, int]] = []
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        self._spent = [(ts, n) for ts, n in self._spent if ts > cutoff]

    def used(self) -> int:
        with self._lock:
            self._prune(time.monotonic())
            return sum(n for _, n in self._spent)

    def sync(self, server_used: int) -> None:
        """Adopt the provider's own usage figure.

        Our estimate only counts what this process spent, but the quota is
        per-organisation and spans processes. When a 429 tells us what the
        server actually counted, believe it.
        """
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            shortfall = server_used - sum(n for _, n in self._spent)
            if shortfall > 0:
                self._spent.append((now, shortfall))

    def reserve(self, tokens: int) -> float:
        """Block until ``tokens`` fit in the window; return seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._prune(now)
                spent = sum(n for _, n in self._spent)
                if spent + tokens <= self.limit or not self._spent:
                    self._spent.append((now, tokens))
                    return waited
                # Wait for the oldest reservation to age out of the window.
                oldest = min(ts for ts, _ in self._spent)
                delay = max(oldest + self.window - now, 0.1) + 0.2
            logger.info("token budget: %d/%d used, waiting %.1fs for headroom",
                        spent, self.limit, delay)
            time.sleep(delay)
            waited += delay


def estimate_tokens(text: str) -> int:
    """Rough token count. Four characters per token is close enough here."""
    return len(text) // 4 + 1


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


_USED_TOKENS = re.compile(r"Used (\d+)")
_RETRY_HINT = re.compile(r"try again in ([0-9.]+)\s*(ms|s)\b")


def _parse_used_tokens(detail: str) -> int:
    """Tokens the provider says we have already spent this window."""
    match = _USED_TOKENS.search(detail)
    return int(match.group(1)) if match else 0


def _parse_retry_delay(detail: str, retry_after: str | None) -> float:
    """How long the provider wants us to wait, from the header or the message."""
    if retry_after:
        try:
            return min(float(retry_after), 30.0)
        except ValueError:
            pass
    match = _RETRY_HINT.search(detail)
    if match:
        value = float(match.group(1))
        seconds = value / 1000 if match.group(2) == "ms" else value
        return min(seconds + 0.3, 30.0)
    return 2.0


def _error_detail(response: httpx.Response) -> str:
    """The provider's own error message, when it sends one."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or payload)[:300]


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
                    "options": {
                        "temperature": temperature,
                        "num_predict": MAX_OUTPUT_TOKENS,
                    },
                },
                timeout=OLLAMA_TIMEOUT,
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

    def __init__(
        self,
        api_key: str | None = None,
        model: str = GROQ_MODEL,
        budget: TokenBudget | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.groq_api_key
        self.model = model
        self.budget = budget if budget is not None else _groq_budget

    def complete(self, *, system: str, user: str, temperature: float = 0.2) -> str:
        if not self.api_key:
            raise LLMError("GROQ_API_KEY is not set")

        def _complete_without_json_mode() -> str:
            retry = httpx.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system + "\n\nReturn only JSON."},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temperature,
                    "max_tokens": MAX_OUTPUT_TOKENS,
                },
                timeout=GROQ_TIMEOUT,
            )
            if retry.status_code >= 400:
                raise LLMError(f"groq returned {retry.status_code}: {_error_detail(retry)}")
            return retry.json()["choices"][0]["message"]["content"]

        def call() -> str:
            # Claim the window before spending it, so the later agents in a
            # pipeline are not starved by the earlier ones.
            self.budget.reserve(
                estimate_tokens(system) + estimate_tokens(user) + MAX_OUTPUT_TOKENS
            )
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
                    "max_tokens": MAX_OUTPUT_TOKENS,
                    "response_format": {"type": "json_object"},
                },
                timeout=GROQ_TIMEOUT,
            )
            if response.status_code == 429:
                # The free tier meters tokens per minute across the whole
                # organisation, so our local estimate can be behind. The 429
                # body carries both the server's usage and how long to wait --
                # adopt both rather than guessing our way through the backoff.
                detail = _error_detail(response)
                self.budget.sync(_parse_used_tokens(detail))
                time.sleep(_parse_retry_delay(detail, response.headers.get("retry-after")))
                raise LLMError(f"groq rate limit hit (429): {detail}")
            if response.status_code >= 400:
                # Carry the server's explanation: a bare status code turns a
                # one-line fix (a rejected parameter) into a guessing game.
                detail = _error_detail(response)
                if response.status_code == 400 and "json" in detail.lower():
                    # Strict JSON mode rejects a response it could not validate.
                    # Our own parser is tolerant, so ask again without the
                    # constraint rather than losing the whole call.
                    return _complete_without_json_mode()
                raise LLMError(f"groq returned {response.status_code}: {detail}")
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


_groq_budget = TokenBudget(GROQ_TOKENS_PER_MINUTE)


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
