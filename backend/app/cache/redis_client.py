"""Redis cache for completed triage results.

Re-running the pipeline for an incident that has already been triaged with the
same model costs four LLM calls and produces the same answer, so the result is
cached and replayed instead (Section 10 of the build spec).

The cache is strictly optional: if Redis is unreachable the pipeline still runs,
just uncached, and ``/health`` says so. Tests inject :class:`InMemoryCache`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import redis

from app.config import settings

logger = logging.getLogger("incident_sentinel.cache")

DEFAULT_TTL_SECONDS = 60 * 60 * 24  # a day; incidents are immutable once stored
KEY_PREFIX = "incident-sentinel:triage"
# Bump when the cached payload's shape changes, so stale entries are ignored.
CACHE_VERSION = "v1"


def triage_key(incident_id: Any, provider: str, model: str) -> str:
    """Identical requests share a key; a different model is a different request."""
    return f"{KEY_PREFIX}:{CACHE_VERSION}:{incident_id}:{provider}:{model or 'default'}"


class Cache(Protocol):
    """What the API needs from a cache."""

    enabled: bool

    def get(self, key: str) -> dict[str, Any] | None: ...
    def set(self, key: str, value: dict[str, Any], ttl: int = DEFAULT_TTL_SECONDS) -> None: ...
    def delete(self, key: str) -> None: ...
    def ping(self) -> bool: ...


class RedisCache:
    """Thin JSON wrapper over Redis that never raises at the call site."""

    def __init__(self, url: str | None = None, ttl: int = DEFAULT_TTL_SECONDS) -> None:
        self.url = url or settings.redis_url
        self.ttl = ttl
        self._client: redis.Redis | None = None
        self.enabled = True

    @property
    def client(self) -> redis.Redis | None:
        if self._client is None and self.enabled:
            try:
                self._client = redis.Redis.from_url(
                    self.url,
                    decode_responses=True,
                    socket_connect_timeout=3,
                    socket_timeout=3,
                )
            except (ValueError, redis.RedisError) as exc:
                logger.warning("redis unavailable (%s); caching disabled", exc)
                self.enabled = False
                self._client = None
        return self._client

    def get(self, key: str) -> dict[str, Any] | None:
        client = self.client
        if client is None:
            return None
        try:
            raw = client.get(key)
        except redis.RedisError as exc:
            logger.warning("redis GET failed (%s); continuing uncached", exc)
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("discarding unreadable cache entry %s", key)
            self.delete(key)
            return None

    def set(self, key: str, value: dict[str, Any], ttl: int | None = None) -> None:
        client = self.client
        if client is None:
            return
        try:
            client.setex(key, ttl or self.ttl, json.dumps(value, default=str))
        except redis.RedisError as exc:
            logger.warning("redis SET failed (%s); result not cached", exc)

    def delete(self, key: str) -> None:
        client = self.client
        if client is None:
            return
        try:
            client.delete(key)
        except redis.RedisError:
            pass

    def ping(self) -> bool:
        client = self.client
        if client is None:
            return False
        try:
            return bool(client.ping())
        except redis.RedisError:
            return False


class InMemoryCache:
    """Process-local stand-in used by tests (and when Redis is absent)."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}
        self.enabled = True
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict[str, Any] | None:
        value = self.store.get(key)
        if value is None:
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(json.dumps(value, default=str))

    def set(self, key: str, value: dict[str, Any], ttl: int = DEFAULT_TTL_SECONDS) -> None:
        self.store[key] = value

    def delete(self, key: str) -> None:
        self.store.pop(key, None)

    def ping(self) -> bool:
        return True


_cache: Cache | None = None


def get_cache() -> Cache:
    """Process-wide cache instance."""
    global _cache
    if _cache is None:
        _cache = RedisCache()
    return _cache


def set_cache(cache: Cache | None) -> None:
    """Swap the cache (tests use this; pass None to reset)."""
    global _cache
    _cache = cache
