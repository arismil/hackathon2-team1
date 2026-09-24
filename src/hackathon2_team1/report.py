"""Executive vendor assessment report (deterministic rendering of validated structured data)."""

from __future__ import annotations

from .guardrails.evidence import effective_status
from .guardrails.injection import detect_injection
from .schemas import (
    DomainAssessment,
    EvidenceChunk,
    FindingStatus,
    QualityMetrics,
    RiskDecision,
    VendorAssessmentRequest,
)

_ICON = {"PASS": "PASS", "PARTIAL": "PARTIAL", "FAIL": "FAIL", "UNKNOWN": "UNKNOWN"}
_DOMAIN_TITLE = {
    "security": "Security",
    "procurement_commercial": "Procurement / Commercial",
    "legal_compliance": "Legal / Compliance",
    "ai_governance": "AI Governance",
}


def guard_summary(decision: RiskDecision, domains: list[DomainAssessment]) -> tuple[str, list[str]]:
    """Output guardrail: the narrative must not contradict the policy-validated decision."""
    problems = []
    s = decision.executive_summary or ""
    if not s.strip():
        problems.append("empty summary")
    if detect_injection(s).detected:
        problems.append("summary contains instruction-like / injected content")
    if decision.overrides:
        problems.append("rules overrode the model's proposal; model narrative no longer matches the decision")
    if decision.recommendation.value.lower() not in s.lower():
        problems.append("summary does not state the final recommendation")
    if not problems:
        return s, []
    fails = [f for d in domains for f in d.findings if effective_status(f) == FindingStatus.FAIL]
    unknowns = [f for d in domains for f in d.findings if effective_status(f) == FindingStatus.UNKNOWN]
    text = (
        f"Recommendation: {decision.recommendation.value} - overall risk {decision.overall_risk.value} "
        f"(AI risk tier {decision.ai_risk_tier.value}). "
        f"{len(fails)} control failure(s): " + "; ".join(f.title for f in fails[:5]) + ". "
        f"{len(unknowns)} item(s) UNKNOWN due to missing/unverified evidence: "
        + "; ".join(f.title for f in unknowns[:5]) + ". "
        f"Final decision requires an authorised human approver ({decision.required_human_role})."
    )
    return text, problems


def _verified(f) -> str:
    if f.verified:
        return "yes"
    return "n/a (missing)" if f.evidence_basis.value == "MISSING" else "NO"


def _cite(c) -> str:
    mark = "verified" if c.verified else f"UNVERIFIED: {c.verification_note}"
    return f"`{c.chunk_id}` \"{c.quote[:160]}\" ({mark})"


def render_report(
    assessment_id: str,
    req: VendorAssessmentRequest,
    domains: list[DomainAssessment],
    decision: RiskDecision,
    evidence: dict[str, EvidenceChunk],
    quality: QualityMetrics | None,
    status: str,
    human: dict | None = None,
    plan: dict | None = None,
    degraded: list[str] | None = None,
) -> str:
    L: list[str] = []
    L += [f"# Vendor Assessment - {req.vendor_name}", ""]
    L += [f"**Assessment ID:** {assessment_id}  ", f"**Status:** {status}  ",
          f"**Scope:** {req.service_description}; {req.number_of_users} users; data classification "
          f"{req.data_classification.value}", ""]
    L += ["## 1. Recommendation", "",
          "| Item | Value |", "|---|---|",
          f"| AI recommendation (policy-validated) | **{decision.recommendation.value}** |",
          f"| Overall risk (pre-remediation) | **{decision.overall_risk.value}** |",
          f"| Residual risk if conditions met | {getattr(decision.residual_risk_if_conditions_met, 'value', 'n/a')} |",
          f"| AI risk tier (AI-004) | {decision.ai_risk_tier.value} |",
          *[f"| {_DOMAIN_TITLE.get(k, k)} risk | {v.value} |" for k, v in decision.domain_risks.items()],
          f"| Human approval required | {'YES' if decision.human_review_required else 'no'} "
          f"(role: {decision.required_human_role}) |",
          f"| Decisions a human may record | {', '.join(r.value for r in decision.allowed_final_decisions)} |",
          ""]
    if human:
        L += ["### Human decision", "",
              f"**{human['decision']}** by {human['approver']} ({human['role']}). {human.get('comments', '')}", ""]
    else:
        L += ["> **PENDING HUMAN REVIEW** - this is a recommendation only; no approval has been granted "
              "(AI-004 §6, PR-001 §4, VR-006 §5).", ""]
    L += ["## 2. Executive summary", "", decision.executive_summary, ""]
    if decision.overrides:
        L += ["**Guardrail overrides applied:**", *[f"- {o}" for o in decision.overrides], ""]
    if degraded:
        L += ["**Operational degradation:**", *[f"- {d}" for d in sorted(set(degraded))], ""]

    L += ["## 3. Key risks", "", *[f"- {k}" for k in decision.key_risks], ""]
    L += ["## 4. Required conditions / remediation", "", "| # | Condition | Owner | Timing |", "|---|---|---|---|"]
    L += [f"| {i} | {c.condition} | {c.owner} | {c.timing} |" for i, c in enumerate(decision.conditions, 1)]
    L += ["", "## 5. Required approvals", "", *[f"- [ ] {a}" for a in decision.required_approvals], ""]

    L += ["## 6. Findings by domain", "",
          "Evidence basis: RETRIEVED = stated in a cited document; INFERRED = reasoning from evidence; "
          "MISSING = not found. Only verified citations support a claim.", ""]
    for d in domains:
        L += [f"### {_DOMAIN_TITLE.get(d.domain.value, d.domain.value)} - {decision.domain_risks.get(d.domain.value, d.risk_rating).value}"
              f" ({d.agent})", "", d.summary, ""]
        L += ["| Check | Status | Sev. | Mandatory | Policy | Basis | Verified |", "|---|---|---|---|---|---|---|"]
        for f in d.findings:
            eff = effective_status(f)
            st = _ICON[f.status.value] + (f" (treated as {eff.value})" if eff != f.status else "")
            L.append(f"| {f.title} | {st} | {f.severity.value} | {'yes' if f.mandatory_control else 'no'} | "
                     f"{f.policy_reference or '-'} | {f.evidence_basis.value} | {_verified(f)} |")
        L.append("")
        for f in d.findings:
            if f.status == FindingStatus.PASS and f.verified and not f.guardrail_notes:
                continue
            L += [f"**{f.title}** - {f.status.value}. Requirement: {f.requirement} Vendor: {f.vendor_evidence} "
                  f"Reasoning: {f.reasoning}" + (f" Remediation: {f.remediation}" if f.remediation else "")]
            L += [f"  - {_cite(c)}" for c in f.citations]
            L += [f"  - guardrail: {n}" for n in f.guardrail_notes]
            L.append("")
        if d.contradictions:
            L += ["Contradictions:", *[f"- {c.description} " + " ".join(_cite(x) for x in c.citations) for c in d.contradictions], ""]

    unknowns = [(d.domain.value, f) for d in domains for f in d.findings if effective_status(f) == FindingStatus.UNKNOWN]
    L += ["## 7. Missing / unverified evidence (UNKNOWN - never treated as PASS)", ""]
    L += [f"- [{dom}] {f.title}: {f.vendor_evidence}" for dom, f in unknowns]
    L += [f"- [{d.domain.value}] {m}" for d in domains for m in d.missing_evidence]
    L.append("")

    injected = [c for c in evidence.values() if c.injection_flags]
    L += ["## 8. Guardrail & policy-rule log", "", "| Rule | Policy | Triggered | Detail |", "|---|---|---|---|"]
    L += [f"| {r.rule_id} | {r.policy_reference} | {'YES' if r.triggered else 'no'} | {r.detail} |" for r in decision.rules]
    L.append("")
    if injected:
        L += ["**Prompt-injection detections (content quarantined, never shown to agents as instructions):**"]
        L += [f"- `{c.chunk_id}` ({c.source}, trust={c.trust}) patterns: {', '.join(c.injection_flags)}" for c in injected]
        L.append("")
    if plan and plan.get("guardrail_notes"):
        L += ["**Plan guardrail notes:**", *[f"- {n}" for n in plan["guardrail_notes"]], ""]

    if quality:
        L += ["## 9. Quality metrics (online self-evaluation)", "", "| Metric | Value |", "|---|---|"]
        L += [f"| {k} | {v} |" for k, v in quality.model_dump().items()]
        L.append("")

    used = sorted({c.chunk_id for d in domains for f in d.findings for c in f.citations if c.verified})
    L += ["## 10. Evidence register (verified citations)", ""]
    for cid in used:
        c = evidence.get(cid)
        if c:
            L.append(f"- `{cid}` - {c.source} / {c.section} (trust: {c.trust})")
    L.append("")
    return "\n".join(L)
