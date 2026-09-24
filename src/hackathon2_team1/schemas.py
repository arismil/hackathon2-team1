"""Typed contracts shared by the planner, specialists, guardrails, MCP server and API.

Fields annotated with ``SkipJsonSchema`` are filled in by deterministic code (guardrails,
workflow) and are hidden from the JSON schema the LLM is asked to produce.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator
from pydantic.json_schema import SkipJsonSchema


class DataClassification(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


class Domain(StrEnum):
    SECURITY = "security"
    PROCUREMENT = "procurement_commercial"
    LEGAL = "legal_compliance"
    AI_GOVERNANCE = "ai_governance"


class AgentName(StrEnum):
    SECURITY = "security_agent"
    PROCUREMENT = "procurement_agent"
    LEGAL = "legal_compliance_agent"
    AI_GOVERNANCE = "ai_governance_agent"


DOMAIN_AGENT: dict[Domain, AgentName] = {
    Domain.SECURITY: AgentName.SECURITY,
    Domain.PROCUREMENT: AgentName.PROCUREMENT,
    Domain.LEGAL: AgentName.LEGAL,
    Domain.AI_GOVERNANCE: AgentName.AI_GOVERNANCE,
}


class FindingStatus(StrEnum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class EvidenceBasis(StrEnum):
    RETRIEVED = "RETRIEVED"  # directly stated in a cited, retrieved document
    INFERRED = "INFERRED"  # reasoning from retrieved evidence, not stated verbatim
    MISSING = "MISSING"  # required evidence was not found in the corpus


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


RISK_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}


def max_risk(*levels: RiskLevel) -> RiskLevel:
    return max(levels, key=lambda r: RISK_ORDER[r]) if levels else RiskLevel.LOW


class Recommendation(StrEnum):
    APPROVE = "APPROVE"
    CONDITIONAL_APPROVAL = "CONDITIONAL APPROVAL"
    REJECT = "REJECT"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


# --------------------------------------------------------------------------- request


class VendorAssessmentRequest(BaseModel):
    """FR01 - structured vendor assessment request."""

    vendor_name: str = Field(min_length=2, max_length=120)
    service_description: str = Field(min_length=5, max_length=500)
    number_of_users: int = Field(gt=0, le=1_000_000)
    data_classification: DataClassification
    business_request: str = Field(min_length=10, max_length=4000)
    requested_by: str = Field(default="business-owner", max_length=120)
    business_owner: str | None = Field(default=None, max_length=120)
    contract_term_months: int = Field(default=12, gt=0, le=120)
    restricted_data_exception_approved: bool = False

    @field_validator("vendor_name", "service_description", "business_request")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


# --------------------------------------------------------------------------- plan


class CheckItem(BaseModel):
    check_id: str = Field(description="short snake_case id, e.g. incident_notification")
    description: str
    policy_hint: str | None = Field(default=None, description="NFS policy that likely governs this check")


class PlanTask(BaseModel):
    task_id: str
    domain: Domain
    assigned_agent: AgentName
    objective: str
    required_checks: list[CheckItem]
    status: SkipJsonSchema[TaskStatus] = TaskStatus.PENDING
    attempts: SkipJsonSchema[int] = 0
    notes: SkipJsonSchema[list[str]] = Field(default_factory=list)


class AssessmentPlan(BaseModel):
    objective: str
    assumptions: list[str] = Field(default_factory=list)
    tasks: list[PlanTask]
    revision: SkipJsonSchema[int] = 1
    planner_source: SkipJsonSchema[str] = "llm"  # "llm" | "fallback_template"
    guardrail_notes: SkipJsonSchema[list[str]] = Field(default_factory=list)


# --------------------------------------------------------------------------- findings


class Citation(BaseModel):
    chunk_id: str = Field(description="exact chunk_id from a tool result, e.g. information-security-policy::6")
    quote: str = Field(description="short verbatim quote copied exactly from that chunk's text")
    verified: SkipJsonSchema[bool | None] = None
    verification_note: SkipJsonSchema[str | None] = None


class Finding(BaseModel):
    check_id: str
    title: str
    status: FindingStatus
    severity: Severity
    mandatory_control: bool = Field(description="true if the NFS policy states this as mandatory (must/required)")
    policy_reference: str | None = Field(default=None, description="e.g. 'IS-010 §6'")
    requirement: str = Field(description="what NFS policy requires")
    vendor_evidence: str = Field(description="what the vendor evidence states; 'NOT PROVIDED' if missing")
    evidence_basis: EvidenceBasis
    citations: list[Citation] = Field(default_factory=list)
    reasoning: str = Field(description="brief reasoning; label anything not stated in evidence as inference")
    remediation: str | None = None
    contract_remediable: bool = Field(
        default=True, description="true if a contractual/configuration condition could close the gap before go-live"
    )
    # set by guardrails
    domain: SkipJsonSchema[Domain | None] = None
    verified: SkipJsonSchema[bool] = False
    guardrail_notes: SkipJsonSchema[list[str]] = Field(default_factory=list)


class Contradiction(BaseModel):
    description: str
    citations: list[Citation] = Field(default_factory=list)
    impact: str = ""


class DomainAssessment(BaseModel):
    domain: Domain
    summary: str
    risk_rating: RiskLevel
    findings: list[Finding]
    missing_evidence: list[str] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    required_conditions: list[str] = Field(default_factory=list)
    # domain-specific facts (optional)
    annual_contract_value_eur: float | None = Field(default=None, description="procurement: recurring annual value")
    year_one_cost_eur: float | None = Field(default=None, description="procurement: total year-one cost")
    required_approvals: list[str] = Field(default_factory=list, description="approvals required by policy")
    ai_risk_tier: RiskLevel | None = Field(default=None, description="ai governance: AI-004 risk tier")
    # set by workflow
    agent: SkipJsonSchema[str] = ""
    task_id: SkipJsonSchema[str] = ""
    failed: SkipJsonSchema[bool] = False
    error: SkipJsonSchema[str | None] = None
    computed_risk: SkipJsonSchema[RiskLevel | None] = None


# --------------------------------------------------------------------------- evidence ledger


class EvidenceChunk(BaseModel):
    chunk_id: str
    doc_id: str
    source: str
    title: str
    section: str
    doc_type: str
    trust: str
    text: str
    vendor: str = ""
    injection_flags: list[str] = Field(default_factory=list)
    retrieved_by: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)


class ToolEvent(BaseModel):
    agent: str
    tool: str
    args: dict
    ok: bool
    duration_ms: int
    transport: str
    error: str | None = None
    denied: bool = False
    result_chunks: list[str] = Field(default_factory=list)
    result: dict | None = None


# --------------------------------------------------------------------------- decision


class Condition(BaseModel):
    condition: str
    owner: str = Field(description="e.g. Procurement, Legal, Information Security, Vendor")
    timing: str = Field(description="before contract signature | before go-live | post go-live")


class RiskDecisionDraft(BaseModel):
    """LLM proposal - validated and constrained by decision rules afterwards."""

    recommendation: Recommendation
    overall_risk: RiskLevel
    rationale: str
    key_risks: list[str]
    conditions: list[Condition] = Field(default_factory=list)
    residual_risk_if_conditions_met: RiskLevel | None = None
    executive_summary: str


class RuleResult(BaseModel):
    rule_id: str
    policy_reference: str
    triggered: bool
    detail: str


class RiskDecision(BaseModel):
    recommendation: Recommendation
    overall_risk: RiskLevel
    domain_risks: dict[str, RiskLevel]
    ai_risk_tier: RiskLevel
    rationale: str
    key_risks: list[str]
    conditions: list[Condition]
    residual_risk_if_conditions_met: RiskLevel | None = None
    executive_summary: str
    allowed_final_decisions: list[Recommendation]
    human_review_required: bool
    required_human_role: str
    required_approvals: list[str]
    rules: list[RuleResult]
    overrides: list[str] = Field(default_factory=list)
    llm_draft: RiskDecisionDraft | None = None


class HumanDecision(BaseModel):
    decision: Recommendation
    approver: str = Field(min_length=2)
    role: str
    comments: str = ""


class QualityMetrics(BaseModel):
    findings_total: int
    findings_verified: int
    citations_total: int
    citations_valid: int
    citation_validity: float
    groundedness: float
    check_coverage: float
    unknown_findings: int
    injection_detections: int
    tool_calls: int
    tool_failures: int
    tool_denials: int
    failed_tasks: int
