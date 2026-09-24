"""Metrics Correlation Agent.

Computes, for every service/metric pair, the pre-incident baseline, the peak
after onset and when the metric first deviated -- then asks the LLM to read the
propagation order. The metrics that *stayed normal* are reported too: they are
what rules rival explanations out.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

from app.agents import AgentOutput, EvidenceBook, IncidentContext, render_topology
from app.llm.client import LLMClient, LLMError
from app.llm.prompts import METRICS_CORRELATION_SYSTEM, METRICS_CORRELATION_USER

AGENT_NAME = "metrics_correlation"

MAX_EVIDENCE = 10

# A metric counts as deviating when it clears both a relative and an absolute
# bar, so a jittery near-zero baseline cannot manufacture a huge ratio.
DEVIATION = {
    "latency_p95_ms": {"ratio": 1.5, "floor": 20.0},
    "error_rate": {"ratio": 3.0, "floor": 0.02},
    "cpu_pct": {"ratio": 1.3, "floor": 15.0},
    "memory_mb": {"ratio": 1.25, "floor": 64.0},
    "conn_pool_used_pct": {"ratio": 1.3, "floor": 20.0},
}
DEFAULT_DEVIATION = {"ratio": 1.5, "floor": 0.0}


def _stats(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
    return mean, stdev


def analyse_metrics(ctx: IncidentContext) -> dict[str, Any]:
    """Baseline vs peak, onset time and propagation order. No LLM involved."""
    anomalies: list[dict[str, Any]] = []
    stable: list[dict[str, Any]] = []

    metric_names = sorted({point.metric for point in ctx.metrics})
    for service in ctx.services():
        for metric in metric_names:
            series = ctx.series(service, metric)
            if not series:
                continue
            pre = [p.value for p in series if p.timestamp < ctx.incident_start]
            post = [p for p in series if p.timestamp >= ctx.incident_start]
            if not pre or not post:
                continue

            baseline, stdev = _stats(pre)
            peak_point = max(post, key=lambda p: p.value)
            peak = peak_point.value
            ratio = peak / baseline if baseline else 0.0
            rule = DEVIATION.get(metric, DEFAULT_DEVIATION)
            deviated = ratio >= rule["ratio"] and peak >= rule["floor"]

            record = {
                "service": service,
                "metric": metric,
                "baseline": round(baseline, 3),
                "peak": round(peak, 3),
                "ratio": round(ratio, 2),
                "stdev": round(stdev, 3),
            }

            if not deviated:
                stable.append(record)
                continue

            # First post-onset sample at least halfway to the peak.
            threshold = baseline + (peak - baseline) * 0.25
            crossing = next((p for p in post if p.value >= threshold), peak_point)
            record.update(
                {
                    "onset_offset_s": round(ctx.offset(crossing.timestamp)),
                    "peak_at": peak_point.timestamp,
                    "peak_point": peak_point,
                    "crossing_point": crossing,
                    "sustained": statistics.fmean([p.value for p in post[-4:]]) >= threshold,
                }
            )
            anomalies.append(record)

    anomalies.sort(key=lambda a: (a["onset_offset_s"], -a["ratio"]))

    # Propagation order: services ranked by their earliest deviating metric.
    first_seen: dict[str, int] = {}
    for anomaly in anomalies:
        first_seen.setdefault(anomaly["service"], anomaly["onset_offset_s"])
    propagation = sorted(first_seen.items(), key=lambda kv: kv[1])

    return {
        "anomalies": anomalies,
        "stable": stable,
        "propagation_order": [{"service": s, "onset_offset_s": t} for s, t in propagation],
        "origin_candidate": propagation[0][0] if propagation else None,
        "deviating_services": [s for s, _ in propagation],
    }


def notable_stable(findings: dict[str, Any], services: list[str]) -> list[str]:
    """Normal-looking metrics that a diagnosis has to stay consistent with.

    Only the metrics that discriminate between failure modes are worth the
    prompt space: a flat memory curve, a healthy datastore, an idle CPU.
    """
    deviating = {(a["service"], a["metric"]) for a in findings["anomalies"]}
    affected = set(findings["deviating_services"])
    interesting = ("memory_mb", "conn_pool_used_pct", "cpu_pct", "latency_p95_ms")

    notes: list[str] = []
    for record in findings["stable"]:
        key = (record["service"], record["metric"])
        if key in deviating or record["metric"] not in interesting:
            continue
        # Mention a stable metric when the service is otherwise implicated, or
        # when it is a dependency of something that is.
        if record["service"] in affected:
            notes.append(
                f"{record['service']} {record['metric']} stayed flat "
                f"({record['baseline']} -> {record['peak']})"
            )
    return notes[:8]


def _collect_evidence(findings: dict[str, Any]) -> EvidenceBook:
    book = EvidenceBook()
    for anomaly in findings["anomalies"][:MAX_EVIDENCE]:
        excerpt = (
            f"{anomaly['metric']} {anomaly['baseline']} -> {anomaly['peak']} "
            f"(x{anomaly['ratio']}), first deviated +{anomaly['onset_offset_s']}s after onset"
        )
        book.add_metric(anomaly["peak_point"], excerpt)
    return book


def _render_deviations(findings: dict[str, Any]) -> str:
    if not findings["anomalies"]:
        return "(no metric deviated)"
    return "\n".join(
        f"{a['service']} {a['metric']}: {a['baseline']} -> {a['peak']} (x{a['ratio']}), "
        f"first deviated +{a['onset_offset_s']}s, "
        f"{'still elevated at window end' if a['sustained'] else 'recovered before window end'}"
        for a in findings["anomalies"]
    )


def run(
    ctx: IncidentContext,
    llm: LLMClient,
    log_findings: AgentOutput | None = None,
) -> AgentOutput:
    """Correlate the incident's metrics, informed by the log agent's findings."""
    started = time.perf_counter()
    findings = analyse_metrics(ctx)
    book = _collect_evidence(findings)
    stable_notes = notable_stable(findings, ctx.services())

    handoff = (
        f"{log_findings.summary}\n{log_findings.reasoning}"
        if log_findings
        else "(log analysis unavailable)"
    )

    user = METRICS_CORRELATION_USER.format(
        window_start=ctx.window_start.isoformat(),
        window_end=ctx.window_end.isoformat(),
        incident_start=ctx.incident_start.isoformat(),
        topology=render_topology(),
        deviations=_render_deviations(findings),
        stable="\n".join(stable_notes) or "(nothing notable stayed normal)",
        log_findings=handoff,
        evidence=book.render(),
    )

    try:
        response = llm.complete_json(system=METRICS_CORRELATION_SYSTEM, user=user)
    except LLMError:
        response = {}

    cited: list[str] = []
    for anomaly in response.get("anomalies", []) or []:
        if isinstance(anomaly, dict):
            cited.extend(anomaly.get("evidence_ids", []) or [])
    evidence = book.resolve(cited) or book.items[:5]

    summary = str(response.get("summary") or _fallback_summary(findings))
    reasoning = str(response.get("reasoning") or "")

    serialisable = {
        "anomalies": [
            {
                k: (v.isoformat() if hasattr(v, "isoformat") else v)
                for k, v in anomaly.items()
                if k not in ("peak_point", "crossing_point")
            }
            for anomaly in findings["anomalies"]
        ],
        "propagation_order": findings["propagation_order"],
        "origin_candidate": findings["origin_candidate"],
        "stable_notes": stable_notes,
        "llm_origin_candidates": response.get("origin_candidates", []),
    }

    return AgentOutput(
        agent_name=AGENT_NAME,
        summary=summary,
        reasoning=reasoning,
        findings=serialisable,
        evidence=evidence,
        input_summary=(
            f"{len(ctx.metrics)} metric samples; {len(findings['anomalies'])} deviating series "
            f"across {len(findings['deviating_services'])} services"
        ),
        duration_ms=int((time.perf_counter() - started) * 1000),
        model=llm.model,
    )


def _fallback_summary(findings: dict[str, Any]) -> str:
    if not findings["anomalies"]:
        return "No metric deviated meaningfully from its baseline."
    order = " -> ".join(
        f"{p['service']} (+{p['onset_offset_s']}s)" for p in findings["propagation_order"]
    )
    return f"Deviation appears first in {findings['origin_candidate']}; propagation: {order}."
