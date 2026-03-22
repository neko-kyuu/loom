from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from pc_config.prompts import render_prompt_messages

from ..llm import openai_chat_completions_url, parse_llm_response
from ..models import ForumThread, MemoryEntry, Message, utc_now_iso
from .compact import compact_recent_events
from .dedup import dedup_merge_memories_on_write
from .embeddings import maybe_upsert_memory_summary_embeddings
from .events import add_private_event_safely
from .maintenance import (
    build_maintenance_delete,
    build_maintenance_merge_target,
    build_maintenance_rewrite,
    can_apply_maintenance_op,
    maintenance_upserts_from_payload,
    parse_maintenance_ops,
)

if TYPE_CHECKING:
    from ..tick_runner import TickRunner


_MEMORY_TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,8}|[A-Za-z0-9_]{3,24}")
_MEMORY_STOPWORDS = {
    "这个",
    "那个",
    "什么",
    "为什么",
    "怎么",
    "我们",
    "你们",
    "他们",
    "自己",
    "已经",
    "还是",
    "如果",
    "因为",
    "所以",
    "然后",
    "但是",
    "就是",
    "一个",
    "一些",
    "这种",
    "那种",
    "这里",
    "那里",
    "当前",
    "最近",
    "论坛",
    "帖子",
    "thread",
    "reply",
    "selected",
}
_MEMORY_ALLOWED_KINDS = {"autobiography", "relationship", "recent_event", "secret"}


def _trim_memory_text(text: str, *, max_len: int) -> str:
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(cleaned) > max_len:
        return cleaned[:max_len] + "…"
    return cleaned


def _extract_memory_keywords(runner: "TickRunner", *parts: str) -> list[str]:
    limit = max(1, int(runner._settings.memory_recall_max_keywords))  # noqa: SLF001
    keywords: list[str] = []
    seen: set[str] = set()

    def add_keyword(value: str | None) -> None:
        if not isinstance(value, str):
            return
        text = value.strip()
        if not text:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        keywords.append(text)

    combined = "\n".join(part for part in parts if isinstance(part, str) and part.strip())
    for pc in runner._engine.pcs:  # noqa: SLF001
        if pc.name and pc.name in combined:
            add_keyword(pc.name)

    weights: dict[str, int] = {}
    for match in _MEMORY_TOKEN_PATTERN.findall(combined):
        token = match.strip()
        if not token:
            continue
        norm = token.casefold()
        if norm in _MEMORY_STOPWORDS or norm.isdigit() or len(token) <= 1:
            continue
        weights[token] = weights.get(token, 0) + 1

    for token, _ in sorted(weights.items(), key=lambda item: (-item[1], -len(item[0]), item[0])):
        add_keyword(token)
        if len(keywords) >= limit:
            break

    return keywords[:limit]


def _normalize_recent_event_topic_part(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    tokens = _MEMORY_TOKEN_PATTERN.findall(value)
    if not tokens:
        return None
    cleaned = "_".join(token.strip().casefold() for token in tokens if token.strip())
    cleaned = cleaned.strip("_")
    return cleaned[:24] if cleaned else None


def _build_recent_event_merge_key(
    *,
    scope: str,
    scope_id: str | None,
    owner_pc_id: str | None,
    conversation_id: str | None,
    thread_id: str | None,
    subject_type: str | None,
    subject_id: str | None,
    keywords: list[str],
) -> str | None:
    anchor: str | None = None
    if isinstance(thread_id, str) and thread_id.strip():
        anchor = f"thread:{thread_id.strip()}"
    elif scope == "direct" and isinstance(scope_id, str) and scope_id.strip():
        anchor = f"direct:{scope_id.strip()}"
    elif isinstance(conversation_id, str) and conversation_id.strip():
        anchor = f"conv:{conversation_id.strip()}"
    elif scope == "pc" and isinstance(owner_pc_id, str) and owner_pc_id.strip():
        anchor = f"pc:{owner_pc_id.strip()}"

    topic_terms: list[str] = []
    for value in keywords:
        part = _normalize_recent_event_topic_part(value)
        if part and part not in topic_terms:
            topic_terms.append(part)
        if len(topic_terms) >= 2:
            break

    if not anchor or not topic_terms:
        return None

    parts = ["recent_event", anchor]
    if subject_type:
        parts.append(f"subject:{subject_type}")
    if subject_id:
        parts.append(f"id:{subject_id}")
    parts.extend(topic_terms)
    merge_key = "|".join(parts)
    return merge_key[:160] if merge_key else None


def _limit_recent_event_entries(*, entries: list[MemoryEntry], max_items: int) -> list[MemoryEntry]:
    max_items = max(0, int(max_items))
    if max_items <= 0:
        return [entry for entry in entries if entry.kind != "recent_event"]

    kept_recent_events = 0
    out: list[MemoryEntry] = []
    for entry in entries:
        if entry.kind != "recent_event":
            out.append(entry)
            continue
        if kept_recent_events >= max_items:
            continue
        out.append(entry)
        kept_recent_events += 1
    return out


def _recent_event_keep_sort_key(memory: MemoryEntry) -> tuple[int, int, int, str, str]:
    protected = 1 if memory.pinned or memory.edit_state != "normal" or memory.source_type == "maintenance_compact" else 0
    return (
        protected,
        int(memory.score),
        int(memory.importance),
        str(memory.updated_at or ""),
        str(memory.created_at or ""),
    )


def _select_recent_event_prune_ids(memories: list[MemoryEntry], *, limit: int) -> set[str]:
    limit = max(0, int(limit))
    active = [memory for memory in memories if not memory.deleted_at]
    if limit <= 0:
        ranked = sorted(active, key=_recent_event_keep_sort_key, reverse=True)
    elif len(active) <= limit:
        return set()
    else:
        ranked = sorted(active, key=_recent_event_keep_sort_key, reverse=True)

    keep_ids = {memory.id for memory in ranked[:limit]}
    prunable = [
        memory
        for memory in active
        if not memory.pinned and memory.edit_state == "normal" and memory.source_type != "maintenance_compact"
    ]
    return {memory.id for memory in prunable if memory.id not in keep_ids}


def _collect_recent_event_prune_ids(
    memories: list[MemoryEntry],
    *,
    per_scope_limit: int,
    per_conversation_limit: int,
    per_thread_limit: int,
) -> set[str]:
    prune_ids = _select_recent_event_prune_ids(memories, limit=per_scope_limit)

    memories_by_conversation: dict[str, list[MemoryEntry]] = defaultdict(list)
    memories_by_thread: dict[str, list[MemoryEntry]] = defaultdict(list)
    for memory in memories:
        meta = memory.meta if isinstance(memory.meta, dict) else {}
        conversation_id = meta.get("conversation_id")
        thread_id = meta.get("thread_id")
        if isinstance(conversation_id, str) and conversation_id.strip():
            memories_by_conversation[conversation_id.strip()].append(memory)
        if isinstance(thread_id, str) and thread_id.strip():
            memories_by_thread[thread_id.strip()].append(memory)

    for group in memories_by_conversation.values():
        prune_ids.update(_select_recent_event_prune_ids(group, limit=per_conversation_limit))
    for group in memories_by_thread.values():
        prune_ids.update(_select_recent_event_prune_ids(group, limit=per_thread_limit))
    return prune_ids


def _select_memory_writer_model(*, runner: "TickRunner", actor_pc_id: str | None) -> str | None:
    if isinstance(runner._settings.openai_memory_model, str) and runner._settings.openai_memory_model.strip():  # noqa: SLF001
        return runner._settings.openai_memory_model  # noqa: SLF001
    if actor_pc_id:
        model = getattr(next((p for p in runner._engine.pcs if p.id == actor_pc_id), None), "model", None)  # noqa: SLF001
        if isinstance(model, str) and model.strip():
            return model
    if isinstance(runner._settings.openai_dm_model, str) and runner._settings.openai_dm_model.strip() and not actor_pc_id:  # noqa: SLF001
        return runner._settings.openai_dm_model  # noqa: SLF001
    if isinstance(runner._settings.openai_model, str) and runner._settings.openai_model.strip():  # noqa: SLF001
        return runner._settings.openai_model  # noqa: SLF001
    return None


def _memory_scope_for_message(
    *, runner: "TickRunner", message: Message, actor_pc_id: str | None, kind: str
) -> tuple[str, str | None, str | None] | None:
    if kind in {"autobiography", "relationship", "secret"}:
        if not actor_pc_id:
            return None
        return "pc", None, actor_pc_id

    if message.channel == "broadcast":
        return "public", None, None
    if message.channel == "direct":
        if message.from_actor.kind == "pc" and isinstance(message.from_actor.id, str) and message.from_actor.id.strip():
            from_pc_id = message.from_actor.id.strip()
            target_pc = next((actor.id for actor in (message.to or []) if actor.kind == "pc" and actor.id), None)
            if isinstance(target_pc, str) and target_pc.strip():
                return "direct", runner._direct_memory_scope_id(pc_id=from_pc_id, peer_kind="pc", peer_id=target_pc.strip()), None  # noqa: SLF001
            if any(actor.kind == "dm" for actor in (message.to or [])):
                return "direct", runner._direct_memory_scope_id(pc_id=from_pc_id, peer_kind="dm", peer_id="dm"), None  # noqa: SLF001

        if message.from_actor.kind == "dm":
            target_pc = next((actor.id for actor in (message.to or []) if actor.kind == "pc" and actor.id), None)
            if isinstance(target_pc, str) and target_pc.strip():
                return "direct", runner._direct_memory_scope_id(pc_id=target_pc.strip(), peer_kind="dm", peer_id="dm"), None  # noqa: SLF001

        return "direct", message.conversation_id, None
    return None


def _normalize_memory_upsert(
    *,
    runner: "TickRunner",
    item: Any,
    message: Message,
    action_type: str,
    actor_pc_id: str | None,
    actor_name: str,
    thread: ForumThread | None,
) -> MemoryEntry | None:
    if not isinstance(item, dict):
        return None

    raw_kind = runner._clean_str(item.get("kind"))  # noqa: SLF001
    if raw_kind not in _MEMORY_ALLOWED_KINDS:
        return None

    scope_data = _memory_scope_for_message(runner=runner, message=message, actor_pc_id=actor_pc_id, kind=raw_kind)
    if scope_data is None:
        return None
    scope, scope_id, owner_pc_id = scope_data

    summary = _trim_memory_text(
        str(item.get("summary") or ""),
        max_len=max(1, int(runner._settings.memory_write_summary_chars)),  # noqa: SLF001
    )
    content = _trim_memory_text(
        str(item.get("content") or ""),
        max_len=max(1, int(runner._settings.memory_write_content_chars)),  # noqa: SLF001
    )
    if not summary or not content:
        return None

    importance_raw = item.get("importance")
    importance = int(importance_raw) if isinstance(importance_raw, (int, float)) else 0
    importance = max(0, min(10, importance))

    subject_type = runner._clean_str(item.get("subject_type"))  # noqa: SLF001
    subject_id = runner._clean_str(item.get("subject_id"))  # noqa: SLF001
    merge_key = runner._clean_str(item.get("merge_key"))  # noqa: SLF001
    source_type = runner._clean_str(item.get("source_type")) or "llm_write"  # noqa: SLF001

    source_ref_id = message.send_batch_id or message.id
    thread_title = thread.title if isinstance(thread, ForumThread) else None
    source_excerpt = _trim_memory_text(
        message.content,
        max_len=max(1, int(runner._settings.memory_write_source_excerpt_chars)),  # noqa: SLF001
    )
    keywords_raw = item.get("keywords")
    keywords: list[str] = []
    if isinstance(keywords_raw, list):
        for value in keywords_raw:
            if isinstance(value, str) and value.strip():
                keywords.append(value.strip())
    if not keywords:
        keywords = _extract_memory_keywords(runner, summary, content, thread_title or "", source_excerpt)
    if not merge_key and raw_kind in {"autobiography", "relationship", "secret"}:
        merge_parts = [raw_kind]
        if subject_type:
            merge_parts.append(subject_type)
        if subject_id:
            merge_parts.append(subject_id)
        merge_parts.extend(keywords[:3] if keywords else [summary])
        merge_key = "_".join(part.strip().casefold().replace(" ", "_") for part in merge_parts if part and part.strip())
        merge_key = merge_key[:120] if merge_key else None
    if not merge_key and raw_kind == "recent_event":
        merge_key = _build_recent_event_merge_key(
            scope=scope,
            scope_id=scope_id,
            owner_pc_id=owner_pc_id,
            conversation_id=message.conversation_id,
            thread_id=message.thread_id,
            subject_type=subject_type,
            subject_id=subject_id,
            keywords=keywords,
        )

    meta = {
        "ref_type": "message",
        "ref_id": source_ref_id,
        "message_id": message.id,
        "conversation_id": message.conversation_id,
        "send_batch_id": message.send_batch_id,
        "thread_id": message.thread_id,
        "channel": message.channel,
        "channel_id": message.conversation_id if message.channel == "broadcast" else None,
        "thread_title": thread_title,
        "action_type": action_type,
        "actor_name": actor_name,
        "merge_key": merge_key,
        "keywords": keywords,
        "source_excerpt": source_excerpt,
    }
    stable_ref = merge_key or source_ref_id
    stable_key = "|".join(
        [
            scope,
            scope_id or "",
            owner_pc_id or "",
            raw_kind,
            subject_type or "",
            subject_id or "",
            stable_ref,
            "" if merge_key else summary.casefold(),
        ]
    )
    return MemoryEntry(
        id=str(uuid5(NAMESPACE_URL, stable_key)),
        scope=scope,
        scope_id=scope_id,
        owner_pc_id=owner_pc_id,
        kind=raw_kind,
        content=content,
        summary=summary,
        subject_type=subject_type,
        subject_id=subject_id,
        importance=importance,
        score=importance,
        source_type=source_type,
        meta=meta,
    )


async def _soft_delete_memories_for_retention(
    *,
    runner: "TickRunner",
    memory_ids: set[str],
    actor_pc_id: str | None,
    reason: str,
) -> int:
    ids = [memory_id for memory_id in memory_ids if isinstance(memory_id, str) and memory_id.strip()]
    if not ids:
        return 0

    deleted = 0
    deleted_ids: list[str] = []
    for memory_id in ids:
        memory = await runner._store.get_memory(memory_id)  # noqa: SLF001
        if memory is None or memory.deleted_at or memory.kind != "recent_event":
            continue
        if memory.pinned or memory.edit_state != "normal":
            continue
        memory.deleted_at = utc_now_iso()
        memory.edit_state = "deleted"
        memory.updated_at = memory.deleted_at
        memory.source_type = "maintenance"
        memory.revision += 1
        meta = dict(memory.meta or {})
        meta["delete_reason"] = reason
        meta["deleted_by"] = "recent_event_retention"
        memory.meta = meta
        await runner._store.upsert_memory(memory)  # noqa: SLF001
        deleted += 1
        deleted_ids.append(memory_id)

    if deleted <= 0:
        return 0

    try:
        await runner._store.delete_memory_summary_embeddings(memory_ids=deleted_ids)  # noqa: SLF001
    except Exception:
        pass

    await add_private_event_safely(
        runner=runner,
        pc_id=actor_pc_id,
        type="memory_recent_event_retention",
        summary=f"recent_event retention pruned {deleted} memories",
        consequences={"deleted_ids": deleted_ids, "reason": reason, "deleted": deleted},
    )
    return deleted


async def _enforce_recent_event_retention(
    *,
    runner: "TickRunner",
    entries: list[MemoryEntry],
    actor_pc_id: str | None,
) -> None:
    recent_entries = [entry for entry in entries if entry.kind == "recent_event" and not entry.deleted_at]
    if not recent_entries:
        return

    per_scope_limit = max(1, int(getattr(runner._settings, "memory_recent_event_max_per_scope", 12)))  # noqa: SLF001
    per_conversation_limit = max(
        1, int(getattr(runner._settings, "memory_recent_event_max_per_conversation", 8))
    )  # noqa: SLF001
    per_thread_limit = max(1, int(getattr(runner._settings, "memory_recent_event_max_per_thread", 6)))  # noqa: SLF001

    seen_scope_keys: set[tuple[str, str | None, str | None]] = set()
    for entry in recent_entries:
        scope_key = (entry.scope, entry.owner_pc_id, entry.scope_id)
        if scope_key in seen_scope_keys:
            continue
        seen_scope_keys.add(scope_key)

        memories = await runner._store.list_memories(  # noqa: SLF001
            scope=entry.scope,
            owner_pc_id=entry.owner_pc_id,
            scope_id=entry.scope_id,
            kind="recent_event",
            limit=1000,
        )
        prune_ids = _collect_recent_event_prune_ids(
            memories,
            per_scope_limit=per_scope_limit,
            per_conversation_limit=per_conversation_limit,
            per_thread_limit=per_thread_limit,
        )
        if not prune_ids:
            continue
        await _soft_delete_memories_for_retention(
            runner=runner,
            memory_ids=prune_ids,
            actor_pc_id=actor_pc_id,
            reason="recent_event_retention_limit",
        )


def _deterministic_memory_write_upserts(*, runner: "TickRunner", message: Message, actor_name: str, thread: ForumThread | None) -> list[dict[str, Any]]:
    excerpt = _trim_memory_text(
        message.content,
        max_len=max(1, int(runner._settings.memory_write_content_chars)),  # noqa: SLF001
    )
    if not excerpt:
        return []

    thread_title = thread.title if isinstance(thread, ForumThread) else None
    if message.channel == "broadcast":
        if thread_title:
            summary = f"《{thread_title}》新增发言"
            content = f"{actor_name}在《{thread_title}》提到：{excerpt}"
        else:
            summary = f"{actor_name}发布共享信息"
            content = f"{actor_name}说：{excerpt}"
        keywords = _extract_memory_keywords(runner, thread_title or "", actor_name, excerpt)
        return [
            {
                "kind": "recent_event",
                "summary": summary,
                "content": content,
                "importance": 1,
                "source_type": "deterministic_write",
                "keywords": keywords,
            }
        ]

    target_pc = next((actor for actor in (message.to or []) if actor.kind == "pc" and actor.id), None)
    target_name = (target_pc.name if target_pc and target_pc.name else None) or "对方"
    keywords = _extract_memory_keywords(runner, actor_name, target_name, excerpt)
    entry: dict[str, Any] = {
        "kind": "recent_event",
        "summary": f"{actor_name}与{target_name}有新的私聊",
        "content": f"{actor_name}对{target_name}说：{excerpt}",
        "importance": 1,
        "source_type": "deterministic_write",
        "keywords": keywords,
    }
    if target_pc and target_pc.id:
        entry["subject_type"] = "pc"
        entry["subject_id"] = target_pc.id
    return [entry]


def _pack_existing_memory_for_write(memory: MemoryEntry) -> dict[str, Any]:
    merge_key = None
    if isinstance(memory.meta, dict):
        raw_merge_key = memory.meta.get("merge_key")
        if isinstance(raw_merge_key, str) and raw_merge_key.strip():
            merge_key = raw_merge_key.strip()
    return {
        "id": memory.id,
        "scope": memory.scope,
        "kind": memory.kind,
        "owner_pc_id": memory.owner_pc_id,
        "scope_id": memory.scope_id,
        "summary": memory.summary,
        "content": memory.content,
        "subject_type": memory.subject_type,
        "subject_id": memory.subject_id,
        "score": memory.score,
        "pinned": memory.pinned,
        "edit_state": memory.edit_state,
        "source_type": memory.source_type,
        "deleted_at": memory.deleted_at,
        "merge_key": merge_key,
    }


async def _list_existing_memories_for_write(
    *,
    runner: "TickRunner",
    actor_pc_id: str | None,
    actor_name: str,
    message: Message,
    thread: ForumThread | None,
) -> list[MemoryEntry]:
    thread_title = thread.title if isinstance(thread, ForumThread) else ""
    keywords = _extract_memory_keywords(runner, actor_name, thread_title, message.content)
    direct_scope_id: str | None = None
    if message.channel == "direct":
        scope_data = _memory_scope_for_message(runner=runner, message=message, actor_pc_id=actor_pc_id, kind="recent_event")
        if scope_data is not None:
            _, direct_scope_id, _ = scope_data

    memories = await runner._store.search_memories(  # noqa: SLF001
        keywords=keywords,
        owner_pc_id=actor_pc_id,
        direct_scope_id=direct_scope_id,
        limit=max(1, int(runner._settings.memory_write_existing_max_items)),  # noqa: SLF001
    )
    if not memories and actor_pc_id:
        memories = await runner._store.list_memories(  # noqa: SLF001
            scope="pc",
            owner_pc_id=actor_pc_id,
            limit=max(1, int(runner._settings.memory_write_existing_max_items)),  # noqa: SLF001
        )
    max_items = max(1, int(runner._settings.memory_write_existing_max_items))  # noqa: SLF001
    return memories[:max_items]


async def _list_public_memories_for_write(
    *,
    runner: "TickRunner",
    actor_name: str,
    message: Message,
    thread: ForumThread | None,
) -> list[MemoryEntry]:
    thread_title = thread.title if isinstance(thread, ForumThread) else ""
    keywords = _extract_memory_keywords(runner, actor_name, thread_title, message.content)
    max_items = max(1, int(runner._settings.memory_write_existing_max_items))  # noqa: SLF001

    memories = await runner._store.search_memories(  # noqa: SLF001
        keywords=keywords,
        include_public=True,
        limit=max_items,
    )
    if not memories:
        memories = await runner._store.list_memories(  # noqa: SLF001
            scope="public",
            limit=max_items,
        )
    return memories[:max_items]


def _pack_memories_for_write(memories: list[MemoryEntry]) -> list[dict[str, Any]]:
    return [_pack_existing_memory_for_write(memory) for memory in memories]


def _extract_structured_writer_payload(*, runner: "TickRunner", response: Any) -> dict[str, Any] | None:
    parsed = response.get("parsed") if isinstance(response, dict) else None
    structured: Any | None = None
    if isinstance(parsed, dict) and parsed.get("kind") == "structured":
        structured = parsed.get("structured")
    elif isinstance(parsed, dict) and parsed.get("kind") == "markdown":
        markdown = parsed.get("markdown")
        if isinstance(markdown, str):
            structured = runner._try_parse_json_loose(markdown)  # noqa: SLF001

    if structured is None:
        raw = response.get("raw") if isinstance(response, dict) else None
        parsed_raw = parse_llm_response(raw)
        if parsed_raw["kind"] == "structured":
            structured = parsed_raw.get("structured")
        elif parsed_raw["kind"] == "markdown" and isinstance(parsed_raw.get("markdown"), str):
            structured = runner._try_parse_json_loose(parsed_raw["markdown"])  # noqa: SLF001

    return structured if isinstance(structured, dict) else None


async def _llm_memory_write_plan(
    *,
    runner: "TickRunner",
    actor_pc_id: str | None,
    actor_name: str,
    action_type: str,
    message: Message,
    thread: ForumThread | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, MemoryEntry]]:
    max_items = max(1, int(runner._settings.memory_write_max_items))  # noqa: SLF001
    if runner._settings.demo_fake or runner._llm is None:  # noqa: SLF001
        return _deterministic_memory_write_upserts(runner=runner, message=message, actor_name=actor_name, thread=thread), [], {}
    if not (runner._settings.openai_base_url and runner._settings.openai_api_key):  # noqa: SLF001
        return _deterministic_memory_write_upserts(runner=runner, message=message, actor_name=actor_name, thread=thread), [], {}

    model = _select_memory_writer_model(runner=runner, actor_pc_id=actor_pc_id)
    if not model:
        return _deterministic_memory_write_upserts(runner=runner, message=message, actor_name=actor_name, thread=thread), [], {}

    scope_hint = "public + pc" if message.channel == "broadcast" else "direct + pc"
    thread_payload: dict[str, Any] | None = None
    if isinstance(thread, ForumThread):
        thread_payload = {"id": thread.id, "channel_id": thread.channel_id, "title": thread.title}

    actor_memories = await _list_existing_memories_for_write(
        runner=runner,
        actor_pc_id=actor_pc_id,
        actor_name=actor_name,
        message=message,
        thread=thread,
    )
    public_memories = await _list_public_memories_for_write(
        runner=runner,
        actor_name=actor_name,
        message=message,
        thread=thread,
    )
    candidate_memories: dict[str, MemoryEntry] = {}
    for memory in [*actor_memories, *public_memories]:
        candidate_memories[memory.id] = memory

    messages = render_prompt_messages(
        "tick_runner.memory_write",
        {
            "max_items": str(max_items),
            "summary_max_chars": str(max(1, int(runner._settings.memory_write_summary_chars))),  # noqa: SLF001
            "content_max_chars": str(max(1, int(runner._settings.memory_write_content_chars))),  # noqa: SLF001
            "pcs_personas": runner._format_pcs_personas_for_prompt(),  # noqa: SLF001
            "action_type": action_type,
            "scope_hint": scope_hint,
            "actor_name": actor_name,
            "message_json": json.dumps(message.model_dump(), ensure_ascii=False),
            "thread_json": json.dumps(thread_payload, ensure_ascii=False),
            "existing_memories_json": json.dumps(_pack_memories_for_write(actor_memories), ensure_ascii=False),
            "public_memories_json": json.dumps(_pack_memories_for_write(public_memories), ensure_ascii=False),
        },
    )
    url = openai_chat_completions_url(runner._settings.openai_base_url)  # noqa: SLF001
    res = await runner._llm.chat(  # noqa: SLF001
        url=url,
        apikey=runner._settings.openai_api_key,  # noqa: SLF001
        model=model,
        messages=messages,
        tools=None,
    )
    structured = _extract_structured_writer_payload(runner=runner, response=res)
    if structured is None:
        return _deterministic_memory_write_upserts(runner=runner, message=message, actor_name=actor_name, thread=thread), [], {}

    return (
        maintenance_upserts_from_payload(structured, max_items=max_items),
        parse_maintenance_ops(
            structured,
            max_ops=max(0, int(getattr(runner._settings, "memory_maintenance_max_ops", max_items))),
        ),
        candidate_memories,
    )


async def _apply_maintenance_ops(
    *,
    runner: "TickRunner",
    ops: list[dict[str, Any]],
    candidate_memories: dict[str, MemoryEntry],
    actor_pc_id: str | None,
) -> tuple[list[MemoryEntry], set[str], list[tuple[str, str | None, str | None]]]:
    if not bool(getattr(runner._settings, "memory_maintenance_enabled", True)):  # noqa: SLF001
        return [], set(), []
    if not ops or not candidate_memories:
        return [], set(), []

    summary_max_chars = max(1, int(runner._settings.memory_write_summary_chars))  # noqa: SLF001
    content_max_chars = max(1, int(runner._settings.memory_write_content_chars))  # noqa: SLF001
    touched_ids: set[str] = set()
    changed: list[MemoryEntry] = []
    deleted_ids: set[str] = set()
    recent_scope_filters: set[tuple[str, str | None, str | None]] = set()

    for op in ops:
        op_type = str(op.get("type") or "").strip()
        reason = str(op.get("reason") or "").strip() or None

        if op_type == "rewrite":
            target_id = str(op.get("target_id") or "").strip()
            item = op.get("memory")
            existing = candidate_memories.get(target_id)
            if not target_id or target_id in touched_ids or existing is None:
                continue
            if not isinstance(item, dict) or not can_apply_maintenance_op(existing, op_type="rewrite"):
                continue
            rewritten = build_maintenance_rewrite(
                existing=existing,
                item=item,
                source_type="llm_maintenance",
                reason=reason,
                summary_max_chars=summary_max_chars,
                content_max_chars=content_max_chars,
            )
            if rewritten is None:
                continue
            touched_ids.add(target_id)
            candidate_memories[target_id] = rewritten
            changed.append(rewritten)
            if rewritten.kind == "recent_event" and not rewritten.deleted_at:
                recent_scope_filters.add((rewritten.scope, rewritten.owner_pc_id, rewritten.scope_id))
            continue

        if op_type == "delete":
            target_id = str(op.get("target_id") or "").strip()
            existing = candidate_memories.get(target_id)
            if not target_id or target_id in touched_ids or existing is None:
                continue
            if not can_apply_maintenance_op(existing, op_type="delete"):
                continue
            deleted = build_maintenance_delete(
                existing=existing,
                source_type="llm_maintenance",
                reason=reason,
            )
            touched_ids.add(target_id)
            candidate_memories[target_id] = deleted
            changed.append(deleted)
            deleted_ids.add(target_id)
            continue

        if op_type == "merge":
            raw_target_ids = op.get("target_ids")
            item = op.get("memory")
            if not isinstance(raw_target_ids, list) or not isinstance(item, dict):
                continue
            target_ids: list[str] = []
            for value in raw_target_ids:
                target_id = str(value or "").strip()
                if target_id and target_id not in target_ids:
                    target_ids.append(target_id)
            if len(target_ids) < 2 or any(target_id in touched_ids for target_id in target_ids):
                continue

            targets = [candidate_memories.get(target_id) for target_id in target_ids]
            if any(target is None for target in targets):
                continue
            memories = [target for target in targets if target is not None]
            primary = memories[0]
            if not all(can_apply_maintenance_op(memory, op_type="merge") for memory in memories):
                continue
            if not all(
                memory.scope == primary.scope
                and memory.scope_id == primary.scope_id
                and memory.owner_pc_id == primary.owner_pc_id
                and memory.kind == primary.kind
                and memory.subject_id == primary.subject_id
                and memory.subject_type == primary.subject_type
                for memory in memories
            ):
                continue
            rewritten = build_maintenance_rewrite(
                existing=primary,
                item=item,
                source_type="llm_maintenance",
                reason=reason,
                summary_max_chars=summary_max_chars,
                content_max_chars=content_max_chars,
            )
            if rewritten is None:
                continue
            merged = build_maintenance_merge_target(
                primary=primary,
                rewritten=rewritten,
                merged_source_ids=target_ids[1:],
                reason=reason,
            )
            changed.append(merged)
            candidate_memories[primary.id] = merged
            touched_ids.add(primary.id)
            recent_scope_filters.add((merged.scope, merged.owner_pc_id, merged.scope_id))
            for source in memories[1:]:
                deleted = build_maintenance_delete(
                    existing=source,
                    source_type="llm_maintenance",
                    reason=reason or "merge",
                    source_memory_id=merged.id,
                )
                changed.append(deleted)
                candidate_memories[source.id] = deleted
                touched_ids.add(source.id)
                deleted_ids.add(source.id)

    if not changed:
        return [], set(), []

    await runner._store.upsert_memories(changed)  # noqa: SLF001
    if deleted_ids:
        try:
            await runner._store.delete_memory_summary_embeddings(memory_ids=list(deleted_ids))  # noqa: SLF001
        except Exception:
            pass
    active_changed = [memory for memory in changed if not memory.deleted_at]
    if active_changed:
        await maybe_upsert_memory_summary_embeddings(runner=runner, memories=active_changed)

    await add_private_event_safely(
        runner=runner,
        pc_id=actor_pc_id,
        type="memory_maintenance_ops",
        summary=f"memory maintenance applied: changed={len(changed)}, deleted={len(deleted_ids)}",
        consequences={
            "changed_ids": [memory.id for memory in changed],
            "deleted_ids": list(deleted_ids),
            "ops_count": len(ops),
        },
    )
    return changed, deleted_ids, list(recent_scope_filters)


async def write_memories_for_message(
    runner: "TickRunner",
    *,
    action_type: str,
    actor_pc_id: str | None,
    actor_name: str,
    message: Message,
    thread: ForumThread | None = None,
) -> None:
    try:
        raw_upserts, maintenance_ops, candidate_memories = await _llm_memory_write_plan(
            runner=runner,
            actor_pc_id=actor_pc_id,
            actor_name=actor_name,
            action_type=action_type,
            message=message,
            thread=thread,
        )
        maintenance_scope_filters: list[tuple[str, str | None, str | None]] = []
        if maintenance_ops:
            _changed, _deleted_ids, maintenance_scope_filters = await _apply_maintenance_ops(
                runner=runner,
                ops=maintenance_ops,
                candidate_memories=candidate_memories,
                actor_pc_id=actor_pc_id,
            )
        entries: list[MemoryEntry] = []
        for item in raw_upserts[: max(1, int(runner._settings.memory_write_max_items))]:  # noqa: SLF001
            entry = _normalize_memory_upsert(
                runner=runner,
                item=item,
                message=message,
                action_type=action_type,
                actor_pc_id=actor_pc_id,
                actor_name=actor_name,
                thread=thread,
            )
            if entry is not None:
                entries.append(entry)
        if entries:
            entries = _limit_recent_event_entries(
                entries=entries,
                max_items=max(0, int(getattr(runner._settings, "memory_write_recent_event_max_items", 1))),  # noqa: SLF001
            )
            entries, precomputed = await dedup_merge_memories_on_write(runner=runner, entries=entries)
            await runner._store.upsert_memories(entries)  # noqa: SLF001
            if precomputed:
                try:
                    await runner._store.upsert_memory_summary_embeddings(  # noqa: SLF001
                        model=(getattr(runner._settings, "openai_embedding_model", None) or "").strip(),  # noqa: SLF001
                        items=precomputed,
                        updated_at=utc_now_iso(),
                    )
                except Exception:
                    pass
            await maybe_upsert_memory_summary_embeddings(runner=runner, memories=entries)
            recent_scope_filters = list(
                {
                    (entry.scope, entry.owner_pc_id, entry.scope_id)
                    for entry in entries
                    if entry.kind == "recent_event" and not entry.deleted_at
                }
            )
            recent_scope_filters.extend(maintenance_scope_filters)
            if recent_scope_filters:
                await compact_recent_events(
                    runner=runner,
                    actor_pc_id=actor_pc_id,
                    scope_filters=list(dict.fromkeys(recent_scope_filters)),
                )
            await _enforce_recent_event_retention(runner=runner, entries=entries, actor_pc_id=actor_pc_id)
        elif maintenance_scope_filters:
            await compact_recent_events(
                runner=runner,
                actor_pc_id=actor_pc_id,
                scope_filters=list(dict.fromkeys(maintenance_scope_filters)),
            )
    except Exception as exc:
        await add_private_event_safely(
            runner=runner,
            pc_id=actor_pc_id,
            type="memory_write_error",
            summary=f"memory write failed for {action_type}: {type(exc).__name__}",
            consequences={
                "action_type": action_type,
                "actor_pc_id": actor_pc_id,
                "actor_name": actor_name,
                "message_id": message.id,
                "conversation_id": message.conversation_id,
                "thread_id": message.thread_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return
