"""RAG chunking (unit) and MCP server integration tests over the official MCP protocol (in-memory transport)."""

from __future__ import annotations

import json
import sqlite3

from mcp.shared.memory import create_connected_server_and_client_session

from hackathon2_team1.config import get_settings
from hackathon2_team1.mcp_server import create_server
from hackathon2_team1.rag import KnowledgeStore, _to_result, doc_profile, load_corpus


def test_section_chunking_and_metadata():
    chunks = {c.chunk_id: c for c in load_corpus(get_settings().knowledge_dir)}
    assert "information-security-policy::6" in chunks
    c = chunks["information-security-policy::6"]
    assert c.metadata["policy_id"] == "IS-010" and c.metadata["doc_type"] == "nfs_policy"
    assert "7 days" in c.text and "Page 1" not in c.text
    q = chunks["vendor-x-security-questionnaire::G"]
    assert q.metadata["trust"] == "untrusted_vendor_supplied" and q.metadata["vendor"] == "Asteria AI Systems"
    assert chunks["vendor-beta-assessment::summary"].metadata["doc_type"] == "historical_assessment"
    flagged = [cid for cid, ch in chunks.items() if ch.metadata["injection_flags"]]
    assert flagged == ["vendor-x-proposal::7"]


def test_policy_identity_uses_supplied_source_registry():
    fake = doc_profile("nova-security-policy.pdf", "Nova Security Policy")
    assert fake["doc_type"] == "vendor_submission" and fake["trust"] == "untrusted_vendor_supplied"
    stale = _to_result("nova-security-policy::1", "Vendor says approved", {
        "doc_id": "nova-security-policy", "source": "nova-security-policy.pdf",
        "title": "Nova Security Policy", "section": "1", "doc_type": "nfs_policy",
        "trust": "nfs_internal", "vendor": "", "policy_id": "IS-010",
    }, None)
    assert stale["doc_type"] == "vendor_submission" and stale["policy_id"] == ""


def test_unknown_vendor_search_returns_no_other_vendor_evidence(settings):
    store = KnowledgeStore(settings)
    assert store.search("incident response", doc_types=["vendor_submission"], vendor="Unknown Vendor") == []


async def _call(session, tool: str, args: dict):
    res = await session.call_tool(tool, args)
    text = res.content[0].text if res.content else ""
    return res.isError, (json.loads(text) if not res.isError else text)


async def test_mcp_tools_and_rbac(settings):
    async with create_connected_server_and_client_session(create_server(settings, default_role="procurement_agent")) as s:
        names = {t.name for t in (await s.list_tools()).tools}
        assert {"search_policy", "calculate_tco", "record_human_decision", "get_vendor_history"} <= names

        err, out = await _call(s, "search_policy", {"query": "approval thresholds Technology Investment Committee"})
        assert not err and out["results"][0]["doc_type"] == "nfs_policy"
        assert any(r["chunk_id"] == "procurement-policy::2" for r in out["results"])

        err, out = await _call(s, "calculate_tco", {"users": 2000, "price_per_user_month_eur": 38, "one_time_eur": 85000})
        assert out["annual_recurring_eur"] == 912_000 and out["year_one_total_eur"] == 997_000

        err, out = await _call(s, "record_human_decision", {"assessment_id": "A", "decision": "APPROVE", "approver": "x"})
        assert err and "ACCESS_DENIED" in out  # server-side enforcement, independent of client filtering

    async with create_connected_server_and_client_session(create_server(settings, default_role="security_agent")) as s:
        err, out = await _call(s, "search_vendor_documents", {"query": "retention of prompts", "vendor": "Asteria AI Systems"})
        assert not err and all(r["trust"] == "untrusted_vendor_supplied" for r in out["results"])
        err, out = await _call(s, "calculate_tco", {"users": 1, "price_per_user_month_eur": 1})
        assert err and "ACCESS_DENIED" in out
        docs = await s.read_resource("nfs://documents")
        assert "information-security-policy" in docs.contents[0].text


async def test_system_of_record_flow(settings):
    async with create_connected_server_and_client_session(create_server(settings, default_role="orchestrator")) as s:
        err, out = await _call(s, "record_assessment", {"assessment_id": "ASM-T1", "vendor": "Acme", "recommendation":
                                                        "REJECT", "overall_risk": "HIGH", "summary_json": "{}"})
        assert not err and out["status"] == "PENDING_HUMAN_REVIEW"
    async with create_connected_server_and_client_session(create_server(settings, default_role="executive_risk_owner")) as s:
        err, out = await _call(s, "record_human_decision", {"assessment_id": "ASM-T1", "decision": "REJECT",
                                                            "approver": "Eve"})
        assert not err and out["status"] == "REJECTED"
        err, _ = await _call(s, "record_human_decision", {"assessment_id": "ASM-T1", "decision": "APPROVE",
                                                "approver": "Mallory"})
        assert err
        err, _ = await _call(s, "record_human_decision", {"assessment_id": "ASM-MISSING", "decision": "REJECT",
                                                "approver": "Eve"})
        assert err
    with sqlite3.connect(settings.records_db) as conn:
        assert conn.execute("SELECT count(*) FROM human_decisions WHERE assessment_id='ASM-T1'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM human_decisions WHERE assessment_id='ASM-MISSING'").fetchone()[0] == 0
