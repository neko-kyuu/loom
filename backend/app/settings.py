from __future__ import annotations

import json
from pathlib import Path
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOOM_", env_file=".env", extra="ignore")

    app_name: str = "loom-backend"
    cors_allow_origins: list[str] = ["http://localhost:5173"]

    sqlite_path: str = "loom.sqlite3"

    # Demo mode: use fake DM/PC responses without calling external LLMs.
    demo_fake: bool = True
    demo_fake_latency_ms: int = 900

    # OpenAI-compatible config (used when demo_fake=false)
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_model: str | None = None

    # Per-actor overrides (optional; can be provided via config.json)
    openai_dm_model: str | None = None
    openai_dm_persona: str = "你是DM（主持人/叙事者）。你负责把用户的话转述给PC们，并补充必要背景；回复简短明确。"

    # Per-PC overrides (optional; defaults live in backend/pc_config/)
    openai_pc_models: dict[str, str | None] = Field(default_factory=dict)
    openai_pc_personas: dict[str, str | None] = Field(default_factory=dict)


@lru_cache
def get_settings() -> Settings:
    config_path = Path("config.json")
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
        if isinstance(data, dict):
            return Settings(**data)
    return Settings()
