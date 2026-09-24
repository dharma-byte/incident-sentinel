"""Root-Cause Reasoning Agent.

Takes the two upstream agents' findings and asks the LLM for a ranked list of
candidate causes. The agent supplies observations only -- never a catalogue of
known failure modes -- so a correct diagnosis is inference rather than a lookup.

Whatever comes back is validated: confidences are clamped, services are checked
against the real topology, and invented citations are dropped.
"""

from __future__ import annotations

import time
from typing import Any

from app.agents import (
    TOPOLOGY,
    AgentOutput,
    EvidenceBook,
    EvidenceItem,
    IncidentContext,
    render_topology,
)
from app.llm.client import LLMClient, LLMError
from app.llm.prompts import ROOT_CAUSE_SYSTEM, ROOT_CAUSE_USER

AGENT_NAME = "root_cause"

MAX_CANDIDATES = 3


def build_constraints(
    log_output: AgentOutput | None, metric_output: AgentOutput | None
) -> list[str]:
    """Observations any accepted explanation must also account for."""
    constraints: list[str] = []

    if metric_output:
        findings = metric_output.findings
        order = findings.get("propagation_order") or []
        if order:
            constraints.append(
                "Deviation order across services: "
                + " then ".join(f"{p['service']} at +{p['onset_offset_s']}s" for p in order)
            )
        for note in findings.get("stable_notes", []) or []:
            constraints.append(f"Stayed normal: {note}")

        # Dependencies that are healthy while their callers suffer.
        deviating = set(findings.get("deviating_services") or [])
        for service in sorted(deviating):
            for dependency in TOPOLOGY.get(service, ()):
                if dependency not in deviating:
                    constraints.append(
                        f"{service} is degraded but its dependency {dependency} shows no "
                        f"metric deviation at all"
                    )

    if log_output:
        volume = log_output.findings.get("volume", {})
        quiet = [
            service
            for service, v in volume.items()
            if not v.get("spike") and service in (log_output.findings.get("key_services") or [])
        ]
        if quiet:
            constraints.append(
                "Implicated by logs but no error-rate spike: " + ", ".join(quiet)
            )
        markers = log_output.findings.get("markers", []) or []
        if not markers:
            constraints.append("No operational marker events (deploys, restarts, kills) were logged")

    return constraints or ["(no additional constraints)"]


def _merge_evidence(*outputs: AgentOutput | None) -> EvidenceBook:
    """Re-expose upstream evidence under its original citation tokens."""
    book = EvidenceBook()
    for output in outputs:
        if not output:
            continue
        for item in output.evidence:
            book._items[item.ref] = item  # tokens are already unique per source type
    return book


def _validate_candidates(
    raw: Any, book: EvidenceBook, services: list[str]
) -> list[dict[str, Any]]:
    """Clamp confidences, check services, drop invented citations."""
    candidates: list[dict[str, Any]] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        cause = str(entry.get("cause") or "").strip()
        if not cause:
            continue
        try:
            confidence = float(entry.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = min(max(confidence, 0.0), 1.0)

        service = str(entry.get("service") or "").strip()
        cited: list[EvidenceItem] = book.resolve(entry.get("supporting_evidence_ids") or [])

        candidates.append(
            {
                "cause": cause,
                "service": service if service in services else service or "unknown",
                "service_known": service in services,
                "confidence": round(confidence, 3),
                "why": str(entry.get("why") or "").strip(),
                "evidence_refs": [item.ref for item in cited],
                "_evidence": cited,
            }
        )

    candidates.sort(key=lambda c: -c["confidence"])
    return candidates[:MAX_CANDIDATES]


def _fallback_candidates(
    metric_output: AgentOutput | None, book: EvidenceBook
) -> list[dict[str, Any]]:
    """If the LLM is unreachable, fall back to the deterministic ordering."""
    origin = (metric_output.findings.get("origin_candidate") if metric_output else None) or "unknown"
    return [
        {
            "cause": (
                f"Degradation originates in {origin}: it is the first service whose metrics "
                "deviate, and the other services follow it."
            ),
            "service": origin,
            "service_known": origin in TOPOLOGY,
            "confidence": 0.35,
            "why": "Derived from metric onset ordering alone; no LLM reasoning was available.",
            "evidence_refs": [item.ref for item in book.items[:4]],
            "_evidence": book.items[:4],
        }
    ]


def run(
    ctx: IncidentContext,
    llm: LLMClient,
    log_output: AgentOutput | None = None,
    metric_output: AgentOutput | None = None,
) -> AgentOutput:
    """Rank candidate root causes from the upstream agents' findings."""
    started = time.perf_counter()
    book = _merge_evidence(log_output, metric_output)
    constraints = build_constraints(log_output, metric_output)

    def digest(output: AgentOutput | None) -> str:
        if not output:
            return "(unavailable)"
        lines = [output.summary, output.reasoning]
        if output.agent_name == "log_analysis":
            for cluster in output.findings.get("clusters", [])[:5]:
                lines.append(
                    f"- {cluster['service']} / {cluster['error_type']}: {cluster['count']} errors "
                    f"from +{cluster['onset_offset_s']}s -- {cluster['sample_message']}"
                )
            for marker in output.findings.get("markers", [])[:6]:
                lines.append(
                    f"- event {marker['event']} in {marker['service']} at "
                    f"+{marker['onset_offset_s']}s -- {marker['sample_message']}"
                )
        else:
            for anomaly in output.findings.get("anomalies", [])[:8]:
                lines.append(
                    f"- {anomaly['service']} {anomaly['metric']}: {anomaly['baseline']} -> "
                    f"{anomaly['peak']} (x{anomaly['ratio']}) from +{anomaly['onset_offset_s']}s"
                )
        return "\n".join(line for line in lines if line)

    user = ROOT_CAUSE_USER.format(
        window_start=ctx.window_start.isoformat(),
        window_end=ctx.window_end.isoformat(),
        incident_start=ctx.incident_start.isoformat(),
        topology=render_topology(),
        log_findings=digest(log_output),
        metric_findings=digest(metric_output),
        constraints="\n".join(f"- {c}" for c in constraints),
        evidence=book.render(),
    )

    try:
        response = llm.complete_json(system=ROOT_CAUSE_SYSTEM, user=user)
    except LLMError:
        response = {}

    candidates = _validate_candidates(response.get("candidates"), book, ctx.services())
    if not candidates:
        candidates = _fallback_candidates(metric_output, book)

    top = candidates[0]
    evidence = top["_evidence"] or book.items[:5]

    summary = str(response.get("summary") or top["cause"])
    reasoning = str(response.get("reasoning") or top["why"])

    return AgentOutput(
        agent_name=AGENT_NAME,
        summary=summary,
        reasoning=reasoning,
        findings={
            "candidates": [
                {k: v for k, v in candidate.items() if not k.startswith("_")}
                for candidate in candidates
            ],
            "constraints": constraints,
            "top_cause": top["cause"],
            "top_service": top["service"],
            "confidence": top["confidence"],
        },
        evidence=evidence,
        input_summary=(
            f"log + metric findings, {len(book)} citable evidence items, "
            f"{len(constraints)} constraints to satisfy"
        ),
        duration_ms=int((time.perf_counter() - started) * 1000),
        model=llm.model,
    )
