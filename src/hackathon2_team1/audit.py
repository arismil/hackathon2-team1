"""Provider-neutral metadata for a run event, without payload storage."""

from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints


NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RunEventMetadata(BaseModel):
    """The caller must supply actor_id only from a trusted authentication boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: NonBlankText
    correlation_id: NonBlankText
    event_type: NonBlankText
    occurred_at: AwareDatetime
    actor_id: NonBlankText | None = None
