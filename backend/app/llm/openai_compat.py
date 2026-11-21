from __future__ import annotations

import asyncio
import time

import httpx

from app.core.exceptions import ConfigurationError
from app.llm.base import LLMResponse


class OpenAICompatLLM:
    """Chat Completions-compatible endpoint (OpenAI, Azure, vLLM, Ollama...)."""

    def __init__(self, *, model: str, api_key: str | None, base_url: str = "https://api.openai.com/v1",
                 client: httpx.AsyncClient | None = None, max_retries: int = 3, backoff: float = 1.0) -> None:
        if not api_key and "api.openai.com" in base_url:
            raise ConfigurationError("NEXUS_LLM_API_KEY is required for the OpenAI provider")
        self.name, self._model = f"openai:{model}", model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client, self._retries, self._backoff = client or httpx.AsyncClient(timeout=60), max_retries, backoff

    async def complete(self, system: str, user: str, *, max_tokens: int = 800, temperature: float = 0.0) -> LLMResponse:
        body = {"model": self._model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        t0 = time.perf_counter()
        for attempt in range(self._retries + 1):
            r = await self._client.post(self._url, json=body, headers=self._headers)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < self._retries:
                await asyncio.sleep(self._backoff * 2**attempt)
                continue
            r.raise_for_status()
            d = r.json()
            u = d.get("usage", {})
            return LLMResponse(d["choices"][0]["message"]["content"] or "", self.name, u.get("prompt_tokens", 0),
                               u.get("completion_tokens", 0), (time.perf_counter() - t0) * 1000)
        raise RuntimeError("unreachable")
