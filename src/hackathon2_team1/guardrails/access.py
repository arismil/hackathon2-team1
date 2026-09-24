"""Role-based authorisation for MCP tools (least privilege). Enforced on both client and server."""

from __future__ import annotations

READ_TOOLS = {"search_policy", "search_vendor_documents", "retrieve_document", "list_documents", "get_vendor_history"}

TOOL_POLICY: dict[str, set[str]] = {
    "security_agent": READ_TOOLS,
    "legal_compliance_agent": READ_TOOLS,
    "ai_governance_agent": READ_TOOLS,
    "procurement_agent": READ_TOOLS | {"calculate_tco"},
    "orchestrator": {"list_documents", "record_assessment", "get_prior_assessments"},
    "evaluator": READ_TOOLS | {"calculate_tco", "get_prior_assessments"},
    # human roles (never given to an agent)
    "executive_risk_owner": {"record_human_decision", "get_prior_assessments", "list_documents"},
    "vendor_risk_manager": {"record_human_decision", "get_prior_assessments", "list_documents"},
}

# Tools an automated agent may never call, regardless of configuration.
HUMAN_ONLY_TOOLS = {"record_human_decision"}
AGENT_ROLES = {"security_agent", "legal_compliance_agent", "ai_governance_agent", "procurement_agent", "orchestrator"}


def is_allowed(role: str, tool: str) -> bool:
    if role in AGENT_ROLES and tool in HUMAN_ONLY_TOOLS:
        return False
    return tool in TOOL_POLICY.get(role, set())


# Human approval authority (VR-006 §5, AI-004 §6)
APPROVER_ROLES_BY_RISK = {
    "HIGH": {"executive_risk_owner"},
    "MEDIUM": {"executive_risk_owner", "vendor_risk_manager"},
    "LOW": {"executive_risk_owner", "vendor_risk_manager"},
}


def can_sign_off(role: str, overall_risk: str) -> bool:
    return role in APPROVER_ROLES_BY_RISK.get(overall_risk, set())
