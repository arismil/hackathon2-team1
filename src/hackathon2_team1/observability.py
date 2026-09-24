"""Langfuse tracing (self-hosted, OpenTelemetry-based SDK) + structured JSON event logs.

Everything degrades to no-ops when Langfuse is not configured, so the agent never fails
because observability is down. JSON event logs go to stdout, which Azure Container Apps
ships to Log Analytics.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from .config import get_settings

_log = logging.getLogger("nfs.events")
_client = None
_trace_failure: ContextVar[str | None] = ContextVar("trace_failure", default=None)
_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_trace_ids_by_session: dict[str, str] = {}

_FAILURE_EVENT_NAMES = {
    "human_review_rejected",
    "injection_quarantined",
    "input_blocked",
    "mcp_fallback",
    "planner_fallback",
    "specialist_budget_exhausted",
    "specialist_failed",
    "synthesis_failed",
    "run_failed",
}


def setup_logging(level: int = logging.INFO) -> None:
    if logging.getLogger().handlers:
        return
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=level, handlers=[h])
    for noisy in ("httpx", "chromadb", "mcp.client", "mcp.server.lowlevel", "openai._base_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def add_log_file(path: Path) -> logging.FileHandler:
    """Also write all log records (incl. JSON events) to `path`."""
    h = logging.FileHandler(path, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(h)
    return h


def event(name: str, **fields: Any) -> None:
    """Structured operational event (one JSON line)."""
    _log.info(json.dumps({"event": name, "ts": round(time.time(), 3), **fields}, default=str))
    tool_failed = name == "tool_call" and not fields.get("ok", True) and not fields.get("denied", False)
    is_named_failure = name in _FAILURE_EVENT_NAMES or name.endswith(("_failed", "_failure", "_fallback"))
    if not is_named_failure and not tool_failed:
        return
    lf = langfuse()
    if not lf:
        return
    try:
        message = str(fields.get("error") or fields.get("reason") or name)
        level = "ERROR" if name.endswith(("_failed", "_failure")) or tool_failed else "WARNING"
        trace_id = _trace_id.get() or _trace_ids_by_session.get(str(fields.get("run_id")))
        with lf.start_as_current_observation(
            name=f"operational.{name}", as_type="span", input=fields,
            output={"event": name, "fields": fields}, level=level, status_message=message,
            trace_context={"trace_id": trace_id} if trace_id else None,
        ):
            pass
    except Exception as e:  # pragma: no cover - observability must never break the run
        _log.warning("langfuse event failed: %s", e)


def langfuse():
    global _client
    s = get_settings()
    if _client is None and s.langfuse_enabled:
        try:
            from langfuse import Langfuse

            # pass keys explicitly: .env is read by Settings, not exported to os.environ outside docker
            _client = Langfuse(public_key=s.langfuse_public_key, secret_key=s.langfuse_secret_key, host=s.langfuse_host or None)
        except Exception as e:  # pragma: no cover - observability must never break the run
            _log.warning("langfuse disabled: %s", e)
    return _client


def callbacks() -> list:
    if not langfuse():
        return []
    from langfuse.langchain import CallbackHandler

    return [CallbackHandler(public_key=get_settings().langfuse_public_key)]


@contextlib.contextmanager
def trace_run(name: str, session_id: str, input: Any = None, tags: list[str] | None = None, metadata: dict | None = None):
    """Root trace for one workflow execution. Yields the trace id (or None)."""
    lf = langfuse()
    if not lf:
        yield None
        return
    from langfuse import propagate_attributes

    with lf.start_as_current_observation(name=name, as_type="agent", input=input, metadata=metadata) as span:
        with propagate_attributes(session_id=session_id, tags=tags or [], trace_name=name):
            token = _trace_failure.set(None)
            trace_token = _trace_id.set(lf.get_current_trace_id())
            _trace_ids_by_session[session_id] = lf.get_current_trace_id()
            try:
                yield lf.get_current_trace_id()
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                _trace_failure.set(error)
                span.update(output={"status": "failed", "error": error}, level="ERROR", status_message=error)
                raise
            finally:
                error = _trace_failure.get()
                if error:
                    span.update(output={"status": "failed", "error": error}, level="ERROR", status_message=error)
                else:
                    span.update(output={"status": "completed"})
                _trace_failure.reset(token)
                _trace_id.reset(trace_token)
                _trace_ids_by_session.pop(session_id, None)


def mark_trace_failed(error: str) -> None:
    """Mark a caught workflow error on the active root trace."""
    _trace_failure.set(error)
    lf = langfuse()
    if lf:
        with contextlib.suppress(Exception):
            lf.update_current_span(output={"status": "failed", "error": error}, level="ERROR",
                                   status_message=error)


@contextlib.contextmanager
def span(name: str, as_type: str = "span", input: Any = None, output: Any = None, metadata: dict | None = None):
    lf = langfuse()
    if not lf:
        yield None
        return
    with lf.start_as_current_observation(name=name, as_type=as_type, input=input, output=output,
                                         metadata=metadata) as s:
        yield s


def score(trace_id: str | None, name: str, value: float | str, comment: str | None = None) -> None:
    lf = langfuse()
    if not (lf and trace_id):
        return
    try:
        data_type = "NUMERIC" if isinstance(value, (int, float)) and not isinstance(value, bool) else "CATEGORICAL"
        if isinstance(value, bool):
            value, data_type = float(value), "BOOLEAN"
        lf.create_score(trace_id=trace_id, name=name, value=value, data_type=data_type, comment=comment)
    except Exception as e:  # pragma: no cover
        _log.warning("langfuse score failed: %s", e)


def flush() -> None:
    lf = langfuse()
    if lf:
        with contextlib.suppress(Exception):
            lf.flush()
