import pytest
from pydantic import ValidationError

from hackathon2_team1.api_errors import PublicApiError


def test_public_error_contains_only_safe_fields() -> None:
    error = PublicApiError(code="invalid_request", message="Invalid request", request_id="run-1")

    assert error.model_dump() == {
        "code": "invalid_request",
        "message": "Invalid request",
        "request_id": "run-1",
    }


def test_public_error_rejects_extra_fields_and_blanks() -> None:
    with pytest.raises(ValidationError):
        PublicApiError(code="", message="Invalid request", request_id="run-1")
    with pytest.raises(ValidationError):
        PublicApiError.model_validate({
            "code": "internal_error",
            "message": "Internal server error",
            "request_id": "run-1",
            "exception": "private detail",
        })
