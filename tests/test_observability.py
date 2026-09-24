from __future__ import annotations

from contextlib import contextmanager

from hackathon2_team1 import observability as obs


class FakeLangfuse:
    def __init__(self):
        self.observations = []
        self.span_updates = []

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        self.observations.append(kwargs)
        yield object()

    def update_current_span(self, **kwargs):
        self.span_updates.append(kwargs)


def test_failure_and_fallback_events_are_sent_to_langfuse(monkeypatch):
    fake = FakeLangfuse()
    monkeypatch.setattr(obs, "_client", fake)

    obs.event("mcp_fallback", run_id="ASM-1", reason="remote unavailable")
    obs.event("tool_call", run_id="ASM-1", tool="search_policy", ok=False, denied=False,
              error="SERVICE_UNAVAILABLE")
    obs.event("tool_call", run_id="ASM-1", tool="search_policy", ok=True, denied=False)

    assert [item["name"] for item in fake.observations] == [
        "operational.mcp_fallback",
        "operational.tool_call",
    ]
    assert fake.observations[0]["level"] == "WARNING"
    assert fake.observations[1]["level"] == "ERROR"


def test_caught_run_failure_updates_active_span(monkeypatch):
    fake = FakeLangfuse()
    monkeypatch.setattr(obs, "_client", fake)

    obs.mark_trace_failed("RuntimeError: workflow stopped")

    assert fake.span_updates == [{
        "output": {"status": "failed", "error": "RuntimeError: workflow stopped"},
        "level": "ERROR",
        "status_message": "RuntimeError: workflow stopped",
    }]
