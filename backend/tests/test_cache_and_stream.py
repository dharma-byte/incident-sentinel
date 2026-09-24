"""Caching and SSE streaming tests (Phase 4).

Caching is exercised against :class:`InMemoryCache` so the suite needs no Redis
server; the Redis-specific behaviour that matters here is that an unreachable
server degrades to "no caching" rather than failing the request.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.cache.redis_client import (
    CACHE_VERSION,
    InMemoryCache,
    RedisCache,
    get_cache,
    set_cache,
    triage_key,
)


@pytest.fixture(autouse=True)
def reset_sse_exit_event():
    """sse-starlette caches an asyncio.Event at module level.

    TestClient spins up a fresh event loop per request, so the cached event
    ends up bound to a dead loop and the second streaming test explodes.
    Clearing it between tests is the documented workaround; a real server has
    a single loop and never hits this.
    """
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit_event = None
    yield
    AppStatus.should_exit_event = None


@pytest.fixture
def memory_cache():
    """Install an in-memory cache for the duration of a test."""
    cache = InMemoryCache()
    set_cache(cache)
    yield cache
    set_cache(None)


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #


def test_triage_key_separates_incidents_and_models():
    incident = uuid.uuid4()
    other = uuid.uuid4()

    assert triage_key(incident, "ollama", "llama3.2:3b") == triage_key(
        incident, "ollama", "llama3.2:3b"
    )
    assert triage_key(incident, "ollama", "llama3.2:3b") != triage_key(
        incident, "ollama", "llama3.2:1b"
    )
    assert triage_key(incident, "groq", "m") != triage_key(incident, "ollama", "m")
    assert triage_key(incident, "ollama", "m") != triage_key(other, "ollama", "m")
    assert CACHE_VERSION in triage_key(incident, "ollama", "m")


# --------------------------------------------------------------------------- #
# Cache behaviour
# --------------------------------------------------------------------------- #


def test_in_memory_cache_round_trips(memory_cache):
    memory_cache.set("k", {"root_cause": "x", "confidence": 0.9})
    assert memory_cache.get("k")["confidence"] == 0.9
    assert memory_cache.hits == 1

    memory_cache.delete("k")
    assert memory_cache.get("k") is None
    assert memory_cache.misses == 1


def test_cached_value_is_a_copy_not_a_reference(memory_cache):
    payload = {"candidates": [{"confidence": 0.5}]}
    memory_cache.set("k", payload)
    fetched = memory_cache.get("k")
    fetched["candidates"][0]["confidence"] = 0.1

    assert memory_cache.get("k")["candidates"][0]["confidence"] == 0.5


def test_redis_cache_degrades_when_the_server_is_unreachable():
    cache = RedisCache(url="redis://127.0.0.1:6399/0")  # nothing listens here

    assert cache.get("missing") is None  # must not raise
    cache.set("k", {"a": 1})  # must not raise
    cache.delete("k")
    assert cache.ping() is False


def test_redis_cache_rejects_a_malformed_url():
    cache = RedisCache(url="not-a-redis-url")
    assert cache.get("k") is None
    assert cache.enabled is False


def test_get_cache_is_a_singleton():
    set_cache(None)
    assert get_cache() is get_cache()
    set_cache(None)


# --------------------------------------------------------------------------- #
# Cached triage endpoint
# --------------------------------------------------------------------------- #


def simulate(client, scenario: str = "bad_deploy") -> str:
    response = client.post("/simulate", json={"scenario_type": scenario, "seed": 1337})
    return response.json()["incident_id"]


def test_second_identical_triage_is_served_from_cache(client, memory_cache):
    incident_id = simulate(client)

    first = client.post(f"/incidents/{incident_id}/triage", json={"provider": "stub"}).json()
    assert first["cached"] is False
    assert memory_cache.store, "the result should have been cached"

    second = client.post(f"/incidents/{incident_id}/triage", json={"provider": "stub"}).json()
    assert second["cached"] is True
    assert second["root_cause"] == first["root_cause"]
    assert memory_cache.hits == 1


def test_refresh_bypasses_the_cache(client, memory_cache):
    incident_id = simulate(client)
    client.post(f"/incidents/{incident_id}/triage", json={"provider": "stub"})

    refreshed = client.post(
        f"/incidents/{incident_id}/triage", json={"provider": "stub", "refresh": True}
    ).json()
    assert refreshed["cached"] is False


def test_cache_is_scoped_to_one_incident(client, memory_cache):
    first_id = simulate(client, "bad_deploy")
    second_id = simulate(client, "slow_query")

    client.post(f"/incidents/{first_id}/triage", json={"provider": "stub"})
    second = client.post(f"/incidents/{second_id}/triage", json={"provider": "stub"}).json()

    assert second["cached"] is False
    assert len(memory_cache.store) == 2


def test_triage_still_works_without_a_cache(client):
    set_cache(RedisCache(url="redis://127.0.0.1:6399/0"))  # unreachable
    try:
        incident_id = simulate(client)
        body = client.post(f"/incidents/{incident_id}/triage", json={"provider": "stub"}).json()
        assert body["status"] == "complete"
        assert body["cached"] is False
    finally:
        set_cache(None)


def test_health_reports_cache_state(client, memory_cache):
    body = client.get("/health").json()
    assert body["cache"] == "up"
    assert body["status"] == "ok"


# --------------------------------------------------------------------------- #
# SSE streaming
# --------------------------------------------------------------------------- #


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """Turn a raw SSE body into (event, payload) pairs."""
    events: list[tuple[str, dict]] = []
    name: str | None = None
    for line in text.splitlines():
        if line.startswith("event:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and name:
            payload = line.split(":", 1)[1].strip()
            try:
                events.append((name, json.loads(payload)))
            except json.JSONDecodeError:
                pass
            name = None
    return events


def test_stream_emits_a_step_per_agent_then_completes(client, memory_cache):
    incident_id = simulate(client, "memory_leak")

    with client.stream(
        "GET", f"/incidents/{incident_id}/triage/stream", params={"provider": "stub"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = parse_sse("".join(response.iter_text()))

    names = [name for name, _ in events]
    assert names[0] == "status"
    assert names.count("step") == 4
    assert names[-1] == "complete"

    steps = [payload for name, payload in events if name == "step"]
    assert [s["agent_name"] for s in steps] == [
        "log_analysis",
        "metrics_correlation",
        "root_cause",
        "fix_suggestion",
    ]
    assert all(s["summary"] for s in steps)
    assert all("evidence_refs" in s for s in steps)

    final = events[-1][1]
    assert final["status"] == "complete"
    assert final["root_cause"]


def test_stream_replays_a_cached_run_without_re_running(client, memory_cache):
    incident_id = simulate(client, "bad_deploy")
    client.post(f"/incidents/{incident_id}/triage", json={"provider": "stub"})

    with client.stream(
        "GET", f"/incidents/{incident_id}/triage/stream", params={"provider": "stub"}
    ) as response:
        events = parse_sse("".join(response.iter_text()))

    assert events[0] == ("status", {"state": "cached", "model": "stub"})
    assert [name for name, _ in events].count("step") == 4
    assert events[-1][1]["cached"] is True
    assert memory_cache.hits == 1


def test_stream_404s_for_an_unknown_incident(client, memory_cache):
    response = client.get(
        f"/incidents/{uuid.uuid4()}/triage/stream", params={"provider": "stub"}
    )
    assert response.status_code == 404
