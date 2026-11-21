from __future__ import annotations

import asyncio
import time

import httpx

from app.core.exceptions import ConfigurationError
from app.llm.base import LLMResponse


class AnthropicLLM:
    """Anthropic Messages API over httpx. The model id comes from NEXUS_LLM_MODEL (never hard-coded here)."""

    def __init__(self, *, model: str, api_key: str | None, base_url: str = "https://api.anthropic.com",
                 client: httpx.AsyncClient | None = None, max_retries: int = 3, backoff: float = 1.0) -> None:
        if not api_key:
            raise ConfigurationError("NEXUS_LLM_API_KEY is required for the Anthropic provider")
        self.name, self._model = f"anthropic:{model}", model
        self._url = base_url.rstrip("/") + "/v1/messages"
        self._headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        self._client, self._retries, self._backoff = client or httpx.AsyncClient(timeout=60), max_retries, backoff

    async def complete(self, system: str, user: str, *, max_tokens: int = 800, temperature: float = 0.0) -> LLMResponse:
        body = {"model": self._model, "max_tokens": max_tokens, "temperature": temperature, "system": system,
                "messages": [{"role": "user", "content": user}]}
        t0 = time.perf_counter()
        for attempt in range(self._retries + 1):
            r = await self._client.post(self._url, json=body, headers=self._headers)
            if r.status_code in (429, 500, 502, 503, 504, 529) and attempt < self._retries:
                await asyncio.sleep(self._backoff * 2**attempt)
                continue
            r.raise_for_status()
            d = r.json()
            text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
            u = d.get("usage", {})
            return LLMResponse(text, self.name, u.get("input_tokens", 0), u.get("output_tokens", 0), (time.perf_counter() - t0) * 1000)
        raise RuntimeError("unreachable")
