from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from ..models import MemoryEntry, utc_now_iso
from .embeddings import maybe_upsert_memory_summary_embeddings
from .events import add_private_event_safely

if TYPE_CHECKING:
    from ..tick_runner import TickRunner


def _trim_text(text: str, *, max_len: int) -> str:
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(cleaned) > max_len:
        return cleaned[:max_len].rstrip() + "…"
    return cleaned


def _compact_group_key_for_memory(memory: MemoryEntry) -> str:
    meta = memory.meta if isinstance(memory.meta, dict) else {}
    thread_id = meta.get("thread_id")
    conversation_id = meta.get("conversation_id")

    if isinstance(thread_id, str) and thread_id.strip():
        anchor = f"thread:{thread_id.strip()}"
    elif memory.scope == "direct" and isinstance(memory.scope_id, str) and memory.scope_id.strip():
        anchor = f"direct:{memory.scope_id.strip()}"
    elif isinstance(conversation_id, str) and conversation_id.strip():
        anchor = f"conv:{conversation_id.strip()}"
    elif memory.scope == "pc" and isinstance(memory.owner_pc_id, str) and memory.owner_pc_id.strip():
        anchor = f"pc:{memory.owner_pc_id.strip()}"
    else:
        anchor = f"scope:{memory.scope}:{memory.owner_pc_id or ''}:{memory.scope_id or ''}"

    parts = ["recent_event_compact", anchor]
    if memory.subject_type:
        parts.append(f"subject:{memory.subject_type}")
    if memory.subject_id:
        parts.append(f"id:{memory.subject_id}")
    return "|".join(parts)


def _is_compactable_recent_event(memory: MemoryEntry) -> bool:
    if memory.kind != "recent_event":
        return False
    if memory.deleted_at or memory.pinned:
        return False
    if memory.edit_state != "normal":
        return False
    if memory.source_type == "maintenance_compact":
        return False
    return True


def _is_active_compact_memory(memory: MemoryEntry, *, compact_key: str) -> bool:
    if memory.kind != "recent_event" or memory.deleted_at:
        return False
    if memory.source_type != "maintenance_compact":
        return False
    meta = memory.meta if isinstance(memory.meta, dict) else {}
    return meta.get("compact_key") == compact_key


def _compact_keep_sort_key(memory: MemoryEntry) -> tuple[str, str, str]:
    return (str(memory.updated_at or ""), str(memory.created_at or ""), str(memory.id))


def _dedupe_preserve_order(values: list[str], *, limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out[-limit:] if limit > 0 else out


def _build_compact_summary(*, memory: MemoryEntry, keywords: list[str], max_len: int) -> str:
    meta = memory.meta if isinstance(memory.meta, dict) else {}
    thread_title = str(meta.get("thread_title") or "").strip()
    if thread_title:
        base = f"《{thread_title}》近期进展"
    elif memory.scope == "direct":
        if memory.subject_id:
            base = f"与{memory.subject_id}的近期进展"
        else:
            base = "私聊近期进展"
    elif memory.scope == "public":
        base = "共享近期进展"
    else:
        base = "近期进展"

    if keywords:
        suffix = "、".join(keywords[:2])
        return _trim_text(f"{base}：{suffix}", max_len=max_len)
    return _trim_text(base, max_len=max_len)


def _build_compact_content(*, points: list[str], max_len: int) -> str:
    if not points:
        return ""
    lines = [f"- {point}" for point in points]
    return _trim_text("\n".join(lines), max_len=max_len)


def _build_compacted_memory(
    *,
    group_key: str,
    existing_compact: MemoryEntry | None,
    source_memories: list[MemoryEntry],
    summary_max_chars: int,
    content_max_chars: int,
) -> MemoryEntry:
    now = utc_now_iso()
    base = existing_compact or source_memories[-1]
    existing_meta = dict(existing_compact.meta or {}) if existing_compact and isinstance(existing_compact.meta, dict) else {}

    keywords: list[str] = []
    if isinstance(existing_meta.get("keywords"), list):
        keywords.extend([str(value).strip() for value in existing_meta["keywords"] if str(value).strip()])
    for memory in source_memories:
        meta = memory.meta if isinstance(memory.meta, dict) else {}
        raw_keywords = meta.get("keywords")
        if isinstance(raw_keywords, list):
            keywords.extend([str(value).strip() for value in raw_keywords if str(value).strip()])

    points: list[str] = []
    raw_existing_points = existing_meta.get("compact_points")
    if isinstance(raw_existing_points, list):
        points.extend([str(value).strip() for value in raw_existing_points if str(value).strip()])
    elif existing_compact is not None and isinstance(existing_compact.summary, str) and existing_compact.summary.strip():
        points.append(existing_compact.summary.strip())
    points.extend(memory.summary.strip() for memory in source_memories if isinstance(memory.summary, str) and memory.summary.strip())
    points = _dedupe_preserve_order(points, limit=12)
    keywords = _dedupe_preserve_order(keywords, limit=6)

    summary = _build_compact_summary(memory=base, keywords=keywords, max_len=max(1, summary_max_chars))
    content = _build_compact_content(points=points, max_len=max(1, content_max_chars))

    compacted_from: list[str] = []
    raw_compacted_from = existing_meta.get("compacted_from")
    if isinstance(raw_compacted_from, list):
        compacted_from.extend([str(value).strip() for value in raw_compacted_from if str(value).strip()])
    compacted_from.extend(memory.id for memory in source_memories if isinstance(memory.id, str) and memory.id.strip())
    compacted_from = _dedupe_preserve_order(compacted_from, limit=24)

    meta = dict(existing_meta)
    meta.update(
        {
            "compact_key": group_key,
            "compact_points": points,
            "compacted_from": compacted_from,
            "compacted_at": now,
            "keywords": keywords,
            "merge_key": group_key,
            "retention_class": "ephemeral",
        }
    )

    return MemoryEntry(
        id=existing_compact.id if existing_compact is not None else str(uuid5(NAMESPACE_URL, group_key)),
        scope=base.scope,
        scope_id=base.scope_id,
        owner_pc_id=base.owner_pc_id,
        kind="recent_event",
        created_at=existing_compact.created_at if existing_compact is not None else now,
        updated_at=now,
        summary=summary,
        content=content,
        subject_type=base.subject_type,
        subject_id=base.subject_id,
        importance=max([int(base.importance)] + [int(memory.importance) for memory in source_memories]),
        score=max([int(base.score)] + [int(memory.score) for memory in source_memories]),
        pinned=existing_compact.pinned if existing_compact is not None else False,
        access_count=existing_compact.access_count if existing_compact is not None else 0,
        last_accessed_at=existing_compact.last_accessed_at if existing_compact is not None else None,
        source_type="maintenance_compact",
        source_memory_id=source_memories[-1].id if source_memories else None,
        revision=(int(existing_compact.revision) + 1) if existing_compact is not None else 0,
        meta=meta,
    )


def _mark_compacted_source(memory: MemoryEntry, *, compacted_into_id: str, reason: str) -> MemoryEntry:
    now = utc_now_iso()
    meta = dict(memory.meta or {})
    meta["compacted_into"] = compacted_into_id
    meta["delete_reason"] = reason
    meta["deleted_by"] = "recent_event_compact"
    return memory.model_copy(
        update={
            "deleted_at": now,
            "edit_state": "deleted",
            "updated_at": now,
            "source_type": "maintenance",
            "source_memory_id": compacted_into_id,
            "revision": int(memory.revision) + 1,
            "meta": meta,
        }
    )


def _choose_compaction_batches(
    memories: list[MemoryEntry], *, min_sources: int, max_sources: int
) -> list[tuple[str, MemoryEntry | None, list[MemoryEntry]]]:
    groups: dict[str, list[MemoryEntry]] = defaultdict(list)
    for memory in memories:
        groups[_compact_group_key_for_memory(memory)].append(memory)

    batches: list[tuple[str, MemoryEntry | None, list[MemoryEntry]]] = []
    for group_key, group_memories in groups.items():
        existing_compact: MemoryEntry | None = None
        protected_compact = False
        source_memories: list[MemoryEntry] = []
        for memory in group_memories:
            if _is_active_compact_memory(memory, compact_key=group_key):
                if memory.pinned or memory.edit_state != "normal":
                    protected_compact = True
                elif existing_compact is None:
                    existing_compact = memory
                continue
            if _is_compactable_recent_event(memory):
                source_memories.append(memory)

        if protected_compact:
            continue
        if len(source_memories) < max(1, min_sources):
            continue

        source_memories.sort(key=_compact_keep_sort_key)
        batches.append((group_key, existing_compact, source_memories[: max(1, max_sources)]))
    return batches


async def compact_recent_events(
    *,
    runner: "TickRunner",
    actor_pc_id: str | None = None,
    scope_filters: list[tuple[str, str | None, str | None]] | None = None,
) -> dict[str, int]:
    if not bool(getattr(runner._settings, "memory_recent_event_compact_enabled", True)):  # noqa: SLF001
        return {"groups": 0, "targets_written": 0, "sources_deleted": 0}

    scan_limit = max(20, int(getattr(runner._settings, "memory_recent_event_compact_scan_limit", 300) or 300))  # noqa: SLF001
    min_sources = max(2, int(getattr(runner._settings, "memory_recent_event_compact_min_sources", 4) or 4))  # noqa: SLF001
    max_sources = max(min_sources, int(getattr(runner._settings, "memory_recent_event_compact_max_sources", 6) or 6))  # noqa: SLF001
    summary_max_chars = max(1, int(getattr(runner._settings, "memory_write_summary_chars", 120) or 120))  # noqa: SLF001
    content_max_chars = max(1, int(getattr(runner._settings, "memory_write_content_chars", 400) or 400))  # noqa: SLF001

    scope_keys = list(dict.fromkeys(scope_filters or []))
    scope_memories: list[list[MemoryEntry]] = []
    if scope_keys:
        for scope, owner_pc_id, scope_id in scope_keys:
            memories = await runner._store.list_memories(  # noqa: SLF001
                scope=scope,
                owner_pc_id=owner_pc_id,
                scope_id=scope_id,
                kind="recent_event",
                limit=scan_limit,
            )
            scope_memories.append(memories)
    else:
        memories = await runner._store.list_memories(kind="recent_event", limit=scan_limit)  # noqa: SLF001
        grouped: dict[tuple[str, str | None, str | None], list[MemoryEntry]] = defaultdict(list)
        for memory in memories:
            grouped[(memory.scope, memory.owner_pc_id, memory.scope_id)].append(memory)
        scope_memories.extend(grouped.values())

    compacted_targets: list[MemoryEntry] = []
    deleted_sources: list[MemoryEntry] = []
    groups = 0
    for memories in scope_memories:
        for group_key, existing_compact, source_memories in _choose_compaction_batches(
            memories, min_sources=min_sources, max_sources=max_sources
        ):
            compacted = _build_compacted_memory(
                group_key=group_key,
                existing_compact=existing_compact,
                source_memories=source_memories,
                summary_max_chars=summary_max_chars,
                content_max_chars=content_max_chars,
            )
            deleted = [
                _mark_compacted_source(memory, compacted_into_id=compacted.id, reason="recent_event_compact")
                for memory in source_memories
            ]
            await runner._store.upsert_memories([compacted, *deleted])  # noqa: SLF001
            compacted_targets.append(compacted)
            deleted_sources.extend(deleted)
            groups += 1

    if deleted_sources:
        try:
            await runner._store.delete_memory_summary_embeddings(  # noqa: SLF001
                memory_ids=[memory.id for memory in deleted_sources]
            )
        except Exception:
            pass
    if compacted_targets:
        await maybe_upsert_memory_summary_embeddings(runner=runner, memories=compacted_targets)

    if groups > 0:
        await add_private_event_safely(
            runner=runner,
            pc_id=actor_pc_id,
            type="memory_recent_event_compact",
            summary=f"recent_event compacted {len(deleted_sources)} memories into {len(compacted_targets)} targets",
            consequences={
                "groups": groups,
                "targets_written": len(compacted_targets),
                "sources_deleted": len(deleted_sources),
                "target_ids": [memory.id for memory in compacted_targets],
                "source_ids": [memory.id for memory in deleted_sources],
            },
        )

    return {
        "groups": groups,
        "targets_written": len(compacted_targets),
        "sources_deleted": len(deleted_sources),
    }
