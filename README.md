# NFS AI-Powered Vendor Risk & Procurement Deep Agent

Hackathon PoC for Northstar Financial Services (NFS). The agent assesses **Asteria AI Systems** (or any vendor in
the corpus) as an enterprise GenAI platform. It works the way a controlled review team would:

**Business request → deep-agent plan → 4 specialist agents (RAG + MCP tools) → evidence guardrails →
policy-as-code risk decision → online evaluation → human sign-off → executive report**, traced end to end in Langfuse.

> A trusted enterprise decision system, not a vendor-assessment chatbot. The model *proposes*. Deterministic
> guardrails based on NFS policy *decide what is allowed*. An authorised human *decides*.

Stack: Python 3.14 · LangGraph + LangChain `create_agent` · Azure OpenAI (chat + embeddings) ·
ChromaDB (persistent) · official MCP Python SDK (FastMCP, streamable HTTP) · FastAPI · Langfuse v3 (self-hosted) · Docker.

See [architecture/architecture.md](architecture/architecture.md) for diagrams and design decisions.

## Quick start

### Option A — Docker Compose (everything, incl. Langfuse)

```bash
cp .env.example .env          # fill in AZURE_OPENAI_ENDPOINT / _API_KEY / deployment names
docker compose up -d --build
open http://localhost:8000    # UI: submit the pre-filled Asteria request, watch the plan, sign off
open http://localhost:3000    # Langfuse (admin@nfs.local / nfs-admin-123)
Copy the created api keys in the .env and then restart the app
with docker compose down then docker compose up -d
```

### Option B — local Python (uv)

```bash
uv sync
cp .env.example .env                              # Azure OpenAI settings
uv run nfs-agent ingest                           # build the ChromaDB index (Azure embeddings)
uv run nfs-agent mcp &                            # MCP server on :8001 (optional: without it, agents fail over to in-process MCP)
uv run nfs-agent assess                           # end-to-end Asteria assessment in the terminal (interactive sign-off)
uv run nfs-agent api                              # or: UI/API on :8000
```

Non-interactive sign-off: `uv run nfs-agent assess --decision "CONDITIONAL APPROVAL" --role executive_risk_owner --approver "Jane Doe"`.
Reports are written to `data/reports/` (Markdown, PDF and the full state as JSON).

## Run the full Asteria assessment

This runs the Asteria assessment from the handout (2,000 users, Confidential data) through the full deep-agent
workflow, then scores it. It also runs the retrieval and guardrail suites and the LLM-as-judge scoring, and writes
everything to `evaluation-results/`. Run it locally with uv so the results end up in the repo. Docker does not mount
`evaluation-results/`.

```bash
uv sync
cp .env.example .env                                 # fill in the AZURE_OPENAI_* settings (required)
uv run nfs-agent ingest                              # build the ChromaDB index (first time only)
uv run nfs-agent mcp &                               # optional: remote MCP server on :8001
uv run python -m evaluation.run_eval --judge         # full Asteria assessment + evaluation (a few minutes)
```

Output:

```
evaluation-results/
├── latest.md                  # metrics table of the most recent run
└── <YYYYMMDDTHHMMSSZ>/
    ├── summary.md, results.json
    ├── run.log                # full log of the run: every node, agent, MCP tool call and guardrail event
    ├── E2E-01-report.md/.pdf  # the Asteria executive assessment
    └── E2E-01-state.json      # plan, delegation, decision, findings, tool events, timings
```

The run stops at human review, so the report shows the policy-validated recommendation marked **PENDING
HUMAN REVIEW**. To record a sign-off as well, use `uv run nfs-agent assess` (see above).

## Demo script (final presentation)

| Step | What to show | Where |
|---|---|---|
| 1. Business request | Structured request (FR01); try an injected request → `BLOCKED_BY_INPUT_GUARDRAIL` | UI / `POST /api/assessments` |
| 2. Deep-agent plan | 4 tasks delegated to specialists; plan revisions (follow-ups / retries) | UI progress table, report §8 |
| 3. RAG evidence | Findings with verified citations `chunk_id` + verbatim quote; RETRIEVED / INFERRED / MISSING | Report §6, §10 |
| 4. MCP tools | Tool calls per agent, role headers, `calculate_tco`; `docker compose stop mcp-server` → degraded run still completes | Langfuse TOOL spans, report "Operational degradation" |
| 5. Guardrails | Proposal §7 injection quarantined; unverified claims downgraded; policy rules R01–R10 and overrides | Report §2, §8 |
| 6. Risk synthesis | Recommendation + overall/domain risk + conditions + approval chain (TIC, InfoSec, AI Governance, exec risk owner) | Report §1, §4, §5 |
| 7. Human review | Wrong role → 403; APPROVE on a HIGH-risk vendor → refused; exec risk owner records the decision via a human-only MCP tool | UI review panel |
| 8. Evaluation | `uv run python -m evaluation.run_eval --judge` → metrics table | `evaluation-results/latest.md` |
| 9. Observability | Trace tree: agent → nodes → LLM generations (tokens) → MCP tools → guardrails → evaluator; scores | Langfuse |
| 10. Deployment | `docker compose` locally; Azure Container Apps script + Log Analytics KQL | `deployment/` |

## Mandatory functional requirements

| ID | Requirement | Implementation |
|---|---|---|
| FR01 | Structured request | `VendorAssessmentRequest` (Pydantic) → API / CLI |
| FR02 | Multi-step plan, maintained | `plan` node (LLM + `enforce_plan`), `review_plan` marks tasks done or failed, retries, adds follow-up tasks for uncovered checks |
| FR03 | RAG over NFS corpus | ChromaDB, section-level chunks, Azure embeddings, served via MCP `search_policy` / `search_vendor_documents` / `get_vendor_history` |
| FR04 | Cite evidence | Every finding carries `citations[chunk_id, quote]`; verified against the evidence ledger |
| FR05 | Evidence vs inference vs missing | `evidence_basis` = RETRIEVED / INFERRED / MISSING; unverified claims flagged |
| FR06 | MCP server | `mcp_server.py` (official SDK): 9 tools + 2 resources, streamable HTTP |
| FR07 | ≥3 risk domains | Security, Procurement/Commercial, Legal/Compliance, AI Governance |
| FR08 | Guardrails | Input, plan, tool RBAC (client + server), retrieved-content quarantine, evidence, decision rules, output, human authority |
| FR09 | Prompt-injection resistance | Pattern detection + quarantine before model; untrusted-data prompting; rules no model output can bypass |
| FR10 | Non-compliance, contradictions, UNKNOWN | FAIL/PARTIAL/UNKNOWN statuses, contradictions with citations, inter-agent disagreement detection, missing-evidence register |
| FR11 | Structured risk assessment + recommendation | `RiskDecision` (APPROVE / CONDITIONAL APPROVAL / REJECT, LOW/MEDIUM/HIGH, domain risks, conditions, approvals) |
| FR12 | Human review | LangGraph `interrupt`; role vs risk level; only rule-permitted decisions; recorded via human-only MCP tool |
| FR13 | Automated evaluation | `evaluation/run_eval.py` + `evaluation/cases.yaml` (retrieval, guardrail, E2E suites) |
| FR14 | Failure handling | MCP remote → in-process failover (start-up and mid-run), structured tool errors, agent failure → fail-safe UNKNOWN + retry, LLM outage → safe human-gated REJECT |

## Testing

```bash
uv run pytest            # 19 offline tests (~5 s): no Azure needed (hash embeddings + scripted LLM + in-process MCP)
uv run pytest -m e2e -s  # end-to-end Asteria assessment against Azure OpenAI
```

| Requirement | Tests |
|---|---|
| ≥3 unit tests | `test_unit_guardrails.py` (injection, input guardrail, quote verification, finding normalisation, decision rules, thresholds, RBAC), `test_rag_and_mcp.py::test_section_chunking_and_metadata` |
| ≥2 workflow/integration tests | `test_workflow.py` (full graph with real MCP tools and guardrails, agent-failure retry, input block), `test_rag_and_mcp.py` (MCP protocol, RBAC, system of record) |
| ≥1 prompt-injection/guardrail test | `test_unit_guardrails.py::test_injection_detected_and_quarantined`, `test_mcp_fallback.py::test_quarantine_applied_to_tool_results`, `test_workflow.py` (APPROVE/LOW draft overridden) |
| ≥1 MCP failure/fallback test | `test_mcp_fallback.py`: server unreachable at start, tool outage, **real HTTP server killed mid-run** |
| 1 end-to-end assessment | `test_e2e.py` (Azure) |

The workflow tests use a *scripted* chat model. It makes real tool calls through MCP and builds its answers
from the actual tool results, so the full LangGraph / `create_agent` / MCP / guardrail stack runs offline.

## Evaluation

```bash
uv run python -m evaluation.run_eval                 # retrieval + guardrail suites, + E2E when Azure is configured
uv run python -m evaluation.run_eval --judge         # + LLM-as-judge citation support
```

| Metric (handout §10) | How it is measured |
|---|---|
| Retrieval relevance | 11 labelled queries → hit@5, MRR; in-run recall of key evidence chunks |
| Groundedness | Share of material findings with ≥1 verified citation |
| Citation correctness | Quotes found (near-)verbatim in the retrieved chunk, with exact number matching; optional LLM judge for semantic support |
| Task completion | Required-check coverage × domains completed |
| Tool correctness | Right tools per agent (e.g. `calculate_tco` by Procurement), no restricted-tool attempts, error rate |
| Agent delegation | Planner delegation accuracy (before guardrail correction) + execution by the owning specialist |
| Guardrail compliance | Paused for human, no auto-approval, rule-permitted recommendation, live unauthorised sign-off refused |
| Injection resistance | Not APPROVE / not LOW, retention still reported, injected text never quoted, detection logged |
| Decision quality | Expected recommendation/risk sets, required approvals, known gaps (72h notification, 14/45-day vulnerability SLAs, 30-day retention) and UNKNOWNs (SOC 2 reports, subprocessor list) found |
| Latency / cost | Wall-clock seconds, node timings, token usage per model |

Results are written to `evaluation-results/` and pushed to Langfuse as scores.

## Project structure

```
README.md
architecture/architecture.md        diagrams, guardrail layers, design decisions
src/hackathon2_team1/
  schemas.py        typed contracts            graph.py        LangGraph workflow (deep agent)
  agents.py         planner/specialists/synth  prompts.py      prompts + baseline control library
  mcp_server.py     NFS MCP server (FastMCP)   mcp_client.py   ToolGateway: RBAC, failover, quarantine, ledger
  rag.py            chunking + ChromaDB        records.py      SQLite system of record
  guardrails/       input, injection, access, evidence, decision_rules
  report.py         executive report           service.py      run/resume, usage, tracing
  api.py + static/  FastAPI + UI               cli.py          nfs-agent CLI
  observability.py  Langfuse + JSON events     knowledge/      supplied NFS corpus (PDFs)
tests/                                          offline unit/integration/fallback tests + Azure e2e
evaluation/         run_eval.py, cases.yaml, requests/
evaluation-results/ evaluation outputs (Asteria)
deployment/         Azure Container Apps script + Log Analytics guide
Dockerfile, docker-compose.yml, .env.example, pyproject.toml, uv.lock
```

## Configuration

All settings come from environment variables or `.env` (see `.env.example`). The main ones:

- `AZURE_OPENAI_*`: endpoint, key and deployment names. Leave `LLM_TEMPERATURE` empty for reasoning models.
- `EMBEDDING_PROVIDER`: `azure`, or `hash` for offline use.
- `MCP_MODE`: `remote` (HTTP with failover) or `local`.
- `MCP_FAIL_TOOLS`: simulates a tool outage.
- `MAX_PLAN_ITERATIONS`: how many rounds of retries and follow-up tasks the planner may add.
- `SPECIALIST_MODEL_CALL_LIMIT`: the per-agent LLM call budget.
- `LANGFUSE_*`: Langfuse connection.

## Known limitations (PoC scope)

- Agent identity is an `X-NFS-Role` header. Production would use OAuth/Entra tokens under the MCP authorization spec.
- Injection detection is pattern-based, backed by quarantine, prompting and deterministic rules. A classifier model could be added.
- Single-replica state: Chroma, SQLite checkpoints and the record store live on a local volume.
- Approval thresholds and the other rules are encoded from the supplied policies. A policy change means a code or config change.
