"""FastAPI application: submit assessments, poll progress, record the human decision."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .config import get_settings
from .graph import state_summary
from .guardrails.access import can_sign_off
from .observability import setup_logging
from .schemas import HumanDecision, VendorAssessmentRequest
from .service import AssessmentService, new_assessment_id

STATIC = Path(__file__).parent / "static"
_tasks: dict[str, asyncio.Task] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    s = get_settings()
    s.checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(s.checkpoint_db)) as saver:
        app.state.svc = AssessmentService(saver)
        yield


app = FastAPI(title="NFS Vendor Risk & Procurement Deep Agent", version="0.1.0", lifespan=lifespan)


def _svc() -> AssessmentService:
    return app.state.svc


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health():
    s = get_settings()
    return {"status": "ok", "azure_openai": s.azure_configured, "mcp_server": s.mcp_server_url,
            "mcp_mode": s.mcp_mode, "langfuse": s.langfuse_enabled}


@app.post("/api/assessments", status_code=202)
async def create_assessment(req: VendorAssessmentRequest):
    aid = new_assessment_id()
    _tasks[aid] = asyncio.create_task(_svc().start(req, aid))
    return {"assessment_id": aid, "status": "RUNNING"}


@app.get("/api/assessments/{aid}")
async def get_assessment(aid: str):
    snap = await _svc().get(aid)
    if not snap["values"]:
        if aid in _tasks and not _tasks[aid].done():
            return {"assessment_id": aid, "status": "RUNNING"}
        raise HTTPException(404, "unknown assessment")
    task = _tasks.get(aid)
    running = bool(task and not task.done())
    return {**state_summary(snap["values"]), "running": running, "pending_review": snap["pending_review"],
            "run": snap["run"]}


@app.get("/api/assessments/{aid}/report", response_class=PlainTextResponse)
async def get_report(aid: str):
    snap = await _svc().get(aid)
    if not snap["values"].get("report_markdown"):
        raise HTTPException(404, "report not ready")
    return snap["values"]["report_markdown"]


@app.get("/api/assessments/{aid}/evidence")
async def get_evidence(aid: str):
    snap = await _svc().get(aid)
    v = snap["values"]
    return {"evidence": v.get("evidence", {}), "tool_events": v.get("tool_events", []), "domains": v.get("domains", [])}


@app.post("/api/assessments/{aid}/review")
async def review(aid: str, decision: HumanDecision):
    snap = await _svc().get(aid)
    pr = snap["pending_review"]
    if not pr:
        raise HTTPException(409, "assessment is not awaiting human review")
    # authorisation is enforced again inside the workflow (defence in depth)
    if not can_sign_off(decision.role, pr["overall_risk"]):
        raise HTTPException(403, f"role '{decision.role}' cannot sign off a {pr['overall_risk']}-risk vendor; "
                                 f"requires {pr['required_role']}")
    if decision.decision.value not in pr["allowed_decisions"]:
        raise HTTPException(422, f"decision not permitted by policy rules; allowed: {pr['allowed_decisions']}")
    snap = await _svc().resume(aid, decision)
    return {**state_summary(snap["values"]), "pending_review": snap["pending_review"]}
