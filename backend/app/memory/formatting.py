from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..models import MemoryEntry

if TYPE_CHECKING:
    from ..settings import Settings


def trim_memory_text(text: str, *, max_len: int) -> str:
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(cleaned) > max_len:
        return cleaned[:max_len] + "…"
    return cleaned


def format_memories_for_prompt(*, settings: "Settings", memories: list[MemoryEntry]) -> tuple[list[dict[str, Any]], list[str]]:
    max_items = max(1, int(settings.memory_recall_max_items))
    remaining = max(0, int(settings.memory_recall_budget_chars))
    packed: list[dict[str, Any]] = []
    used_ids: list[str] = []

    for memory in memories:
        if len(packed) >= max_items or remaining <= 0:
            break

        content = trim_memory_text(memory.content, max_len=320)
        summary = trim_memory_text(memory.summary, max_len=120)
        chosen_text = content
        source = "content"
        if len(chosen_text) > remaining:
            chosen_text = summary
            source = "summary"
        if len(chosen_text) > remaining:
            if remaining < 24:
                break
            chosen_text = trim_memory_text(chosen_text, max_len=remaining)
            source = f"{source}_trimmed"

        if not chosen_text:
            continue

        packed.append(
            {
                "id": memory.id,
                "scope": memory.scope,
                "kind": memory.kind,
                "subject_id": memory.subject_id,
                "text": chosen_text,
                "score": memory.score,
                "source": source,
            }
        )
        used_ids.append(memory.id)
        remaining -= len(chosen_text)

    return packed, used_ids

