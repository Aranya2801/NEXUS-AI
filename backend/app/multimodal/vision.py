"""Vision-language adapters: describe an image (figure, chart, diagram, scanned page) in text so it can be indexed and cited.
MockVision is deterministic and says plainly that it is a mock; real providers are tested against mock HTTP transports only."""
from __future__ import annotations

import asyncio
import base64
from typing import Protocol

import httpx

from app.core.config import Settings
from app.core.exceptions import ConfigurationError

CAPTION_PROMPT = ("Describe this image for a search index. State what kind of figure it is (chart, diagram, table, photo, "
                  "scanned page), transcribe any visible text and numbers exactly, and summarise what it shows. "
                  "Do not guess beyond what is visible.")


class VisionModel(Protocol):
    name: str

    async def describe(self, png: bytes, prompt: str = CAPTION_PROMPT, *, max_tokens: int = 400) -> str: ...


class MockVision:
    name = "mock-vision"

    async def describe(self, png: bytes, prompt: str = CAPTION_PROMPT, *, max_tokens: int = 400) -> str:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(png)) as im:
            w, h = im.size
            ink = sum(1 for p in im.convert("L").resize((32, 32)).getdata() if p < 128)
        return f"[mock caption] {w}x{h}px image, {'text-heavy or dark' if ink > 400 else 'mostly light'} content."


class OpenAIVision:
    """Chat Completions with an `image_url` data-URI part (OpenAI, Azure, vLLM, Ollama-compatible)."""

    def __init__(self, *, model: str, api_key: str | None, base_url: str = "https://api.openai.com/v1",
                 client: httpx.AsyncClient | None = None, max_retries: int = 2, backoff: float = 1.0) -> None:
        if not api_key and "api.openai.com" in base_url:
            raise ConfigurationError("NEXUS_VISION_API_KEY is required for the OpenAI vision provider")
        self.name, self._model = f"openai-vision:{model}", model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client, self._retries, self._backoff = client or httpx.AsyncClient(timeout=90), max_retries, backoff

    async def describe(self, png: bytes, prompt: str = CAPTION_PROMPT, *, max_tokens: int = 400) -> str:
        uri = "data:image/png;base64," + base64.b64encode(png).decode()
        body = {"model": self._model, "max_tokens": max_tokens, "temperature": 0.0,
                "messages": [{"role": "user", "content": [{"type": "text", "text": prompt},
                                                          {"type": "image_url", "image_url": {"url": uri}}]}]}
        for attempt in range(self._retries + 1):
            r = await self._client.post(self._url, json=body, headers=self._headers)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < self._retries:
                await asyncio.sleep(self._backoff * 2**attempt)
                continue
            r.raise_for_status()
            return (r.json()["choices"][0]["message"]["content"] or "").strip()
        raise RuntimeError("unreachable")


class AnthropicVision:
    def __init__(self, *, model: str, api_key: str | None, base_url: str = "https://api.anthropic.com",
                 client: httpx.AsyncClient | None = None, max_retries: int = 2, backoff: float = 1.0) -> None:
        if not api_key:
            raise ConfigurationError("NEXUS_VISION_API_KEY is required for the Anthropic vision provider")
        self.name, self._model = f"anthropic-vision:{model}", model
        self._url = base_url.rstrip("/") + "/v1/messages"
        self._headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        self._client, self._retries, self._backoff = client or httpx.AsyncClient(timeout=90), max_retries, backoff

    async def describe(self, png: bytes, prompt: str = CAPTION_PROMPT, *, max_tokens: int = 400) -> str:
        body = {"model": self._model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}},
            {"type": "text", "text": prompt}]}]}
        for attempt in range(self._retries + 1):
            r = await self._client.post(self._url, json=body, headers=self._headers)
            if r.status_code in (429, 500, 502, 503, 529) and attempt < self._retries:
                await asyncio.sleep(self._backoff * 2**attempt)
                continue
            r.raise_for_status()
            return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
        raise RuntimeError("unreachable")


def build_vision(s: Settings) -> VisionModel | None:
    if s.vision_provider == "none":
        return None
    if s.vision_provider == "mock":
        return MockVision()
    if s.vision_provider == "openai":
        return OpenAIVision(model=s.vision_model, api_key=s.vision_api_key, base_url=s.vision_base_url or "https://api.openai.com/v1")
    return AnthropicVision(model=s.vision_model, api_key=s.vision_api_key, base_url=s.vision_base_url or "https://api.anthropic.com")
