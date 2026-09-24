"""LangGraph deep-agent workflow.

validate_input -> plan -> [specialists in parallel] -> review_plan (maintain plan / re-plan gaps & failures)
 -> consolidate (evidence guardrails) -> synthesize (LLM + policy rules) -> quality_check -> record
 -> draft_report -> human_review (interrupt) -> finalize
"""

from __future__ import annotations

import functools
import json
import operator
import time
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from . import observability as obs
from .agents import make_plan, run_specialist, synthesize
from .config import get_settings
from .guardrails.access import can_sign_off
from .guardrails.decision_rules import decide
from .guardrails.evidence import normalize_finding, verify_citation
from .guardrails.input import check_request
from .mcp_client import ToolGateway
from .report import guard_summary, render_report
from .schemas import (
    AssessmentPlan,
    CheckItem,
    Contradiction,
    Domain,
    DomainAssessment,
    EvidenceChunk,
    FindingStatus,
    HumanDecision,
    PlanTask,
    QualityMetrics,
    RiskDecision,
    TaskStatus,
    VendorAssessmentRequest,
)


def _merge_evidence(a: dict, b: dict) -> dict:
    out = dict(a or {})
    for cid, c in (b or {}).items():
        if cid in out:
            m = dict(out[cid])
            m["retrieved_by"] = sorted(set(m["retrieved_by"]) | set(c["retrieved_by"]))
            m["queries"] = list(dict.fromkeys(m["queries"] + c["queries"]))
            m["injection_flags"] = sorted(set(m["injection_flags"]) | set(c["injection_flags"]))
            out[cid] = m
        else:
            out[cid] = c
    return out


def _merge_dict(a: dict, b: dict) -> dict:
    return {**(a or {}), **(b or {})}


class AssessmentState(TypedDict, total=False):
    assessment_id: str
    request: dict
    status: str
    input_check: dict
    plan: dict
    delegation: list[dict]
    iteration: int
    domain_results: Annotated[list[dict], operator.add]
    evidence: Annotated[dict, _merge_evidence]
    tool_events: Annotated[list[dict], operator.add]
    degraded: Annotated[list[str], operator.add]
    domains: list[dict]
    decision: dict
    quality: dict
    record: dict
    report_markdown: str
    review_attempts: Annotated[list[dict], operator.add]
    human_decision: dict
    timings: Annotated[dict, _merge_dict]


def timed(name: str):
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(state, *a, **kw):
            t0 = time.perf_counter()
            out = await fn(state, *a, **kw)
            out = dict(out or {})
            if name != "specialist":
                out["timings"] = {**out.get("timings", {}), name: round(time.perf_counter() - t0, 2)}
            obs.event("node_completed", node=name, run_id=state.get("assessment_id"),
                      seconds=round(time.perf_counter() - t0, 2))
            return out
        return wrapper
    return deco


def _req(state) -> VendorAssessmentRequest:
    return VendorAssessmentRequest.model_validate(state["request"])


def _ledger(state) -> dict[str, EvidenceChunk]:
    return {k: EvidenceChunk.model_validate(v) for k, v in (state.get("evidence") or {}).items()}


# --------------------------------------------------------------------------- nodes


@timed("validate_input")
async def validate_input(state: AssessmentState) -> dict:
    req = _req(state)
    chk = check_request(req)
    if not chk.allowed:
        obs.event("input_blocked", run_id=state["assessment_id"], reasons=chk.reasons)
        md = (f"# Vendor Assessment - {req.vendor_name}\n\n**Status:** BLOCKED_BY_INPUT_GUARDRAIL\n\n"
              + "\n".join(f"- {r}" for r in chk.reasons))
        return {"input_check": chk.model_dump(), "status": "BLOCKED_BY_INPUT_GUARDRAIL", "report_markdown": md}
    return {"input_check": chk.model_dump(), "status": "VALIDATED"}


@timed("plan")
async def plan_node(state: AssessmentState) -> dict:
    req = _req(state)
    catalog: list[dict] = []
    degraded: list[str] = []
    try:
        async with ToolGateway(role="orchestrator", run_id=state["assessment_id"], agent="planner") as gw:
            out = json.loads(await gw.call("list_documents", {}))
            catalog = out.get("documents", [])
            if gw.degraded_reason:
                degraded.append(gw.degraded_reason)
    except Exception as e:
        degraded.append(f"document catalog unavailable: {e}")
    plan, delegation = await make_plan(req, catalog)
    return {"plan": plan.model_dump(mode="json"), "delegation": delegation, "iteration": 1,
            "status": "PLANNED", "degraded": degraded}


async def specialist_node(payload: dict) -> dict:
    t0 = time.perf_counter()
    task = PlanTask.model_validate(payload["task"])
    req = VendorAssessmentRequest.model_validate(payload["request"])
    da, ledger, degraded = await run_specialist(task, req, payload["assessment_id"])
    return {
        "domain_results": [da.model_dump(mode="json")],
        "evidence": {k: v.model_dump(mode="json") for k, v in ledger.chunks.items()},
        "tool_events": [e.model_dump(mode="json") for e in ledger.events],
        "degraded": [f"{task.assigned_agent.value}: {degraded}"] if degraded else [],
        "timings": {f"specialist:{task.task_id}:{task.assigned_agent.value}": round(time.perf_counter() - t0, 2)},
    }


def dispatch(state: AssessmentState):
    if state.get("status") == "BLOCKED_BY_INPUT_GUARDRAIL":
        return END
    plan = AssessmentPlan.model_validate(state["plan"])
    pending = [t for t in plan.tasks if t.status == TaskStatus.PENDING]
    return [Send("specialist", {"task": t.model_dump(mode="json"), "request": state["request"],
                                "assessment_id": state["assessment_id"]}) for t in pending]


@timed("review_plan")
async def review_plan(state: AssessmentState) -> dict:
    """Maintain the plan: mark tasks done/failed, retry failures, add follow-up tasks for uncovered checks."""
    plan = AssessmentPlan.model_validate(state["plan"])
    results = [DomainAssessment.model_validate(r) for r in state.get("domain_results", [])]
    latest = {r.task_id: r for r in results}
    iteration = state.get("iteration", 1)
    can_replan = iteration < get_settings().max_plan_iterations
    new_tasks: list[PlanTask] = []
    for t in plan.tasks:
        if t.status != TaskStatus.PENDING:
            continue
        r = latest.get(t.task_id)
        t.attempts += 1
        if r is None or r.failed:
            t.status = TaskStatus.FAILED
            t.notes.append(f"attempt {t.attempts} failed: {r.error if r else 'no result'}")
            if can_replan:
                new_tasks.append(t.model_copy(update={"task_id": f"{t.task_id}r", "status": TaskStatus.PENDING,
                                                      "notes": [f"retry of {t.task_id}"]}))
            continue
        t.status = TaskStatus.COMPLETED
        covered = {f.check_id for f in r.findings}
        missing = [c for c in t.required_checks if c.check_id not in covered]
        if missing:
            t.notes.append(f"uncovered checks: {[c.check_id for c in missing]}")
            if can_replan:
                new_tasks.append(PlanTask(
                    task_id=f"{t.task_id}f", domain=t.domain, assigned_agent=t.assigned_agent,
                    objective=f"Follow-up for {t.task_id}: assess checks not covered in the first pass",
                    required_checks=[CheckItem.model_validate(c.model_dump()) for c in missing],
                    notes=[f"follow-up of {t.task_id}"]))
    if new_tasks:
        plan.tasks += new_tasks
        plan.revision += 1
        obs.event("plan_revised", run_id=state["assessment_id"], revision=plan.revision,
                  new_tasks=[t.task_id for t in new_tasks])
    return {"plan": plan.model_dump(mode="json"), "iteration": iteration + (1 if new_tasks else 0),
            "status": "REPLANNED" if new_tasks else "RESEARCH_COMPLETE"}


def after_review(state: AssessmentState):
    plan = AssessmentPlan.model_validate(state["plan"])
    if any(t.status == TaskStatus.PENDING for t in plan.tasks):
        return dispatch(state)
    return "consolidate"


_GENERIC = {"data", "ai", "control", "controls", "risk", "evidence", "vendor", "requirements", "check"}


def _disagreements(domains: list[DomainAssessment]):
    items = [(d, f) for d in domains for f in d.findings]
    seen = set()
    for i, (da, fa) in enumerate(items):
        for db, fb in items[i + 1:]:
            if da.domain == db.domain or {fa.status, fb.status} != {FindingStatus.PASS, FindingStatus.FAIL}:
                continue
            topic = (set(fa.check_id.split("_")) & set(fb.check_id.split("_"))) - _GENERIC
            shared = {c.chunk_id for c in fa.citations if c.verified} & {c.chunk_id for c in fb.citations if c.verified}
            key = tuple(sorted((fa.check_id, fb.check_id)))
            if topic and shared and key not in seen:
                seen.add(key)
                yield (da, fa), (db, fb)


@timed("consolidate")
async def consolidate(state: AssessmentState) -> dict:
    """Evidence consolidation + evidence guardrails (citation verification, UNKNOWN handling, disagreements)."""
    ledger = _ledger(state)
    results = [DomainAssessment.model_validate(r) for r in state.get("domain_results", [])]
    merged: dict[Domain, DomainAssessment] = {}
    for r in results:  # results arrive in plan order: base task, then retries / follow-ups
        base = merged.get(r.domain)
        if base is None or (base.failed and not r.failed):
            merged[r.domain] = r.model_copy(deep=True)
            continue
        if r.failed:
            continue
        have = {f.check_id for f in base.findings}
        base.findings += [f for f in r.findings if f.check_id not in have]
        base.missing_evidence += r.missing_evidence
        base.contradictions += r.contradictions
        base.required_conditions += r.required_conditions
        base.required_approvals = base.required_approvals or r.required_approvals
        base.annual_contract_value_eur = base.annual_contract_value_eur or r.annual_contract_value_eur
        base.year_one_cost_eur = base.year_one_cost_eur or r.year_one_cost_eur
        base.ai_risk_tier = base.ai_risk_tier or r.ai_risk_tier
    domains = [merged[d] for d in Domain if d in merged]
    for d in domains:
        d.findings = [normalize_finding(f, ledger) for f in d.findings]
        for f in d.findings:
            f.domain = d.domain
        for c in d.contradictions:
            c.citations = [verify_citation(x, ledger) for x in c.citations]
    # inter-agent disagreement: agents reach opposite conclusions on the same topic from the same evidence
    for a, b in _disagreements(domains):
        (da, fa), (db, fb) = a, b
        shared = sorted({c.chunk_id for c in fa.citations if c.verified} & {c.chunk_id for c in fb.citations if c.verified})
        da.contradictions.append(Contradiction(
            description=f"Inter-agent disagreement: {da.agent}:{fa.check_id}={fa.status.value} vs "
                        f"{db.agent}:{fb.check_id}={fb.status.value} on shared evidence {shared}",
            impact="Human reviewer to resolve; decision rules apply the conservative (FAIL) reading"))
    return {"domains": [d.model_dump(mode="json") for d in domains], "status": "EVIDENCE_CONSOLIDATED"}


@timed("synthesize")
async def synthesize_node(state: AssessmentState) -> dict:
    req = _req(state)
    ledger = _ledger(state)
    domains = [DomainAssessment.model_validate(d) for d in state["domains"]]
    with obs.span("policy_rules_preview", as_type="guardrail"):
        preview = decide(req, domains, ledger, None)
    draft = await synthesize(req, domains, preview.rules, [r.value for r in preview.allowed_final_decisions])
    with obs.span("policy_rules_enforcement", as_type="guardrail", input={"llm_draft": draft.model_dump() if draft else None}) as sp:
        decision = decide(req, domains, ledger, draft)
        summary, problems = guard_summary(decision, domains)
        if problems:
            decision.overrides.append(f"executive summary regenerated by output guardrail: {problems}")
            decision.executive_summary = summary
        if sp:
            sp.update(output={"recommendation": decision.recommendation, "overall_risk": decision.overall_risk,
                              "overrides": decision.overrides})
    return {"decision": decision.model_dump(mode="json"), "domains": [d.model_dump(mode="json") for d in domains],
            "status": "DECISION_PROPOSED"}


def compute_quality(state: AssessmentState) -> QualityMetrics:
    domains = [DomainAssessment.model_validate(d) for d in state["domains"]]
    plan = AssessmentPlan.model_validate(state["plan"])
    findings = [f for d in domains for f in d.findings]
    cits = [c for f in findings for c in f.citations]
    material = [f for f in findings if f.status != FindingStatus.UNKNOWN]
    required = {(t.domain, c.check_id) for t in plan.tasks for c in t.required_checks}
    covered = {(d.domain, f.check_id) for d in domains for f in d.findings if not d.failed}
    events = state.get("tool_events", [])
    return QualityMetrics(
        findings_total=len(findings),
        findings_verified=sum(f.verified for f in findings),
        citations_total=len(cits),
        citations_valid=sum(bool(c.verified) for c in cits),
        citation_validity=round(sum(bool(c.verified) for c in cits) / len(cits), 3) if cits else 0.0,
        groundedness=round(sum(any(c.verified for c in f.citations) for f in material) / len(material), 3) if material else 0.0,
        check_coverage=round(len(required & covered) / len(required), 3) if required else 0.0,
        unknown_findings=sum(f.status == FindingStatus.UNKNOWN for f in findings),
        injection_detections=sum(1 for c in (state.get("evidence") or {}).values() if c["injection_flags"]),
        tool_calls=len(events),
        tool_failures=sum(not e["ok"] and not e["denied"] for e in events),
        tool_denials=sum(e["denied"] for e in events),
        failed_tasks=sum(d.failed for d in domains),
    )


@timed("quality_check")
async def quality_check(state: AssessmentState) -> dict:
    q = compute_quality(state)
    with obs.span("online_quality_check", as_type="evaluator", output=q.model_dump()):
        pass
    return {"quality": q.model_dump(), "status": "QUALITY_CHECKED"}


@timed("record")
async def record_node(state: AssessmentState) -> dict:
    dec = RiskDecision.model_validate(state["decision"])
    req = _req(state)
    summary = {"recommendation": dec.recommendation, "overall_risk": dec.overall_risk,
               "domain_risks": dec.domain_risks, "quality": state.get("quality")}
    try:
        async with ToolGateway(role="orchestrator", run_id=state["assessment_id"], agent="orchestrator") as gw:
            out = await gw.call("record_assessment", {
                "assessment_id": state["assessment_id"], "vendor": req.vendor_name,
                "recommendation": dec.recommendation.value, "overall_risk": dec.overall_risk.value,
                "summary_json": json.dumps(summary, default=str)})
            record = json.loads(out)
            events = [e.model_dump(mode="json") for e in gw.ledger.events]
    except Exception as e:
        record, events = {"error": str(e)}, []
    return {"record": record, "tool_events": events}


def _render(state: AssessmentState, status: str, human: dict | None = None) -> str:
    return render_report(
        state["assessment_id"], _req(state), [DomainAssessment.model_validate(d) for d in state["domains"]],
        RiskDecision.model_validate(state["decision"]), _ledger(state),
        QualityMetrics.model_validate(state["quality"]) if state.get("quality") else None,
        status, human, state.get("plan"), state.get("degraded"),
    )


@timed("draft_report")
async def draft_report(state: AssessmentState) -> dict:
    dec = RiskDecision.model_validate(state["decision"])
    status = "PENDING_HUMAN_REVIEW" if dec.human_review_required else "RECOMMENDATION_ISSUED"
    return {"report_markdown": _render(state, status), "status": status}


async def human_review(state: AssessmentState) -> dict:
    """HITL gate (FR12): the graph pauses here until an authorised human records the final decision."""
    dec = RiskDecision.model_validate(state["decision"])
    if not dec.human_review_required:
        return {}
    last_error = next((a["reason"] for a in reversed(state.get("review_attempts", [])) if not a["accepted"]), None)
    raw = interrupt({
        "assessment_id": state["assessment_id"],
        "recommendation": dec.recommendation.value,
        "overall_risk": dec.overall_risk.value,
        "required_role": dec.required_human_role,
        "allowed_decisions": [r.value for r in dec.allowed_final_decisions],
        "required_approvals": dec.required_approvals,
        "previous_attempt_rejected": last_error,
    })
    try:
        hd = HumanDecision.model_validate(raw)
    except Exception as e:
        return {"review_attempts": [{"input": raw, "accepted": False, "reason": f"invalid input: {e}"}]}
    reason = None
    if not can_sign_off(hd.role, dec.overall_risk.value):
        reason = f"role '{hd.role}' is not authorised to decide a {dec.overall_risk.value}-risk vendor " \
                 f"(requires {dec.required_human_role})"
    elif hd.decision not in dec.allowed_final_decisions:
        reason = f"decision {hd.decision.value} is not permitted by policy rules " \
                 f"(allowed: {[r.value for r in dec.allowed_final_decisions]})"
    if reason:
        obs.event("human_review_rejected", run_id=state["assessment_id"], reason=reason)
        return {"review_attempts": [{"input": hd.model_dump(mode="json"), "accepted": False, "reason": reason}]}
    rec = {}
    try:
        async with ToolGateway(role=hd.role, run_id=state["assessment_id"], agent=f"human:{hd.approver}") as gw:
            rec = json.loads(await gw.call("record_human_decision", {
                "assessment_id": state["assessment_id"], "decision": hd.decision.value,
                "approver": hd.approver, "comments": hd.comments}))
    except Exception as e:
        rec = {"error": str(e)}
    obs.event("human_decision", run_id=state["assessment_id"], decision=hd.decision, role=hd.role)
    return {"human_decision": {**hd.model_dump(mode="json"), "record": rec},
            "review_attempts": [{"input": hd.model_dump(mode="json"), "accepted": True, "reason": None}]}


def after_human(state: AssessmentState):
    dec = state["decision"]
    if dec["human_review_required"] and not state.get("human_decision"):
        return "human_review"
    return "finalize"


@timed("finalize")
async def finalize(state: AssessmentState) -> dict:
    hd = state.get("human_decision")
    if hd:
        status = {"APPROVE": "APPROVED", "CONDITIONAL APPROVAL": "CONDITIONALLY_APPROVED",
                  "REJECT": "REJECTED"}[hd["decision"]] + "_BY_HUMAN"
    else:
        status = "RECOMMENDATION_ISSUED"
    return {"report_markdown": _render(state, status, hd), "status": status}


# --------------------------------------------------------------------------- graph


def build_graph(checkpointer=None):
    g = StateGraph(AssessmentState)
    g.add_node("validate_input", validate_input)
    g.add_node("plan", plan_node)
    g.add_node("specialist", specialist_node)
    g.add_node("review_plan", review_plan)
    g.add_node("consolidate", consolidate)
    g.add_node("synthesize", synthesize_node)
    g.add_node("quality_check", quality_check)
    g.add_node("record", record_node)
    g.add_node("draft_report", draft_report)
    g.add_node("human_review", human_review)
    g.add_node("finalize", finalize)

    g.add_edge(START, "validate_input")
    g.add_conditional_edges("validate_input", lambda s: END if s["status"].startswith("BLOCKED") else "plan", ["plan", END])
    g.add_conditional_edges("plan", dispatch, ["specialist", END])
    g.add_edge("specialist", "review_plan")
    g.add_conditional_edges("review_plan", after_review, ["specialist", "consolidate"])
    g.add_edge("consolidate", "synthesize")
    g.add_edge("synthesize", "quality_check")
    g.add_edge("quality_check", "record")
    g.add_edge("record", "draft_report")
    g.add_edge("draft_report", "human_review")
    g.add_conditional_edges("human_review", after_human, ["human_review", "finalize"])
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


def state_summary(values: dict[str, Any]) -> dict:
    """Compact, API-friendly view of the workflow state."""
    return {k: values.get(k) for k in (
        "assessment_id", "status", "plan", "delegation", "decision", "quality", "human_decision",
        "review_attempts", "degraded", "timings", "record", "input_check")}
