"""API and persistence tests (Phase 2).

These exercise the real PostgreSQL schema -- POST /simulate has to land ~5.7k
log rows and 3k metric rows, and the read endpoints have to return them.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.db.models import Incident, IncidentLog, IncidentMetric, IncidentStatus
from app.simulator.scenarios import SCENARIOS


def simulate(client, scenario: str = "bad_deploy", **overrides) -> dict:
    payload = {"scenario_type": scenario, "seed": 1337, **overrides}
    response = client.post("/simulate", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# health / scenarios
# --------------------------------------------------------------------------- #


def test_health_reports_a_live_database(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["database"] == "up"
    assert body["scenarios"] == 4
    assert body["llm_provider"] in ("ollama", "groq")


def test_scenario_catalogue_lists_all_four(client):
    body = client.get("/scenarios").json()
    assert {entry["key"] for entry in body} == set(SCENARIOS)
    for entry in body:
        assert entry["title"] and entry["description"] and entry["primary_service"]


# --------------------------------------------------------------------------- #
# POST /simulate
# --------------------------------------------------------------------------- #


def test_simulate_persists_the_whole_bundle(client, db_session):
    body = simulate(client, "slow_query")
    incident_id = uuid.UUID(body["incident_id"])

    assert body["scenario_type"] == "slow_query"
    assert body["status"] == IncidentStatus.PENDING
    assert body["log_count"] > 4000 and body["metric_count"] == 3000
    assert len(body["services"]) == 5

    incident = db_session.get(Incident, incident_id)
    assert incident is not None
    assert incident.window_start < incident.triggered_at < incident.window_end
    assert incident.root_cause is None and incident.confidence is None

    def count(model) -> int:
        return db_session.scalar(
            select(func.count()).select_from(model).where(model.incident_id == incident_id)
        )

    assert count(IncidentLog) == body["log_count"]
    assert count(IncidentMetric) == body["metric_count"]


def test_persisted_logs_keep_their_structure(client, db_session):
    body = simulate(client, "conn_pool_exhaustion")
    incident_id = uuid.UUID(body["incident_id"])

    marker = db_session.scalars(
        select(IncidentLog)
        .where(IncidentLog.incident_id == incident_id)
        .where(IncidentLog.attrs["event"].astext == "pool_exhausted")
        .order_by(IncidentLog.timestamp)
    ).first()
    assert marker is not None, "JSONB attrs must survive the round trip"
    assert marker.service == "auth-service"
    assert marker.attrs["pool_size"] == 20
    assert marker.level == "WARN"

    stacked = db_session.scalars(
        select(IncidentLog)
        .where(IncidentLog.incident_id == incident_id)
        .where(IncidentLog.attrs["error_type"].astext == "PoolTimeoutError")
    ).first()
    assert "stack_trace" in stacked.attrs


def test_repeating_a_simulation_creates_a_separate_incident(client, db_session):
    first = simulate(client, "bad_deploy")
    second = simulate(client, "bad_deploy")
    assert first["incident_id"] != second["incident_id"]
    assert db_session.scalar(select(func.count()).select_from(Incident)) == 2


def test_simulate_rejects_bad_input(client):
    assert client.post("/simulate", json={"scenario_type": "solar_flare"}).status_code == 422
    assert client.post("/simulate", json={}).status_code == 422
    bad_window = client.post(
        "/simulate",
        json={"scenario_type": "bad_deploy", "duration_minutes": 10, "incident_start_minute": 10},
    )
    assert bad_window.status_code == 422


def test_simulate_honours_window_overrides(client):
    body = simulate(client, "memory_leak", duration_minutes=12, incident_start_minute=2)
    assert body["metric_count"] == 12 * 60 // 15 * 5 * 5


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #


def test_get_incident_returns_counts(client):
    created = simulate(client, "bad_deploy")
    body = client.get(f"/incidents/{created['incident_id']}").json()

    assert body["id"] == created["incident_id"]
    assert body["scenario_type"] == "bad_deploy"
    assert body["seed"] == 1337
    assert body["log_count"] == created["log_count"]
    assert body["metric_count"] == 3000
    assert body["evidence_count"] == 0  # nothing triaged yet
    assert body["trace_step_count"] == 0


def test_list_incidents_filters_and_orders(client):
    simulate(client, "bad_deploy")
    simulate(client, "memory_leak")
    simulate(client, "slow_query")

    every = client.get("/incidents").json()
    assert len(every) == 3
    assert every[0]["created_at"] >= every[-1]["created_at"]

    filtered = client.get("/incidents", params={"scenario_type": "memory_leak"}).json()
    assert len(filtered) == 1 and filtered[0]["scenario_type"] == "memory_leak"

    assert client.get("/incidents", params={"status": "complete"}).json() == []
    assert len(client.get("/incidents", params={"limit": 2}).json()) == 2


def test_logs_endpoint_filters_by_service_and_level(client):
    created = simulate(client, "bad_deploy")
    incident_id = created["incident_id"]

    errors = client.get(
        f"/incidents/{incident_id}/logs",
        params={"service": "payments-service", "level": "ERROR", "limit": 50},
    ).json()
    assert errors
    assert all(log["service"] == "payments-service" and log["level"] == "ERROR" for log in errors)
    assert all(log["attrs"]["error_type"] == "TypeError" for log in errors)
    assert errors == sorted(errors, key=lambda log: log["timestamp"])


def test_metrics_endpoint_returns_a_single_series(client):
    created = simulate(client, "memory_leak")
    series = client.get(
        f"/incidents/{created['incident_id']}/metrics",
        params={"service": "notification-worker", "metric": "memory_mb"},
    ).json()

    assert len(series) == 120  # 30 minutes at 15s resolution
    assert max(point["value"] for point in series) > 1900


def test_trace_is_empty_until_the_pipeline_runs(client):
    created = simulate(client, "slow_query")
    body = client.get(f"/incidents/{created['incident_id']}/trace").json()

    assert body["incident_id"] == created["incident_id"]
    assert body["status"] == IncidentStatus.PENDING
    assert body["steps"] == [] and body["evidence"] == []
    assert body["root_cause"] is None


def test_unknown_incident_is_a_404(client):
    missing = uuid.uuid4()
    assert client.get(f"/incidents/{missing}").status_code == 404
    assert client.get(f"/incidents/{missing}/trace").status_code == 404
    assert client.get(f"/incidents/{missing}/logs").status_code == 404
    assert client.get(f"/incidents/{uuid.uuid4()}/metrics").status_code == 404
    assert client.get("/incidents/not-a-uuid").status_code == 422


def test_ground_truth_is_stored_but_never_served(client, db_session):
    created = simulate(client, "conn_pool_exhaustion")
    incident = db_session.get(Incident, uuid.UUID(created["incident_id"]))

    assert incident.ground_truth["primary_service"] == "auth-service"
    assert incident.ground_truth["expected_signals"]

    detail = client.get(f"/incidents/{created['incident_id']}").json()
    assert "ground_truth" not in detail
    assert "ground_truth" not in created
