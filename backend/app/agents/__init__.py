"""Shared types for the agent pipeline.

Every agent follows the same contract: it reads the incident context plus the
outputs of prior agents, does its own deterministic evidence extraction, asks
the LLM to reason over that evidence, and returns an :class:`AgentOutput`.

Citations use short tokens (``L3``, ``M7``) rather than UUIDs: small models
reproduce them reliably, and :class:`EvidenceBook` maps each one back to the
stored ``incident_logs`` / ``incident_metrics`` row it came from. Anything the
model invents resolves to nothing and is dropped.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.db import repository
from app.db.models import EvidenceSource, Incident, IncidentLog, IncidentMetric


@dataclass
class EvidenceItem:
    """One citable fact, traceable to a stored row."""

    ref: str  # short token shown to the model, e.g. "L3"
    source: str  # 'log' | 'metric'
    service: str
    excerpt: str
    timestamp: datetime
    source_ref: uuid.UUID | None = None

    def render(self) -> str:
        return f"[{self.ref}] {self.timestamp.isoformat()} {self.service} ({self.source}): {self.excerpt}"


class EvidenceBook:
    """Collects evidence, hands out citation tokens, resolves them back."""

    def __init__(self) -> None:
        self._items: dict[str, EvidenceItem] = {}
        self._counters: dict[str, int] = {"log": 0, "metric": 0}

    def __len__(self) -> int:
        return len(self._items)

    @property
    def items(self) -> list[EvidenceItem]:
        return list(self._items.values())

    def add_log(self, log: IncidentLog, excerpt: str | None = None) -> EvidenceItem:
        self._counters["log"] += 1
        item = EvidenceItem(
            ref=f"L{self._counters['log']}",
            source=EvidenceSource.LOG,
            service=log.service,
            excerpt=excerpt or f"{log.level} {log.message}",
            timestamp=log.timestamp,
            source_ref=log.id,
        )
        self._items[item.ref] = item
        return item

    def add_metric(self, point: IncidentMetric, excerpt: str) -> EvidenceItem:
        self._counters["metric"] += 1
        item = EvidenceItem(
            ref=f"M{self._counters['metric']}",
            source=EvidenceSource.METRIC,
            service=point.service,
            excerpt=excerpt,
            timestamp=point.timestamp,
            source_ref=point.id,
        )
        self._items[item.ref] = item
        return item

    def render(self) -> str:
        return "\n".join(item.render() for item in self._items.values()) or "(none)"

    def resolve(self, refs: Iterable[Any]) -> list[EvidenceItem]:
        """Keep only citations that name real evidence, preserving order."""
        seen: list[EvidenceItem] = []
        for ref in refs or []:
            item = self._items.get(str(ref).strip().upper())
            if item is not None and item not in seen:
                seen.append(item)
        return seen

    def merge(self, other: "EvidenceBook") -> None:
        """Absorb another book's items, keeping their tokens."""
        self._items.update(other._items)
        for kind, count in other._counters.items():
            self._counters[kind] = max(self._counters[kind], count)


@dataclass
class AgentOutput:
    """What one agent contributes to the reasoning trace."""

    agent_name: str
    summary: str
    reasoning: str
    findings: dict[str, Any] = field(default_factory=dict)
    evidence: list[EvidenceItem] = field(default_factory=list)
    input_summary: str = ""
    duration_ms: int = 0
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "summary": self.summary,
            "reasoning": self.reasoning,
            "findings": self.findings,
            "evidence_refs": [item.ref for item in self.evidence],
            "duration_ms": self.duration_ms,
            "model": self.model,
        }


@dataclass
class IncidentContext:
    """The incident's stored data, loaded once and shared by every agent."""

    incident: Incident
    logs: list[IncidentLog]
    metrics: list[IncidentMetric]

    @classmethod
    def load(cls, db: Session, incident_id: uuid.UUID) -> "IncidentContext":
        incident = repository.get_incident(db, incident_id)
        if incident is None:
            raise ValueError(f"incident {incident_id} not found")
        return cls(
            incident=incident,
            logs=repository.fetch_logs(db, incident_id, limit=None),
            metrics=repository.fetch_metrics(db, incident_id),
        )

    # -- convenience ----------------------------------------------------- #

    @property
    def window_start(self) -> datetime:
        return self.incident.window_start

    @property
    def window_end(self) -> datetime:
        return self.incident.window_end

    @property
    def incident_start(self) -> datetime:
        return self.incident.triggered_at

    def offset(self, ts: datetime) -> float:
        """Seconds relative to onset (negative before it)."""
        return (ts - self.incident_start).total_seconds()

    def services(self) -> list[str]:
        return sorted({point.service for point in self.metrics})

    def logs_before(self) -> list[IncidentLog]:
        return [log for log in self.logs if log.timestamp < self.incident_start]

    def logs_after(self) -> list[IncidentLog]:
        return [log for log in self.logs if log.timestamp >= self.incident_start]

    def series(self, service: str, metric: str) -> list[IncidentMetric]:
        return [p for p in self.metrics if p.service == service and p.metric == metric]


# The simulated topology, which the reasoning agents are told about. It mirrors
# app.simulator.generator.SERVICES but is stated here because a real deployment
# would read it from a service catalogue, not from the traffic generator.
TOPOLOGY: dict[str, tuple[str, ...]] = {
    "api-gateway": ("auth-service", "payments-service"),
    "auth-service": ("orders-db",),
    "payments-service": ("orders-db",),
    "orders-db": (),
    "notification-worker": ("orders-db",),
}


def render_topology() -> str:
    lines = []
    for service, deps in TOPOLOGY.items():
        lines.append(f"{service} -> {', '.join(deps) if deps else '(no dependencies)'}")
    return "\n".join(lines)


__all__ = [
    "AgentOutput",
    "EvidenceBook",
    "EvidenceItem",
    "IncidentContext",
    "TOPOLOGY",
    "render_topology",
]
