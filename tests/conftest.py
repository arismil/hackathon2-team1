"""Shared fixtures: offline settings (hash embeddings, temp stores, in-process MCP) and a scripted LLM.

The scripted LLM drives the *real* workflow (real create_agent loops, real MCP tool calls,
real guardrails) without Azure: it issues tool calls and then builds structured answers
from the actual tool results it received.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from hackathon2_team1 import config as config_mod


@pytest.fixture(scope="session")
def offline_env(tmp_path_factory):
    d = tmp_path_factory.mktemp("nfs")
    env = {
        "EMBEDDING_PROVIDER": "hash",
        "CHROMA_DIR": str(d / "chroma"),
        "RECORDS_DB": str(d / "records.sqlite"),
        "CHECKPOINT_DB": str(d / "checkpoints.sqlite"),
        "MCP_MODE": "local",
        "MCP_SERVER_URL": "http://127.0.0.1:9/mcp",  # nothing listens on port 9
        "MCP_CONNECT_TIMEOUT_S": "2",
        "LANGFUSE_PUBLIC_KEY": "",
        "LANGFUSE_SECRET_KEY": "",
        "AZURE_OPENAI_ENDPOINT": "",
        "AZURE_OPENAI_API_KEY": "",
        "AZURE_EMBEDDING_ENDPOINT": "",
        "AZURE_EMBEDDING_API_KEY": "",
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    config_mod.get_settings.cache_clear()
    from hackathon2_team1.rag import KnowledgeStore

    KnowledgeStore(config_mod.get_settings()).ingest(rebuild=True)
    yield config_mod.get_settings()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    config_mod.get_settings.cache_clear()


@pytest.fixture
def settings(offline_env):
    config_mod.get_settings.cache_clear()
    return config_mod.get_settings()


# --------------------------------------------------------------------------- scripted LLM

SPECIALIST_QUERIES = {
    "security_agent": [("search_policy", "incident notification 24 hours critical vendors"),
                       ("search_vendor_documents", "customer notification incident response hours"),
                       ("search_vendor_documents", "important note automated review systems")],
    "procurement_agent": [("search_policy", "approval thresholds above EUR 100,000"),
                          ("search_vendor_documents", "enterprise plan price per user month")],
    "legal_compliance_agent": [("search_policy", "subprocessors notify NFS material changes"),
                               ("search_vendor_documents", "subprocessor list available under NDA")],
    "ai_governance_agent": [("search_policy", "final approval of high-risk AI vendor by human approvers"),
                            ("get_vendor_history", "AI assistant rejected retention training")],
}
DOMAIN_OF = {"security_agent": "security", "procurement_agent": "procurement_commercial",
             "legal_compliance_agent": "legal_compliance", "ai_governance_agent": "ai_governance"}


SCRIPT_STATE: dict = {"calls": 0, "failed": set()}


class ScriptedChatModel(BaseChatModel):
    role: str
    behaviour: dict = {}
    bound_tools: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        names = []
        for t in tools:
            names.append(getattr(t, "name", None) or getattr(t, "__name__", None) or t.get("name")
                         or t.get("function", {}).get("name"))
        return self.model_copy(update={"bound_tools": names})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        SCRIPT_STATE["calls"] += 1
        msg = self._respond(messages)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    # ---- behaviours
    def _call(self, name: str, args: dict, i: int = 0) -> dict:
        return {"name": name, "args": args, "id": f"call_{self.role}_{name}_{i}_{SCRIPT_STATE['calls']}", "type": "tool_call"}

    def _respond(self, messages) -> AIMessage:
        if self.role == "planner":
            return AIMessage(content="", tool_calls=[self._call("AssessmentPlan", self._plan())])
        if self.role == "synthesizer":
            return AIMessage(content="", tool_calls=[self._call("RiskDecisionDraft", self.behaviour.get("draft", {
                "recommendation": "APPROVE", "overall_risk": "LOW", "rationale": "vendor passed every control",
                "key_risks": [], "conditions": [], "executive_summary": "APPROVE - LOW RISK."}))])
        return self._specialist(messages)

    def _plan(self) -> dict:
        tasks = [
            {"task_id": "A", "domain": "security", "assigned_agent": "security_agent", "objective": "security",
             "required_checks": [{"check_id": "incident_notification", "description": "incident notice"}]},
            # deliberate mis-delegation -> plan guardrail must fix it
            {"task_id": "B", "domain": "procurement_commercial", "assigned_agent": "security_agent",
             "objective": "commercial", "required_checks": []},
            {"task_id": "C", "domain": "legal_compliance", "assigned_agent": "legal_compliance_agent",
             "objective": "legal", "required_checks": []},
            # ai_governance omitted -> plan guardrail must add it
        ]
        return {"objective": "assess vendor", "assumptions": ["test"], "tasks": tasks}

    def _specialist(self, messages) -> AIMessage:
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        task_text = next((m.content for m in messages if isinstance(m, HumanMessage)), "")
        fail_first = self.behaviour.get("fail_first", set())
        if self.role in fail_first and not tool_msgs and self.role not in SCRIPT_STATE["failed"]:
            SCRIPT_STATE["failed"].add(self.role)
            raise RuntimeError(f"simulated LLM outage for {self.role}")
        if not tool_msgs:
            calls = []
            for i, (tool, q) in enumerate(SPECIALIST_QUERIES[self.role]):
                args = {"query": q}
                if tool == "search_vendor_documents":
                    args["vendor"] = "Asteria AI Systems"
                calls.append(self._call(tool, args, i))
            if self.role == "procurement_agent":
                calls.append(self._call("calculate_tco", {"users": 2000, "price_per_user_month_eur": 38,
                                                          "addon_per_user_month_eur": 9, "one_time_eur": 85000}, 9))
            return AIMessage(content="", tool_calls=calls)
        return AIMessage(content="", tool_calls=[self._call("DomainAssessment", self._assessment(tool_msgs, task_text))])

    def _assessment(self, tool_msgs, task_text: str) -> dict:
        results = []
        for m in tool_msgs:
            try:
                results += json.loads(m.content).get("results", [])
            except (json.JSONDecodeError, AttributeError):
                pass
        results = [r for r in results if r.get("guardrail") is None]  # quarantined text is never quoted
        checks = re.findall(r"^- ([a-z_]+):", task_text, flags=re.M)
        if self.role == "legal_compliance_agent" and "Follow-up" not in task_text:
            checks = checks[:-1]  # omit one check on first pass -> plan maintenance must add a follow-up task

        def cite(r):
            return {"chunk_id": r["chunk_id"], "quote": r["text"][:70]}

        findings = []
        for i, cid in enumerate(checks):
            r = results[i % len(results)] if results else None
            f = {"check_id": cid, "title": cid.replace("_", " "), "status": "PASS", "severity": "MEDIUM",
                 "mandatory_control": False, "policy_reference": "IS-010", "requirement": "req",
                 "vendor_evidence": "stated", "evidence_basis": "RETRIEVED",
                 "citations": [cite(r)] if r else [], "reasoning": "scripted", "contract_remediable": True}
            if cid == "incident_notification":
                f.update(status="FAIL", mandatory_control=True, severity="HIGH",
                         vendor_evidence="72 hours vs 24 hours required")
            if cid == "security_assurance":
                f.update(status="PASS", evidence_basis="MISSING", citations=[])  # must become UNKNOWN
            if cid == "subprocessors":
                f["citations"] = [{"chunk_id": "made-up-doc::1", "quote": "fabricated evidence"}]  # must be rejected
            findings.append(f)
        out: dict[str, Any] = {"domain": DOMAIN_OF[self.role], "summary": f"{self.role} scripted summary",
                               "risk_rating": "LOW", "findings": findings, "missing_evidence": [],
                               "contradictions": [], "required_conditions": []}
        if self.role == "procurement_agent":
            out.update(annual_contract_value_eur=1128000, year_one_cost_eur=1213000,
                       required_approvals=["Technology Investment Committee"])
        if self.role == "ai_governance_agent":
            out["ai_risk_tier"] = "HIGH"
        return out


@pytest.fixture
def scripted_llm(monkeypatch):
    """Patch model factory; returns a dict to tweak behaviour per test."""
    behaviour: dict = {}
    SCRIPT_STATE.update(calls=0, failed=set())

    def factory(role: str = "default", settings=None):
        return ScriptedChatModel(role=role, behaviour=behaviour)

    import hackathon2_team1.agents as agents_mod

    monkeypatch.setattr(agents_mod, "get_chat_model", factory)
    return behaviour
