"""Assessment service: runs / resumes the workflow with tracing, usage and latency capture."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from . import observability as obs
from .graph import build_graph
from .schemas import HumanDecision, VendorAssessmentRequest


def new_assessment_id() -> str:
    return f"ASM-{datetime.now(UTC):%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"


class AssessmentService:
    def __init__(self, checkpointer=None):
        self.graph = build_graph(checkpointer or InMemorySaver())
        self.runs: dict[str, dict] = {}

    def _config(self, aid: str, callbacks: list) -> dict:
        return {"configurable": {"thread_id": aid}, "callbacks": callbacks, "recursion_limit": 100}

    async def start(self, req: VendorAssessmentRequest, assessment_id: str | None = None) -> dict:
        aid = assessment_id or new_assessment_id()
        self.runs[aid] = {"started_at": datetime.now(UTC).isoformat(), "phases": []}
        init = {"assessment_id": aid, "request": req.model_dump(mode="json"), "status": "RECEIVED"}
        return await self._run(aid, init, "vendor-assessment", {"request": req.model_dump(mode="json")})

    async def resume(self, aid: str, human: HumanDecision | dict) -> dict:
        payload = human.model_dump(mode="json") if isinstance(human, HumanDecision) else human
        self.runs.setdefault(aid, {"phases": []})
        return await self._run(aid, Command(resume=payload), "vendor-assessment-human-review", payload)

    async def _run(self, aid: str, inp, name: str, trace_input) -> dict:
        usage = UsageMetadataCallbackHandler()
        t0 = time.perf_counter()
        error = None
        obs.event("run_started", run_id=aid, phase=name)
        with obs.trace_run(name, session_id=aid, input=trace_input, tags=["nfs-vendor-risk", name],
                           metadata={"assessment_id": aid}) as trace_id:
            try:
                await self.graph.ainvoke(inp, self._config(aid, [usage, *obs.callbacks()]))
            except Exception as e:  # keep the run inspectable; the error is surfaced in the snapshot
                error = f"{type(e).__name__}: {e}"
                obs.event("run_failed", run_id=aid, phase=name, error=error)
                obs.mark_trace_failed(error)
        seconds = round(time.perf_counter() - t0, 2)
        snap = await self.get(aid)
        tokens = {m: dict(u) for m, u in usage.usage_metadata.items()}
        phase = {"phase": name, "seconds": seconds, "trace_id": trace_id, "tokens": tokens, "error": error}
        self.runs[aid]["phases"].append(phase)
        q = (snap["values"] or {}).get("quality") or {}
        for k in ("citation_validity", "groundedness", "check_coverage"):
            if k in q:
                obs.score(trace_id, k, q[k])
        dec = (snap["values"] or {}).get("decision") or {}
        if dec:
            obs.score(trace_id, "recommendation", dec["recommendation"])
            obs.score(trace_id, "overall_risk", dec["overall_risk"])
        obs.event("run_finished", run_id=aid, phase=name, seconds=seconds, status=snap["status"],
                  total_tokens=sum(u.get("total_tokens", 0) for u in tokens.values()))
        obs.flush()
        snap["run"] = self.runs[aid]
        return snap

    async def get(self, aid: str) -> dict:
        snap = await self.graph.aget_state({"configurable": {"thread_id": aid}})
        interrupts = [i.value for t in snap.tasks for i in (t.interrupts or ())]
        values = snap.values or {}
        return {
            "assessment_id": aid,
            "status": values.get("status", "UNKNOWN"),
            "values": values,
            "pending_review": interrupts[0] if interrupts else None,
            "next": list(snap.next),
            "run": self.runs.get(aid, {}),
        }
