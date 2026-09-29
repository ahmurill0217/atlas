from __future__ import annotations

from brain.config import Settings, get_settings
from brain.llm.base import StructuredLLM


def get_llm(settings: Settings | None = None) -> StructuredLLM:
    settings = settings or get_settings()
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set (see .env.example)")
        from brain.llm.openai_llm import OpenAIStructuredLLM

        return OpenAIStructuredLLM(
            settings.llm_model, settings.openai_api_key, settings.llm_temperature, settings.llm_seed,
            settings.llm_max_retries,
        )
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")
