"""Embeddings via any OpenAI-compatible `/embeddings` endpoint (OpenAI, Azure, vLLM, Ollama, ...)."""
from __future__ import annotations

import asyncio

import httpx

from app.core.exceptions import ConfigurationError, RetrievalError


class OpenAICompatEmbedder:
    def __init__(self, *, model: str, dim: int, api_key: str | None, base_url: str,
                 client: httpx.AsyncClient | None = None, batch_size: int = 96, max_retries: int = 3,
                 backoff: float = 1.0) -> None:
        if not api_key and "api.openai.com" in base_url:
            raise ConfigurationError("NEXUS_EMBEDDING_API_KEY is required for the OpenAI embedding provider")
        self.model, self.dim, self.batch_size = model, dim, batch_size
        self.name = f"openai:{model}:{dim}"
        self._url = base_url.rstrip("/") + "/embeddings"
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client, self._retries, self._backoff = client or httpx.AsyncClient(timeout=30), max_retries, backoff

    async def _post(self, batch: list[str]) -> list[list[float]]:
        body: dict = {"model": self.model, "input": batch}
        if self.model.startswith("text-embedding-3"):
            body["dimensions"] = self.dim
        for attempt in range(self._retries + 1):
            try:
                r = await self._client.post(self._url, json=body, headers=self._headers)
                if r.status_code in (429, 500, 502, 503, 504) and attempt < self._retries:
                    await asyncio.sleep(self._backoff * 2**attempt)
                    continue
                r.raise_for_status()
                data = sorted(r.json()["data"], key=lambda d: d["index"])
                vecs = [d["embedding"] for d in data]
                if len(vecs) != len(batch) or any(len(v) != self.dim for v in vecs):
                    raise RetrievalError(f"Embedding API returned unexpected shape (expected dim {self.dim})")
                return vecs
            except httpx.TransportError:
                if attempt == self._retries:
                    raise
                await asyncio.sleep(self._backoff * 2**attempt)
        raise RetrievalError("unreachable")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(await self._post(texts[i : i + self.batch_size]))
        return out
