from __future__ import annotations

from typing import Any

from ..models import MemoryEntry, utc_now_iso

_ALLOWED_KINDS = {"autobiography", "relationship", "recent_event", "secret"}


def extract_keywords(raw: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out[:12]


def trim_memory_text(text: str, *, max_len: int) -> str:
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(cleaned) > max_len:
        return cleaned[:max_len] + "…"
    return cleaned


def parse_maintenance_ops(payload: Any, *, max_ops: int) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw_ops = payload.get("ops")
    if not isinstance(raw_ops, list):
        return []
    return [item for item in raw_ops if isinstance(item, dict)][: max(0, int(max_ops))]


def maintenance_upserts_from_payload(payload: Any, *, max_items: int) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw_upserts = payload.get("upserts")
    if not isinstance(raw_upserts, list):
        return []
    return [item for item in raw_upserts if isinstance(item, dict)][: max(0, int(max_items))]


def can_apply_maintenance_op(memory: MemoryEntry, *, op_type: str) -> bool:
    if memory.deleted_at or memory.edit_state == "deleted":
        return False
    if memory.pinned:
        return False
    if memory.edit_state in {"user_locked", "user_edited"}:
        return False
    if memory.kind == "autobiography":
        return False
    if op_type in {"delete", "merge"} and memory.kind != "recent_event":
        return False
    return op_type in {"rewrite", "merge", "delete"}


def _base_memory_meta(memory: MemoryEntry) -> dict[str, Any]:
    return dict(memory.meta or {}) if isinstance(memory.meta, dict) else {}


def build_maintenance_rewrite(
    *,
    existing: MemoryEntry,
    item: dict[str, Any],
    source_type: str,
    reason: str | None,
    summary_max_chars: int,
    content_max_chars: int,
) -> MemoryEntry | None:
    raw_kind = str(item.get("kind") or existing.kind).strip()
    if raw_kind not in _ALLOWED_KINDS or raw_kind != existing.kind:
        return None

    summary = trim_memory_text(str(item.get("summary") or ""), max_len=max(1, summary_max_chars))
    content = trim_memory_text(str(item.get("content") or ""), max_len=max(1, content_max_chars))
    if not summary or not content:
        return None

    importance_raw = item.get("importance")
    importance = int(importance_raw) if isinstance(importance_raw, (int, float)) else existing.importance
    importance = max(0, min(10, importance))
    subject_type = str(item.get("subject_type") or "").strip() or existing.subject_type
    subject_id = str(item.get("subject_id") or "").strip() or existing.subject_id
    merge_key = str(item.get("merge_key") or "").strip() or None
    keywords = extract_keywords(item.get("keywords"))

    meta = _base_memory_meta(existing)
    if merge_key:
        meta["merge_key"] = merge_key
    if keywords:
        meta["keywords"] = keywords
    if reason:
        meta["maintenance_reason"] = reason
    meta["maintained_at"] = utc_now_iso()

    now = utc_now_iso()
    return existing.model_copy(
        update={
            "updated_at": now,
            "summary": summary,
            "content": content,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "importance": max(existing.importance, importance),
            "score": max(existing.score, importance),
            "source_type": source_type,
            "revision": int(existing.revision) + 1,
            "meta": meta,
        }
    )


def build_maintenance_delete(
    *,
    existing: MemoryEntry,
    source_type: str,
    reason: str | None,
    source_memory_id: str | None = None,
) -> MemoryEntry:
    now = utc_now_iso()
    meta = _base_memory_meta(existing)
    if reason:
        meta["delete_reason"] = reason
    meta["deleted_by"] = source_type
    return existing.model_copy(
        update={
            "deleted_at": now,
            "edit_state": "deleted",
            "updated_at": now,
            "source_type": source_type,
            "source_memory_id": source_memory_id or existing.source_memory_id,
            "revision": int(existing.revision) + 1,
            "meta": meta,
        }
    )


def build_maintenance_merge_target(
    *,
    primary: MemoryEntry,
    rewritten: MemoryEntry,
    merged_source_ids: list[str],
    reason: str | None,
) -> MemoryEntry:
    meta = _base_memory_meta(rewritten)
    existing = meta.get("merged_sources")
    merged_sources = [str(value).strip() for value in existing] if isinstance(existing, list) else []
    for value in merged_source_ids:
        text = str(value or "").strip()
        if text and text not in merged_sources:
            merged_sources.append(text)
    meta["merged_sources"] = merged_sources[-24:]
    if reason:
        meta["maintenance_reason"] = reason
    return rewritten.model_copy(
        update={
            "source_memory_id": primary.id,
            "meta": meta,
        }
    )
