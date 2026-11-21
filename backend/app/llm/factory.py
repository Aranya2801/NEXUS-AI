from __future__ import annotations

from app.core.config import Settings
from app.llm.base import LLM
from app.llm.mock import MockLLM


def build_llm(settings: Settings) -> LLM:
    if settings.llm_provider == "mock":
        return MockLLM()
    if settings.llm_provider == "openai":
        from app.llm.openai_compat import OpenAICompatLLM

        return OpenAICompatLLM(model=settings.llm_model, api_key=settings.llm_api_key,
                               base_url=settings.llm_base_url or "https://api.openai.com/v1")
    from app.llm.anthropic import AnthropicLLM

    return AnthropicLLM(model=settings.llm_model, api_key=settings.llm_api_key,
                        base_url=settings.llm_base_url or "https://api.anthropic.com")


def build_llm_from(m) -> LLM:
    """Builds one model from a routing-config entry (`app.llmops.config.ModelCfg`); the API key is read from the named environment variable."""
    import os

    key = os.environ.get(m.api_key_env) if m.api_key_env else None
    if m.provider == "mock":
        return MockLLM()
    if m.provider == "openai":
        from app.llm.openai_compat import OpenAICompatLLM

        return OpenAICompatLLM(model=m.model, api_key=key, base_url=m.base_url or "https://api.openai.com/v1")
    from app.llm.anthropic import AnthropicLLM

    return AnthropicLLM(model=m.model, api_key=key, base_url=m.base_url or "https://api.anthropic.com")
