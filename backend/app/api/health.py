"""GET /health -- liveness / readiness."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import settings
from app.db.session import check_connection
from app.llm.client import get_llm_client
from app.schemas import HealthResponse
from app.simulator.scenarios import SCENARIOS

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    database = "up" if check_connection() else "down"
    llm = get_llm_client()
    return HealthResponse(
        status="ok" if database == "up" else "degraded",
        environment=settings.environment,
        database=database,
        llm_provider=llm.name,
        llm_available=llm.available(),
        llm_model=llm.model,
        scenarios=len(SCENARIOS),
    )
