"""Agent tests (Phase 3).

Two layers:

* the deterministic analysis each agent performs -- this is where scenario
  correctness is actually asserted, and it needs no LLM;
* the agent wrappers and the LangGraph pipeline, driven by a stub LLM, so the
  trace/evidence persistence is verified without a model.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.agents import AgentOutput, EvidenceBook, IncidentContext
from app.agents import fix_suggestion, log_analysis, metrics_correlation, root_cause
from app.agents.orchestrator import AGENT_SEQUENCE, TriageOrchestrator, triage_incident
from app.db.models import AgentTraceStep, IncidentEvidence, IncidentLog, IncidentMetric, IncidentStatus
from app.llm.client import (
    GroqClient,
    LLMError,
    OllamaClient,
    StubLLMClient,
    _extract_json,
    get_llm_client,
)

# --------------------------------------------------------------------------- #
# Stub responses, keyed on a phrase from each agent's system prompt
# --------------------------------------------------------------------------- #

STUB_RESPONSES = {
    "You are the Log Analysis Agent": {
        "summary": "payments-service is throwing on every charge.",
        "reasoning": "The error cluster is uniform and starts abruptly.",
        "key_services": ["payments-service"],
        "signatures": [
            {
                "service": "payments-service",
                "error_type": "TypeError",
                "meaning": "null arithmetic in the charge path",
                "evidence_ids": ["L1", "L2"],
            }
        ],
    },
    "You are the Metrics Correlation Agent": {
        "summary": "Error rate steps up in payments-service first.",
        "reasoning": "Nothing else moves before it.",
        "origin_candidates": ["payments-service"],
        "anomalies": [
            {
                "service": "payments-service",
                "metric": "error_rate",
                "change": "0.006 -> 0.4",
                "onset_offset_s": 15,
                "evidence_ids": ["M1"],
            }
        ],
    },
    "You are the Root Cause Reasoning Agent": {
        "summary": "A bad payments-service release is failing every charge.",
        "reasoning": "The step change lines up with the rollout and nothing else changed.",
        "candidates": [
            {
                "cause": "payments-service v2.4.1 introduced a null-handling bug in the charge path",
                "service": "payments-service",
                "confidence": 0.86,
                "supporting_evidence_ids": ["L1", "M1"],
                "why": "Memory and pools are flat, so a leak or pool exhaustion does not fit.",
            },
            {
                "cause": "Payment provider degradation",
                "service": "payments-service",
                "confidence": 0.11,
                "supporting_evidence_ids": ["L2"],
                "why": "Would not align so precisely with the rollout marker.",
            },
        ],
    },
    "You are the Fix Suggestion Agent": {
        "summary": "Roll payments-service back to v2.4.0.",
        "reasoning": "Rollback is the fastest way to stop failing charges.",
        "steps": [
            {
                "order": 1,
                "action": "Roll payments-service back to v2.4.0",
                "kind": "mitigate",
                "rationale": "Restores the last known-good charge path",
                "risk": "low",
            },
            {
                "order": 2,
                "action": "Add a null guard in compute_total and a regression test",
                "kind": "fix",
                "rationale": "Stops the same input crashing the endpoint",
                "risk": "low",
            },
        ],
        "prevention": "Gate deploys on a checkout smoke test.",
    },
}


@pytest.fixture
def stub_llm() -> StubLLMClient:
    return StubLLMClient(STUB_RESPONSES)


def context_for(db_session, incident) -> IncidentContext:
    return IncidentContext.load(db_session, incident.id)


# --------------------------------------------------------------------------- #
# LLM client
# --------------------------------------------------------------------------- #


def test_extract_json_survives_small_model_habits():
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('Sure! Here you go: {"a": 1} hope that helps') == {"a": 1}
    with pytest.raises(LLMError):
        _extract_json("no json here at all")


def test_groq_falls_back_to_ollama_without_a_key(monkeypatch):
    monkeypatch.setattr("app.llm.client.settings.groq_api_key", "", raising=False)
    assert isinstance(get_llm_client("groq"), OllamaClient)
    monkeypatch.setattr("app.llm.client.settings.groq_api_key", "gsk_test", raising=False)
    assert isinstance(get_llm_client("groq"), GroqClient)


def test_stub_client_matches_on_prompt_text(stub_llm):
    payload = stub_llm.complete_json(system="You are the Fix Suggestion Agent.", user="x")
    assert payload["steps"][0]["kind"] == "mitigate"
    assert len(stub_llm.calls) == 1


def test_retry_gives_up_and_reports(monkeypatch):
    import app.llm.client as llm_module

    monkeypatch.setattr(llm_module.time, "sleep", lambda _: None)
    attempts = {"n": 0}

    def always_fails() -> str:
        attempts["n"] += 1
        raise LLMError("boom")

    with pytest.raises(LLMError, match=f"after {llm_module.MAX_ATTEMPTS} attempts"):
        llm_module._retry(always_fails, what="test")
    assert attempts["n"] == llm_module.MAX_ATTEMPTS


def test_token_budget_admits_until_the_window_is_full(monkeypatch):
    """The budget must pace calls rather than let later agents starve."""
    import app.llm.client as llm_module

    budget = llm_module.TokenBudget(1000, window_seconds=60.0)
    assert budget.limit == 900  # 10% safety margin

    assert budget.reserve(500) == 0.0
    assert budget.reserve(300) == 0.0
    assert budget.used() == 800

    class Waited(Exception):
        """Raised from the stubbed sleep so the wait is observable."""

    def fake_sleep(delay: float) -> None:
        raise Waited(delay)

    monkeypatch.setattr(llm_module.time, "sleep", fake_sleep)
    # This one does not fit, so it waits for the window to roll instead of
    # firing a request the server would refuse.
    with pytest.raises(Waited) as waited:
        budget.reserve(400)
    assert 0 < waited.value.args[0] <= 61


def test_token_budget_never_deadlocks_on_an_oversized_request():
    """A single request larger than the whole window still goes through."""
    import app.llm.client as llm_module

    budget = llm_module.TokenBudget(1000)
    assert budget.reserve(50_000) == 0.0


def test_token_budget_adopts_the_servers_usage_figure():
    """A 429 tells us the real org-wide usage; trust it over our estimate."""
    import app.llm.client as llm_module

    budget = llm_module.TokenBudget(8000)
    budget.reserve(1000)
    budget.sync(6500)  # the provider counted far more than this process spent
    assert budget.used() == 6500

    budget.sync(3000)  # a lower figure must not erase what we know
    assert budget.used() == 6500


def test_rate_limit_hints_are_parsed_from_the_error_body():
    import app.llm.client as llm_module

    detail = (
        "Rate limit reached for model `openai/gpt-oss-120b` on tokens per minute (TPM): "
        "Limit 8000, Used 6581, Requested 1497. Please try again in 585ms."
    )
    assert llm_module._parse_used_tokens(detail) == 6581
    assert 0.8 < llm_module._parse_retry_delay(detail, None) < 1.0
    assert llm_module._parse_retry_delay(detail, "3.5") == 3.5  # header wins
    assert llm_module._parse_retry_delay("no hint here", None) == 2.0
    assert llm_module._parse_retry_delay("", "not-a-number") == 2.0


# --------------------------------------------------------------------------- #
# Log analysis -- deterministic layer
# --------------------------------------------------------------------------- #


def test_log_analysis_finds_the_deploy_and_its_error_cluster(db_session, make_incident):
    findings = log_analysis.analyse_logs(context_for(db_session, make_incident("bad_deploy")))

    assert "payments-service" in findings["spiking_services"]
    top = findings["clusters"][0]
    assert top["service"] == "payments-service"
    assert top["error_type"] == "TypeError"
    assert top["count"] > 100
    assert "compute_total" in top["stack_trace"]
    assert top["onset_offset_s"] < 120

    events = {(m["service"], m["event"]) for m in findings["markers"]}
    assert ("payments-service", "deploy") in events


def test_log_analysis_finds_the_oom_kill(db_session, make_incident):
    findings = log_analysis.analyse_logs(context_for(db_session, make_incident("memory_leak")))

    events = {m["event"] for m in findings["markers"]}
    assert {"gc_pressure", "oom", "oom_kill", "restart"} <= events
    assert all(m["service"] == "notification-worker" for m in findings["markers"])


def test_log_analysis_finds_pool_exhaustion(db_session, make_incident):
    findings = log_analysis.analyse_logs(
        context_for(db_session, make_incident("conn_pool_exhaustion"))
    )

    assert "auth-service" in findings["spiking_services"]
    types = {(c["service"], c["error_type"]) for c in findings["clusters"]}
    assert ("auth-service", "PoolTimeoutError") in types
    assert any(m["event"] == "pool_exhausted" for m in findings["markers"])


def test_log_analysis_finds_the_slow_query(db_session, make_incident):
    findings = log_analysis.analyse_logs(context_for(db_session, make_incident("slow_query")))

    events = {m["event"] for m in findings["markers"]}
    assert "slow_query" in events and "plan_change" in events
    assert any("q_7712" in m["sample_message"] for m in findings["markers"])


# --------------------------------------------------------------------------- #
# Metrics correlation -- deterministic layer
# --------------------------------------------------------------------------- #


def test_metrics_put_the_database_first_for_a_slow_query(db_session, make_incident):
    findings = metrics_correlation.analyse_metrics(
        context_for(db_session, make_incident("slow_query"))
    )

    assert findings["origin_candidate"] == "orders-db"
    order = [p["service"] for p in findings["propagation_order"]]
    assert order.index("orders-db") < order.index("payments-service") < order.index("api-gateway")

    db_latency = next(
        a for a in findings["anomalies"] if a["service"] == "orders-db" and a["metric"] == "latency_p95_ms"
    )
    assert db_latency["ratio"] > 20
    assert db_latency["sustained"] is True


def test_metrics_keep_the_database_healthy_for_pool_exhaustion(db_session, make_incident):
    findings = metrics_correlation.analyse_metrics(
        context_for(db_session, make_incident("conn_pool_exhaustion"))
    )

    assert findings["origin_candidate"] == "auth-service"

    pool = next(
        a
        for a in findings["anomalies"]
        if a["service"] == "auth-service" and a["metric"] == "conn_pool_used_pct"
    )
    assert pool["peak"] >= 99

    # The decisive negative: orders-db latency must NOT register as deviating.
    deviating = {(a["service"], a["metric"]) for a in findings["anomalies"]}
    assert ("orders-db", "latency_p95_ms") not in deviating


def test_metrics_see_the_memory_ramp(db_session, make_incident):
    findings = metrics_correlation.analyse_metrics(
        context_for(db_session, make_incident("memory_leak"))
    )

    memory = next(
        a
        for a in findings["anomalies"]
        if a["service"] == "notification-worker" and a["metric"] == "memory_mb"
    )
    assert memory["peak"] > 1900
    assert findings["origin_candidate"] == "notification-worker"


def test_metrics_see_a_flat_memory_curve_for_a_bad_deploy(db_session, make_incident):
    ctx = context_for(db_session, make_incident("bad_deploy"))
    findings = metrics_correlation.analyse_metrics(ctx)

    deviating = {(a["service"], a["metric"]) for a in findings["anomalies"]}
    assert ("payments-service", "error_rate") in deviating
    assert ("payments-service", "memory_mb") not in deviating

    notes = metrics_correlation.notable_stable(findings, ctx.services())
    assert any("memory_mb" in note for note in notes)


# --------------------------------------------------------------------------- #
# Agent wrappers
# --------------------------------------------------------------------------- #


def test_every_citation_points_at_a_stored_row(db_session, make_incident, stub_llm):
    ctx = context_for(db_session, make_incident("bad_deploy"))
    outputs = [
        log_analysis.run(ctx, stub_llm),
        metrics_correlation.run(ctx, stub_llm),
    ]
    for output in outputs:
        assert output.evidence, f"{output.agent_name} cited nothing"
        for item in output.evidence:
            model = IncidentLog if item.source == "log" else IncidentMetric
            row = db_session.get(model, item.source_ref)
            assert row is not None, f"{item.ref} does not resolve to a stored row"
            assert row.incident_id == ctx.incident.id
            assert row.service == item.service


def test_agents_degrade_gracefully_without_an_llm(db_session, make_incident):
    class DeadLLM(StubLLMClient):
        def complete(self, **kwargs):
            raise LLMError("no model")

    ctx = context_for(db_session, make_incident("slow_query"))
    output = log_analysis.run(ctx, DeadLLM())

    assert output.summary  # deterministic fallback still describes the incident
    assert output.evidence
    assert output.findings["clusters"]


def test_root_cause_drops_invented_citations(db_session, make_incident):
    ctx = context_for(db_session, make_incident("bad_deploy"))
    log_out = log_analysis.run(ctx, StubLLMClient(STUB_RESPONSES))
    metric_out = metrics_correlation.run(ctx, StubLLMClient(STUB_RESPONSES))

    liar = StubLLMClient(
        {
            "You are the Root Cause Reasoning Agent": {
                "summary": "s",
                "reasoning": "r",
                "candidates": [
                    {
                        "cause": "something",
                        "service": "payments-service",
                        "confidence": 4.2,  # out of range
                        "supporting_evidence_ids": ["L1", "L999", "NOPE"],
                        "why": "w",
                    }
                ],
            }
        }
    )
    output = root_cause.run(ctx, liar, log_out, metric_out)
    candidate = output.findings["candidates"][0]

    assert candidate["confidence"] == 1.0  # clamped
    assert "L999" not in candidate["evidence_refs"]
    assert "NOPE" not in candidate["evidence_refs"]
    assert candidate["evidence_refs"] == ["L1"]


def test_root_cause_constraints_include_the_healthy_dependency(db_session, make_incident, stub_llm):
    ctx = context_for(db_session, make_incident("conn_pool_exhaustion"))
    log_out = log_analysis.run(ctx, stub_llm)
    metric_out = metrics_correlation.run(ctx, stub_llm)

    constraints = root_cause.build_constraints(log_out, metric_out)
    joined = " ".join(constraints)

    assert "Deviation order" in joined
    assert "orders-db" in joined  # the healthy dependency is called out


def test_root_cause_falls_back_when_the_model_returns_nothing(db_session, make_incident, stub_llm):
    ctx = context_for(db_session, make_incident("slow_query"))
    metric_out = metrics_correlation.run(ctx, stub_llm)
    empty = StubLLMClient({"You are the Root Cause Reasoning Agent": {"summary": "", "candidates": []}})

    output = root_cause.run(ctx, empty, None, metric_out)
    candidate = output.findings["candidates"][0]

    assert candidate["service"] == "orders-db"  # from metric onset ordering
    assert 0 < candidate["confidence"] < 0.5  # honest about being a weak inference


def test_fix_suggestion_validates_steps(db_session, make_incident, stub_llm):
    ctx = context_for(db_session, make_incident("bad_deploy"))
    log_out = log_analysis.run(ctx, stub_llm)
    metric_out = metrics_correlation.run(ctx, stub_llm)
    cause_out = root_cause.run(ctx, stub_llm, log_out, metric_out)

    output = fix_suggestion.run(ctx, stub_llm, cause_out)
    steps = output.findings["steps"]

    assert [s["order"] for s in steps] == [1, 2]
    assert steps[0]["kind"] == "mitigate"
    assert all(s["kind"] in ("mitigate", "fix", "verify") for s in steps)
    assert output.findings["prevention"]


def test_fix_suggestion_handles_a_junk_response(db_session, make_incident, stub_llm):
    ctx = context_for(db_session, make_incident("bad_deploy"))
    cause_out = root_cause.run(ctx, stub_llm, None, None)
    junk = StubLLMClient({"You are the Fix Suggestion Agent": {"steps": ["not a dict", {}]}})

    output = fix_suggestion.run(ctx, junk, cause_out)
    assert len(output.findings["steps"]) == 1
    assert output.findings["steps"][0]["kind"] == "mitigate"


# --------------------------------------------------------------------------- #
# Evidence book
# --------------------------------------------------------------------------- #


def test_evidence_book_tokens_and_resolution(db_session, make_incident):
    ctx = context_for(db_session, make_incident("bad_deploy"))
    book = EvidenceBook()
    first = book.add_log(ctx.logs[0])
    point = book.add_metric(ctx.metrics[0], "cpu 30 -> 90")

    assert first.ref == "L1" and point.ref == "M1"
    assert book.resolve(["L1", "m1"]) == [first, point]  # case-insensitive
    assert book.resolve(["L9"]) == []
    assert book.resolve([]) == []
    assert "[L1]" in book.render()


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def test_pipeline_writes_a_four_step_trace(db_session, make_incident, stub_llm):
    incident = make_incident("bad_deploy")
    result = triage_incident(db_session, incident.id, llm=stub_llm)

    assert result["status"] == IncidentStatus.COMPLETE
    assert result["steps"] == 4
    assert "v2.4.1" in result["root_cause"]
    assert result["confidence"] == 0.86
    assert result["fix"]["steps"][0]["kind"] == "mitigate"

    steps = db_session.scalars(
        select(AgentTraceStep)
        .where(AgentTraceStep.incident_id == incident.id)
        .order_by(AgentTraceStep.step_order)
    ).all()
    assert [s.agent_name for s in steps] == list(AGENT_SEQUENCE)
    assert [s.step_order for s in steps] == [1, 2, 3, 4]
    assert all(s.output_summary and s.input_summary for s in steps)
    assert all(s.duration_ms is not None for s in steps)

    db_session.refresh(incident)
    assert incident.status == IncidentStatus.COMPLETE
    assert incident.confidence == 0.86


def test_trace_evidence_refs_resolve_to_evidence_rows(db_session, make_incident, stub_llm):
    incident = make_incident("conn_pool_exhaustion")
    triage_incident(db_session, incident.id, llm=stub_llm)

    evidence = db_session.scalars(
        select(IncidentEvidence).where(IncidentEvidence.incident_id == incident.id)
    ).all()
    by_id = {row.id: row for row in evidence}
    assert evidence

    steps = db_session.scalars(
        select(AgentTraceStep).where(AgentTraceStep.incident_id == incident.id)
    ).all()
    cited = {ref for step in steps for ref in step.evidence_refs}
    assert cited, "no step cited any evidence"
    assert cited <= set(by_id)

    for row in evidence:
        assert row.source in ("log", "metric")
        model = IncidentLog if row.source == "log" else IncidentMetric
        assert db_session.get(model, row.source_ref) is not None


def test_rerunning_triage_replaces_the_previous_trace(db_session, make_incident, stub_llm):
    incident = make_incident("bad_deploy")
    triage_incident(db_session, incident.id, llm=stub_llm)
    triage_incident(db_session, incident.id, llm=stub_llm)

    steps = db_session.scalars(
        select(AgentTraceStep).where(AgentTraceStep.incident_id == incident.id)
    ).all()
    assert len(steps) == 4, "a re-run must replace the trace, not append to it"


def test_pipeline_emits_step_callbacks(db_session, make_incident, stub_llm):
    incident = make_incident("memory_leak")
    seen: list[str] = []

    TriageOrchestrator(
        db_session, llm=stub_llm, on_step=lambda name, output: seen.append(name)
    ).run(incident.id)

    assert seen == list(AGENT_SEQUENCE)  # what the SSE endpoint will stream


def test_pipeline_rejects_an_unknown_incident(db_session, stub_llm):
    with pytest.raises(ValueError, match="not found"):
        triage_incident(db_session, uuid.uuid4(), llm=stub_llm)


# --------------------------------------------------------------------------- #
# Triage endpoint
# --------------------------------------------------------------------------- #


def test_triage_endpoint_runs_the_pipeline(client, db_session):
    created = client.post("/simulate", json={"scenario_type": "bad_deploy", "seed": 1337}).json()
    incident_id = created["incident_id"]

    response = client.post(f"/incidents/{incident_id}/triage", json={"provider": "stub"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "complete"
    assert body["root_cause"]
    assert body["steps"] == 4

    trace = client.get(f"/incidents/{incident_id}/trace").json()
    assert [step["agent_name"] for step in trace["steps"]] == list(AGENT_SEQUENCE)
    assert trace["evidence"]
    assert trace["status"] == "complete"

    evidence_ids = {item["id"] for item in trace["evidence"]}
    for step in trace["steps"]:
        assert set(step["evidence_refs"]) <= evidence_ids

    detail = client.get(f"/incidents/{incident_id}").json()
    assert detail["status"] == "complete"
    assert detail["trace_step_count"] == 4
    assert detail["evidence_count"] > 0


def test_triage_endpoint_404s_for_an_unknown_incident(client):
    assert client.post(f"/incidents/{uuid.uuid4()}/triage", json={"provider": "stub"}).status_code == 404
