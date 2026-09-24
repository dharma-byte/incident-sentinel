"""Log Analysis Agent.

Deterministically clusters the incident's stored log lines -- error signatures,
marker events, volume shifts -- then asks the LLM to interpret what the clusters
mean. Every citation it emits points at a real ``incident_logs`` row.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from typing import Any

from app.agents import AgentOutput, EvidenceBook, IncidentContext
from app.llm.client import LLMClient, LLMError
from app.llm.prompts import LOG_ANALYSIS_SYSTEM, LOG_ANALYSIS_USER

AGENT_NAME = "log_analysis"

MAX_CLUSTERS = 5
MAX_MARKERS = 6
SPIKE_RATIO = 3.0  # post/pre error-rate multiple that counts as a spike
SPIKE_FLOOR = 0.5  # errors per minute below which a ratio is just noise


def _per_minute(count: int, seconds: float) -> float:
    return count / (seconds / 60.0) if seconds > 0 else 0.0


def analyse_logs(ctx: IncidentContext) -> dict[str, Any]:
    """Extract error clusters, marker events and volume shifts.

    Pure function of the stored rows -- no LLM involved, so it is directly
    testable and its numbers are reproducible.
    """
    before, after = ctx.logs_before(), ctx.logs_after()
    pre_seconds = max((ctx.incident_start - ctx.window_start).total_seconds(), 1.0)
    post_seconds = max((ctx.window_end - ctx.incident_start).total_seconds(), 1.0)

    volume: dict[str, dict[str, float]] = {}
    for service in ctx.services():
        pre_errors = sum(1 for log in before if log.service == service and log.level == "ERROR")
        post_errors = sum(1 for log in after if log.service == service and log.level == "ERROR")
        pre_rate = _per_minute(pre_errors, pre_seconds)
        post_rate = _per_minute(post_errors, post_seconds)
        ratio = post_rate / pre_rate if pre_rate > 0 else (post_rate if post_rate else 0.0)
        volume[service] = {
            "pre_errors_per_min": round(pre_rate, 2),
            "post_errors_per_min": round(post_rate, 2),
            "ratio": round(ratio, 1),
            "spike": post_rate >= SPIKE_FLOOR and ratio >= SPIKE_RATIO,
        }

    # Error clusters: (service, error_type) over the post-onset window.
    grouped: dict[tuple[str, str], list] = defaultdict(list)
    for log in after:
        if log.level == "ERROR":
            grouped[(log.service, str(log.attrs.get("error_type", "unknown")))].append(log)

    clusters = []
    for (service, error_type), entries in grouped.items():
        entries.sort(key=lambda log: log.timestamp)
        first = entries[0]
        clusters.append(
            {
                "service": service,
                "error_type": error_type,
                "count": len(entries),
                "first_seen": first.timestamp,
                "last_seen": entries[-1].timestamp,
                "onset_offset_s": round(ctx.offset(first.timestamp)),
                "sample_message": first.message,
                "stack_trace": first.attrs.get("stack_trace"),
                "sample_log": first,
            }
        )
    clusters.sort(key=lambda c: (-c["count"], c["onset_offset_s"]))
    clusters = clusters[:MAX_CLUSTERS]

    # Marker events: operational events the services logged about themselves.
    marker_groups: dict[tuple[str, str], list] = defaultdict(list)
    for log in after:
        event = log.attrs.get("event")
        if event:
            marker_groups[(log.service, str(event))].append(log)

    markers = []
    for (service, event), entries in marker_groups.items():
        entries.sort(key=lambda log: log.timestamp)
        first = entries[0]
        markers.append(
            {
                "service": service,
                "event": event,
                "count": len(entries),
                "first_seen": first.timestamp,
                "onset_offset_s": round(ctx.offset(first.timestamp)),
                "sample_message": first.message,
                "sample_log": first,
            }
        )
    markers.sort(key=lambda m: m["onset_offset_s"])
    markers = markers[:MAX_MARKERS]

    levels = Counter(log.level for log in after)

    return {
        "volume": volume,
        "clusters": clusters,
        "markers": markers,
        "level_counts": dict(levels),
        "spiking_services": [s for s, v in volume.items() if v["spike"]],
        "total_logs": len(ctx.logs),
    }


def _collect_evidence(findings: dict[str, Any]) -> EvidenceBook:
    book = EvidenceBook()
    for cluster in findings["clusters"]:
        log = cluster["sample_log"]
        excerpt = f"{log.message}"
        if cluster["stack_trace"]:
            first_frame = str(cluster["stack_trace"]).strip().splitlines()[-1]
            excerpt = f"{log.message} | {first_frame}"
        book.add_log(log, f"{excerpt} (x{cluster['count']} in this service)")
    for marker in findings["markers"]:
        book.add_log(marker["sample_log"], f"{marker['sample_message']} (x{marker['count']})")
    return book


def _render_volume(findings: dict[str, Any]) -> str:
    rows = []
    for service, v in findings["volume"].items():
        flag = "  <-- spike" if v["spike"] else ""
        rows.append(
            f"{service}: {v['pre_errors_per_min']}/min -> {v['post_errors_per_min']}/min "
            f"(x{v['ratio']}){flag}"
        )
    return "\n".join(rows)


def _render_clusters(findings: dict[str, Any]) -> str:
    if not findings["clusters"]:
        return "(no error clusters)"
    rows = []
    for cluster in findings["clusters"]:
        row = (
            f"{cluster['service']} / {cluster['error_type']}: {cluster['count']} errors, "
            f"first at +{cluster['onset_offset_s']}s -- {cluster['sample_message']}"
        )
        if cluster["stack_trace"]:
            row += f"\n    stack: {str(cluster['stack_trace']).strip().splitlines()[-1]}"
        rows.append(row)
    return "\n".join(rows)


def _render_markers(findings: dict[str, Any]) -> str:
    if not findings["markers"]:
        return "(no marker events)"
    return "\n".join(
        f"{m['service']} / {m['event']} (x{m['count']}) at +{m['onset_offset_s']}s -- {m['sample_message']}"
        for m in findings["markers"]
    )


def run(ctx: IncidentContext, llm: LLMClient) -> AgentOutput:
    """Analyse the incident's logs and return the agent's trace contribution."""
    started = time.perf_counter()
    findings = analyse_logs(ctx)
    book = _collect_evidence(findings)

    user = LOG_ANALYSIS_USER.format(
        window_start=ctx.window_start.isoformat(),
        window_end=ctx.window_end.isoformat(),
        incident_start=ctx.incident_start.isoformat(),
        volume_table=_render_volume(findings),
        clusters=_render_clusters(findings),
        markers=_render_markers(findings),
        evidence=book.render(),
    )

    try:
        response = llm.complete_json(system=LOG_ANALYSIS_SYSTEM, user=user)
    except LLMError:
        response = {}

    cited: list[str] = []
    for signature in response.get("signatures", []) or []:
        if isinstance(signature, dict):
            cited.extend(signature.get("evidence_ids", []) or [])
    evidence = book.resolve(cited) or book.items[:5]  # always cite something real

    summary = str(response.get("summary") or _fallback_summary(findings))
    reasoning = str(response.get("reasoning") or "")

    # Keep only what is JSON-serialisable; ORM rows stay out of the trace.
    serialisable = {
        "volume": findings["volume"],
        "spiking_services": findings["spiking_services"],
        "level_counts": findings["level_counts"],
        "clusters": [
            {k: (v.isoformat() if hasattr(v, "isoformat") else v)
             for k, v in cluster.items() if k != "sample_log"}
            for cluster in findings["clusters"]
        ],
        "markers": [
            {k: (v.isoformat() if hasattr(v, "isoformat") else v)
             for k, v in marker.items() if k != "sample_log"}
            for marker in findings["markers"]
        ],
        "key_services": response.get("key_services", findings["spiking_services"]),
        "signatures": response.get("signatures", []),
    }

    return AgentOutput(
        agent_name=AGENT_NAME,
        summary=summary,
        reasoning=reasoning,
        findings=serialisable,
        evidence=evidence,
        input_summary=(
            f"{findings['total_logs']} log lines across {len(ctx.services())} services; "
            f"{len(findings['clusters'])} error clusters, {len(findings['markers'])} marker events"
        ),
        duration_ms=int((time.perf_counter() - started) * 1000),
        model=llm.model,
    )


def _fallback_summary(findings: dict[str, Any]) -> str:
    """Used when the LLM is unavailable -- the deterministic findings still stand."""
    spiking = findings["spiking_services"]
    if not spiking:
        return "No service shows a clear error-rate spike in the incident window."
    top = findings["clusters"][0] if findings["clusters"] else None
    detail = f"; dominant signature {top['error_type']} in {top['service']}" if top else ""
    return f"Error volume spiked in {', '.join(spiking)}{detail}."
