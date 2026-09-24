"""Pydantic request/response models for the API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.simulator.scenarios import SCENARIOS


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Scenarios / simulate
# --------------------------------------------------------------------------- #


class ScenarioInfo(BaseModel):
    key: str
    title: str
    description: str
    primary_service: str
    affected_services: list[str]


class SimulateRequest(BaseModel):
    scenario_type: str = Field(..., description="one of the registered failure scenarios")
    seed: int = Field(1337, ge=0, description="same seed reproduces the same incident")
    duration_minutes: int = Field(30, ge=5, le=180)
    incident_start_minute: int = Field(10, ge=1)

    @field_validator("scenario_type")
    @classmethod
    def known_scenario(cls, value: str) -> str:
        if value not in SCENARIOS:
            raise ValueError(f"unknown scenario_type {value!r}; expected one of {', '.join(sorted(SCENARIOS))}")
        return value

    @field_validator("incident_start_minute")
    @classmethod
    def onset_inside_window(cls, value: int, info) -> int:
        duration = info.data.get("duration_minutes")
        if duration is not None and value >= duration:
            raise ValueError("incident_start_minute must fall inside duration_minutes")
        return value


class SimulateResponse(BaseModel):
    incident_id: uuid.UUID
    scenario_type: str
    status: str
    seed: int
    triggered_at: datetime
    window_start: datetime
    window_end: datetime
    services: list[str]
    log_count: int
    metric_count: int


# --------------------------------------------------------------------------- #
# Incidents
# --------------------------------------------------------------------------- #


class IncidentSummary(ORMModel):
    id: uuid.UUID
    scenario_type: str
    status: str
    triggered_at: datetime
    created_at: datetime
    root_cause: str | None = None
    confidence: float | None = None


class IncidentDetail(IncidentSummary):
    seed: int
    window_start: datetime
    window_end: datetime
    log_count: int = 0
    metric_count: int = 0
    evidence_count: int = 0
    trace_step_count: int = 0


class LogOut(ORMModel):
    id: uuid.UUID
    timestamp: datetime
    service: str
    level: str
    message: str
    trace_id: str
    attrs: dict[str, Any] = Field(default_factory=dict)


class MetricPointOut(ORMModel):
    timestamp: datetime
    service: str
    metric: str
    value: float


class EvidenceOut(ORMModel):
    id: uuid.UUID
    source: str
    service: str
    excerpt: str
    timestamp: datetime
    source_ref: uuid.UUID | None = None


class TraceStepOut(ORMModel):
    id: uuid.UUID
    agent_name: str
    step_order: int
    input_summary: str | None = None
    output_summary: str | None = None
    reasoning: str | None = None
    evidence_refs: list[uuid.UUID] = Field(default_factory=list)
    duration_ms: int | None = None
    created_at: datetime


class TraceResponse(BaseModel):
    incident_id: uuid.UUID
    status: str
    root_cause: str | None = None
    confidence: float | None = None
    steps: list[TraceStepOut] = Field(default_factory=list)
    evidence: list[EvidenceOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


class HealthResponse(BaseModel):
    status: str
    environment: str
    database: str
    llm_provider: str
    scenarios: int
