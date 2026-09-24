"""Deep-agent components: planner, specialist agents (tool-using ReAct agents over MCP) and synthesiser."""

from __future__ import annotations

import asyncio
import json
import logging

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import HumanMessage, SystemMessage

from .config import Settings, get_settings
from .llm import get_chat_model
from .mcp_client import EvidenceLedger, ToolGateway
from .observability import event
from .prompts import (
    AGENT_TITLES,
    CONTROL_LIBRARY,
    DOMAIN_FOCUS,
    PLANNER_SYSTEM,
    SPECIALIST_SYSTEM,
    SPECIALIST_TASK,
    SYNTHESIS_SYSTEM,
)
from .schemas import (
    DOMAIN_AGENT,
    AssessmentPlan,
    CheckItem,
    Domain,
    DomainAssessment,
    EvidenceBasis,
    Finding,
    FindingStatus,
    PlanTask,
    RiskDecisionDraft,
    RiskLevel,
    RuleResult,
    Severity,
    TaskStatus,
    VendorAssessmentRequest,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- planner


def _baseline_checks(domain: Domain) -> list[CheckItem]:
    return [CheckItem(check_id=c, description=d, policy_hint=p) for c, d, p in CONTROL_LIBRARY[domain]]


def template_plan(req: VendorAssessmentRequest) -> AssessmentPlan:
    return AssessmentPlan(
        objective=f"Assess {req.vendor_name} for: {req.service_description}",
        assumptions=["Planner LLM unavailable - baseline control-library plan used"],
        tasks=[
            PlanTask(task_id=f"T{i}", domain=d, assigned_agent=DOMAIN_AGENT[d],
                     objective=f"Assess {d.value.replace('_', ' ')} risk of {req.vendor_name}",
                     required_checks=_baseline_checks(d))
            for i, d in enumerate(Domain, start=1)
        ],
        planner_source="fallback_template",
    )


def enforce_plan(plan: AssessmentPlan) -> tuple[AssessmentPlan, list[dict]]:
    """Plan guardrail: every mandatory domain covered, correct delegation, baseline checks present."""
    delegation = [
        {"task_id": t.task_id, "domain": t.domain.value, "assigned_agent": t.assigned_agent.value,
         "correct": DOMAIN_AGENT[t.domain] == t.assigned_agent}
        for t in plan.tasks
    ]
    notes = list(plan.guardrail_notes)
    tasks: dict[Domain, PlanTask] = {}
    for t in plan.tasks:
        if DOMAIN_AGENT[t.domain] != t.assigned_agent:
            notes.append(f"{t.task_id}: re-delegated {t.assigned_agent} -> {DOMAIN_AGENT[t.domain]}")
            t.assigned_agent = DOMAIN_AGENT[t.domain]
        if t.domain in tasks:  # merge duplicate domain tasks
            tasks[t.domain].required_checks += t.required_checks
            notes.append(f"{t.task_id}: merged into {tasks[t.domain].task_id} (duplicate domain)")
        else:
            tasks[t.domain] = t
    for d in Domain:
        if d not in tasks:
            notes.append(f"added missing mandatory domain task: {d.value}")
            tasks[d] = PlanTask(task_id="", domain=d, assigned_agent=DOMAIN_AGENT[d],
                                objective=f"Assess {d.value.replace('_', ' ')} risk", required_checks=[])
    for i, d in enumerate(Domain, start=1):
        t = tasks[d]
        t.task_id = f"T{i}"
        have = {c.check_id for c in t.required_checks}
        added = [c for c in _baseline_checks(d) if c.check_id not in have]
        if added:
            t.required_checks += added
            notes.append(f"{t.task_id}: added baseline checks {[c.check_id for c in added]}")
        seen: set[str] = set()
        t.required_checks = [c for c in t.required_checks if not (c.check_id in seen or seen.add(c.check_id))]
        t.status, t.attempts = TaskStatus.PENDING, 0
    plan.tasks = [tasks[d] for d in Domain]
    plan.guardrail_notes = notes
    return plan, delegation


async def make_plan(req: VendorAssessmentRequest, catalog: list[dict], run_id: str = "") -> tuple[AssessmentPlan, list[dict]]:
    catalog_txt = "\n".join(
        f"- {d['doc_id']} ({d['doc_type']}, trust={d['trust']}) {d.get('policy_id', '')} {d['title']}" for d in catalog
    ) or "(catalog unavailable)"
    baseline = "\n".join(
        f"{d.value}: " + ", ".join(c for c, _, _ in CONTROL_LIBRARY[d]) for d in Domain
    )
    msgs = [
        SystemMessage(PLANNER_SYSTEM),
        HumanMessage(
            f"REQUEST:\n{req.model_dump_json(indent=2)}\n\nDOCUMENT CATALOG:\n{catalog_txt}\n\n"
            f"BASELINE CHECKS (always include, add request-specific ones):\n{baseline}"
        ),
    ]
    try:
        llm = get_chat_model("planner").with_structured_output(AssessmentPlan, method="function_calling")
        plan = await llm.ainvoke(msgs, config={"run_name": "deep_agent_planner"})
        if not isinstance(plan, AssessmentPlan) or not plan.tasks:
            raise ValueError("planner returned no tasks")
    except Exception as e:
        log.warning("planner failed, using template plan: %s", e)
        event("planner_fallback", run_id=run_id, error=str(e)[:300])
        plan = template_plan(req)
    return enforce_plan(plan)


# --------------------------------------------------------------------------- specialists


def failed_assessment(task: PlanTask, error: str) -> DomainAssessment:
    """Fail-safe output: every required check UNKNOWN (never PASS) when an agent fails."""
    return DomainAssessment(
        domain=task.domain,
        summary=f"Specialist agent failed ({error[:200]}). Domain NOT assessed - all checks UNKNOWN.",
        risk_rating=RiskLevel.MEDIUM,
        findings=[
            Finding(check_id=c.check_id, title=c.description, status=FindingStatus.UNKNOWN, severity=Severity.HIGH,
                    mandatory_control=False, policy_reference=c.policy_hint, requirement=c.description,
                    vendor_evidence="NOT ASSESSED - agent failure", evidence_basis=EvidenceBasis.MISSING,
                    reasoning="Agent failure; recorded as UNKNOWN (fail-safe).")
            for c in task.required_checks
        ],
        missing_evidence=[f"{task.domain.value} assessment incomplete due to agent failure"],
        failed=True,
        error=error[:500],
    )


async def run_specialist(task: PlanTask, req: VendorAssessmentRequest, run_id: str,
                         settings: Settings | None = None) -> tuple[DomainAssessment, EvidenceLedger, str | None]:
    settings = settings or get_settings()
    agent_name = task.assigned_agent.value
    ledger = EvidenceLedger()
    degraded = None
    system = SPECIALIST_SYSTEM.format(
        title=AGENT_TITLES[task.domain], domain=task.domain.value, focus=DOMAIN_FOCUS[task.domain],
        vendor=req.vendor_name, max_calls=max(4, settings.specialist_model_call_limit - 4),
    )
    user = SPECIALIST_TASK.format(
        request=req.model_dump_json(indent=2), task_id=task.task_id, objective=task.objective,
        checks="\n".join(f"- {c.check_id}: {c.description} (hint: {c.policy_hint or '-'})" for c in task.required_checks),
        domain=task.domain.value,
    )
    try:
        async with ToolGateway(role=agent_name, ledger=ledger, settings=settings, run_id=run_id) as gw:
            model = get_chat_model(agent_name)
            agent = create_agent(
                model,
                tools=gw.tools(),
                system_prompt=system,
                response_format=ToolStrategy(DomainAssessment),
                middleware=[ModelCallLimitMiddleware(run_limit=settings.specialist_model_call_limit, exit_behavior="end")],
                name=agent_name,
            )
            result = await asyncio.wait_for(
                agent.ainvoke({"messages": [HumanMessage(user)]},
                              config={"recursion_limit": 80, "run_name": agent_name,
                                      "metadata": {"task_id": task.task_id, "domain": task.domain.value}}),
                timeout=settings.specialist_timeout_s,
            )
            da = result.get("structured_response")
            if da is None:  # call budget exhausted before the agent answered -> force the structured answer
                event("specialist_budget_exhausted", run_id=run_id, agent=agent_name)
                forced = model.with_structured_output(DomainAssessment, method="function_calling")
                da = await forced.ainvoke(
                    [SystemMessage(system), *result["messages"],
                     HumanMessage("Tool budget exhausted. Return the DomainAssessment now using only evidence already "
                                  "retrieved; record anything you could not check as UNKNOWN / MISSING.")],
                    config={"run_name": f"{agent_name}_finalize"},
                )
            degraded = gw.degraded_reason
    except Exception as e:
        log.warning("specialist %s failed: %s: %s", agent_name, type(e).__name__, str(e)[:200])
        event("specialist_failed", run_id=run_id, agent=agent_name, error=f"{type(e).__name__}: {e}"[:300])
        da = failed_assessment(task, f"{type(e).__name__}: {e}")
    da.domain, da.agent, da.task_id = task.domain, agent_name, task.task_id
    for f in da.findings:
        f.domain = task.domain
    return da, ledger, degraded


# --------------------------------------------------------------------------- synthesis


def _finding_digest(domains: list[DomainAssessment]) -> list[dict]:
    from .guardrails.evidence import effective_status

    return [
        {
            "domain": d.domain.value, "check_id": f.check_id, "title": f.title, "status": f.status.value,
            "effective_status": effective_status(f).value, "severity": f.severity.value,
            "mandatory": f.mandatory_control, "policy": f.policy_reference, "requirement": f.requirement,
            "vendor_evidence": f.vendor_evidence, "verified": f.verified, "remediation": f.remediation,
            "contract_remediable": f.contract_remediable, "guardrail_notes": f.guardrail_notes,
        }
        for d in domains for f in d.findings
    ]


async def synthesize(req: VendorAssessmentRequest, domains: list[DomainAssessment], rules: list[RuleResult],
                     allowed: list[str], run_id: str = "") -> RiskDecisionDraft | None:
    payload = {
        "request": req.model_dump(mode="json"),
        "domain_summaries": [
            {"domain": d.domain.value, "risk_rating": d.risk_rating.value, "computed_risk": getattr(d.computed_risk, "value", None),
             "summary": d.summary, "missing_evidence": d.missing_evidence,
             "contradictions": [c.description for c in d.contradictions], "conditions": d.required_conditions,
             "failed": d.failed}
            for d in domains
        ],
        "findings": _finding_digest(domains),
        "triggered_policy_rules": [r.model_dump() for r in rules if r.triggered],
        "recommendations_permitted_by_rules": allowed,
    }
    try:
        llm = get_chat_model("synthesizer").with_structured_output(RiskDecisionDraft, method="function_calling")
        return await llm.ainvoke(
            [SystemMessage(SYNTHESIS_SYSTEM), HumanMessage(json.dumps(payload, indent=1, default=str))],
            config={"run_name": "risk_synthesizer"},
        )
    except Exception as e:
        log.warning("synthesis failed: %s", e)
        event("synthesis_failed", run_id=run_id, error=str(e)[:300])
        return None
