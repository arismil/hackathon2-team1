"""MCP tool gateway used by every agent.

- Connects to the NFS MCP server over streamable HTTP with the agent's role (least privilege).
- Keeps an in-process MCP server (same tools, in-memory MCP transport) as a warm standby:
  if the remote server is unreachable or a call fails at transport level, the call is
  retried there (FR14) and the run is marked degraded.
- Applies guardrails to every tool result before it reaches a model: prompt-injection
  quarantine of retrieved text, client-side authorisation, error normalisation.
- Records every retrieved chunk in an evidence ledger (used for citation verification)
  and every call in a tool-event log (used for tool-correctness evaluation).
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import AsyncExitStack

import httpx
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.shared.memory import create_connected_server_and_client_session

from .config import Settings, get_settings
from .guardrails.access import is_allowed
from .guardrails.injection import quarantine_text
from .observability import event
from .schemas import EvidenceChunk, ToolEvent

_LLM_FIELDS = ("chunk_id", "source", "policy_id", "section", "trust", "text")
_RETRYABLE = ("SERVICE_UNAVAILABLE",)


class EvidenceLedger:
    def __init__(self) -> None:
        self.chunks: dict[str, EvidenceChunk] = {}
        self.events: list[ToolEvent] = []

    def add(self, r: dict, agent: str, query: str | None) -> None:
        c = self.chunks.get(r["chunk_id"])
        if c is None:
            c = EvidenceChunk(
                chunk_id=r["chunk_id"], doc_id=r["doc_id"], source=r["source"], title=r["title"],
                section=r["section"], doc_type=r["doc_type"], trust=r["trust"], text=r["text"],
                injection_flags=list(r.get("injection_flags") or []),
            )
            self.chunks[c.chunk_id] = c
        if agent not in c.retrieved_by:
            c.retrieved_by.append(agent)
        if query and query not in c.queries:
            c.queries.append(query)


def _to_text(raw) -> str:
    if isinstance(raw, tuple):
        raw = raw[0]
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in raw)
    return str(raw)


class ToolGateway:
    def __init__(self, role: str, ledger: EvidenceLedger | None = None, settings: Settings | None = None,
                 run_id: str = "", agent: str | None = None):
        self.role = role
        self.agent = agent or role
        self.ledger = ledger or EvidenceLedger()
        self.settings = settings or get_settings()
        self.run_id = run_id
        self.transport = "none"
        self.degraded_reason: str | None = None
        self._stack = AsyncExitStack()
        self._remote: dict[str, BaseTool] = {}
        self._local: dict[str, BaseTool] = {}

    # ------------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> ToolGateway:
        await self._stack.__aenter__()
        await self._open_local()  # warm standby (cheap: in-process, lazy index access)
        if self.settings.mcp_mode == "remote":
            try:
                await self._open_remote()
                self.transport = "mcp-http"
            except Exception as e:
                self._degrade(f"remote MCP server unavailable ({type(e).__name__}: {str(e)[:160]})")
        else:
            self.transport = "mcp-inprocess"
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.__aexit__(*exc)

    async def _open_remote(self) -> None:
        url = self.settings.mcp_server_url
        # cheap reachability probe so a dead server fails fast instead of hanging a session
        async with httpx.AsyncClient(timeout=self.settings.mcp_connect_timeout_s) as h:
            await h.get(url.rsplit("/", 1)[0] + "/")
        client = MultiServerMCPClient({
            "nfs": {
                "transport": "streamable_http",
                "url": url,
                "headers": {"X-NFS-Role": self.role, "X-NFS-Run": self.run_id},
                "timeout": self.settings.mcp_connect_timeout_s,
            }
        }, handle_tool_errors=False)
        # connection-bound tools open a short-lived session per call (the server is stateless), so a server
        # dying mid-run fails only that call - it cannot cancel the agent's task via a long-lived task group
        tools = await client.get_tools()
        self._remote = {t.name: t for t in tools}

    async def _open_local(self) -> None:
        from .mcp_server import create_server

        server = create_server(self.settings, default_role=self.role, log_level="WARNING")
        session = await self._stack.enter_async_context(create_connected_server_and_client_session(server))
        tools = await load_mcp_tools(session, handle_tool_errors=False)
        self._local = {t.name: t for t in tools}

    def _degrade(self, reason: str) -> None:
        if self.degraded_reason is None:
            self.degraded_reason = reason
            event("mcp_fallback", run_id=self.run_id, agent=self.agent, reason=reason)
        self.transport = "mcp-inprocess-fallback"

    # ------------------------------------------------------------------ tools for agents

    def tools(self) -> list[BaseTool]:
        catalog = self._remote or self._local
        return [self._wrap(t) for name, t in catalog.items() if is_allowed(self.role, name)]

    def _wrap(self, tool: BaseTool) -> BaseTool:
        async def _acall(**kwargs):
            return await self.call(tool.name, kwargs)

        return StructuredTool(name=tool.name, description=tool.description, args_schema=tool.args_schema,
                              coroutine=_acall)

    async def call(self, name: str, args: dict) -> str:
        t0 = time.perf_counter()
        if not is_allowed(self.role, name):
            return self._finish(name, args, t0, ok=False, denied=True,
                                error=f"ACCESS_DENIED: role '{self.role}' may not call '{name}' (client policy)")
        use_remote = self.transport == "mcp-http" and name in self._remote
        tool = self._remote[name] if use_remote else self._local.get(name)
        if tool is None:
            return self._finish(name, args, t0, ok=False, error=f"TOOL_NOT_FOUND: {name}")
        try:
            raw = await asyncio.wait_for(tool.ainvoke(args), timeout=self.settings.mcp_tool_timeout_s)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if "ACCESS_DENIED" in msg:
                return self._finish(name, args, t0, ok=False, denied=True, error=msg)
            transport_error = not isinstance(e, ToolException) or any(k in msg for k in _RETRYABLE)
            if use_remote and transport_error and name in self._local:
                self._degrade(f"tool '{name}' failed on remote MCP ({msg[:160]})")
                try:
                    raw = await asyncio.wait_for(self._local[name].ainvoke(args), timeout=self.settings.mcp_tool_timeout_s)
                except Exception as e2:
                    return self._finish(name, args, t0, ok=False, error=f"{type(e2).__name__}: {e2}")
            else:
                return self._finish(name, args, t0, ok=False, error=msg)
        return self._finish(name, args, t0, ok=True, text=_to_text(raw))

    def _finish(self, name: str, args: dict, t0: float, ok: bool, text: str = "", error: str | None = None,
                denied: bool = False) -> str:
        chunk_ids: list[str] = []
        if ok:
            text, chunk_ids = self._guard_result(text, args)
        ev = ToolEvent(agent=self.agent, tool=name, args=args, ok=ok, denied=denied, error=error,
                       duration_ms=int((time.perf_counter() - t0) * 1000), transport=self.transport,
                       result_chunks=chunk_ids)
        self.ledger.events.append(ev)
        event("tool_call", run_id=self.run_id, **ev.model_dump(exclude={"args"}), query=args.get("query"))
        if ok:
            return text
        return json.dumps({
            "error": error,
            "instruction": "The tool call failed. Do not assume the missing information; if the evidence cannot be "
                           "obtained another way, record the affected check as UNKNOWN with evidence_basis MISSING.",
        })

    def _guard_result(self, text: str, args: dict) -> tuple[str, list[str]]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text, []
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return text, []
        safe, ids = [], []
        for r in results:
            if not isinstance(r, dict) or "chunk_id" not in r:
                continue
            self.ledger.add(r, self.agent, args.get("query"))
            ids.append(r["chunk_id"])
            clean, inj = quarantine_text(r["text"], r["chunk_id"])
            item = {k: r.get(k) for k in _LLM_FIELDS}
            if inj.detected or r.get("injection_flags"):
                item["text"] = clean
                item["guardrail"] = "QUARANTINED_PROMPT_INJECTION"
                self.ledger.chunks[r["chunk_id"]].injection_flags = sorted(
                    set(self.ledger.chunks[r["chunk_id"]].injection_flags) | set(inj.patterns))
                event("injection_quarantined", run_id=self.run_id, agent=self.agent, chunk_id=r["chunk_id"],
                      patterns=inj.patterns)
            safe.append(item)
        data["results"] = safe
        return json.dumps(data, ensure_ascii=False), ids
