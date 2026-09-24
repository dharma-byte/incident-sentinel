"""Synthetic log and metric generator for simulated microservices.

The generator produces a fully reproducible :class:`IncidentBundle` for a given
scenario and seed: structured JSON logs with realistic noise plus the injected
failure signal, and per-service metric time series (latency, error rate, CPU,
memory, connection-pool usage).

Everything downstream -- the API, the agents, the UI -- consumes bundles
produced here, so no real infrastructure is ever required.

Run it directly to dump a bundle to disk::

    python -m app.simulator.generator --scenario bad_deploy --out ../data
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:  # pragma: no cover - import cycle guard; scenarios imports us
    from app.simulator.scenarios import Scenario

METRIC_NAMES: tuple[str, ...] = (
    "latency_p95_ms",
    "error_rate",
    "cpu_pct",
    "memory_mb",
    "conn_pool_used_pct",
)

LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


# --------------------------------------------------------------------------- #
# Service topology
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ServiceProfile:
    """Steady-state behaviour of one simulated microservice."""

    name: str
    depends_on: tuple[str, ...]
    logs_per_min: int
    latency_p95_ms: float
    error_rate: float
    cpu_pct: float
    memory_mb: float
    conn_pool_used_pct: float

    def baseline(self, metric: str) -> float:
        return float(getattr(self, metric))


SERVICES: dict[str, ServiceProfile] = {
    "api-gateway": ServiceProfile(
        name="api-gateway",
        depends_on=("auth-service", "payments-service"),
        logs_per_min=60,
        latency_p95_ms=120.0,
        error_rate=0.004,
        cpu_pct=35.0,
        memory_mb=512.0,
        conn_pool_used_pct=25.0,
    ),
    "auth-service": ServiceProfile(
        name="auth-service",
        depends_on=("orders-db",),
        logs_per_min=40,
        latency_p95_ms=45.0,
        error_rate=0.002,
        cpu_pct=28.0,
        memory_mb=384.0,
        conn_pool_used_pct=30.0,
    ),
    "payments-service": ServiceProfile(
        name="payments-service",
        depends_on=("orders-db",),
        logs_per_min=45,
        latency_p95_ms=180.0,
        error_rate=0.006,
        cpu_pct=42.0,
        memory_mb=640.0,
        conn_pool_used_pct=35.0,
    ),
    "orders-db": ServiceProfile(
        name="orders-db",
        depends_on=(),
        logs_per_min=20,
        latency_p95_ms=18.0,
        error_rate=0.001,
        cpu_pct=38.0,
        memory_mb=1536.0,
        conn_pool_used_pct=40.0,
    ),
    "notification-worker": ServiceProfile(
        name="notification-worker",
        depends_on=("orders-db",),
        logs_per_min=25,
        latency_p95_ms=90.0,
        error_rate=0.003,
        cpu_pct=22.0,
        memory_mb=420.0,
        conn_pool_used_pct=15.0,
    ),
}

SERVICE_NAMES: tuple[str, ...] = tuple(SERVICES)

# Physical bounds applied after a scenario has distorted a baseline value.
METRIC_BOUNDS: dict[str, tuple[float, float]] = {
    "latency_p95_ms": (1.0, 60_000.0),
    "error_rate": (0.0, 1.0),
    "cpu_pct": (0.0, 100.0),
    "memory_mb": (16.0, 16_384.0),
    "conn_pool_used_pct": (0.0, 100.0),
}


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclass
class LogRecord:
    """One structured JSON log line emitted by a simulated service."""

    id: str
    timestamp: datetime
    service: str
    level: str
    message: str
    trace_id: str
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "service": self.service,
            "level": self.level,
            "message": self.message,
            "trace_id": self.trace_id,
            **self.attrs,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


@dataclass
class MetricPoint:
    """One sample of one metric for one service."""

    timestamp: datetime
    service: str
    metric: str
    value: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "service": self.service,
            "metric": self.metric,
            "value": round(self.value, 4),
        }


@dataclass
class GroundTruth:
    """What actually went wrong.

    Kept strictly out of the agent input path -- it exists so tests (and a
    future evaluation harness) can score a diagnosis, never so an agent can
    read the answer.
    """

    scenario_key: str
    title: str
    primary_service: str
    affected_services: tuple[str, ...]
    summary: str
    expected_signals: tuple[str, ...]
    injected_events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_key": self.scenario_key,
            "title": self.title,
            "primary_service": self.primary_service,
            "affected_services": list(self.affected_services),
            "summary": self.summary,
            "expected_signals": list(self.expected_signals),
            "injected_events": self.injected_events,
        }


@dataclass
class IncidentBundle:
    """Everything one simulated incident produced."""

    incident_id: str
    scenario_key: str
    seed: int
    window_start: datetime
    window_end: datetime
    incident_start: datetime
    services: tuple[str, ...]
    logs: list[LogRecord]
    metrics: list[MetricPoint]
    ground_truth: GroundTruth

    # -- querying -------------------------------------------------------- #

    def logs_for(self, service: str | None = None, level: str | None = None) -> list[LogRecord]:
        return [
            log
            for log in self.logs
            if (service is None or log.service == service)
            and (level is None or log.level == level)
        ]

    def series(self, service: str, metric: str) -> list[MetricPoint]:
        return [m for m in self.metrics if m.service == service and m.metric == metric]

    def values(self, service: str, metric: str) -> list[float]:
        return [m.value for m in self.series(service, metric)]

    def peak(self, service: str, metric: str) -> float:
        return max(self.values(service, metric))

    def baseline_mean(self, service: str, metric: str) -> float:
        """Mean of the metric over the pre-incident part of the window."""
        pre = [m.value for m in self.series(service, metric) if m.timestamp < self.incident_start]
        return sum(pre) / len(pre) if pre else 0.0

    # -- serialisation --------------------------------------------------- #

    def metadata(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "scenario_key": self.scenario_key,
            "seed": self.seed,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "incident_start": self.incident_start.isoformat(),
            "services": list(self.services),
            "log_count": len(self.logs),
            "metric_count": len(self.metrics),
        }

    def to_dict(self, include_ground_truth: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            **self.metadata(),
            "logs": [log.to_dict() for log in self.logs],
            "metrics": [m.to_dict() for m in self.metrics],
        }
        if include_ground_truth:
            payload["ground_truth"] = self.ground_truth.to_dict()
        return payload

    def write_logs_jsonl(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for log in self.logs:
                fh.write(log.to_json() + "\n")
        return path

    def write_metrics_csv(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["timestamp", "service", "metric", "value"])
            for m in self.metrics:
                writer.writerow([m.timestamp.isoformat(), m.service, m.metric, round(m.value, 4)])
        return path


# --------------------------------------------------------------------------- #
# Noise templates
# --------------------------------------------------------------------------- #

NOISE_TEMPLATES: dict[str, tuple[tuple[str, str], ...]] = {
    "api-gateway": (
        ("INFO", "{method} {path} {status} in {duration_ms}ms"),
        ("INFO", "route matched {path} -> {upstream} (retries=0)"),
        ("DEBUG", "rate limit bucket user_{user} at {tokens}/100 tokens"),
        ("INFO", "health probe /healthz 200 in 3ms"),
    ),
    "auth-service": (
        ("INFO", "token validated for user_{user} (ttl {tokens}s)"),
        ("INFO", "POST /auth/token 200 in {duration_ms}ms"),
        ("DEBUG", "jwks cache hit kid=k-{tokens}"),
        ("INFO", "session refreshed for user_{user}"),
    ),
    "payments-service": (
        ("INFO", "charge authorized amount={amount} currency=USD provider=stripe-sim in {duration_ms}ms"),
        ("INFO", "POST /payments/charge {status} in {duration_ms}ms"),
        ("DEBUG", "idempotency key pk_{tokens} reused=false"),
        ("INFO", "settlement batch queued size={tokens}"),
    ),
    "orders-db": (
        ("INFO", "query executed in {duration_ms}ms rows={tokens}"),
        ("INFO", "checkpoint complete: wrote {tokens} buffers"),
        ("DEBUG", "autovacuum: table public.orders index scans: 1"),
        ("INFO", "connection accepted from 10.0.{tokens}.14"),
    ),
    "notification-worker": (
        ("INFO", "dispatched email notification job=nj_{tokens} in {duration_ms}ms"),
        ("INFO", "queue depth {tokens} lag=0.4s"),
        ("DEBUG", "gc: collected {tokens} objects"),
        ("INFO", "ack job=nj_{tokens} attempt=1"),
    ),
}

DEFAULT_ERRORS: dict[str, tuple[str, dict[str, Any]]] = {
    "api-gateway": (
        "upstream call failed: {upstream} returned 502",
        {"error_type": "UpstreamError", "status": 502},
    ),
    "auth-service": (
        "token verification failed: signature mismatch",
        {"error_type": "InvalidTokenError", "status": 401},
    ),
    "payments-service": (
        "charge failed: provider declined transaction",
        {"error_type": "ProviderError", "status": 502},
    ),
    "orders-db": (
        "statement failed: deadlock detected on relation orders",
        {"error_type": "DeadlockDetected", "sqlstate": "40P01"},
    ),
    "notification-worker": (
        "job failed: smtp relay refused connection",
        {"error_type": "SMTPConnectError", "status": 421},
    ),
}

HTTP_PATHS = ("/api/v1/checkout", "/api/v1/orders", "/api/v1/cart", "/api/v1/profile")
HTTP_METHODS = ("GET", "POST", "PUT")


# --------------------------------------------------------------------------- #
# Generator
# --------------------------------------------------------------------------- #


class IncidentGenerator:
    """Turns a :class:`~app.simulator.scenarios.Scenario` into an incident bundle.

    The same ``(scenario, seed, window)`` triple always yields identical output,
    so demos and tests are reproducible.
    """

    def __init__(
        self,
        scenario: "Scenario",
        *,
        seed: int = 1337,
        duration_minutes: int = 30,
        incident_start_minute: int = 10,
        metric_interval_seconds: int = 15,
        start_time: datetime | None = None,
    ) -> None:
        if incident_start_minute >= duration_minutes:
            raise ValueError("incident_start_minute must fall inside the window")
        self.scenario = scenario
        self.seed = seed
        self.duration_minutes = duration_minutes
        self.incident_start_minute = incident_start_minute
        self.metric_interval_seconds = metric_interval_seconds
        self.window_start = (start_time or self._default_start()).replace(microsecond=0)
        self.incident_start = self.window_start + timedelta(minutes=incident_start_minute)
        self.window_end = self.window_start + timedelta(minutes=duration_minutes)
        self._rng = random.Random(seed)

    @staticmethod
    def _default_start() -> datetime:
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        return now - timedelta(minutes=30)

    # -- helpers --------------------------------------------------------- #

    def _ticks(self) -> Iterator[tuple[datetime, float]]:
        """Yield ``(timestamp, seconds_since_incident_start)`` for each sample."""
        step = timedelta(seconds=self.metric_interval_seconds)
        ts = self.window_start
        while ts < self.window_end:
            yield ts, (ts - self.incident_start).total_seconds()
            ts += step

    def _wobble(self, profile: ServiceProfile, metric: str, ts: datetime) -> float:
        """Baseline value with a slow sinusoidal drift plus gaussian jitter."""
        base = profile.baseline(metric)
        elapsed = (ts - self.window_start).total_seconds()
        drift = 1.0 + 0.04 * math.sin(elapsed / 220.0 + len(metric))
        jitter = self._rng.gauss(1.0, 0.03 if metric != "error_rate" else 0.15)
        return max(base * drift * jitter, 0.0)

    @staticmethod
    def _clamp(metric: str, value: float) -> float:
        low, high = METRIC_BOUNDS[metric]
        return min(max(value, low), high)

    def _trace_id(self) -> str:
        return f"{self._rng.getrandbits(64):016x}"

    def _record_id(self) -> str:
        return str(uuid.UUID(int=self._rng.getrandbits(128), version=4))

    # -- metrics --------------------------------------------------------- #

    def _generate_metrics(self) -> tuple[list[MetricPoint], dict[tuple[str, int], float]]:
        """Build every metric point; also index error_rate by (service, minute)."""
        points: list[MetricPoint] = []
        error_rate_index: dict[tuple[str, int], float] = {}

        for ts, t in self._ticks():
            for name, profile in SERVICES.items():
                for metric in METRIC_NAMES:
                    baseline = self._wobble(profile, metric, ts)
                    value = self.scenario.metric_value(name, metric, baseline, t, self._rng)
                    value = self._clamp(metric, value)
                    points.append(MetricPoint(timestamp=ts, service=name, metric=metric, value=value))
                    if metric == "error_rate":
                        minute = int((ts - self.window_start).total_seconds() // 60)
                        # Later samples in a minute overwrite earlier ones; the log
                        # generator only needs a representative rate per minute.
                        error_rate_index[(name, minute)] = value
        return points, error_rate_index

    # -- logs ------------------------------------------------------------ #

    def _noise_log(self, service: str, ts: datetime) -> LogRecord:
        level, template = self._rng.choice(NOISE_TEMPLATES[service])
        values = {
            "method": self._rng.choice(HTTP_METHODS),
            "path": self._rng.choice(HTTP_PATHS),
            "status": self._rng.choice((200, 200, 200, 201, 204, 304)),
            "duration_ms": self._rng.randint(8, 240),
            "user": self._rng.randint(1000, 9999),
            "tokens": self._rng.randint(1, 250),
            "amount": f"{self._rng.uniform(4, 480):.2f}",
            "upstream": self._rng.choice(("auth-service", "payments-service")),
        }
        return LogRecord(
            id=self._record_id(),
            timestamp=ts,
            service=service,
            level=level,
            message=template.format(**values),
            trace_id=self._trace_id(),
            attrs={"duration_ms": values["duration_ms"], "env": "prod"},
        )

    def _error_log(self, service: str, ts: datetime, t: float) -> LogRecord:
        injected = self.scenario.error_log(service, ts, t, self._rng)
        if injected is None:
            message, attrs = DEFAULT_ERRORS[service]
            message = message.format(upstream=self._rng.choice(("auth-service", "payments-service")))
            attrs = dict(attrs)
        else:
            message, attrs = injected
            attrs = dict(attrs)
        attrs.setdefault("env", "prod")
        return LogRecord(
            id=self._record_id(),
            timestamp=ts,
            service=service,
            level="ERROR",
            message=message,
            trace_id=self._trace_id(),
            attrs=attrs,
        )

    def _generate_logs(self, error_rate_index: dict[tuple[str, int], float]) -> list[LogRecord]:
        logs: list[LogRecord] = []

        for minute in range(self.duration_minutes):
            minute_start = self.window_start + timedelta(minutes=minute)
            for name, profile in SERVICES.items():
                error_rate = error_rate_index.get((name, minute), profile.error_rate)
                for _ in range(profile.logs_per_min):
                    offset = self._rng.uniform(0, 60)
                    ts = minute_start + timedelta(seconds=offset)
                    t = (ts - self.incident_start).total_seconds()
                    if self._rng.random() < error_rate:
                        logs.append(self._error_log(name, ts, t))
                    else:
                        logs.append(self._noise_log(name, ts))

        # Scenario marker events (deploys, OOM kills, slow-query warnings...).
        for ts, t in self._ticks():
            for service, level, message, attrs in self.scenario.timeline_logs(ts, t, self._rng):
                logs.append(
                    LogRecord(
                        id=self._record_id(),
                        timestamp=ts,
                        service=service,
                        level=level,
                        message=message,
                        trace_id=self._trace_id(),
                        attrs={**attrs, "env": "prod"},
                    )
                )

        logs.sort(key=lambda log: (log.timestamp, log.service))
        return logs

    # -- entrypoint ------------------------------------------------------ #

    def generate(self) -> IncidentBundle:
        self._rng = random.Random(self.seed)  # fresh stream per generate() call
        metrics, error_rate_index = self._generate_metrics()
        logs = self._generate_logs(error_rate_index)
        ground_truth = self.scenario.ground_truth(self.incident_start)
        incident_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"incident-sentinel/{self.scenario.key}/{self.seed}/{self.window_start.isoformat()}",
            )
        )
        return IncidentBundle(
            incident_id=incident_id,
            scenario_key=self.scenario.key,
            seed=self.seed,
            window_start=self.window_start,
            window_end=self.window_end,
            incident_start=self.incident_start,
            services=SERVICE_NAMES,
            logs=logs,
            metrics=metrics,
            ground_truth=ground_truth,
        )


def generate_incident(scenario_key: str, *, seed: int = 1337, **kwargs: Any) -> IncidentBundle:
    """Convenience wrapper used by the API and the tests."""
    from app.simulator.scenarios import get_scenario  # local: scenarios imports this module

    return IncidentGenerator(get_scenario(scenario_key), seed=seed, **kwargs).generate()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    from app.simulator.scenarios import SCENARIOS

    parser = argparse.ArgumentParser(description="Generate a synthetic incident bundle.")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--duration-minutes", type=int, default=30)
    parser.add_argument("--incident-start-minute", type=int, default=10)
    parser.add_argument("--out", type=Path, default=None, help="directory for logs.jsonl / metrics.csv")
    parser.add_argument("--show", type=int, default=8, help="how many ERROR lines to preview")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    bundle = generate_incident(
        args.scenario,
        seed=args.seed,
        duration_minutes=args.duration_minutes,
        incident_start_minute=args.incident_start_minute,
    )
    gt = bundle.ground_truth

    print(f"incident   {bundle.incident_id}")
    print(f"scenario   {gt.title}  ({bundle.scenario_key}, seed={bundle.seed})")
    print(f"window     {bundle.window_start.isoformat()} -> {bundle.window_end.isoformat()}")
    print(f"onset      {bundle.incident_start.isoformat()}")
    print(f"volume     {len(bundle.logs)} logs / {len(bundle.metrics)} metric points")
    print(f"errors     {len(bundle.logs_for(level='ERROR'))} ERROR lines")
    print()
    print("metric shift (pre-incident mean -> peak)")
    for service in bundle.services:
        parts = []
        for metric in METRIC_NAMES:
            base, peak = bundle.baseline_mean(service, metric), bundle.peak(service, metric)
            # Ignore ratios that are large only because the baseline is ~zero.
            if base and peak / base > 1.5 and peak > (0.02 if metric == "error_rate" else 1.0):
                fmt = "{:.3f}" if metric == "error_rate" else "{:.1f}"
                parts.append(f"{metric} {fmt.format(base)}->{fmt.format(peak)}")
        if parts:
            print(f"  {service:<21} {', '.join(parts)}")

    signal = [log for log in bundle.logs if log.timestamp >= bundle.incident_start]
    markers = [log for log in signal if "event" in log.attrs]
    errors = [log for log in signal if log.level == "ERROR" and log.service in gt.affected_services]

    print(f"\ninjected marker events (first {args.show})")
    for log in markers[: args.show]:
        print(f"  {log.timestamp.isoformat()} {log.service:<20} {log.message[:100]}")
    print(f"\npost-onset ERROR lines in affected services (first {args.show})")
    for log in errors[: args.show]:
        print(f"  {log.timestamp.isoformat()} {log.service:<20} {log.message[:100]}")

    if args.out:
        out = Path(args.out) / bundle.scenario_key
        logs_path = bundle.write_logs_jsonl(out / "logs.jsonl")
        metrics_path = bundle.write_metrics_csv(out / "metrics.csv")
        meta_path = out / "incident.json"
        meta_path.write_text(
            json.dumps({**bundle.metadata(), "ground_truth": gt.to_dict()}, indent=2), encoding="utf-8"
        )
        print(f"\nwrote {logs_path}\n      {metrics_path}\n      {meta_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
