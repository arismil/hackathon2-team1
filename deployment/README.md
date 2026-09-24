# Deployment

## Local (Docker Compose) — primary PoC target

```bash
cp .env.example .env            # fill in the AZURE_OPENAI_* values
docker compose up -d --build    # mcp-server, api, Langfuse v3 (web, worker, postgres, clickhouse, redis, minio)
```

| Service | URL |
|---|---|
| UI + API | http://localhost:8000 (Swagger: `/docs`) |
| MCP server (streamable HTTP) | http://localhost:8001/mcp |
| Langfuse | http://localhost:3000 — `admin@nfs.local` / `nfs-admin-123` (dev only); set `LANGFUSE_WEB_PORT` if port 3000 is taken |

The MCP server builds the ChromaDB index on first start. The index lives in the `nfs-data` volume, shared
with the API container, which uses it for the in-process MCP fallback. Langfuse is provisioned headlessly
(organisation, project and API keys from `.env`), so traces show up without any manual setup.

**Failure demo (FR14):** add `MCP_FAIL_TOOLS=search_vendor_documents` to the `mcp-server` environment, or run
`docker compose stop mcp-server` during an assessment. Agents fail over to the in-process MCP server and the
report lists the run as *degraded*.

**Operational logs:** the app writes one JSON line per operational event to stdout, e.g.
`docker compose logs -f api | grep '"event"'`. Useful events: `mcp_fallback` (degraded runs),
`injection_quarantined`, `human_review_rejected`, `run_finished` (latency and token totals).

### PoC limitations (deliberate)

- Agent identity uses an `X-NFS-Role` header. Production would use real tokens under the MCP authorization spec, plus a secrets store.
