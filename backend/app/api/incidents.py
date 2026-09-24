"""Incident read endpoints.

``POST /incidents/{id}/triage`` arrives in Phase 3 with the agent pipeline.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import repository
from app.db.models import Incident
from app.db.session import get_db
from app.schemas import (
    IncidentDetail,
    IncidentSummary,
    LogOut,
    MetricPointOut,
    TraceResponse,
)

router = APIRouter(prefix="/incidents", tags=["incidents"])


def _load(db: Session, incident_id: uuid.UUID) -> Incident:
    incident = repository.get_incident(db, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"incident {incident_id} not found")
    return incident


@router.get("", response_model=list[IncidentSummary])
def list_incidents(
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    scenario_type: str | None = None,
    incident_status: str | None = Query(None, alias="status"),
    db: Session = Depends(get_db),
) -> list[Incident]:
    """Past incidents, newest first, with the history list's filters."""
    return repository.list_incidents(
        db, limit=limit, offset=offset, scenario_type=scenario_type, status=incident_status
    )


@router.get("/{incident_id}", response_model=IncidentDetail)
def get_incident(incident_id: uuid.UUID, db: Session = Depends(get_db)) -> IncidentDetail:
    """Metadata, status and (once triaged) the final diagnosis."""
    incident = _load(db, incident_id)
    return IncidentDetail(
        **IncidentSummary.model_validate(incident).model_dump(),
        seed=incident.seed,
        window_start=incident.window_start,
        window_end=incident.window_end,
        **repository.incident_counts(db, incident_id),
    )


@router.get("/{incident_id}/trace", response_model=TraceResponse)
def get_trace(incident_id: uuid.UUID, db: Session = Depends(get_db)) -> TraceResponse:
    """The step-by-step reasoning trace behind the UI timeline."""
    incident = _load(db, incident_id)
    return TraceResponse(
        incident_id=incident.id,
        status=incident.status,
        root_cause=incident.root_cause,
        confidence=incident.confidence,
        steps=repository.fetch_trace_steps(db, incident_id),
        evidence=repository.fetch_evidence(db, incident_id),
    )


@router.get("/{incident_id}/logs", response_model=list[LogOut])
def get_logs(
    incident_id: uuid.UUID,
    service: str | None = None,
    level: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> list[LogOut]:
    """Stored log lines -- lets a reviewer check any citation by hand."""
    _load(db, incident_id)
    return repository.fetch_logs(db, incident_id, service=service, level=level, limit=limit)


@router.get("/{incident_id}/metrics", response_model=list[MetricPointOut])
def get_metrics(
    incident_id: uuid.UUID,
    service: str | None = None,
    metric: str | None = None,
    db: Session = Depends(get_db),
) -> list[MetricPointOut]:
    """Stored metric series, for the evidence panel's charts."""
    _load(db, incident_id)
    return repository.fetch_metrics(db, incident_id, service=service, metric=metric)
