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
from typing import Any

from .config import get_settings

_log = logging.getLogger("nfs.events")
_client = None


def setup_logging(level: int = logging.INFO) -> None:
    if logging.getLogger().handlers:
        return
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=level, handlers=[h])
    for noisy in ("httpx", "chromadb", "mcp.client", "mcp.server.lowlevel", "openai._base_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def event(name: str, **fields: Any) -> None:
    """Structured operational event (one JSON line)."""
    _log.info(json.dumps({"event": name, "ts": round(time.time(), 3), **fields}, default=str))


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
            yield lf.get_current_trace_id()
            span.update(output={"status": "completed"})


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
