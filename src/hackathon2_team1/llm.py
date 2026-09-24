"""Azure OpenAI model factories (patched by tests with scripted fakes)."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings

from .config import Settings, get_settings


def get_chat_model(role: str = "default", settings: Settings | None = None) -> BaseChatModel:
    s = settings or get_settings()
    if not s.azure_configured:
        raise RuntimeError("Azure OpenAI is not configured (AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY)")
    kwargs = {}
    if s.llm_temperature is not None:
        kwargs["temperature"] = s.llm_temperature
    return AzureChatOpenAI(
        azure_endpoint=s.azure_openai_endpoint,
        api_key=s.azure_openai_api_key,
        api_version=s.azure_openai_api_version,
        azure_deployment=s.azure_openai_chat_deployment,
        timeout=s.llm_timeout_s,
        max_retries=s.llm_max_retries,
        name=role,
        **kwargs,
    )


def get_embeddings(settings: Settings | None = None) -> AzureOpenAIEmbeddings:
    s = settings or get_settings()
    if not s.azure_configured:
        raise RuntimeError("Azure OpenAI is not configured (AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY)")
    return AzureOpenAIEmbeddings(
        azure_endpoint=s.azure_openai_endpoint,
        api_key=s.azure_openai_api_key,
        api_version=s.azure_openai_api_version,
        azure_deployment=s.azure_openai_embedding_deployment,
        max_retries=s.llm_max_retries,
    )
