"""Tests for the synthetic incident generator and the four failure scenarios.

The scenario-signature tests double as the contract the agents will rely on in
Phase 3: if a scenario stops looking distinct here, no amount of prompting will
make the diagnosis correct.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.simulator.generator import (
    METRIC_NAMES,
    SERVICE_NAMES,
    IncidentBundle,
    IncidentGenerator,
    generate_incident,
    main,
)
from app.simulator.scenarios import SCENARIOS, get_scenario, list_scenarios, ramp

FIXED_START = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
ALL_KEYS = sorted(SCENARIOS)


def build(key: str, *, seed: int = 1337, **kwargs) -> IncidentBundle:
    """Deterministic bundle with a pinned window, so assertions are stable."""
    kwargs.setdefault("start_time", FIXED_START)
    return IncidentGenerator(get_scenario(key), seed=seed, **kwargs).generate()


def onset_offset(bundle: IncidentBundle, service: str, metric: str, factor: float = 1.5) -> float | None:
    """Seconds after incident start at which ``metric`` first exceeds ``factor`` x baseline."""
    base = bundle.baseline_mean(service, metric)
    for point in bundle.series(service, metric):
        if point.timestamp >= bundle.incident_start and point.value > base * factor:
            return (point.timestamp - bundle.incident_start).total_seconds()
    return None


def messages(bundle: IncidentBundle, service: str | None = None) -> str:
    return "\n".join(log.message for log in bundle.logs_for(service))


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_four_scenarios_are_registered():
    assert ALL_KEYS == [
        "bad_deploy",
        "conn_pool_exhaustion",
        "memory_leak",
        "slow_query",
    ]


def test_scenario_catalogue_is_ui_ready():
    for entry in list_scenarios():
        assert entry["key"] in SCENARIOS
        assert entry["title"] and entry["description"]
        assert entry["primary_service"] in SERVICE_NAMES
        assert set(entry["affected_services"]) <= set(SERVICE_NAMES)


def test_unknown_scenario_is_rejected():
    with pytest.raises(ValueError, match="unknown scenario"):
        get_scenario("solar_flare")


def test_scenario_instances_are_not_shared():
    # fire-once state must not leak between runs
    assert get_scenario("bad_deploy") is not get_scenario("bad_deploy")


def test_ramp_is_bounded_and_monotonic():
    assert ramp(-5, 0, 10) == 0.0
    assert ramp(50, 0, 10) == 1.0
    assert 0 < ramp(5, 0, 10) < 1
    assert ramp(3, 0, 10) < ramp(7, 0, 10)


# --------------------------------------------------------------------------- #
# Shape of the generated data
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ALL_KEYS)
def test_bundle_metadata(key):
    bundle = build(key)
    assert bundle.scenario_key == key
    assert bundle.services == SERVICE_NAMES
    assert bundle.window_start == FIXED_START
    assert bundle.incident_start == FIXED_START + timedelta(minutes=10)
    assert bundle.window_end == FIXED_START + timedelta(minutes=30)
    assert bundle.ground_truth.primary_service in SERVICE_NAMES


@pytest.mark.parametrize("key", ALL_KEYS)
def test_metric_grid_is_complete(key):
    bundle = build(key)
    ticks = 30 * 60 // 15
    assert len(bundle.metrics) == ticks * len(SERVICE_NAMES) * len(METRIC_NAMES)
    for service in SERVICE_NAMES:
        for metric in METRIC_NAMES:
            assert len(bundle.series(service, metric)) == ticks


@pytest.mark.parametrize("key", ALL_KEYS)
def test_metric_values_stay_physical(key):
    bundle = build(key)
    for point in bundle.metrics:
        assert bundle.window_start <= point.timestamp < bundle.window_end
        if point.metric == "error_rate":
            assert 0.0 <= point.value <= 1.0
        elif point.metric in ("cpu_pct", "conn_pool_used_pct"):
            assert 0.0 <= point.value <= 100.0
        else:
            assert point.value > 0


@pytest.mark.parametrize("key", ALL_KEYS)
def test_logs_are_well_formed(key):
    bundle = build(key)
    assert len(bundle.logs) > 4000  # 190 logs/min over 30 minutes, plus markers
    assert bundle.logs == sorted(bundle.logs, key=lambda log: log.timestamp)
    for log in bundle.logs:
        assert log.service in SERVICE_NAMES
        assert log.level in ("DEBUG", "INFO", "WARN", "ERROR")
        assert log.message and "{" not in log.message  # every placeholder was filled
        assert len(log.trace_id) == 16
        assert bundle.window_start <= log.timestamp < bundle.window_end
        parsed = json.loads(log.to_json())
        assert parsed["id"] == log.id
        assert parsed["timestamp"].startswith("2026-03-01")


@pytest.mark.parametrize("key", ALL_KEYS)
def test_noise_dominates_before_the_incident(key):
    """Pre-incident traffic must look boring, or nothing is detectable."""
    bundle = build(key)
    pre = [log for log in bundle.logs if log.timestamp < bundle.incident_start]
    post = [log for log in bundle.logs if log.timestamp >= bundle.incident_start]
    pre_errors = sum(log.level == "ERROR" for log in pre) / len(pre)
    post_errors = sum(log.level == "ERROR" for log in post) / len(post)
    assert pre_errors < 0.02
    assert post_errors > pre_errors


@pytest.mark.parametrize("key", ALL_KEYS)
def test_error_logs_carry_actionable_attributes(key):
    bundle = build(key)
    errors = bundle.logs_for(level="ERROR")
    assert errors, "every scenario must produce ERROR lines"
    assert all("error_type" in log.attrs for log in errors)
    assert any("stack_trace" in log.attrs for log in errors)


# --------------------------------------------------------------------------- #
# Reproducibility
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ALL_KEYS)
def test_same_seed_reproduces_the_incident(key):
    a, b = build(key), build(key)
    assert a.incident_id == b.incident_id
    assert [log.to_dict() for log in a.logs] == [log.to_dict() for log in b.logs]
    assert [m.to_dict() for m in a.metrics] == [m.to_dict() for m in b.metrics]


@pytest.mark.parametrize("key", ALL_KEYS)
def test_different_seed_changes_the_noise(key):
    a, b = build(key, seed=1), build(key, seed=2)
    assert a.incident_id != b.incident_id
    assert [log.message for log in a.logs] != [log.message for log in b.logs]
    # ...but the injected signal survives the reseed
    assert a.ground_truth.primary_service == b.ground_truth.primary_service


def test_window_must_contain_the_onset():
    with pytest.raises(ValueError, match="incident_start_minute"):
        build("bad_deploy", duration_minutes=10, incident_start_minute=10)


# --------------------------------------------------------------------------- #
# Scenario signatures
# --------------------------------------------------------------------------- #


def test_bad_deploy_steps_the_error_rate_right_after_the_rollout():
    bundle = build("bad_deploy")
    payments = "payments-service"

    assert bundle.baseline_mean(payments, "error_rate") < 0.02
    assert bundle.peak(payments, "error_rate") > 0.30
    # the step is fast: elevated within a minute of the deploy marker
    assert onset_offset(bundle, payments, "error_rate", factor=5) <= 60

    deploys = [log for log in bundle.logs if log.attrs.get("event") == "deploy"]
    assert len(deploys) == 1
    assert deploys[0].service == payments
    assert deploys[0].timestamp == bundle.incident_start
    assert "v2.4.1" in deploys[0].message

    errors = bundle.logs_for(payments, "ERROR")
    assert len(errors) > 100
    assert all(log.attrs["error_type"] == "TypeError" for log in errors)
    assert "compute_total" in errors[-1].attrs["stack_trace"]

    # memory and pool stay flat -- this is what separates it from the other three
    assert bundle.peak(payments, "memory_mb") / bundle.baseline_mean(payments, "memory_mb") < 1.2
    assert bundle.peak(payments, "conn_pool_used_pct") < 45


def test_memory_leak_climbs_then_restarts():
    bundle = build("memory_leak")
    worker = "notification-worker"
    memory = bundle.series(worker, "memory_mb")

    assert bundle.baseline_mean(worker, "memory_mb") < 450
    assert bundle.peak(worker, "memory_mb") > 1900  # reaches the 2048MB limit

    post = [m for m in memory if m.timestamp >= bundle.incident_start]
    climb = [m for m in post if (m.timestamp - bundle.incident_start).total_seconds() < 840]
    values = [m.value for m in climb]
    assert values[-1] > values[0] * 3
    # monotonic climb (allowing for the 1% jitter on each sample)
    assert all(b > a * 0.97 for a, b in zip(values, values[1:]))

    after_crash = [
        m.value
        for m in post
        if 870 <= (m.timestamp - bundle.incident_start).total_seconds() <= 900
    ]
    assert after_crash and min(after_crash) < 500, "the restart must reset RSS"

    kills = [log for log in bundle.logs if log.attrs.get("event") == "oom_kill"]
    restarts = [log for log in bundle.logs if log.attrs.get("event") == "restart"]
    assert len(kills) == 1 and kills[0].attrs["exit_code"] == 137
    assert len(restarts) == 1
    assert "OOMKilled" in kills[0].message
    assert any(log.attrs.get("event") == "gc_pressure" for log in bundle.logs)

    # no deploy, and the error rate only spikes at the crash
    assert not [log for log in bundle.logs if log.attrs.get("event") == "deploy"]
    early = [
        m.value
        for m in bundle.series(worker, "error_rate")
        if 0 <= (m.timestamp - bundle.incident_start).total_seconds() < 600
    ]
    assert max(early) < 0.10


def test_slow_query_propagates_from_the_database_upstream():
    bundle = build("slow_query")

    db_ratio = bundle.peak("orders-db", "latency_p95_ms") / bundle.baseline_mean("orders-db", "latency_p95_ms")
    assert db_ratio > 20
    assert bundle.peak("orders-db", "cpu_pct") > 85

    db_onset = onset_offset(bundle, "orders-db", "latency_p95_ms", factor=2)
    pay_onset = onset_offset(bundle, "payments-service", "latency_p95_ms", factor=2)
    gw_onset = onset_offset(bundle, "api-gateway", "latency_p95_ms", factor=2)
    assert db_onset is not None and pay_onset is not None and gw_onset is not None
    assert db_onset < pay_onset < gw_onset, "the database must degrade first"

    slow = [log for log in bundle.logs if log.attrs.get("event") == "slow_query"]
    assert len(slow) >= 5
    assert all(log.service == "orders-db" for log in slow)
    assert all(log.attrs["query_id"] == "q_7712" for log in slow)
    assert "Seq Scan" in slow[0].attrs["plan"]
    assert any(log.attrs.get("event") == "plan_change" for log in bundle.logs)

    # memory flat, pools elevated but never pinned -- separates it from 2 and 4
    assert bundle.peak("orders-db", "memory_mb") / bundle.baseline_mean("orders-db", "memory_mb") < 1.2
    assert bundle.peak("orders-db", "conn_pool_used_pct") < 95


def test_connection_pool_exhaustion_cascades_while_the_db_stays_healthy():
    bundle = build("conn_pool_exhaustion")

    assert bundle.peak("auth-service", "conn_pool_used_pct") > 99
    assert bundle.peak("auth-service", "latency_p95_ms") > 1500
    assert bundle.peak("auth-service", "error_rate") > 0.35

    # the database is a victim of held connections, not the cause
    db_latency_ratio = bundle.peak("orders-db", "latency_p95_ms") / bundle.baseline_mean(
        "orders-db", "latency_p95_ms"
    )
    assert db_latency_ratio < 1.5
    assert bundle.peak("orders-db", "cpu_pct") < 60

    auth_onset = onset_offset(bundle, "auth-service", "error_rate", factor=5)
    gw_onset = onset_offset(bundle, "api-gateway", "error_rate", factor=5)
    worker_onset = onset_offset(bundle, "notification-worker", "error_rate", factor=5)
    assert auth_onset < gw_onset < worker_onset, "failures follow the dependency order"

    exhausted = [log for log in bundle.logs if log.attrs.get("event") == "pool_exhausted"]
    assert len(exhausted) >= 4
    assert all(log.attrs["pool"] == "orders-db" and log.attrs["pool_size"] == 20 for log in exhausted)
    assert all(log.service == "auth-service" for log in exhausted)
    assert any(log.attrs.get("event") == "idle_in_transaction" for log in bundle.logs)

    pool_errors = [
        log for log in bundle.logs_for("auth-service", "ERROR") if log.attrs["error_type"] == "PoolTimeoutError"
    ]
    assert len(pool_errors) > 50
    assert bundle.peak("auth-service", "memory_mb") / bundle.baseline_mean("auth-service", "memory_mb") < 1.2


def test_each_scenario_blames_a_different_service():
    primaries = {key: build(key).ground_truth.primary_service for key in ALL_KEYS}
    assert len(set(primaries.values())) == len(ALL_KEYS)


@pytest.mark.parametrize("key", ALL_KEYS)
def test_ground_truth_is_populated_but_never_leaks_into_the_payload(key):
    bundle = build(key)
    gt = bundle.ground_truth
    assert gt.scenario_key == key
    assert gt.summary and len(gt.expected_signals) >= 3
    assert gt.injected_events and gt.injected_events[0]["timestamp"]

    assert "ground_truth" not in bundle.to_dict()
    assert "ground_truth" in bundle.to_dict(include_ground_truth=True)


# --------------------------------------------------------------------------- #
# Output files / CLI
# --------------------------------------------------------------------------- #


def test_writers_round_trip(tmp_path):
    bundle = build("slow_query")

    logs_path = bundle.write_logs_jsonl(tmp_path / "logs.jsonl")
    lines = logs_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(bundle.logs)
    first = json.loads(lines[0])
    assert {"id", "timestamp", "service", "level", "message", "trace_id"} <= set(first)

    metrics_path = bundle.write_metrics_csv(tmp_path / "metrics.csv")
    rows = metrics_path.read_text(encoding="utf-8").strip().splitlines()
    assert rows[0] == "timestamp,service,metric,value"
    assert len(rows) == len(bundle.metrics) + 1


def test_cli_prints_a_summary_and_writes_files(tmp_path, capsys):
    exit_code = main(["--scenario", "bad_deploy", "--seed", "7", "--out", str(tmp_path)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Bad deploy" in out
    assert "injected marker events" in out
    assert "post-onset ERROR lines" in out
    assert "deployment completed: payments-service v2.4.1" in out
    assert (tmp_path / "bad_deploy" / "logs.jsonl").exists()
    assert (tmp_path / "bad_deploy" / "metrics.csv").exists()
    meta = json.loads((tmp_path / "bad_deploy" / "incident.json").read_text(encoding="utf-8"))
    assert meta["scenario_key"] == "bad_deploy"
    assert meta["ground_truth"]["primary_service"] == "payments-service"


def test_generate_incident_helper_accepts_generator_kwargs():
    bundle = generate_incident("memory_leak", seed=5, duration_minutes=12, incident_start_minute=2)
    assert bundle.scenario_key == "memory_leak"
    assert (bundle.window_end - bundle.window_start) == timedelta(minutes=12)
