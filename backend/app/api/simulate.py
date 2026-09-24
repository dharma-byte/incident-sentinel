"""POST /simulate -- trigger a synthetic incident and store it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.db import repository
from app.db.session import get_db
from app.schemas import ScenarioInfo, SimulateRequest, SimulateResponse
from app.simulator.generator import IncidentGenerator
from app.simulator.scenarios import get_scenario, list_scenarios

router = APIRouter(tags=["simulate"])


@router.get("/scenarios", response_model=list[ScenarioInfo])
def get_scenarios() -> list[ScenarioInfo]:
    """Catalogue behind the UI's simulate panel."""
    return [ScenarioInfo(**entry) for entry in list_scenarios()]


@router.post("/simulate", response_model=SimulateResponse, status_code=status.HTTP_201_CREATED)
def simulate(payload: SimulateRequest, db: Session = Depends(get_db)) -> SimulateResponse:
    """Generate one incident (logs + metrics) and persist it to PostgreSQL."""
    bundle = IncidentGenerator(
        get_scenario(payload.scenario_type),
        seed=payload.seed,
        duration_minutes=payload.duration_minutes,
        incident_start_minute=payload.incident_start_minute,
    ).generate()

    incident = repository.create_incident_from_bundle(db, bundle)

    return SimulateResponse(
        incident_id=incident.id,
        scenario_type=incident.scenario_type,
        status=incident.status,
        seed=incident.seed,
        triggered_at=incident.triggered_at,
        window_start=incident.window_start,
        window_end=incident.window_end,
        services=list(bundle.services),
        log_count=len(bundle.logs),
        metric_count=len(bundle.metrics),
    )
