"""Incident read endpoints.

``POST /incidents/{id}/triage`` arrives in Phase 3 with the agent pipeline.
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from typing import Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.agents.orchestrator import triage_incident
from app.cache.redis_client import get_cache, triage_key
from app.db import repository
from app.db.models import Incident
from app.db.session import SessionLocal, get_db
from app.llm.client import LLMError, get_llm_client
from app.schemas import (
    IncidentDetail,
    IncidentSummary,
    LogOut,
    MetricPointOut,
    TraceResponse,
    TriageRequest,
    TriageResponse,
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


def _require_llm(provider: str | None):
    llm = get_llm_client(provider)
    if not llm.available():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"LLM provider '{llm.name}' is not reachable. Start Ollama (or set GROQ_API_KEY), "
            f"or pass {{\"provider\": \"stub\"}} to run the pipeline without a model.",
        )
    return llm


@router.post("/{incident_id}/triage", response_model=TriageResponse)
def triage(
    incident_id: uuid.UUID,
    payload: TriageRequest | None = None,
    db: Session = Depends(get_db),
) -> TriageResponse:
    """Run the full agent pipeline against the incident.

    Synchronous: the response arrives when the last agent finishes. An
    identical request (same incident, same model) is served from Redis instead
    of spending four more LLM calls -- pass ``refresh`` to force a re-run.
    Use ``GET /incidents/{id}/triage/stream`` to watch the steps arrive live.
    """
    _load(db, incident_id)
    llm = _require_llm(payload.provider if payload else None)

    cache = get_cache()
    key = triage_key(incident_id, llm.name, llm.model)
    if not (payload and payload.refresh):
        cached = cache.get(key)
        if cached:
            return TriageResponse(**{**cached, "cached": True})

    try:
        result = triage_incident(db, incident_id, llm=llm)
    except LLMError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"LLM call failed: {exc}") from exc

    cache.set(key, result)
    return TriageResponse(**result)


@router.get("/{incident_id}/triage/stream")
def triage_stream(
    incident_id: uuid.UUID,
    provider: str | None = None,
    refresh: bool = False,
    db: Session = Depends(get_db),
) -> EventSourceResponse:
    """Stream the pipeline's progress as Server-Sent Events.

    A GET endpoint because that is what the browser's EventSource can call.
    Events: ``status`` once at the start, ``step`` as each agent finishes,
    then ``complete`` (or ``error``).
    """
    _load(db, incident_id)
    llm = _require_llm(provider)
    cache = get_cache()
    key = triage_key(incident_id, llm.name, llm.model)
    cached = None if refresh else cache.get(key)

    return EventSourceResponse(
        _triage_events(incident_id, llm, cache, key, cached),
        ping=15,  # keep intermediaries from closing an idle connection
    )


async def _triage_events(incident_id, llm, cache, key, cached):
    """Yield SSE events while the pipeline runs on a worker thread."""
    if cached:
        yield {"event": "status", "data": json.dumps({"state": "cached", "model": llm.model})}
        for step in cached.get("trace", []):
            yield {"event": "step", "data": json.dumps(step)}
        yield {"event": "complete", "data": json.dumps({**cached, "cached": True})}
        return

    yield {
        "event": "status",
        "data": json.dumps({"state": "running", "provider": llm.name, "model": llm.model}),
    }

    events: queue.Queue = queue.Queue()
    result: dict[str, Any] = {}

    def worker() -> None:
        # Its own session: this runs off the request's thread.
        db = SessionLocal()
        try:
            result["value"] = triage_incident(
                db,
                incident_id,
                llm=llm,
                on_step=lambda name, output: events.put(
                    {"event": "step", "data": json.dumps(output.to_dict(), default=str)}
                ),
            )
        except Exception as exc:  # surfaced to the client as an error event
            result["error"] = str(exc)
        finally:
            db.close()
            events.put(None)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    while True:
        try:
            event = events.get_nowait()
        except queue.Empty:
            await anyio.sleep(0.2)
            continue
        if event is None:
            break
        yield event

    if "error" in result:
        yield {"event": "error", "data": json.dumps({"detail": result["error"]})}
        return

    payload = result.get("value", {})
    cache.set(key, payload)
    yield {"event": "complete", "data": json.dumps(payload, default=str)}


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
