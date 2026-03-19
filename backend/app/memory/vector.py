from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from ..llm import openai_embeddings_url

if TYPE_CHECKING:
    from ..llm import LlmService
    from ..settings import Settings


def memory_vector_enabled(*, settings: "Settings", llm: "LlmService | None") -> bool:
    if not bool(getattr(settings, "memory_vector_enabled", True)):
        return False
    if settings.demo_fake:
        return False
    if llm is None:
        return False
    if not (settings.openai_embedding_api_key or settings.openai_api_key):
        return False
    if not (settings.openai_embedding_url or settings.openai_base_url):
        return False
    model = getattr(settings, "openai_embedding_model", None)
    return isinstance(model, str) and model.strip() != ""


def embedding_api_key(settings: "Settings") -> str | None:
    if isinstance(settings.openai_embedding_api_key, str) and settings.openai_embedding_api_key.strip():
        return settings.openai_embedding_api_key.strip()
    if isinstance(settings.openai_api_key, str) and settings.openai_api_key.strip():
        return settings.openai_api_key.strip()
    return None


def embedding_url(settings: "Settings") -> str | None:
    if isinstance(settings.openai_embedding_url, str) and settings.openai_embedding_url.strip():
        return settings.openai_embedding_url.strip()
    if isinstance(settings.openai_base_url, str) and settings.openai_base_url.strip():
        return openai_embeddings_url(settings.openai_base_url)
    return None


def sha256_text(text: str) -> str:
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()

