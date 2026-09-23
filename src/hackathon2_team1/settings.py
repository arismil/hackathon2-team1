"""Application settings that do not depend on a model or service provider."""

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: str = Field(default="local", min_length=1)

    @classmethod
    def from_environment(cls, variables: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if variables is None else variables
        return cls(environment=source.get("APP_ENV", "local"))
