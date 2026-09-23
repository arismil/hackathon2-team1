from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from hackathon2_team1.evidence import EvidenceReference, SourceReference


def test_evidence_reference_retains_source_provenance() -> None:
    reference = EvidenceReference(
        evidence_id="e-1",
        source=SourceReference(
            source_id="doc-1",
            locator="section 2",
            version="v1",
        ),
        retrieved_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
    )

    assert reference.evidence_id == "e-1"
    assert reference.source.source_id == "doc-1"
    assert reference.source.locator == "section 2"
    assert reference.source.version == "v1"
    assert reference.retrieved_at.utcoffset().total_seconds() == 0


def test_source_reference_allows_unknown_locator_and_version() -> None:
    source = SourceReference(source_id="record-1")

    assert source.locator is None
    assert source.version is None


@pytest.mark.parametrize("source_id", [None, ""])
def test_source_reference_requires_source_id(source_id: str | None) -> None:
    with pytest.raises(ValidationError):
        SourceReference.model_validate({} if source_id is None else {"source_id": source_id})


@pytest.mark.parametrize("field", ["locator", "version"])
def test_source_reference_rejects_blank_optional_metadata(field: str) -> None:
    with pytest.raises(ValidationError):
        SourceReference.model_validate({"source_id": "doc-1", field: ""})


def test_evidence_reference_requires_evidence_id() -> None:
    with pytest.raises(ValidationError):
        EvidenceReference(
            evidence_id="",
            source=SourceReference(source_id="doc-1"),
            retrieved_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        )


def test_caller_supplied_access_scope_is_not_accepted_as_provenance() -> None:
    with pytest.raises(ValidationError):
        SourceReference.model_validate({"source_id": "doc-1", "access_scope": "team-a"})


def test_evidence_reference_does_not_store_source_content() -> None:
    with pytest.raises(ValidationError):
        EvidenceReference.model_validate(
            {
                "evidence_id": "e-1",
                "source": {"source_id": "doc-1"},
                "retrieved_at": "2026-09-23T00:00:00Z",
                "excerpt": "source text",
            }
        )


def test_evidence_requires_timezone_aware_retrieval_time() -> None:
    with pytest.raises(ValidationError):
        EvidenceReference(
            evidence_id="e-1",
            source=SourceReference(source_id="doc-1"),
            retrieved_at=datetime(2026, 9, 23),
        )
