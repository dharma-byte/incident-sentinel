"""Query helpers shared by the API and (from Phase 3) the agents.

Keeping persistence here means the routers stay thin and the agents read
evidence through the same code path the API does.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from app.db.models import (
    AgentTraceStep,
    Incident,
    IncidentEvidence,
    IncidentLog,
    IncidentMetric,
    IncidentStatus,
)
from app.simulator.generator import IncidentBundle

# Rows per executemany batch when loading a bundle (~5.7k logs per incident).
INSERT_CHUNK = 1000


def _chunks(rows: list[dict], size: int = INSERT_CHUNK):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


def create_incident_from_bundle(db: Session, bundle: IncidentBundle) -> Incident:
    """Persist a simulated incident with all of its logs and metrics.

    Row ids are freshly generated rather than taken from the bundle: bundle ids
    are a deterministic function of ``(scenario, seed, window)``, so re-running
    the same simulation would otherwise collide on the primary key.
    """
    incident = Incident(
        id=uuid.uuid4(),
        scenario_type=bundle.scenario_key,
        triggered_at=bundle.incident_start,
        status=IncidentStatus.PENDING,
        seed=bundle.seed,
        window_start=bundle.window_start,
        window_end=bundle.window_end,
        ground_truth=bundle.ground_truth.to_dict(),
    )
    db.add(incident)
    db.flush()  # assign the PK before the bulk inserts reference it

    log_rows = [
        {
            "id": uuid.uuid4(),
            "incident_id": incident.id,
            "timestamp": log.timestamp,
            "service": log.service,
            "level": log.level,
            "message": log.message,
            "trace_id": log.trace_id,
            "attrs": log.attrs,
        }
        for log in bundle.logs
    ]
    metric_rows = [
        {
            "id": uuid.uuid4(),
            "incident_id": incident.id,
            "timestamp": point.timestamp,
            "service": point.service,
            "metric": point.metric,
            "value": point.value,
        }
        for point in bundle.metrics
    ]

    for chunk in _chunks(log_rows):
        db.execute(insert(IncidentLog), chunk)
    for chunk in _chunks(metric_rows):
        db.execute(insert(IncidentMetric), chunk)

    db.commit()
    db.refresh(incident)
    return incident


def set_incident_status(db: Session, incident: Incident, status: str) -> Incident:
    incident.status = status
    db.commit()
    db.refresh(incident)
    return incident


def add_evidence(
    db: Session,
    incident_id: uuid.UUID,
    *,
    source: str,
    service: str,
    excerpt: str,
    timestamp: datetime,
    source_ref: uuid.UUID | None = None,
) -> IncidentEvidence:
    evidence = IncidentEvidence(
        incident_id=incident_id,
        source=source,
        service=service,
        excerpt=excerpt,
        timestamp=timestamp,
        source_ref=source_ref,
    )
    db.add(evidence)
    db.flush()
    return evidence


def add_trace_step(
    db: Session,
    incident_id: uuid.UUID,
    *,
    agent_name: str,
    step_order: int,
    input_summary: str | None = None,
    output_summary: str | None = None,
    reasoning: str | None = None,
    evidence_refs: list[uuid.UUID] | None = None,
    duration_ms: int | None = None,
) -> AgentTraceStep:
    step = AgentTraceStep(
        incident_id=incident_id,
        agent_name=agent_name,
        step_order=step_order,
        input_summary=input_summary,
        output_summary=output_summary,
        reasoning=reasoning,
        evidence_refs=evidence_refs or [],
        duration_ms=duration_ms,
    )
    db.add(step)
    db.flush()
    return step


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def get_incident(db: Session, incident_id: uuid.UUID) -> Incident | None:
    return db.get(Incident, incident_id)


def list_incidents(
    db: Session,
    *,
    limit: int = 25,
    offset: int = 0,
    scenario_type: str | None = None,
    status: str | None = None,
) -> list[Incident]:
    stmt = select(Incident).order_by(Incident.created_at.desc()).limit(limit).offset(offset)
    if scenario_type:
        stmt = stmt.where(Incident.scenario_type == scenario_type)
    if status:
        stmt = stmt.where(Incident.status == status)
    return list(db.scalars(stmt))


def fetch_logs(
    db: Session,
    incident_id: uuid.UUID,
    *,
    service: str | None = None,
    level: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = 500,
) -> list[IncidentLog]:
    stmt = (
        select(IncidentLog)
        .where(IncidentLog.incident_id == incident_id)
        .order_by(IncidentLog.timestamp)
    )
    if service:
        stmt = stmt.where(IncidentLog.service == service)
    if level:
        stmt = stmt.where(IncidentLog.level == level)
    if since:
        stmt = stmt.where(IncidentLog.timestamp >= since)
    if until:
        stmt = stmt.where(IncidentLog.timestamp <= until)
    if limit:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt))


def fetch_metrics(
    db: Session,
    incident_id: uuid.UUID,
    *,
    service: str | None = None,
    metric: str | None = None,
) -> list[IncidentMetric]:
    stmt = (
        select(IncidentMetric)
        .where(IncidentMetric.incident_id == incident_id)
        .order_by(IncidentMetric.timestamp)
    )
    if service:
        stmt = stmt.where(IncidentMetric.service == service)
    if metric:
        stmt = stmt.where(IncidentMetric.metric == metric)
    return list(db.scalars(stmt))


def fetch_evidence(db: Session, incident_id: uuid.UUID) -> list[IncidentEvidence]:
    stmt = (
        select(IncidentEvidence)
        .where(IncidentEvidence.incident_id == incident_id)
        .order_by(IncidentEvidence.timestamp)
    )
    return list(db.scalars(stmt))


def fetch_trace_steps(db: Session, incident_id: uuid.UUID) -> list[AgentTraceStep]:
    stmt = (
        select(AgentTraceStep)
        .where(AgentTraceStep.incident_id == incident_id)
        .order_by(AgentTraceStep.step_order)
    )
    return list(db.scalars(stmt))


def incident_counts(db: Session, incident_id: uuid.UUID) -> dict[str, int]:
    """Row counts used by GET /incidents/{id}."""

    def count(model) -> int:
        return db.scalar(
            select(func.count()).select_from(model).where(model.incident_id == incident_id)
        ) or 0

    return {
        "log_count": count(IncidentLog),
        "metric_count": count(IncidentMetric),
        "evidence_count": count(IncidentEvidence),
        "trace_step_count": count(AgentTraceStep),
    }


def services_for(db: Session, incident_id: uuid.UUID) -> list[str]:
    stmt = (
        select(IncidentMetric.service)
        .where(IncidentMetric.incident_id == incident_id)
        .distinct()
        .order_by(IncidentMetric.service)
    )
    return list(db.scalars(stmt))
