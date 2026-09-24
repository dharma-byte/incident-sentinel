"""SQLAlchemy models (Section 7 of the build spec).

The three tables the spec names -- ``incidents``, ``incident_evidence`` and
``agent_trace_steps`` -- are implemented verbatim. Two extra tables,
``incident_logs`` and ``incident_metrics``, hold the raw output of the
simulator so that every evidence citation can point at a real stored row
rather than a re-generated string.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class IncidentStatus:
    """Allowed values for ``incidents.status``."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"

    ALL = (PENDING, RUNNING, COMPLETE, FAILED)


class EvidenceSource:
    LOG = "log"
    METRIC = "metric"

    ALL = (LOG, METRIC)


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'complete', 'failed')", name="ck_incidents_status"
        ),
        Index("ix_incidents_created_at", "created_at"),
        Index("ix_incidents_scenario_type", "scenario_type"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    scenario_type: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default=IncidentStatus.PENDING)
    root_cause: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # -- beyond Section 7: what it takes to replay and score an incident ---- #
    seed: Mapped[int] = mapped_column(Integer, nullable=False, default=1337)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # What the simulator actually injected. Used only by tests / scoring --
    # never handed to an agent and never returned by the public API.
    ground_truth: Mapped[dict | None] = mapped_column(JSONB)

    logs: Mapped[list["IncidentLog"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", passive_deletes=True
    )
    metrics: Mapped[list["IncidentMetric"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", passive_deletes=True
    )
    evidence: Mapped[list["IncidentEvidence"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan", passive_deletes=True
    )
    trace_steps: Mapped[list["AgentTraceStep"]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AgentTraceStep.step_order",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Incident {self.id} {self.scenario_type} {self.status}>"


class IncidentLog(Base):
    """One structured log line produced by the simulator."""

    __tablename__ = "incident_logs"
    __table_args__ = (
        Index("ix_incident_logs_incident_ts", "incident_id", "timestamp"),
        Index("ix_incident_logs_incident_service_level", "incident_id", "service", "level"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    incident_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    service: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    attrs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    incident: Mapped[Incident] = relationship(back_populates="logs")


class IncidentMetric(Base):
    """One metric sample for one service."""

    __tablename__ = "incident_metrics"
    __table_args__ = (
        Index("ix_incident_metrics_lookup", "incident_id", "service", "metric", "timestamp"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    incident_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    service: Mapped[str] = mapped_column(Text, nullable=False)
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)

    incident: Mapped[Incident] = relationship(back_populates="metrics")


class IncidentEvidence(Base):
    """A log line or metric window an agent cited in its reasoning."""

    __tablename__ = "incident_evidence"
    __table_args__ = (
        CheckConstraint("source IN ('log', 'metric')", name="ck_evidence_source"),
        Index("ix_incident_evidence_incident", "incident_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    incident_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    service: Mapped[str] = mapped_column(Text, nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Back-pointer to the incident_logs / incident_metrics row this came from,
    # so a citation is always checkable against stored data.
    source_ref: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    incident: Mapped[Incident] = relationship(back_populates="evidence")


class AgentTraceStep(Base):
    """One agent's contribution to the reasoning trace shown in the UI."""

    __tablename__ = "agent_trace_steps"
    __table_args__ = (
        Index("ix_agent_trace_steps_incident_order", "incident_id", "step_order", unique=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    incident_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    input_summary: Mapped[str | None] = mapped_column(Text)
    output_summary: Mapped[str | None] = mapped_column(Text)
    reasoning: Mapped[str | None] = mapped_column(Text)
    evidence_refs: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, default=list
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    incident: Mapped[Incident] = relationship(back_populates="trace_steps")


__all__ = [
    "Base",
    "Incident",
    "IncidentLog",
    "IncidentMetric",
    "IncidentEvidence",
    "AgentTraceStep",
    "IncidentStatus",
    "EvidenceSource",
]
