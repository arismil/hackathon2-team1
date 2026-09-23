"""A per-case evaluation observation without a rubric or release policy."""

from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, StrictBool, StrictFloat, StrictInt, StrictStr, StringConstraints


NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class EvaluationObservation(BaseModel):
    """Records a check result; interpretation belongs to a future evaluator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: NonBlankText
    check_id: NonBlankText
    result: StrictBool | StrictInt | StrictFloat | StrictStr
    observed_at: AwareDatetime
