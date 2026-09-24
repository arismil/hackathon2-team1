"""Policy-as-code decision guardrails (FR10/FR11/FR12).

The LLM *proposes* a recommendation; these deterministic rules, each traceable to an NFS
policy clause, bound what can be recommended and who must sign off. Model output (or a
prompt injection that fooled the model) can never produce an outcome the rules forbid.
"""

from __future__ import annotations

from ..schemas import (
    Condition,
    DataClassification,
    Domain,
    DomainAssessment,
    EvidenceChunk,
    FindingStatus,
    Recommendation,
    RiskDecision,
    RiskDecisionDraft,
    RiskLevel,
    RuleResult,
    Severity,
    VendorAssessmentRequest,
    max_risk,
)
from .evidence import effective_status

APPROVAL_THRESHOLDS = [  # PR-001 §2 (annual value, EUR)
    (25_000, ["Business owner", "Procurement"]),
    (100_000, ["Business owner", "Procurement", "Finance"]),
    (float("inf"), ["Business owner", "Procurement", "Finance", "Technology Investment Committee"]),
]
AI_TIER_FLOOR = {
    DataClassification.PUBLIC: RiskLevel.LOW,
    DataClassification.INTERNAL: RiskLevel.MEDIUM,
    DataClassification.CONFIDENTIAL: RiskLevel.HIGH,  # AI-004 §2
    DataClassification.RESTRICTED: RiskLevel.HIGH,
}
_SENSITIVE = {DataClassification.CONFIDENTIAL, DataClassification.RESTRICTED}


def compute_domain_risk(da: DomainAssessment) -> RiskLevel:
    risk = RiskLevel.LOW
    if da.failed:
        return RiskLevel.HIGH  # fail safe: controls that could not be assessed are treated as not demonstrated
    for f in da.findings:
        st = effective_status(f)
        if st == FindingStatus.FAIL and (f.mandatory_control or f.severity == Severity.CRITICAL):
            return RiskLevel.HIGH  # VR-006 §2: a single mandatory-control failure can drive HIGH
        if st in (FindingStatus.FAIL, FindingStatus.PARTIAL):
            risk = max_risk(risk, RiskLevel.MEDIUM)
        if st == FindingStatus.UNKNOWN and f.mandatory_control:
            risk = max_risk(risk, RiskLevel.MEDIUM)
    return risk


def required_approvals(annual_value: float | None, req: VendorAssessmentRequest) -> tuple[list[str], str]:
    if annual_value is None:
        base, note = APPROVAL_THRESHOLDS[-1][1], "annual value UNKNOWN - highest threshold applied"
    else:
        base = next(a for limit, a in APPROVAL_THRESHOLDS if annual_value <= limit)
        note = f"annual value EUR {annual_value:,.0f}"
    approvals = list(base)
    if req.data_classification in _SENSITIVE:
        approvals.append("Information Security (before contract signature)")  # PR-001 §3
    approvals.append("AI Governance review")  # PR-001 §4
    if req.data_classification == DataClassification.RESTRICTED:
        approvals.append("CISO + Legal written exception")  # DC-002 §4
    return approvals, note


def _merge_approvals(*lists: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for lst in lists:
        for a in lst:
            key = a.lower().split("(")[0].strip()
            if key and key not in seen:
                seen.add(key)
                out.append(a)
    return out


def decide(
    req: VendorAssessmentRequest,
    domains: list[DomainAssessment],
    ledger: dict[str, EvidenceChunk],
    draft: RiskDecisionDraft | None,
) -> RiskDecision:
    rules: list[RuleResult] = []
    allowed = {Recommendation.APPROVE, Recommendation.CONDITIONAL_APPROVAL, Recommendation.REJECT}
    extra_conditions: list[Condition] = []

    def rule(rule_id: str, ref: str, triggered: bool, detail: str):
        rules.append(RuleResult(rule_id=rule_id, policy_reference=ref, triggered=triggered, detail=detail))
        return triggered

    by_domain = {d.domain: d for d in domains}
    findings = [f for d in domains for f in d.findings]
    mandatory = [f for f in findings if f.mandatory_control]
    mand_fail = [f for f in mandatory if effective_status(f) == FindingStatus.FAIL]
    mand_open = [f for f in mandatory if effective_status(f) != FindingStatus.PASS]
    mand_unknown = [f for f in mandatory if effective_status(f) == FindingStatus.UNKNOWN]
    unfixable = [f for f in mand_fail if not f.contract_remediable]
    sec = by_domain.get(Domain.SECURITY)
    sec_fail = [f for f in (sec.findings if sec else []) if f.mandatory_control and effective_status(f) != FindingStatus.PASS]
    sensitive = req.data_classification in _SENSITIVE

    domain_risks: dict[str, RiskLevel] = {}
    for d in domains:
        d.computed_risk = compute_domain_risk(d)
        domain_risks[d.domain.value] = max_risk(d.risk_rating, d.computed_risk)
    missing_domains = [x for x in Domain if x not in by_domain or by_domain[x].failed]
    computed_overall = max_risk(*domain_risks.values()) if domain_risks else RiskLevel.HIGH

    if rule("R01", "VR-006 §2", bool(mand_fail), f"{len(mand_fail)} mandatory-control failure(s): "
            + "; ".join(f.title for f in mand_fail[:6])):
        computed_overall = RiskLevel.HIGH
    if rule("R02", "IS-010 §8", sensitive and bool(sec_fail),
            "security mandatory control(s) not met for Confidential/Restricted data -> HIGH security risk, "
            "no unconditional production approval"):
        domain_risks[Domain.SECURITY.value] = RiskLevel.HIGH
        computed_overall = RiskLevel.HIGH
        allowed.discard(Recommendation.APPROVE)
    if rule("R03", "VR-006 §4; PR-001 §7", bool(mand_unknown),
            f"{len(mand_unknown)} mandatory control(s) UNKNOWN (missing/unverified evidence is not PASS): "
            + "; ".join(f.title for f in mand_unknown[:6])):
        allowed.discard(Recommendation.APPROVE)
        computed_overall = max_risk(computed_overall, RiskLevel.MEDIUM)
    if rule("R04", "VR-006 §3", bool(mand_open), "APPROVE requires all mandatory controls satisfied"):
        allowed.discard(Recommendation.APPROVE)
    if rule("R11", "VR-006 §4", not findings or any(effective_status(f) == FindingStatus.UNKNOWN for f in findings),
            "an incomplete assessment or UNKNOWN finding cannot support unconditional APPROVE"):
        allowed.discard(Recommendation.APPROVE)
    if rule("R05", "VR-006 §3", bool(unfixable),
            "mandatory control(s) cannot be met even with contractual remediation: "
            + "; ".join(f.title for f in unfixable)):
        allowed &= {Recommendation.REJECT}
    restricted = req.data_classification == DataClassification.RESTRICTED
    if rule("R06", "DC-002 §4", restricted and not req.restricted_data_exception_approved,
            "Restricted data must not be submitted to external generative AI without CISO+Legal written exception"):
        allowed &= {Recommendation.REJECT}
        computed_overall = RiskLevel.HIGH

    injected = sorted({c.chunk_id for c in ledger.values() if c.injection_flags})
    if rule("R07", "AI-004 §5", bool(injected),
            f"embedded instructions targeting automated reviewers found in {injected}; content quarantined and "
            "ignored; vendor evidence reliability must be confirmed by a human"):
        allowed.discard(Recommendation.APPROVE)
        extra_conditions.append(Condition(
            condition=f"Vendor to formally retract/explain instructions embedded in its submission ({', '.join(injected)}) "
                      "and re-attest the accuracy of all submitted evidence",
            owner="Procurement / Vendor", timing="before contract signature"))
    if rule("R08", "VR-006 §4", bool(missing_domains),
            f"domain assessment missing or failed: {[m.value for m in missing_domains]} - fail safe: an "
            "incomplete assessment cannot support any approval; re-run required"):
        allowed &= {Recommendation.REJECT}
        computed_overall = RiskLevel.HIGH

    ai_da = by_domain.get(Domain.AI_GOVERNANCE)
    ai_tier = max_risk(AI_TIER_FLOOR[req.data_classification], *([ai_da.ai_risk_tier] if ai_da and ai_da.ai_risk_tier else []))

    # ---- reconcile the LLM draft with the rules
    overrides: list[str] = []
    if draft is None:
        overrides.append("LLM synthesis unavailable - deterministic fallback recommendation used")
        rec = (Recommendation.CONDITIONAL_APPROVAL
               if Recommendation.CONDITIONAL_APPROVAL in allowed and not mand_fail else Recommendation.REJECT)
        overall = computed_overall
    else:
        rec, overall = draft.recommendation, draft.overall_risk
        if overall != max_risk(overall, computed_overall):
            overrides.append(f"overall risk raised {overall} -> {computed_overall} by policy rules")
            overall = computed_overall
        if rec not in allowed:
            new = (Recommendation.CONDITIONAL_APPROVAL
                   if rec == Recommendation.APPROVE and Recommendation.CONDITIONAL_APPROVAL in allowed
                   else Recommendation.REJECT)
            overrides.append(f"recommendation {rec} not permitted by triggered rules -> {new}")
            rec = new

    proc = by_domain.get(Domain.PROCUREMENT)
    annual = proc.annual_contract_value_eur if proc else None
    policy_approvals, value_note = required_approvals(annual, req)
    rule("R09", "PR-001 §2-§4", True, f"approval chain from {value_note}: {', '.join(policy_approvals)}")
    if overall == RiskLevel.HIGH:
        policy_approvals.append("Accountable executive risk owner (risk acceptance)")  # VR-006 §5
    approvals = _merge_approvals(policy_approvals, proc.required_approvals if proc else [])

    human_required = ai_tier == RiskLevel.HIGH or overall == RiskLevel.HIGH or rec != Recommendation.REJECT
    rule("R10", "AI-004 §6; PR-001 §4; VR-006 §5", human_required,
         "final decision must be taken by an authorised human approver; AI output is a recommendation only")
    human_role = "executive_risk_owner" if overall == RiskLevel.HIGH else "vendor_risk_manager"

    allowed_final = [r for r in Recommendation if r in allowed] or [Recommendation.REJECT]
    base = draft or RiskDecisionDraft(
        recommendation=rec, overall_risk=overall, rationale="Deterministic fallback: see triggered policy rules.",
        key_risks=[], conditions=[], executive_summary="")
    key_risks = list(base.key_risks)
    if overrides or not key_risks:  # model narrative unusable -> derive key risks from validated findings
        derived = [f"{f.title} ({f.policy_reference or 'policy'}): {effective_status(f).value} - {f.vendor_evidence}"
                   for f in mand_fail + mand_unknown]
        key_risks = derived + [k for k in key_risks if k not in derived]
    conditions = list(base.conditions)
    if not conditions:
        conditions = [
            Condition(condition=f.remediation, owner=(f.domain.value if f.domain else "Risk owner"),
                      timing="before contract signature" if f.contract_remediable else "before go-live")
            for f in findings if f.remediation and effective_status(f) != FindingStatus.PASS
        ]
    return RiskDecision(
        recommendation=rec,
        overall_risk=overall,
        domain_risks=domain_risks,
        ai_risk_tier=ai_tier,
        rationale=base.rationale,
        key_risks=key_risks[:12],
        conditions=[*conditions, *extra_conditions],
        residual_risk_if_conditions_met=base.residual_risk_if_conditions_met,
        executive_summary=base.executive_summary,
        allowed_final_decisions=allowed_final,
        human_review_required=human_required,
        required_human_role=human_role,
        required_approvals=approvals,
        rules=rules,
        overrides=overrides,
        llm_draft=draft,
    )
