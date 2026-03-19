from __future__ import annotations

from typing import Protocol

from ..llm import LlmService
from ..settings import Settings
from .vector import embedding_api_key, embedding_url


class VectorProvider(Protocol):
    async def embed_texts(self, *, model: str, inputs: list[str], timeout_s: float) -> list[list[float]]: ...


class LlmVectorProvider:
    def __init__(self, *, llm: LlmService, settings: Settings) -> None:
        self._llm = llm
        self._settings = settings

    async def embed_texts(self, *, model: str, inputs: list[str], timeout_s: float) -> list[list[float]]:
        url = embedding_url(self._settings)
        if not url:
            raise RuntimeError("missing embedding url")
        apikey = embedding_api_key(self._settings)
        if not apikey:
            raise RuntimeError("missing embedding api key")
        return await self._llm.embeddings(
            url=url,
            apikey=apikey,
            model=model,
            inputs=inputs,
            timeout_s=float(timeout_s),
        )

