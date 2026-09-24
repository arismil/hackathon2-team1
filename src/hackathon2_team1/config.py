"""Runtime configuration (env / .env driven)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Azure OpenAI (chat). New names first, legacy names as fallback.
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = Field(
        "2024-12-01-preview", validation_alias=AliasChoices("OPENAI_API_VERSION", "AZURE_OPENAI_API_VERSION")
    )
    azure_openai_chat_deployment: str = Field(
        "gpt-4.1-mini",
        validation_alias=AliasChoices("AZURE_OPENAI_DEPLOYMENT_NAME", "AZURE_OPENAI_CHAT_DEPLOYMENT"),
    )
    # Azure OpenAI (embeddings) - may live on a separate resource; falls back to the chat resource
    azure_embedding_endpoint: str = ""
    azure_embedding_api_key: str = ""
    azure_embedding_api_version: str = Field(
        "2023-05-15", validation_alias=AliasChoices("AZURE_EMBEDDING_API_VERSION", "AZURE_OPENAI_API_VERSION")
    )
    azure_openai_embedding_deployment: str = "text-embedding-3-small"
    llm_temperature: float | None = 0.0
    llm_timeout_s: float = 120.0
    llm_max_retries: int = 5

    # RAG
    knowledge_dir: Path = PACKAGE_DIR / "knowledge"
    chroma_dir: Path = PROJECT_DIR / "data" / "chroma"
    # "azure" for real runs, "hash" for offline tests (lexical hashing embeddings, no network)
    embedding_provider: str = "azure"
    retrieval_top_k: int = 5

    # MCP
    mcp_server_url: str = "http://localhost:8001/mcp"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8001
    mcp_connect_timeout_s: float = 10.0
    mcp_tool_timeout_s: float = 60.0
    # "remote" = streamable HTTP with in-process fallback; "local" = in-process MCP server only
    mcp_mode: str = "remote"
    records_db: Path = PROJECT_DIR / "data" / "records.sqlite"

    # Workflow
    checkpoint_db: Path = PROJECT_DIR / "data" / "checkpoints.sqlite"
    max_plan_iterations: int = 2
    specialist_model_call_limit: int = 14
    specialist_timeout_s: float = 300.0

    # Observability (Langfuse reads LANGFUSE_* itself; these just gate enabling it)
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""

    @field_validator("llm_temperature", mode="before")
    @classmethod
    def _empty_temperature(cls, v):
        return None if v in ("", "none", "None", None) else v

    @field_validator("azure_embedding_endpoint")
    @classmethod
    def _embedding_base_url(cls, v: str) -> str:
        # Accept a full ".../openai/deployments/<name>/embeddings?api-version=..." URL; the SDK wants the base
        if not v:
            return v
        parts = urlsplit(v)
        return f"{parts.scheme}://{parts.netloc}/" if parts.scheme and parts.netloc else v

    @property
    def azure_configured(self) -> bool:
        return bool(self.azure_openai_endpoint and self.azure_openai_api_key)

    @property
    def embedding_endpoint(self) -> str:
        return self.azure_embedding_endpoint or self.azure_openai_endpoint

    @property
    def embedding_api_key(self) -> str:
        return self.azure_embedding_api_key or self.azure_openai_api_key

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
