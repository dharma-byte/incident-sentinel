"""Orchestrator: a LangGraph state machine over the four agents.

Each node runs one agent, hands its output to the next, and persists the step
to ``agent_trace_steps`` together with the evidence it cited -- so the trace is
durable and inspectable even if a later node fails.

``on_step`` fires as each node completes; Phase 4's SSE endpoint hangs off it.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app.agents import AgentOutput, EvidenceItem, IncidentContext
from app.agents import fix_suggestion, log_analysis, metrics_correlation, root_cause
from app.db import repository
from app.db.models import Incident, IncidentStatus
from app.llm.client import LLMClient, get_llm_client

logger = logging.getLogger("incident_sentinel.orchestrator")

AGENT_SEQUENCE = (
    log_analysis.AGENT_NAME,
    metrics_correlation.AGENT_NAME,
    root_cause.AGENT_NAME,
    fix_suggestion.AGENT_NAME,
)

StepCallback = Callable[[str, AgentOutput], None]


class TriageState(TypedDict, total=False):
    """What flows between nodes. Each agent adds its own output."""

    incident_id: str
    log_output: AgentOutput
    metric_output: AgentOutput
    root_cause_output: AgentOutput
    fix_output: AgentOutput
    failures: list[str]


class TriageOrchestrator:
    """Runs the pipeline for one incident and writes the reasoning trace."""

    def __init__(
        self,
        db: Session,
        llm: LLMClient | None = None,
        on_step: StepCallback | None = None,
    ) -> None:
        self.db = db
        self.llm = llm or get_llm_client()
        self.on_step = on_step
        self._evidence_ids: dict[str, uuid.UUID] = {}
        self._step_order = 0
        self.graph = self._build_graph()

    # -- graph ----------------------------------------------------------- #

    def _build_graph(self):
        builder = StateGraph(TriageState)
        builder.add_node(log_analysis.AGENT_NAME, self._node_log_analysis)
        builder.add_node(metrics_correlation.AGENT_NAME, self._node_metrics)
        builder.add_node(root_cause.AGENT_NAME, self._node_root_cause)
        builder.add_node(fix_suggestion.AGENT_NAME, self._node_fix)

        builder.add_edge(START, log_analysis.AGENT_NAME)
        builder.add_edge(log_analysis.AGENT_NAME, metrics_correlation.AGENT_NAME)
        builder.add_edge(metrics_correlation.AGENT_NAME, root_cause.AGENT_NAME)
        builder.add_edge(root_cause.AGENT_NAME, fix_suggestion.AGENT_NAME)
        builder.add_edge(fix_suggestion.AGENT_NAME, END)
        return builder.compile()

    # -- nodes ----------------------------------------------------------- #

    def _node_log_analysis(self, state: TriageState) -> dict[str, Any]:
        output = log_analysis.run(self._ctx, self.llm)
        self._persist(output)
        return {"log_output": output}

    def _node_metrics(self, state: TriageState) -> dict[str, Any]:
        output = metrics_correlation.run(self._ctx, self.llm, state.get("log_output"))
        self._persist(output)
        return {"metric_output": output}

    def _node_root_cause(self, state: TriageState) -> dict[str, Any]:
        output = root_cause.run(
            self._ctx, self.llm, state.get("log_output"), state.get("metric_output")
        )
        self._persist(output)
        return {"root_cause_output": output}

    def _node_fix(self, state: TriageState) -> dict[str, Any]:
        output = fix_suggestion.run(self._ctx, self.llm, state.get("root_cause_output"))
        self._persist(output)
        return {"fix_output": output}

    # -- persistence ----------------------------------------------------- #

    def _evidence_id(self, item: EvidenceItem) -> uuid.UUID:
        """Store an evidence row once per incident, however often it is cited."""
        if item.ref not in self._evidence_ids:
            row = repository.add_evidence(
                self.db,
                self._incident.id,
                source=item.source,
                service=item.service,
                excerpt=item.excerpt,
                timestamp=item.timestamp,
                source_ref=item.source_ref,
            )
            self._evidence_ids[item.ref] = row.id
        return self._evidence_ids[item.ref]

    def _persist(self, output: AgentOutput) -> None:
        self._step_order += 1
        refs = [self._evidence_id(item) for item in output.evidence]
        reasoning = output.reasoning
        if output.findings:
            # Keep the structured findings with the step; the UI renders them.
            reasoning = reasoning or ""
        repository.add_trace_step(
            self.db,
            self._incident.id,
            agent_name=output.agent_name,
            step_order=self._step_order,
            input_summary=output.input_summary,
            output_summary=output.summary,
            reasoning=reasoning,
            evidence_refs=refs,
            duration_ms=output.duration_ms,
        )
        self.db.commit()
        logger.info(
            "step %d/%d %s done in %dms (%d evidence)",
            self._step_order,
            len(AGENT_SEQUENCE),
            output.agent_name,
            output.duration_ms,
            len(refs),
        )
        if self.on_step:
            self.on_step(output.agent_name, output)

    # -- entrypoint ------------------------------------------------------ #

    def run(self, incident_id: uuid.UUID) -> dict[str, Any]:
        """Triage one incident end to end, returning a result summary."""
        started = time.perf_counter()
        self._ctx = IncidentContext.load(self.db, incident_id)
        self._incident: Incident = self._ctx.incident
        self._evidence_ids.clear()
        self._step_order = 0

        # A re-run replaces the previous trace rather than appending to it.
        self._clear_previous_trace()
        repository.set_incident_status(self.db, self._incident, IncidentStatus.RUNNING)

        try:
            state: TriageState = self.graph.invoke({"incident_id": str(incident_id)})
        except Exception as exc:
            logger.exception("triage failed for %s", incident_id)
            repository.set_incident_status(self.db, self._incident, IncidentStatus.FAILED)
            raise

        diagnosis = state.get("root_cause_output")
        if diagnosis:
            self._incident.root_cause = diagnosis.findings.get("top_cause") or diagnosis.summary
            self._incident.confidence = diagnosis.findings.get("confidence")
        self._incident.status = IncidentStatus.COMPLETE
        self.db.commit()
        self.db.refresh(self._incident)

        return {
            "incident_id": str(incident_id),
            "status": self._incident.status,
            "root_cause": self._incident.root_cause,
            "confidence": self._incident.confidence,
            "steps": len(AGENT_SEQUENCE),
            "candidates": (diagnosis.findings.get("candidates") if diagnosis else []) or [],
            "fix": (state.get("fix_output").findings if state.get("fix_output") else {}),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "model": self.llm.model,
        }

    def _clear_previous_trace(self) -> None:
        for step in repository.fetch_trace_steps(self.db, self._incident.id):
            self.db.delete(step)
        for evidence in repository.fetch_evidence(self.db, self._incident.id):
            self.db.delete(evidence)
        self.db.commit()


def triage_incident(
    db: Session,
    incident_id: uuid.UUID,
    llm: LLMClient | None = None,
    on_step: StepCallback | None = None,
) -> dict[str, Any]:
    """Convenience wrapper used by the API and the tests."""
    return TriageOrchestrator(db, llm=llm, on_step=on_step).run(incident_id)
