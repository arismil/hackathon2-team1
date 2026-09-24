"""MCP failure / fallback tests (FR14)."""

from __future__ import annotations

import json

from hackathon2_team1.mcp_client import ToolGateway


async def test_remote_down_falls_back_to_inprocess_mcp(settings, monkeypatch):
    monkeypatch.setattr(settings, "mcp_mode", "remote")  # remote URL points at a closed port
    async with ToolGateway("security_agent", settings=settings, run_id="t") as gw:
        assert gw.transport == "mcp-inprocess-fallback" and "unavailable" in gw.degraded_reason
        out = json.loads(await gw.call("search_policy", {"query": "vulnerability remediation 7 days"}))
        assert out["results"] and gw.ledger.events[-1].ok
        assert gw.ledger.events[-1].transport == "mcp-inprocess-fallback"


async def test_tool_outage_returns_structured_error_not_exception(settings, monkeypatch):
    monkeypatch.setenv("MCP_FAIL_TOOLS", "search_vendor_documents")
    async with ToolGateway("security_agent", settings=settings, run_id="t") as gw:
        out = json.loads(await gw.call("search_vendor_documents", {"query": "retention"}))
        assert "SERVICE_UNAVAILABLE" in out["error"] and "UNKNOWN" in out["instruction"]
        ev = gw.ledger.events[-1]
        assert not ev.ok and not ev.denied
        # other tools keep working
        assert json.loads(await gw.call("search_policy", {"query": "retention"}))["results"]


async def test_quarantine_applied_to_tool_results(settings):
    async with ToolGateway("ai_governance_agent", settings=settings, run_id="t") as gw:
        out = json.loads(await gw.call("retrieve_document", {"doc_id": "vendor-x-proposal"}))
        sec7 = next(r for r in out["results"] if r["chunk_id"] == "vendor-x-proposal::7")
        assert sec7["guardrail"] == "QUARANTINED_PROMPT_INJECTION"
        assert "IGNORE ALL PREVIOUS" not in sec7["text"]
        # the ledger keeps the original for human review / audit
        assert "IGNORE ALL PREVIOUS" in gw.ledger.chunks["vendor-x-proposal::7"].text


async def test_remote_http_server_dies_mid_run(settings, monkeypatch):
    """Real streamable-HTTP MCP server, stopped between two calls -> transparent in-process failover."""
    import asyncio
    import socket
    import threading

    import uvicorn

    from hackathon2_team1.mcp_server import create_server

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_server(settings).streamable_http_app(), host="127.0.0.1",
                                           port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        await asyncio.sleep(0.05)
    monkeypatch.setattr(settings, "mcp_mode", "remote")
    monkeypatch.setattr(settings, "mcp_server_url", f"http://127.0.0.1:{port}/mcp")

    async with ToolGateway("procurement_agent", settings=settings, run_id="t") as gw:
        assert gw.transport == "mcp-http"
        tco = json.loads(await gw.call("calculate_tco", {"users": 2000, "price_per_user_month_eur": 38}))
        assert tco["annual_recurring_eur"] == 912_000 and gw.ledger.events[-1].transport == "mcp-http"
        server.should_exit = True
        thread.join(timeout=10)
        out = json.loads(await gw.call("search_policy", {"query": "competitive sourcing"}))
        assert out["results"] and gw.ledger.events[-1].transport == "mcp-inprocess-fallback"
        assert gw.degraded_reason
