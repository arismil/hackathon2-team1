from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from hackathon2_team1.audit import RunEventMetadata


def test_run_event_records_only_provider_neutral_metadata() -> None:
    event = RunEventMetadata(
        run_id="run-1",
        correlation_id="request-1",
        event_type="started",
        occurred_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        actor_id="authenticated-user-1",
    )

    assert event.model_dump() == {
        "run_id": "run-1",
        "correlation_id": "request-1",
        "event_type": "started",
        "occurred_at": datetime(2026, 9, 23, tzinfo=timezone.utc),
        "actor_id": "authenticated-user-1",
    }


def test_run_event_requires_aware_time_and_nonblank_identifiers() -> None:
    with pytest.raises(ValidationError):
        RunEventMetadata(run_id=" ", correlation_id="request-1", event_type="started", occurred_at=datetime.now(timezone.utc))
    with pytest.raises(ValidationError):
        RunEventMetadata(run_id="run-1", correlation_id="request-1", event_type="started", occurred_at=datetime(2026, 9, 23))
    with pytest.raises(ValidationError):
        RunEventMetadata(run_id="run-1", correlation_id="request-1", event_type="started", occurred_at=datetime.now(timezone.utc), actor_id=" ")


def test_run_event_rejects_payloads() -> None:
    with pytest.raises(ValidationError):
        RunEventMetadata.model_validate({
            "run_id": "run-1",
            "correlation_id": "request-1",
            "event_type": "started",
            "occurred_at": datetime.now(timezone.utc),
            "document_body": "content",
        })
