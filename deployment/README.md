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

## Azure Container Apps

```bash
az login
set -a; source .env; set +a
./deployment/deploy-azure-container-apps.sh
```

The script:

1. Creates a resource group, an Azure Container Registry, and builds the image there (`az acr build`).
2. Creates a Container Apps environment, which automatically creates a Log Analytics workspace.
3. Deploys `nfs-mcp-server` with **internal** ingress (not reachable from the internet).
4. Deploys `nfs-vendor-risk-api` with **external** HTTPS ingress, `MCP_SERVER_URL=http://nfs-mcp-server/mcp`.
5. Stores the Azure OpenAI key (and optionally the Langfuse secret key) as Container Apps secrets.

Optional Langfuse in Azure: set `LANGFUSE_AZURE_HOST` to a Langfuse instance reachable from Azure before running the script.

### Azure observability (Log Analytics)

The app writes one JSON line per operational event to stdout. Container Apps sends these to Log Analytics:

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s in ("nfs-vendor-risk-api", "nfs-mcp-server")
| where Log_s has "\"event\""
| extend e = parse_json(extract(@"(\{.*\})", 1, Log_s))
| project TimeGenerated, app=ContainerAppName_s, event=tostring(e.event), run_id=tostring(e.run_id),
          agent=tostring(e.agent), tool=tostring(e.tool), ok=tobool(e.ok), transport=tostring(e.transport),
          seconds=todouble(e.seconds), detail=e
| order by TimeGenerated desc
```

Useful filters: `event == "mcp_fallback"` (degraded runs), `event == "injection_quarantined"`,
`event == "human_review_rejected"`, `event == "run_finished"` (latency and token totals).

### PoC limitations (deliberate)

- A single replica per app. Chroma, SQLite checkpoints and the system of record sit on container storage, so the index is rebuilt on restart (about 70 embedding calls). For persistence, mount an Azure Files share at `/data`.
- Agent identity uses an `X-NFS-Role` header on the internal network. Production would use Entra ID tokens under the MCP authorization spec, plus Key Vault for secrets.
