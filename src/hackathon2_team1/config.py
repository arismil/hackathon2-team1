"""Runtime configuration (env / .env driven)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Azure OpenAI
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_chat_deployment: str = "gpt-4.1"
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

    @property
    def azure_configured(self) -> bool:
        return bool(self.azure_openai_endpoint and self.azure_openai_api_key)

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
