"""The injectable failure patterns (Section 4 of the build spec).

Each scenario shapes the baseline metrics produced by
:mod:`app.simulator.generator` and injects its own log signal. The four
scenarios are deliberately given *distinguishable* signatures so that the
downstream agents have to reason from evidence rather than guess:

===========================  ==========================================================
scenario                     signature that separates it from the others
===========================  ==========================================================
bad_deploy                   step change in error_rate at a deploy marker; memory flat
memory_leak                  monotonic memory ramp -> OOM kill -> restart sawtooth
slow_query                   latency ramp *originating in the database*, DB CPU pegged
conn_pool_exhaustion         pool pinned at 100% while the database itself stays healthy
===========================  ==========================================================

``t`` is always "seconds since incident onset" and is negative before it.
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from app.simulator.generator import SERVICES, GroundTruth

# A timeline log is (service, level, message, attrs).
TimelineLog = tuple[str, str, str, dict[str, Any]]


def ramp(t: float, start: float, end: float, *, smooth: bool = True) -> float:
    """Severity in ``[0, 1]`` as ``t`` moves from ``start`` to ``end``."""
    if t <= start:
        return 0.0
    if t >= end:
        return 1.0
    x = (t - start) / (end - start)
    return x * x * (3.0 - 2.0 * x) if smooth else x


class Scenario:
    """Base class: by default a scenario changes nothing (a quiet system)."""

    key: str = "none"
    title: str = "No incident"
    description: str = "Steady state."
    primary_service: str = ""
    affected_services: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._fired: set[str] = set()

    # -- hooks ----------------------------------------------------------- #

    def metric_value(
        self, service: str, metric: str, baseline: float, t: float, rng: random.Random
    ) -> float:
        """Return the observed value for one metric sample."""
        return baseline

    def error_log(
        self, service: str, ts: datetime, t: float, rng: random.Random
    ) -> tuple[str, dict[str, Any]] | None:
        """Scenario-specific ERROR line, or ``None`` for the generic one."""
        return None

    def timeline_logs(self, ts: datetime, t: float, rng: random.Random) -> list[TimelineLog]:
        """One-off / periodic marker events (deploys, OOM kills, slow queries)."""
        return []

    def ground_truth(self, incident_start: datetime) -> GroundTruth:
        raise NotImplementedError

    # -- helpers --------------------------------------------------------- #

    def _fire_once(self, key: str) -> bool:
        """True the first time it is called with ``key`` for this instance."""
        if key in self._fired:
            return False
        self._fired.add(key)
        return True

    def _every(self, t: float, period: float, key: str) -> bool:
        """True once per ``period`` seconds of incident time."""
        return self._fire_once(f"{key}-{int(t // period)}")


# --------------------------------------------------------------------------- #
# 1. Bad deploy
# --------------------------------------------------------------------------- #


class BadDeployScenario(Scenario):
    """A new payments-service build starts throwing on every charge."""

    key = "bad_deploy"
    title = "Bad deploy (payments-service v2.4.1)"
    description = (
        "A deploy of payments-service v2.4.1 introduces a null-handling bug in the "
        "charge path; the error rate steps up seconds after the rollout marker."
    )
    primary_service = "payments-service"
    affected_services = ("payments-service", "api-gateway")

    VERSION = "v2.4.1"
    PREVIOUS_VERSION = "v2.4.0"
    COMMIT = "9f3c1ab"

    STACK = (
        "Traceback (most recent call last):\n"
        '  File "/app/payments/api.py", line 88, in charge\n'
        "    total = compute_total(order, discount)\n"
        '  File "/app/payments/pricing.py", line 41, in compute_total\n'
        "    return subtotal + discount.amount\n"
        "TypeError: unsupported operand type(s) for +: 'Decimal' and 'NoneType'"
    )

    def metric_value(self, service, metric, baseline, t, rng):
        if t < 0:
            return baseline
        sev = ramp(t, 0, 45)
        if service == "payments-service":
            if metric == "error_rate":
                return baseline + 0.38 * sev
            if metric == "latency_p95_ms":
                return baseline * (1 + 0.45 * sev)
            if metric == "cpu_pct":
                return baseline + 12 * sev
        elif service == "api-gateway":
            sev_gw = ramp(t, 20, 75)
            if metric == "error_rate":
                return baseline + 0.12 * sev_gw
            if metric == "latency_p95_ms":
                return baseline * (1 + 0.25 * sev_gw)
        return baseline

    def error_log(self, service, ts, t, rng):
        if t < 0:
            return None
        if service == "payments-service":
            return (
                "unhandled exception in POST /payments/charge",
                {
                    "error_type": "TypeError",
                    "status": 500,
                    "version": self.VERSION,
                    "endpoint": "/payments/charge",
                    "stack_trace": self.STACK,
                },
            )
        if service == "api-gateway":
            return (
                "upstream payments-service returned 500 for POST /api/v1/checkout",
                {"error_type": "UpstreamError", "status": 502, "upstream": "payments-service"},
            )
        return None

    def timeline_logs(self, ts, t, rng):
        events: list[TimelineLog] = []
        if t >= 0 and self._fire_once("deploy"):
            events.append(
                (
                    "payments-service",
                    "INFO",
                    f"deployment completed: payments-service {self.VERSION} "
                    f"(git {self.COMMIT}) rolled out to 6/6 replicas",
                    {
                        "event": "deploy",
                        "version": self.VERSION,
                        "previous_version": self.PREVIOUS_VERSION,
                        "commit": self.COMMIT,
                        "replicas": 6,
                    },
                )
            )
        if t >= 60 and self._every(t, 180, "readiness"):
            events.append(
                (
                    "payments-service",
                    "WARN",
                    f"readiness probe degraded: 5xx ratio {0.3 + rng.random() * 0.15:.2f} "
                    f"over last 60s on {self.VERSION}",
                    {"event": "probe_degraded", "version": self.VERSION},
                )
            )
        return events

    def ground_truth(self, incident_start):
        return GroundTruth(
            scenario_key=self.key,
            title=self.title,
            primary_service=self.primary_service,
            affected_services=self.affected_services,
            summary=(
                f"payments-service {self.VERSION} (commit {self.COMMIT}) shipped a TypeError in "
                "compute_total; charges began failing immediately after rollout and the failure "
                "surfaced at api-gateway as 502s."
            ),
            expected_signals=(
                "step change in payments-service error_rate within ~45s of the deploy marker",
                "deploy event log naming version v2.4.1",
                "identical TypeError stack trace repeated across payments-service errors",
                "memory and connection-pool metrics stay flat (rules out leak / pool exhaustion)",
            ),
            injected_events=[
                {
                    "timestamp": incident_start.isoformat(),
                    "kind": "deploy",
                    "detail": f"payments-service {self.PREVIOUS_VERSION} -> {self.VERSION}",
                }
            ],
        )


# --------------------------------------------------------------------------- #
# 2. Memory leak
# --------------------------------------------------------------------------- #


class MemoryLeakScenario(Scenario):
    """notification-worker leaks heap until the container is OOM-killed."""

    key = "memory_leak"
    title = "Memory leak (notification-worker)"
    description = (
        "notification-worker retains every dispatched job payload; RSS climbs steadily "
        "until the container is OOM-killed and restarted, producing a sawtooth."
    )
    primary_service = "notification-worker"
    affected_services = ("notification-worker",)

    BASE_MB = 420.0
    LIMIT_MB = 2048.0
    LEAK_MB_PER_S = 1.9
    CLIMB_S = 855.0  # BASE_MB + LEAK_MB_PER_S * CLIMB_S ~= LIMIT_MB
    RESTART_S = 45.0
    CYCLE_S = CLIMB_S + RESTART_S

    STACK = (
        "Traceback (most recent call last):\n"
        '  File "/app/worker/dispatch.py", line 132, in run_batch\n'
        "    batch = [self._cache.setdefault(job.id, job.payload) for job in jobs]\n"
        "MemoryError: unable to allocate 64 MiB for notification batch"
    )

    def _phase(self, t: float) -> float:
        return t % self.CYCLE_S

    def _memory_mb(self, t: float) -> float:
        phase = self._phase(t)
        if phase >= self.CLIMB_S:  # killed, container coming back up
            return self.BASE_MB * 1.02
        return self.BASE_MB + self.LEAK_MB_PER_S * phase

    def _fill(self, t: float) -> float:
        """Heap fill fraction in ``[0, 1]``."""
        return (self._memory_mb(t) - self.BASE_MB) / (self.LIMIT_MB - self.BASE_MB)

    def metric_value(self, service, metric, baseline, t, rng):
        if t < 0 or service != "notification-worker":
            return baseline
        phase, fill = self._phase(t), self._fill(t)
        crashing = phase >= self.CLIMB_S
        if metric == "memory_mb":
            return self._memory_mb(t) * rng.gauss(1.0, 0.01)
        if metric == "cpu_pct":  # GC works harder as the heap fills
            return baseline + 38 * fill**1.5
        if metric == "latency_p95_ms":  # stop-the-world pauses
            return baseline * (1 + 2.6 * fill**2) * (4.0 if crashing else 1.0)
        if metric == "error_rate":
            if crashing:
                return 0.55
            return baseline + 0.02 * ramp(fill, 0.85, 1.0)
        return baseline

    def error_log(self, service, ts, t, rng):
        if t < 0 or service != "notification-worker":
            return None
        if self._phase(t) >= self.CLIMB_S:
            return (
                "job dropped: worker unavailable during restart",
                {"error_type": "WorkerUnavailable", "queue_depth": rng.randint(400, 900)},
            )
        return (
            "job failed while allocating batch buffer",
            {
                "error_type": "MemoryError",
                "rss_mb": round(self._memory_mb(t)),
                "limit_mb": int(self.LIMIT_MB),
                "stack_trace": self.STACK,
            },
        )

    def timeline_logs(self, ts, t, rng):
        if t < 0:
            return []
        events: list[TimelineLog] = []
        phase, fill, cycle = self._phase(t), self._fill(t), int(t // self.CYCLE_S)
        rss = round(self._memory_mb(t))

        if fill > 0.35 and self._every(t, 120, "gc"):
            events.append(
                (
                    "notification-worker",
                    "WARN",
                    f"gc pause {int(120 + 700 * fill)}ms; rss {rss}MB / limit "
                    f"{int(self.LIMIT_MB)}MB ({fill * 100:.0f}% of heap), reclaimed "
                    f"{rng.randint(4, 18)}MB",
                    {"event": "gc_pressure", "rss_mb": rss, "limit_mb": int(self.LIMIT_MB)},
                )
            )
        if phase >= self.CLIMB_S and self._fire_once(f"oom-{cycle}"):
            events.extend(
                [
                    (
                        "notification-worker",
                        "ERROR",
                        f"MemoryError: unable to allocate 64 MiB (rss {int(self.LIMIT_MB)}MB "
                        f"/ limit {int(self.LIMIT_MB)}MB)",
                        {
                            "event": "oom",
                            "error_type": "MemoryError",
                            "rss_mb": int(self.LIMIT_MB),
                            "stack_trace": self.STACK,
                        },
                    ),
                    (
                        "notification-worker",
                        "ERROR",
                        "container killed: OOMKilled (exit code 137)",
                        {
                            "event": "oom_kill",
                            "error_type": "OOMKilled",
                            "exit_code": 137,
                            "restart_count": cycle + 1,
                        },
                    ),
                    (
                        "notification-worker",
                        "INFO",
                        f"supervisor restarted notification-worker (restart #{cycle + 1}), "
                        "rss back to baseline",
                        {"event": "restart", "restart_count": cycle + 1},
                    ),
                ]
            )
        return events

    def ground_truth(self, incident_start):
        return GroundTruth(
            scenario_key=self.key,
            title=self.title,
            primary_service=self.primary_service,
            affected_services=self.affected_services,
            summary=(
                "notification-worker holds every dispatched job payload in an unbounded cache; "
                f"RSS climbs from {int(self.BASE_MB)}MB to the {int(self.LIMIT_MB)}MB container "
                "limit in ~14 minutes, the container is OOM-killed (exit 137) and restarts, and "
                "the climb begins again."
            ),
            expected_signals=(
                "monotonic memory_mb ramp in notification-worker, reset by a restart",
                "GC pause warnings growing in frequency as the heap fills",
                "OOMKilled / exit code 137 log at the crash",
                "error_rate stays normal until the crash window (rules out a bad deploy)",
            ),
            injected_events=[
                {
                    "timestamp": incident_start.isoformat(),
                    "kind": "leak_start",
                    "detail": f"unbounded job cache leaking ~{self.LEAK_MB_PER_S} MB/s",
                }
            ],
        )


# --------------------------------------------------------------------------- #
# 3. Slow query / DB contention
# --------------------------------------------------------------------------- #


class SlowQueryScenario(Scenario):
    """A lost index turns an orders-db join into a sequential scan."""

    key = "slow_query"
    title = "Slow query / DB contention (orders-db)"
    description = (
        "After a stats refresh the planner drops idx_order_items_order_id and falls back to a "
        "sequential scan; orders-db p95 latency climbs and the delay propagates upstream."
    )
    primary_service = "orders-db"
    affected_services = ("orders-db", "payments-service", "api-gateway")

    QUERY = (
        "SELECT o.id, o.total, oi.sku FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.id "
        "WHERE o.customer_id = $1 AND o.created_at > $2"
    )
    QUERY_ID = "q_7712"

    def metric_value(self, service, metric, baseline, t, rng):
        if t < 0:
            return baseline
        if service == "orders-db":
            sev = ramp(t, 0, 240)
            if metric == "latency_p95_ms":
                return baseline * (1 + 75 * sev)
            if metric == "cpu_pct":
                return baseline + 52 * sev
            if metric == "conn_pool_used_pct":  # queries hold connections longer
                return baseline + 25 * sev
            if metric == "error_rate":
                return baseline + 0.02 * ramp(t, 180, 420)
        elif service == "payments-service":
            sev = ramp(t, 30, 300)
            if metric == "latency_p95_ms":
                return baseline * (1 + 8 * sev)
            if metric == "error_rate":
                return baseline + 0.09 * ramp(t, 150, 420)
            if metric == "cpu_pct":
                return baseline + 8 * sev
        elif service == "api-gateway":
            sev = ramp(t, 60, 360)
            if metric == "latency_p95_ms":
                return baseline * (1 + 14 * sev)
            if metric == "error_rate":
                return baseline + 0.06 * ramp(t, 210, 480)
        elif service == "notification-worker":
            if metric == "latency_p95_ms":
                return baseline * (1 + 2 * ramp(t, 90, 420))
        return baseline

    def error_log(self, service, ts, t, rng):
        if t < 0:
            return None
        if service == "orders-db":
            return (
                f"canceling statement due to statement_timeout (5000ms), query_id={self.QUERY_ID}",
                {"error_type": "QueryCanceled", "sqlstate": "57014", "query_id": self.QUERY_ID},
            )
        if service == "payments-service":
            return (
                "order lookup failed: query to orders-db exceeded statement timeout",
                {
                    "error_type": "QueryCanceledError",
                    "status": 504,
                    "downstream": "orders-db",
                    "query_id": self.QUERY_ID,
                    "stack_trace": (
                        "Traceback (most recent call last):\n"
                        '  File "/app/payments/repo.py", line 57, in fetch_order\n'
                        "    row = await conn.fetchrow(ORDER_LOOKUP_SQL, customer_id, since)\n"
                        "asyncpg.exceptions.QueryCanceledError: canceling statement due to "
                        "statement timeout"
                    ),
                },
            )
        if service == "api-gateway":
            return (
                "upstream payments-service timed out after 5000ms on POST /api/v1/orders",
                {"error_type": "GatewayTimeout", "status": 504, "upstream": "payments-service"},
            )
        return None

    def timeline_logs(self, ts, t, rng):
        if t < 0:
            return []
        events: list[TimelineLog] = []
        sev = ramp(t, 0, 240)
        if self._fire_once("plan_flip"):
            events.append(
                (
                    "orders-db",
                    "WARN",
                    "planner switched to Seq Scan on public.order_items after autovacuum stats "
                    "refresh; index idx_order_items_order_id no longer used",
                    {"event": "plan_change", "relation": "public.order_items", "query_id": self.QUERY_ID},
                )
            )
        if sev > 0.12 and self._every(t, 60, "slow_query"):
            duration = int(18 * (1 + 75 * sev))
            events.append(
                (
                    "orders-db",
                    "WARN",
                    f"slow query {duration}ms (query_id={self.QUERY_ID}): {self.QUERY}",
                    {
                        "event": "slow_query",
                        "query_id": self.QUERY_ID,
                        "duration_ms": duration,
                        "rows_scanned": 2_418_733,
                        "plan": "Seq Scan on order_items (rows=2418733 width=48)",
                    },
                )
            )
        if sev > 0.5 and self._every(t, 150, "lock_wait"):
            events.append(
                (
                    "orders-db",
                    "WARN",
                    f"process holding shared lock on public.order_items for "
                    f"{rng.randint(2, 9)}s; {rng.randint(8, 40)} queries waiting",
                    {"event": "lock_wait", "relation": "public.order_items"},
                )
            )
        return events

    def ground_truth(self, incident_start):
        return GroundTruth(
            scenario_key=self.key,
            title=self.title,
            primary_service=self.primary_service,
            affected_services=self.affected_services,
            summary=(
                "orders-db stopped using idx_order_items_order_id after a stats refresh and began "
                f"sequentially scanning order_items for {self.QUERY_ID}. DB p95 latency rose ~75x "
                "and the delay propagated to payments-service and then api-gateway as timeouts."
            ),
            expected_signals=(
                "latency ramp starts in orders-db and reaches dependents later (30s / 60s lag)",
                "orders-db CPU saturates while its memory stays flat",
                "repeated slow-query warnings naming query_id q_7712 and a Seq Scan plan",
                "connection pools rise but never pin at 100% (rules out pool exhaustion)",
            ),
            injected_events=[
                {
                    "timestamp": incident_start.isoformat(),
                    "kind": "plan_change",
                    "detail": "idx_order_items_order_id dropped from the plan for q_7712",
                }
            ],
        )


# --------------------------------------------------------------------------- #
# 4. Connection pool exhaustion
# --------------------------------------------------------------------------- #


class ConnectionPoolExhaustionScenario(Scenario):
    """auth-service saturates its database pool and the failure cascades."""

    key = "conn_pool_exhaustion"
    title = "Connection pool exhaustion (auth-service)"
    description = (
        "auth-service leaks connections back to a 20-slot pool; once the pool pins at 100% "
        "callers queue on acquire and the timeouts cascade to api-gateway and "
        "notification-worker while orders-db itself stays healthy."
    )
    primary_service = "auth-service"
    affected_services = ("auth-service", "api-gateway", "notification-worker")

    POOL_SIZE = 20

    def metric_value(self, service, metric, baseline, t, rng):
        if t < 0:
            return baseline
        if service == "auth-service":
            saturation = ramp(t, 0, 180)
            if metric == "conn_pool_used_pct":
                return baseline + (100 - baseline) * saturation
            if metric == "latency_p95_ms":  # time spent waiting on acquire
                return baseline * (1 + 55 * ramp(t, 30, 240))
            if metric == "error_rate":
                return baseline + 0.45 * ramp(t, 60, 240)
            if metric == "cpu_pct":  # threads are blocked, not busy
                return baseline + 5 * saturation
        elif service == "api-gateway":
            # Everything downstream of auth-service lags it: the gateway only
            # starts failing once its own calls sit in the acquire queue.
            sev = ramp(t, 75, 330)
            if metric == "latency_p95_ms":
                return baseline * (1 + 24 * sev)
            if metric == "error_rate":
                return baseline + 0.30 * ramp(t, 150, 390)
            if metric == "conn_pool_used_pct":
                return baseline + 40 * sev
        elif service == "notification-worker":
            sev = ramp(t, 240, 480)
            if metric == "error_rate":
                return baseline + 0.18 * sev
            if metric == "latency_p95_ms":
                return baseline * (1 + 3 * sev)
        elif service == "orders-db":
            # The database is fine: it just holds a lot of idle-in-transaction sessions.
            sev = ramp(t, 30, 240)
            if metric == "conn_pool_used_pct":
                return baseline + 30 * sev
            if metric == "cpu_pct":
                return baseline + 6 * sev
        return baseline

    def error_log(self, service, ts, t, rng):
        if t < 0:
            return None
        if service == "auth-service":
            return (
                f"timed out waiting for a connection from pool 'orders-db' after 10.0s "
                f"({self.POOL_SIZE}/{self.POOL_SIZE} in use)",
                {
                    "error_type": "PoolTimeoutError",
                    "status": 503,
                    "pool": "orders-db",
                    "pool_size": self.POOL_SIZE,
                    "in_use": self.POOL_SIZE,
                    "waiters": rng.randint(20, 90),
                    "stack_trace": (
                        "Traceback (most recent call last):\n"
                        '  File "/app/auth/session.py", line 74, in load_session\n'
                        "    async with pool.acquire(timeout=10.0) as conn:\n"
                        "asyncpg.exceptions._base.InterfaceError: timeout acquiring connection"
                    ),
                },
            )
        if service == "api-gateway":
            return (
                f"upstream auth-service returned 504 after {rng.randint(9800, 30100)}ms",
                {"error_type": "GatewayTimeout", "status": 504, "upstream": "auth-service"},
            )
        if service == "notification-worker":
            return (
                f"token refresh failed: auth-service unavailable (retry {rng.randint(2, 5)}/5)",
                {"error_type": "AuthUnavailable", "upstream": "auth-service"},
            )
        return None

    def timeline_logs(self, ts, t, rng):
        if t < 0:
            return []
        events: list[TimelineLog] = []
        saturation = ramp(t, 0, 180)
        if self._fire_once("first_wait"):
            events.append(
                (
                    "auth-service",
                    "WARN",
                    f"connection acquire wait exceeded 1s for the first time "
                    f"(pool 'orders-db' max_size={self.POOL_SIZE}, checked out "
                    f"{self.POOL_SIZE - 2}/{self.POOL_SIZE})",
                    {"event": "pool_pressure", "pool": "orders-db", "pool_size": self.POOL_SIZE},
                )
            )
        if saturation > 0.45 and self._every(t, 45, "pool_exhausted"):
            waiters = int(20 + 90 * saturation)
            events.append(
                (
                    "auth-service",
                    "WARN",
                    f"connection pool 'orders-db' exhausted: {self.POOL_SIZE}/{self.POOL_SIZE} "
                    f"in use, {waiters} waiters, oldest wait {4 + 9 * saturation:.1f}s",
                    {
                        "event": "pool_exhausted",
                        "pool": "orders-db",
                        "pool_size": self.POOL_SIZE,
                        "in_use": self.POOL_SIZE,
                        "waiters": waiters,
                    },
                )
            )
        if saturation > 0.6 and self._every(t, 120, "idle_in_tx"):
            events.append(
                (
                    "orders-db",
                    "INFO",
                    f"{rng.randint(14, 19)} sessions idle in transaction from auth-service "
                    "(longest 62s); server load nominal",
                    {"event": "idle_in_transaction", "client": "auth-service"},
                )
            )
        if saturation > 0.8 and self._every(t, 90, "circuit"):
            events.append(
                (
                    "api-gateway",
                    "WARN",
                    "circuit breaker half-open for upstream auth-service after 12 consecutive "
                    "timeouts",
                    {"event": "circuit_breaker", "upstream": "auth-service"},
                )
            )
        return events

    def ground_truth(self, incident_start):
        return GroundTruth(
            scenario_key=self.key,
            title=self.title,
            primary_service=self.primary_service,
            affected_services=self.affected_services,
            summary=(
                f"auth-service exhausted its {self.POOL_SIZE}-connection pool to orders-db "
                "(sessions left idle in transaction). Callers queued on acquire, auth-service "
                "began returning 503/504s, and the timeouts cascaded to api-gateway and "
                "notification-worker. orders-db itself stayed healthy throughout."
            ),
            expected_signals=(
                "auth-service conn_pool_used_pct pinned at 100% while orders-db latency stays flat",
                "pool-exhausted warnings naming pool 'orders-db' with a growing waiter count",
                "failures appear in dependency order: auth-service -> api-gateway -> "
                "notification-worker",
                "auth-service CPU stays low (threads blocked on acquire, not working)",
            ),
            injected_events=[
                {
                    "timestamp": incident_start.isoformat(),
                    "kind": "pool_saturation",
                    "detail": f"auth-service pool 'orders-db' ({self.POOL_SIZE} slots) begins saturating",
                }
            ],
        )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

SCENARIOS: dict[str, type[Scenario]] = {
    cls.key: cls
    for cls in (
        BadDeployScenario,
        MemoryLeakScenario,
        SlowQueryScenario,
        ConnectionPoolExhaustionScenario,
    )
}


def get_scenario(key: str) -> Scenario:
    """Return a fresh scenario instance (they carry per-run fire-once state)."""
    try:
        return SCENARIOS[key]()
    except KeyError:
        raise ValueError(
            f"unknown scenario {key!r}; expected one of {', '.join(sorted(SCENARIOS))}"
        ) from None


def list_scenarios() -> list[dict[str, Any]]:
    """Catalogue for the API / UI simulate panel."""
    return [
        {
            "key": cls.key,
            "title": cls.title,
            "description": cls.description,
            "primary_service": cls.primary_service,
            "affected_services": list(cls.affected_services),
        }
        for cls in SCENARIOS.values()
    ]


__all__ = [
    "SCENARIOS",
    "SERVICES",
    "Scenario",
    "BadDeployScenario",
    "MemoryLeakScenario",
    "SlowQueryScenario",
    "ConnectionPoolExhaustionScenario",
    "get_scenario",
    "list_scenarios",
    "ramp",
]
