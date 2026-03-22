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
    demo_forum_seed_path: str | None = "demo_forum_seed.json"

    # OpenAI-compatible config (used when demo_fake=false)
    openai_base_url: str | None = None
    # Optional: override embeddings endpoint URL (OpenAI-compatible `/embeddings`).
    # If not set, embeddings URL defaults to `{openai_base_url}/embeddings`.
    openai_embedding_url: str | None = None
    openai_api_key: str | None = None
    # Optional: override embeddings API key (defaults to openai_api_key).
    openai_embedding_api_key: str | None = None
    openai_model: str | None = None

    # Per-actor overrides (optional; can be provided via config.json)
    openai_dm_model: str | None = None
    openai_dm_persona: str = "你是DM（主持人/叙事者）。你负责把用户的话转述给PC们，并补充必要背景；回复简短明确。"
    openai_memory_model: str | None = None
    openai_embedding_model: str | None = None

    # Per-PC overrides (optional; defaults live in backend/pc_config/)
    openai_pc_models: dict[str, str | None] = Field(default_factory=dict)
    openai_pc_personas: dict[str, str | None] = Field(default_factory=dict)

    memory_recall_budget_chars: int = 1200
    memory_recall_max_items: int = 6
    memory_recall_max_keywords: int = 12
    memory_write_max_items: int = 3
    memory_write_existing_max_items: int = 6
    memory_write_summary_chars: int = 120
    memory_write_content_chars: int = 400
    memory_write_source_excerpt_chars: int = 200
    memory_write_recent_event_max_items: int = 1
    memory_maintenance_enabled: bool = True
    memory_maintenance_max_ops: int = 3
    memory_write_dedup_enabled: bool = True
    memory_write_dedup_min_sim: float = 0.9
    memory_write_dedup_scan_limit: int = 200
    memory_write_dedup_max_age_days: int = 14
    memory_recent_event_max_per_scope: int = 24
    memory_recent_event_max_per_conversation: int = 16
    memory_recent_event_max_per_thread: int = 12
    memory_recent_event_compact_enabled: bool = True
    memory_recent_event_compact_interval_ticks: int = 50
    memory_recent_event_compact_scan_limit: int = 300
    memory_recent_event_compact_min_sources: int = 4
    memory_recent_event_compact_max_sources: int = 6
    memory_decay_interval_ticks: int = 50
    memory_decay_k: int = 1
    memory_decay_threshold: int = -3

    # v5: hybrid memory recall (lexical + optional vector)
    memory_vector_enabled: bool = True
    memory_vector_embed_secrets: bool = False
    memory_vector_min_sim: float = 0.72
    memory_vector_top_k: int = 30
    memory_vector_scan_limit: int = 1200
    memory_hybrid_lex_candidates: int = 40
    memory_hybrid_w_sim: float = 1.0
    memory_hybrid_w_lex: float = 0.35
    memory_hybrid_w_score: float = 0.05
    memory_hybrid_w_pinned: float = 0.2

    # v4 tool-calling executor limits
    v4_max_tool_rounds: int = 3
    v4_max_tool_calls_per_round: int = 2
    # NOTE: This is a char-budget across all tool outputs in one tick.
    # Keep it large enough to allow memory_search + one context fetch.
    v4_max_total_tool_output_chars: int = 60_000

    # v5: public docs (doc_search) via MCP GraphRAG
    doc_search_enabled: bool = False
    # MCP stdio command, e.g. ["python", "server.py", "--config", "config.json"]
    doc_search_mcp_command: list[str] | None = None
    doc_search_mcp_cwd: str | None = None
    doc_search_mcp_init_timeout_s: float = 15.0
    doc_search_mcp_request_timeout_s: float = 45.0

    doc_search_min_score: float | None = None
    doc_search_allowed_path_prefixes: list[str] = Field(default_factory=list)
    doc_search_max_limit: int = 8
    doc_search_max_text_chars: int = 1200
    doc_search_max_results: int = 15
    doc_search_max_hops: int = 1


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
