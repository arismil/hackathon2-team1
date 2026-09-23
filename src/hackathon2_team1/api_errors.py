"""Public error data exposed at the service boundary."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class PublicApiError(BaseModel):
    """Safe fields only; internal exception details belong outside this contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: NonBlankText
    message: NonBlankText
    request_id: NonBlankText
