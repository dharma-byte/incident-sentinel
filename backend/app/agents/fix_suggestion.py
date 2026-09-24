"""Fix Suggestion Agent.

Turns the top-ranked cause and its evidence into ordered remediation steps:
stop the bleeding, then fix it properly, then verify.
"""

from __future__ import annotations

import time
from typing import Any

from app.agents import AgentOutput, EvidenceItem, IncidentContext
from app.llm.client import LLMClient, LLMError
from app.llm.prompts import FIX_SUGGESTION_SYSTEM, FIX_SUGGESTION_USER

AGENT_NAME = "fix_suggestion"

MAX_STEPS = 6
VALID_KINDS = ("mitigate", "fix", "verify")


def _validate_steps(raw: Any) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for index, entry in enumerate(raw or [], start=1):
        if not isinstance(entry, dict):
            continue
        action = str(entry.get("action") or "").strip()
        if not action:
            continue
        kind = str(entry.get("kind") or "").strip().lower()
        steps.append(
            {
                "order": index,
                "action": action,
                "kind": kind if kind in VALID_KINDS else "fix",
                "rationale": str(entry.get("rationale") or "").strip(),
                "risk": str(entry.get("risk") or "low").strip(),
            }
        )
    return steps[:MAX_STEPS]


def run(
    ctx: IncidentContext,
    llm: LLMClient,
    root_cause_output: AgentOutput | None = None,
) -> AgentOutput:
    """Propose remediation for the top-ranked root cause."""
    started = time.perf_counter()

    findings = root_cause_output.findings if root_cause_output else {}
    candidates = findings.get("candidates", []) or []
    top = candidates[0] if candidates else {}
    evidence: list[EvidenceItem] = list(root_cause_output.evidence) if root_cause_output else []

    alternatives = (
        "\n".join(
            f"- {c['cause']} (confidence {c['confidence']})" for c in candidates[1:]
        )
        or "(none)"
    )

    user = FIX_SUGGESTION_USER.format(
        root_cause=top.get("cause", "unknown"),
        confidence=top.get("confidence", 0.0),
        evidence="\n".join(item.render() for item in evidence) or "(none)",
        alternatives=alternatives,
    )

    try:
        response = llm.complete_json(system=FIX_SUGGESTION_SYSTEM, user=user)
    except LLMError:
        response = {}

    steps = _validate_steps(response.get("steps"))
    if not steps:
        steps = [
            {
                "order": 1,
                "action": (
                    f"Investigate {top.get('service', 'the implicated service')} directly; "
                    "automated remediation could not be generated."
                ),
                "kind": "mitigate",
                "rationale": "The reasoning model was unavailable, so only the diagnosis stands.",
                "risk": "low",
            }
        ]

    summary = str(response.get("summary") or steps[0]["action"])
    reasoning = str(response.get("reasoning") or "")

    return AgentOutput(
        agent_name=AGENT_NAME,
        summary=summary,
        reasoning=reasoning,
        findings={
            "steps": steps,
            "prevention": str(response.get("prevention") or ""),
            "addresses": top.get("cause", ""),
            "service": top.get("service", ""),
        },
        evidence=evidence[:5],
        input_summary=(
            f"top cause in {top.get('service', 'unknown')} at confidence "
            f"{top.get('confidence', 0.0)}, {len(evidence)} evidence items"
        ),
        duration_ms=int((time.perf_counter() - started) * 1000),
        model=llm.model,
    )
