from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from hackathon2_team1.evaluation import EvaluationObservation


@pytest.mark.parametrize("result", [True, 0, 0.75, "needs_review"])
def test_observation_records_scalar_result_per_case(result: bool | int | float | str) -> None:
    observation = EvaluationObservation(
        case_id="case-1",
        check_id="check-1",
        result=result,
        observed_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
    )

    assert observation.case_id == "case-1"
    assert observation.check_id == "check-1"
    assert observation.result == result
    assert type(observation.result) is type(result)


def test_observation_rejects_blank_ids_naive_time_and_extra_policy() -> None:
    with pytest.raises(ValidationError):
        EvaluationObservation(case_id=" ", check_id="check-1", result=True, observed_at=datetime.now(timezone.utc))
    with pytest.raises(ValidationError):
        EvaluationObservation(case_id="case-1", check_id="check-1", result=True, observed_at=datetime(2026, 9, 23))
    with pytest.raises(ValidationError):
        EvaluationObservation.model_validate({
            "case_id": "case-1",
            "check_id": "check-1",
            "result": True,
            "observed_at": datetime.now(timezone.utc),
            "threshold": 0.8,
        })
    with pytest.raises(ValidationError):
        EvaluationObservation(case_id="case-1", check_id="check-1", result={"payload": "content"}, observed_at=datetime.now(timezone.utc))
