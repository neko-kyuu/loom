from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from pc_config.prompts import render_prompt_messages

from .actions import (
    ActionValidationContext,
    CreateThreadAction,
    DmAction,
    NoopAction,
    ReplyAction,
    validate_action,
)
from .db import SqliteStore
from .doc_search import DocSearchService
from .engine import DemoEngine
from .llm import LlmService, openai_chat_completions_url, openai_embeddings_url, parse_llm_response
from .models import Actor, Event, ForumThread, MemoryEntry, Message, PcActivity, TickRecord, utc_now_iso
from .settings import Settings
from .v4_executor import ToolCallLimits, run_tool_calling_loop
from .ws import ConnectionManager


@dataclass
class TickRunnerState:
    next_pc_index: int = 0
    turn_no: int = 0
    last_turn_by_pc: dict[str, str] | None = None


class TickRunner:
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

    def __init__(
        self,
        *,
        store: SqliteStore,
        ws: ConnectionManager,
        engine: DemoEngine,
        settings: Settings,
        llm: LlmService | None = None,
        doc_search: DocSearchService | None = None,
        tick_s: float = 60.0,
        dm_digest_s: float = 600.0,
        dm_bootstrap_lookback_s: float = 600.0,
        state_key: str = "tick_runner_state",
    ) -> None:
        self._store = store
        self._ws = ws
        self._engine = engine
        self._settings = settings
        self._llm = llm
        self._doc_search = doc_search
        self._tick_s = tick_s
        self._dm_digest_s = dm_digest_s
        self._dm_bootstrap_lookback_s = dm_bootstrap_lookback_s
        self._state_key = state_key

        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_forever(), name="tick-runner")

    async def _load_state(self) -> TickRunnerState:
        raw = await self._store.get_setting_json(self._state_key)
        if not raw:
            return TickRunnerState()
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return TickRunnerState()
        if not isinstance(data, dict):
            return TickRunnerState()
        idx = data.get("next_pc_index")
        turn_no = data.get("turn_no")
        last_turn_by_pc = data.get("last_turn_by_pc")
        out = TickRunnerState()
        if isinstance(idx, int) and idx >= 0:
            out.next_pc_index = idx
        if isinstance(turn_no, int) and turn_no >= 0:
            out.turn_no = turn_no
        if isinstance(last_turn_by_pc, dict):
            cleaned: dict[str, str] = {}
            for k, v in last_turn_by_pc.items():
                if not isinstance(k, str) or not k.strip():
                    continue
                if not isinstance(v, str) or not v.strip():
                    continue
                cleaned[k.strip()] = v.strip()
            out.last_turn_by_pc = cleaned
        return out

    async def _save_state(self, state: TickRunnerState) -> None:
        last_turn_by_pc = state.last_turn_by_pc or {}
        await self._store.set_setting_json(
            self._state_key,
            json.dumps(
                {
                    "next_pc_index": state.next_pc_index,
                    "turn_no": state.turn_no,
                    "last_turn_by_pc": last_turn_by_pc,
                },
                ensure_ascii=False,
            ),
        )

    async def _run_forever(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                # Keep ticking even if a turn fails.
                pass
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, self._tick_s - elapsed))

    async def run_once(self) -> None:
        if self._engine.is_paused():
            return
        if not self._engine.pcs:
            return

        async with self._lock:
            if self._engine.is_paused():
                return

            state = await self._load_state()
            pc = self._engine.pcs[state.next_pc_index % len(self._engine.pcs)]
            since = (state.last_turn_by_pc or {}).get(pc.id)
            state.next_pc_index = (state.next_pc_index + 1) % max(1, len(self._engine.pcs))
            state.turn_no += 1
            await self._save_state(state)

            tick = TickRecord(pc_id=pc.id, status="running", action={})
            await self._store.upsert_tick(tick)

            t0 = time.monotonic()
            refs: list[dict[str, Any]] = []
            try:
                if self._settings.demo_fake:
                    raw_action = await self._deterministic_action(pc_id=pc.id, turn_no=state.turn_no)
                else:
                    raw_action = await self._llm_action(pc_id=pc.id, pc_name=pc.name, persona=pc.persona, since=since)
                raw_for_validation: Any = raw_action
                if isinstance(raw_action, dict) and isinstance(raw_action.get("action"), dict):
                    raw_for_validation = raw_action["action"]
                ctx = await self._build_action_context()
                tick.action = raw_action if isinstance(raw_action, dict) else {"_raw": str(raw_action)}
                action, errors = validate_action(raw_for_validation, ctx=ctx)
                if errors:
                    tick.error = "; ".join(errors)
                await self._store.upsert_tick(tick)
                refs = await self._apply_action(pc_id=pc.id, action=action)
            except Exception as exc:  # noqa: BLE001
                tick.status = "failed"
                tick.error = f"{type(exc).__name__}: {exc}"
                tick.duration_ms = int((time.monotonic() - t0) * 1000)
                await self._store.upsert_tick(tick)
                raise
            else:
                tick.status = "done"
                tick.result_refs = refs
                tick.duration_ms = int((time.monotonic() - t0) * 1000)
                await self._store.upsert_tick(tick)
                if state.last_turn_by_pc is None:
                    state.last_turn_by_pc = {}
                state.last_turn_by_pc[pc.id] = tick.started_at
                await self._save_state(state)

            await self._maybe_run_memory_decay(turn_no=state.turn_no)
            await self._run_dm_digest_if_due()

    async def _maybe_run_memory_decay(self, *, turn_no: int) -> None:
        interval = max(0, int(self._settings.memory_decay_interval_ticks))
        if interval <= 0 or turn_no <= 0 or (turn_no % interval) != 0:
            return

        stats = await self._store.decay_memories(
            k=self._settings.memory_decay_k,
            threshold=self._settings.memory_decay_threshold,
        )
        await self._store.add_event(
            Event(
                pc_id=None,
                type="memory_decay",
                summary=(
                    f"memory decay at turn {turn_no}: decayed={stats['decayed']}, "
                    f"deleted={stats['deleted']}, remaining={stats['remaining']}"
                ),
                visibility="private",
                consequences={"turn_no": turn_no, **stats},
            )
        )

    @staticmethod
    def _parse_iso_utc_loose(s: str) -> datetime | None:
        try:
            dt = datetime.fromisoformat(s)
        except Exception:  # noqa: BLE001
            return None
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc)

    async def _build_action_context(self) -> ActionValidationContext:
        convs = await self._store.list_conversations()
        forum_channel_ids = {c.id for c in convs if c.kind == "forum"}
        pc_ids = {p.id for p in self._engine.pcs}
        thread_channel_by_id: dict[str, str] = {}
        for cid in forum_channel_ids:
            for t in await self._store.list_forum_threads(cid):
                thread_channel_by_id[t.id] = t.channel_id
        return ActionValidationContext(
            forum_channel_ids=forum_channel_ids,
            pc_ids=pc_ids,
            thread_channel_by_id=thread_channel_by_id,
        )

    @staticmethod
    def _clean_str(v: Any) -> str | None:
        if not isinstance(v, str):
            return None
        s = v.strip()
        return s if s else None

    def _pack_dm_message(self, m: Message, *, max_chars: int = 800) -> dict[str, object]:
        max_len = max(0, int(max_chars))
        full = (m.content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        content_len = len(full)
        truncated = content_len > max_len
        content = (full[:max_len] + "…") if truncated else full
        next_start_char: int | None = max_len if truncated else None

        return {
            "id": m.id,
            "timestamp": m.timestamp,
            "from": (m.from_actor.name or m.from_actor.kind),
            "to": [a.name or a.kind for a in (m.to or [])],
            "content": content,
            "content_len": content_len,
            "content_truncated": truncated,
            "start_char": 0,
            "max_chars": max_len,
            "next_start_char": next_start_char,
        }

    @staticmethod
    def _direct_memory_scope_id(*, pc_id: str, peer_kind: str, peer_id: str) -> str:
        if peer_kind == "dm":
            return f"dm_to_{pc_id}"
        if peer_kind == "pc":
            left, right = sorted([pc_id, peer_id])
            return f"pc_pair:{left}:{right}"
        return f"direct:{pc_id}:{peer_kind}:{peer_id}"

    async def _build_dm_peer_context(
        self,
        *,
        pc_id: str,
        peer_kind: str,
        peer_id: str,
        recent_n: int = 24,
        max_chars_per_message: int = 800,
    ) -> dict[str, object]:
        conv_id = f"dm_to_{pc_id}"
        msgs = await self._store.list_messages(conv_id, limit=240)

        def involves_peer(m: Message) -> bool:
            if peer_kind == "dm":
                if m.from_actor.kind == "dm":
                    return True
                return any(a.kind == "dm" for a in (m.to or []))
            if m.from_actor.kind == "pc" and m.from_actor.id == peer_id:
                return True
            return any(a.kind == "pc" and a.id == peer_id for a in (m.to or []))

        filtered = [m for m in msgs if involves_peer(m)]
        tail = filtered[-max(0, int(recent_n)) :]

        if peer_kind == "dm":
            peer_name = "DM"
        else:
            peer = next((p for p in self._engine.pcs if p.id == peer_id), None)
            peer_name = (peer.name if peer else peer_id) or peer_id

        scope_id = self._direct_memory_scope_id(pc_id=pc_id, peer_kind=peer_kind, peer_id=peer_id)

        return {
            "peer": {"kind": peer_kind, "id": peer_id, "name": peer_name, "scope_id": scope_id},
            "scope_id": scope_id,
            "recent_messages": [self._pack_dm_message(m, max_chars=max_chars_per_message) for m in tail],
        }

    @staticmethod
    def _summarize_message(m: Message, max_len: int = 200) -> str:
        who = m.from_actor.name or m.from_actor.kind
        content = (m.content or "").strip().replace("\r\n", "\n").replace("\r", "\n")
        if len(content) > max_len:
            content = content[:max_len] + "…"
        content = content.replace("\n", "\\n")
        return f"{who}: {content}"

    def _format_pcs_personas_for_prompt(self) -> str:
        if not self._engine.pcs:
            return ""
        blocks: list[str] = []
        for idx, p in enumerate(self._engine.pcs):
            name = (p.name or p.id or "").strip() or p.id
            persona = (p.persona or "").strip() or f"你是{name}。"
            letter = chr(ord("A") + idx) if 0 <= idx < 26 else str(idx + 1)
            blocks.append(f"{name}（{p.id}）:")
            blocks.append(persona)
            blocks.append("")
        return "\n".join(blocks).strip()

    @staticmethod
    def _trim_memory_text(text: str, *, max_len: int) -> str:
        cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(cleaned) > max_len:
            return cleaned[:max_len] + "…"
        return cleaned

    def _memory_vector_enabled(self) -> bool:
        if not bool(getattr(self._settings, "memory_vector_enabled", True)):
            return False
        if self._settings.demo_fake:
            return False
        if self._llm is None:
            return False
        if not (self._settings.openai_embedding_api_key or self._settings.openai_api_key):
            return False
        if not (self._settings.openai_embedding_url or self._settings.openai_base_url):
            return False
        model = getattr(self._settings, "openai_embedding_model", None)
        return isinstance(model, str) and model.strip() != ""

    def _embedding_api_key(self) -> str | None:
        if isinstance(self._settings.openai_embedding_api_key, str) and self._settings.openai_embedding_api_key.strip():
            return self._settings.openai_embedding_api_key.strip()
        if isinstance(self._settings.openai_api_key, str) and self._settings.openai_api_key.strip():
            return self._settings.openai_api_key.strip()
        return None

    def _embedding_url(self) -> str | None:
        if isinstance(self._settings.openai_embedding_url, str) and self._settings.openai_embedding_url.strip():
            return self._settings.openai_embedding_url.strip()
        if isinstance(self._settings.openai_base_url, str) and self._settings.openai_base_url.strip():
            return openai_embeddings_url(self._settings.openai_base_url)
        return None

    @staticmethod
    def _sha256_text(text: str) -> str:
        cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()

    async def _maybe_upsert_memory_summary_embeddings(self, memories: list[MemoryEntry]) -> None:
        if not self._memory_vector_enabled():
            return
        if not memories:
            return

        embed_secrets = bool(getattr(self._settings, "memory_vector_embed_secrets", False))
        model = (getattr(self._settings, "openai_embedding_model", None) or "").strip()
        if not model:
            return
        url = self._embedding_url()
        if not url:
            return
        apikey = self._embedding_api_key()
        if not apikey:
            return

        candidates: list[MemoryEntry] = []
        hashes: dict[str, str] = {}
        for memory in memories:
            if memory.deleted_at or memory.edit_state == "deleted":
                continue
            if memory.kind == "secret" and not embed_secrets:
                continue
            if not isinstance(memory.summary, str) or not memory.summary.strip():
                continue
            h = self._sha256_text(memory.summary)
            hashes[memory.id] = h
            candidates.append(memory)

        if not candidates:
            return

        try:
            existing = await self._store.get_memory_summary_embedding_hashes(
                memory_ids=[m.id for m in candidates],
                model=model,
            )
            to_embed = [m for m in candidates if existing.get(m.id) != hashes.get(m.id)]
            if not to_embed:
                return

            vectors = await self._llm.embeddings(
                url=url,
                apikey=apikey,
                model=model,
                inputs=[m.summary for m in to_embed],
                timeout_s=60.0,
            )
            now = utc_now_iso()
            await self._store.upsert_memory_summary_embeddings(
                model=model,
                items=[(m.id, hashes[m.id], vectors[i]) for i, m in enumerate(to_embed)],
                updated_at=now,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                await self._store.add_event(
                    Event(
                        type="memory_embedding_error",
                        summary=f"memory summary embedding failed: {type(exc).__name__}",
                        visibility="private",
                        consequences={"error": f"{type(exc).__name__}: {exc}"},
                    )
                )
            except Exception:
                pass

    def _format_memories_for_prompt(self, memories: list[MemoryEntry]) -> tuple[list[dict[str, Any]], list[str]]:
        max_items = max(1, int(self._settings.memory_recall_max_items))
        remaining = max(0, int(self._settings.memory_recall_budget_chars))
        packed: list[dict[str, Any]] = []
        used_ids: list[str] = []

        for memory in memories:
            if len(packed) >= max_items or remaining <= 0:
                break

            content = self._trim_memory_text(memory.content, max_len=320)
            summary = self._trim_memory_text(memory.summary, max_len=120)
            chosen_text = content
            source = "content"
            if len(chosen_text) > remaining:
                chosen_text = summary
                source = "summary"
            if len(chosen_text) > remaining:
                if remaining < 24:
                    break
                chosen_text = self._trim_memory_text(chosen_text, max_len=remaining)
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

    def _extract_memory_keywords(self, *parts: str) -> list[str]:
        limit = max(1, int(self._settings.memory_recall_max_keywords))
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
        for pc in self._engine.pcs:
            if pc.name and pc.name in combined:
                add_keyword(pc.name)

        weights: dict[str, int] = {}
        for match in self._MEMORY_TOKEN_PATTERN.findall(combined):
            token = match.strip()
            if not token:
                continue
            norm = token.casefold()
            if norm in self._MEMORY_STOPWORDS or norm.isdigit() or len(token) <= 1:
                continue
            weights[token] = weights.get(token, 0) + 1

        for token, _ in sorted(weights.items(), key=lambda item: (-item[1], -len(item[0]), item[0])):
            add_keyword(token)
            if len(keywords) >= limit:
                break

        return keywords[:limit]

    def _select_memory_writer_model(self, *, actor_pc_id: str | None) -> str | None:
        if isinstance(self._settings.openai_memory_model, str) and self._settings.openai_memory_model.strip():
            return self._settings.openai_memory_model
        if actor_pc_id:
            model = getattr(next((p for p in self._engine.pcs if p.id == actor_pc_id), None), "model", None)
            if isinstance(model, str) and model.strip():
                return model
        if isinstance(self._settings.openai_dm_model, str) and self._settings.openai_dm_model.strip() and not actor_pc_id:
            return self._settings.openai_dm_model
        if isinstance(self._settings.openai_model, str) and self._settings.openai_model.strip():
            return self._settings.openai_model
        return None

    def _memory_scope_for_message(
        self,
        *,
        message: Message,
        actor_pc_id: str | None,
        kind: str,
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
                    return "direct", self._direct_memory_scope_id(pc_id=from_pc_id, peer_kind="pc", peer_id=target_pc.strip()), None
                if any(actor.kind == "dm" for actor in (message.to or [])):
                    return "direct", self._direct_memory_scope_id(pc_id=from_pc_id, peer_kind="dm", peer_id="dm"), None

            if message.from_actor.kind == "dm":
                target_pc = next((actor.id for actor in (message.to or []) if actor.kind == "pc" and actor.id), None)
                if isinstance(target_pc, str) and target_pc.strip():
                    return "direct", self._direct_memory_scope_id(pc_id=target_pc.strip(), peer_kind="dm", peer_id="dm"), None

            return "direct", message.conversation_id, None
        return None

    def _normalize_memory_upsert(
        self,
        *,
        item: Any,
        message: Message,
        action_type: str,
        actor_pc_id: str | None,
        actor_name: str,
        thread: ForumThread | None,
    ) -> MemoryEntry | None:
        if not isinstance(item, dict):
            return None

        raw_kind = self._clean_str(item.get("kind"))
        if raw_kind not in self._MEMORY_ALLOWED_KINDS:
            return None

        scope_data = self._memory_scope_for_message(message=message, actor_pc_id=actor_pc_id, kind=raw_kind)
        if scope_data is None:
            return None
        scope, scope_id, owner_pc_id = scope_data

        summary = self._trim_memory_text(
            str(item.get("summary") or ""),
            max_len=max(1, int(self._settings.memory_write_summary_chars)),
        )
        content = self._trim_memory_text(
            str(item.get("content") or ""),
            max_len=max(1, int(self._settings.memory_write_content_chars)),
        )
        if not summary or not content:
            return None

        importance_raw = item.get("importance")
        importance = int(importance_raw) if isinstance(importance_raw, (int, float)) else 0
        importance = max(0, min(10, importance))

        subject_type = self._clean_str(item.get("subject_type"))
        subject_id = self._clean_str(item.get("subject_id"))
        merge_key = self._clean_str(item.get("merge_key"))
        source_type = self._clean_str(item.get("source_type")) or "llm_write"

        source_ref_id = message.send_batch_id or message.id
        thread_title = thread.title if isinstance(thread, ForumThread) else None
        source_excerpt = self._trim_memory_text(
            message.content,
            max_len=max(1, int(self._settings.memory_write_source_excerpt_chars)),
        )
        keywords_raw = item.get("keywords")
        keywords: list[str] = []
        if isinstance(keywords_raw, list):
            for value in keywords_raw:
                if isinstance(value, str) and value.strip():
                    keywords.append(value.strip())
        if not keywords:
            keywords = self._extract_memory_keywords(summary, content, thread_title or "", source_excerpt)
        if not merge_key and raw_kind in {"autobiography", "relationship", "secret"}:
            merge_parts = [raw_kind]
            if subject_type:
                merge_parts.append(subject_type)
            if subject_id:
                merge_parts.append(subject_id)
            merge_parts.extend(keywords[:3] if keywords else [summary])
            merge_key = "_".join(part.strip().casefold().replace(" ", "_") for part in merge_parts if part and part.strip())
            merge_key = merge_key[:120] if merge_key else None

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

    def _deterministic_memory_write_upserts(
        self,
        *,
        message: Message,
        actor_name: str,
        thread: ForumThread | None,
    ) -> list[dict[str, Any]]:
        excerpt = self._trim_memory_text(
            message.content,
            max_len=max(1, int(self._settings.memory_write_content_chars)),
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
            keywords = self._extract_memory_keywords(thread_title or "", actor_name, excerpt)
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
        keywords = self._extract_memory_keywords(actor_name, target_name, excerpt)
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

    @staticmethod
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
            "summary": memory.summary,
            "content": memory.content,
            "subject_type": memory.subject_type,
            "subject_id": memory.subject_id,
            "score": memory.score,
            "edit_state": memory.edit_state,
            "deleted_at": memory.deleted_at,
            "merge_key": merge_key,
        }

    async def _list_existing_memories_for_write(
        self,
        *,
        actor_pc_id: str | None,
        actor_name: str,
        message: Message,
        thread: ForumThread | None,
    ) -> list[dict[str, Any]]:
        thread_title = thread.title if isinstance(thread, ForumThread) else ""
        keywords = self._extract_memory_keywords(actor_name, thread_title, message.content)
        include_public = message.channel == "broadcast"
        direct_scope_id: str | None = None
        if message.channel == "direct":
            scope_data = self._memory_scope_for_message(message=message, actor_pc_id=actor_pc_id, kind="recent_event")
            if scope_data is not None:
                _, direct_scope_id, _ = scope_data

        memories = await self._store.search_memories(
            keywords=keywords,
            owner_pc_id=actor_pc_id,
            include_public=include_public,
            direct_scope_id=direct_scope_id,
            limit=max(1, int(self._settings.memory_write_existing_max_items)),
        )
        if not memories and actor_pc_id:
            memories = await self._store.list_memories(
                scope="pc",
                owner_pc_id=actor_pc_id,
                limit=max(1, int(self._settings.memory_write_existing_max_items)),
            )
        return [self._pack_existing_memory_for_write(memory) for memory in memories[: max(1, int(self._settings.memory_write_existing_max_items))]]

    async def _llm_memory_write_upserts(
        self,
        *,
        actor_pc_id: str | None,
        actor_name: str,
        action_type: str,
        message: Message,
        thread: ForumThread | None,
    ) -> list[dict[str, Any]]:
        if self._settings.demo_fake or self._llm is None:
            return self._deterministic_memory_write_upserts(message=message, actor_name=actor_name, thread=thread)
        if not (self._settings.openai_base_url and self._settings.openai_api_key):
            return self._deterministic_memory_write_upserts(message=message, actor_name=actor_name, thread=thread)

        model = self._select_memory_writer_model(actor_pc_id=actor_pc_id)
        if not model:
            return self._deterministic_memory_write_upserts(message=message, actor_name=actor_name, thread=thread)

        if message.channel == "broadcast":
            scope_hint = "public + pc"
        else:
            scope_hint = "direct + pc"

        thread_payload: dict[str, Any] | None = None
        if isinstance(thread, ForumThread):
            thread_payload = {
                "id": thread.id,
                "channel_id": thread.channel_id,
                "title": thread.title,
            }
        existing_memories = await self._list_existing_memories_for_write(
            actor_pc_id=actor_pc_id,
            actor_name=actor_name,
            message=message,
            thread=thread,
        )

        messages = render_prompt_messages(
            "tick_runner.memory_write",
            {
                "max_items": str(max(1, int(self._settings.memory_write_max_items))),
                "summary_max_chars": str(max(1, int(self._settings.memory_write_summary_chars))),
                "content_max_chars": str(max(1, int(self._settings.memory_write_content_chars))),
                "pcs_personas": self._format_pcs_personas_for_prompt(),
                "action_type": action_type,
                "scope_hint": scope_hint,
                "actor_name": actor_name,
                "message_json": json.dumps(message.model_dump(), ensure_ascii=False),
                "thread_json": json.dumps(thread_payload, ensure_ascii=False),
                "existing_memories_json": json.dumps(existing_memories, ensure_ascii=False),
            },
        )
        url = openai_chat_completions_url(self._settings.openai_base_url)
        res = await self._llm.chat(
            url=url,
            apikey=self._settings.openai_api_key,
            model=model,
            messages=messages,
            tools=None,
        )

        parsed = res.get("parsed") if isinstance(res, dict) else None
        structured: Any | None = None
        if isinstance(parsed, dict) and parsed.get("kind") == "structured":
            structured = parsed.get("structured")
        elif isinstance(parsed, dict) and parsed.get("kind") == "markdown":
            markdown = parsed.get("markdown")
            if isinstance(markdown, str):
                structured = self._try_parse_json_loose(markdown)

        if structured is None:
            raw = res.get("raw") if isinstance(res, dict) else None
            parsed_raw = parse_llm_response(raw)
            if parsed_raw["kind"] == "structured":
                structured = parsed_raw.get("structured")
            elif parsed_raw["kind"] == "markdown" and isinstance(parsed_raw.get("markdown"), str):
                structured = self._try_parse_json_loose(parsed_raw["markdown"])

        if not isinstance(structured, dict):
            return self._deterministic_memory_write_upserts(message=message, actor_name=actor_name, thread=thread)

        upserts = structured.get("upserts")
        if not isinstance(upserts, list):
            return []
        return [item for item in upserts if isinstance(item, dict)][: max(1, int(self._settings.memory_write_max_items))]

    async def _write_memories_for_message(
        self,
        *,
        action_type: str,
        actor_pc_id: str | None,
        actor_name: str,
        message: Message,
        thread: ForumThread | None = None,
    ) -> None:
        try:
            raw_upserts = await self._llm_memory_write_upserts(
                actor_pc_id=actor_pc_id,
                actor_name=actor_name,
                action_type=action_type,
                message=message,
                thread=thread,
            )
            entries: list[MemoryEntry] = []
            for item in raw_upserts[: max(1, int(self._settings.memory_write_max_items))]:
                entry = self._normalize_memory_upsert(
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
                entries, precomputed = await self._dedup_merge_memories_on_write(entries)
                await self._store.upsert_memories(entries)
                if precomputed:
                    try:
                        await self._store.upsert_memory_summary_embeddings(
                            model=(getattr(self._settings, "openai_embedding_model", None) or "").strip(),
                            items=precomputed,
                            updated_at=utc_now_iso(),
                        )
                    except Exception:
                        pass
                await self._maybe_upsert_memory_summary_embeddings(entries)
        except Exception as exc:
            try:
                await self._store.add_event(
                    Event(
                        pc_id=actor_pc_id,
                        type="memory_write_error",
                        summary=f"memory write failed for {action_type}: {type(exc).__name__}",
                        visibility="private",
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
                )
            except Exception:
                pass
            return

    def _memory_write_dedup_enabled(self) -> bool:
        if not bool(getattr(self._settings, "memory_write_dedup_enabled", True)):
            return False
        return self._memory_vector_enabled()

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = 0.0
        an = 0.0
        bn = 0.0
        for i, av in enumerate(a):
            bv = float(b[i])
            avf = float(av)
            dot += avf * bv
            an += avf * avf
            bn += bv * bv
        denom = (math.sqrt(an) or 0.0) * (math.sqrt(bn) or 0.0)
        if denom <= 0:
            return 0.0
        return dot / denom

    def _merge_memory_meta_for_dedup(self, *, existing: dict[str, Any], incoming: dict[str, Any], sim: float, now: str) -> dict[str, Any]:
        meta = dict(existing or {})
        merged = meta.get("merged_sources")
        if not isinstance(merged, list):
            merged = []
        inc: dict[str, Any] = {}
        for k in ("ref_type", "ref_id", "message_id", "conversation_id", "thread_id", "send_batch_id", "action_type"):
            v = incoming.get(k) if isinstance(incoming, dict) else None
            if isinstance(v, str) and v.strip():
                inc[k] = v
        inc["merged_at"] = now
        inc["sim"] = round(float(sim), 6)
        merged.append(inc)
        meta["merged_sources"] = merged[-24:]
        meta["dedup_last_sim"] = round(float(sim), 6)
        meta["dedup_last_at"] = now

        keywords: list[str] = []
        for src in (existing, incoming):
            if not isinstance(src, dict):
                continue
            raw = src.get("keywords")
            if isinstance(raw, list):
                for v in raw:
                    if isinstance(v, str) and v.strip():
                        keywords.append(v.strip())
        if keywords:
            deduped: list[str] = []
            seen: set[str] = set()
            for k in keywords:
                kk = k.casefold()
                if kk in seen:
                    continue
                seen.add(kk)
                deduped.append(k)
                if len(deduped) >= 18:
                    break
            meta["keywords"] = deduped

        if "merge_key" not in meta or not meta.get("merge_key"):
            mk = incoming.get("merge_key") if isinstance(incoming, dict) else None
            if isinstance(mk, str) and mk.strip():
                meta["merge_key"] = mk.strip()
        return meta

    def _merge_memory_content_for_dedup(self, *, existing: str, incoming: str, max_len: int) -> str:
        a = (existing or "").strip()
        b = (incoming or "").strip()
        if not b:
            return self._trim_memory_text(a, max_len=max_len)
        if not a:
            return self._trim_memory_text(b, max_len=max_len)
        if b in a:
            return self._trim_memory_text(a, max_len=max_len)
        if a in b:
            return self._trim_memory_text(b, max_len=max_len)
        sep = "\n" if ("\n" in a or "\n" in b) else "；"
        merged = f"{a.rstrip('…')}{sep}{b}"
        return self._trim_memory_text(merged, max_len=max_len)

    async def _dedup_merge_memories_on_write(
        self, entries: list[MemoryEntry]
    ) -> tuple[list[MemoryEntry], list[tuple[str, str, list[float]]]]:
        if not entries:
            return [], []
        if not self._memory_write_dedup_enabled():
            return entries, []

        model = (getattr(self._settings, "openai_embedding_model", None) or "").strip()
        if not model:
            return entries, []
        url = self._embedding_url()
        apikey = self._embedding_api_key()
        if not url or not apikey:
            return entries, []

        embed_secrets = bool(getattr(self._settings, "memory_vector_embed_secrets", False))
        eligible: list[MemoryEntry] = []
        for e in entries:
            if e.kind == "secret" and not embed_secrets:
                continue
            if not isinstance(e.summary, str) or not e.summary.strip():
                continue
            eligible.append(e)
        if not eligible:
            return entries, []

        try:
            min_sim = float(getattr(self._settings, "memory_write_dedup_min_sim", 0.9) or 0.9)
            scan_limit = int(getattr(self._settings, "memory_write_dedup_scan_limit", 200) or 200)
            scan_limit = max(20, min(2000, scan_limit))
            max_age_days = int(getattr(self._settings, "memory_write_dedup_max_age_days", 14) or 14)
            max_age_days = max(0, min(365, max_age_days))
            updated_after: str | None = None
            if max_age_days > 0:
                updated_after = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()

            vectors = await self._llm.embeddings(
                url=url,
                apikey=apikey,
                model=model,
                inputs=[e.summary for e in eligible],
                timeout_s=30.0,
            )
            vec_by_id: dict[str, list[float]] = {eligible[i].id: vectors[i] for i in range(min(len(eligible), len(vectors)))}

            merged_by_id: dict[str, MemoryEntry] = {}
            order: list[str] = []
            precomputed: list[tuple[str, str, list[float]]] = []

            for entry in entries:
                vec = vec_by_id.get(entry.id)
                if vec is None:
                    # Not eligible for vector dedup; keep as-is.
                    if entry.id not in merged_by_id:
                        merged_by_id[entry.id] = entry
                        order.append(entry.id)
                    else:
                        merged_by_id[entry.id] = entry
                    continue

                candidates = await self._store.list_memory_summary_embeddings_for_write_dedup(
                    scope=entry.scope,
                    owner_pc_id=entry.owner_pc_id,
                    scope_id=entry.scope_id,
                    kind=entry.kind,
                    subject_id=entry.subject_id,
                    model=model,
                    scan_limit=scan_limit,
                    updated_after=updated_after,
                )

                # Optional extra constraints for recent_event: require same conversation/thread if present.
                conv_required: str | None = None
                thread_required: str | None = None
                if entry.kind == "recent_event" and isinstance(entry.meta, dict):
                    conv_required = (
                        entry.meta.get("conversation_id") if isinstance(entry.meta.get("conversation_id"), str) else None
                    )
                    thread_required = entry.meta.get("thread_id") if isinstance(entry.meta.get("thread_id"), str) else None

                best_id: str | None = None
                best_sim = 0.0
                for mid, cvec, meta_json in candidates:
                    if mid == entry.id:
                        continue
                    if conv_required or thread_required:
                        try:
                            meta = json.loads(meta_json or "{}") if isinstance(meta_json, str) else {}
                        except Exception:  # noqa: BLE001
                            meta = {}
                        if conv_required and meta.get("conversation_id") != conv_required:
                            continue
                        if thread_required and meta.get("thread_id") != thread_required:
                            continue
                    sim = self._cosine_sim(vec, cvec)
                    if sim > best_sim:
                        best_sim = sim
                        best_id = mid

                if best_id is None or best_sim < min_sim:
                    if entry.id not in merged_by_id:
                        merged_by_id[entry.id] = entry
                        order.append(entry.id)
                    else:
                        merged_by_id[entry.id] = entry
                    precomputed.append((entry.id, self._sha256_text(entry.summary), vec))
                    continue

                base = merged_by_id.get(best_id)
                if base is None:
                    base = await self._store.get_memory(best_id)
                if base is None:
                    if entry.id not in merged_by_id:
                        merged_by_id[entry.id] = entry
                        order.append(entry.id)
                    else:
                        merged_by_id[entry.id] = entry
                    continue

                now = utc_now_iso()
                max_summary = max(1, int(self._settings.memory_write_summary_chars))
                max_content = max(1, int(self._settings.memory_write_content_chars))

                incoming_summary = entry.summary
                next_summary = base.summary
                if base.summary.strip() and base.summary.strip() in incoming_summary and len(incoming_summary) <= max_summary:
                    next_summary = incoming_summary
                    precomputed.append((best_id, self._sha256_text(next_summary), vec))
                else:
                    # If the chosen summary equals the incoming summary, we can safely reuse the
                    # incoming embedding to avoid a second identical embeddings request later.
                    if self._sha256_text(next_summary) == self._sha256_text(incoming_summary):
                        precomputed.append((best_id, self._sha256_text(next_summary), vec))

                next_content = self._merge_memory_content_for_dedup(
                    existing=base.content,
                    incoming=entry.content,
                    max_len=max_content,
                )

                next_importance = max(int(base.importance), int(entry.importance))
                next_score = max(int(base.score), int(entry.score), next_importance)

                next_meta = self._merge_memory_meta_for_dedup(
                    existing=(base.meta if isinstance(base.meta, dict) else {}),
                    incoming=(entry.meta if isinstance(entry.meta, dict) else {}),
                    sim=float(best_sim),
                    now=now,
                )

                merged = base.model_copy(
                    update={
                        "updated_at": now,
                        "summary": next_summary,
                        "content": next_content,
                        "importance": next_importance,
                        "score": next_score,
                        "source_type": entry.source_type or base.source_type,
                        "revision": int(base.revision) + 1,
                        "meta": next_meta,
                    }
                )

                merged_by_id[best_id] = merged
                if best_id not in order:
                    order.append(best_id)

                try:
                    await self._store.add_event(
                        Event(
                            pc_id=entry.owner_pc_id,
                            type="memory_write_dedup_merge",
                            summary=f"dedup merge: {entry.kind} -> {best_id}",
                            visibility="private",
                            consequences={
                                "merged_into_id": best_id,
                                "incoming_id": entry.id,
                                "kind": entry.kind,
                                "scope": entry.scope,
                                "scope_id": entry.scope_id,
                                "owner_pc_id": entry.owner_pc_id,
                                "subject_id": entry.subject_id,
                                "sim": round(float(best_sim), 6),
                                "source": {
                                    "message_id": (entry.meta or {}).get("message_id") if isinstance(entry.meta, dict) else None,
                                    "conversation_id": (entry.meta or {}).get("conversation_id") if isinstance(entry.meta, dict) else None,
                                    "thread_id": (entry.meta or {}).get("thread_id") if isinstance(entry.meta, dict) else None,
                                    "send_batch_id": (entry.meta or {}).get("send_batch_id") if isinstance(entry.meta, dict) else None,
                                },
                            },
                        )
                    )
                except Exception:
                    pass

            deduped = [merged_by_id[mid] for mid in order if mid in merged_by_id]
            return deduped, precomputed
        except Exception as exc:  # noqa: BLE001
            try:
                await self._store.add_event(
                    Event(
                        type="memory_write_dedup_error",
                        summary=f"memory write dedup failed: {type(exc).__name__}",
                        visibility="private",
                        consequences={"error": f"{type(exc).__name__}: {exc}"},
                    )
                )
            except Exception:
                pass
            return entries, []

    @staticmethod
    def _try_parse_json_loose(text: str) -> Any | None:
        s = (text or "").strip()
        if not s:
            return None
        if s.startswith("```") and s.endswith("```"):
            inner = s[3:-3].lstrip()
            if "\n" in inner:
                first, rest = inner.split("\n", 1)
                if first.strip() in {"json", "javascript", "js"}:
                    s = rest.strip()
                else:
                    s = inner.strip()
            else:
                s = inner.strip()
        try:
            return json.loads(s)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _v4_tools() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "forum_list_threads",
                    "description": "List thread digests for a forum channel (bounded).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "channel_id": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 12},
                            "order": {"type": "string", "enum": ["active", "new"], "default": "active"},
                        },
                        "required": ["channel_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "forum_get_thread_context",
                    "description": "Fetch compact forum thread context (op + recent posts) with bounded length.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "thread_id": {"type": "string"},
                            "channel_id": {"type": "string"},
                            "recent_n": {"type": "integer", "minimum": 1, "maximum": 12, "default": 12},
                            "max_chars_per_post": {"type": "integer", "minimum": 200, "maximum": 700, "default": 700},
                        },
                        "required": ["thread_id", "channel_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "forum_get_post",
                    "description": "Fetch a specific forum post with optional slicing by start_char to avoid truncation.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "thread_id": {"type": "string"},
                            "channel_id": {"type": "string"},
                            "post_id": {"type": "string"},
                            "start_char": {"type": "integer", "minimum": 0, "default": 0},
                            "max_chars": {"type": "integer", "minimum": 200, "maximum": 8000, "default": 6000},
                        },
                        "required": ["thread_id", "channel_id", "post_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "dm_list_inbox",
                    "description": "List inbox digest items for a PC (bounded, peer-aggregated).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pc_id": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 24, "default": 12},
                            "lines_per_peer": {"type": "integer", "minimum": 1, "maximum": 4, "default": 2},
                            "max_chars_per_line": {"type": "integer", "minimum": 120, "maximum": 800, "default": 320},
                        },
                        "required": ["pc_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "dm_get_peer_context",
                    "description": "Fetch compact DM context between this PC and a peer (PC or DM).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pc_id": {"type": "string"},
                            "peer_kind": {"type": "string", "enum": ["pc", "dm"]},
                            "peer_id": {"type": "string"},
                            "recent_n": {"type": "integer", "minimum": 1, "maximum": 14, "default": 14},
                            "max_chars_per_message": {"type": "integer", "minimum": 200, "maximum": 600, "default": 600},
                        },
                        "required": ["pc_id", "peer_kind", "peer_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "dm_get_message",
                    "description": "Fetch a specific DM message with optional slicing by start_char to avoid truncation.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pc_id": {"type": "string"},
                            "peer_kind": {"type": "string", "enum": ["pc", "dm"]},
                            "peer_id": {"type": "string"},
                            "message_id": {"type": "string"},
                            "start_char": {"type": "integer", "minimum": 0, "default": 0},
                            "max_chars": {"type": "integer", "minimum": 200, "maximum": 8000, "default": 6000},
                        },
                        "required": ["pc_id", "peer_kind", "peer_id", "message_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "memory_search",
                    "description": "Search memories for the current PC (private + optional public + optional validated direct scope).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pc_id": {"type": "string"},
                            "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 12},
                            "include_public": {"type": "boolean", "default": True},
                            "direct_scope_id": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 60, "default": 20},
                        },
                        "required": ["pc_id", "keywords"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "doc_search",
                    "description": "Search public reference docs (rules/setting). Returns short titled snippets (bounded).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query_text": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 8, "default": 5},
                            "text_chars": {"type": "integer", "minimum": 120, "maximum": 1200, "default": 600},
                            "hops": {"type": "integer", "minimum": 0, "maximum": 1, "default": 0},
                        },
                        "required": ["query_text"],
                    },
                },
            },
        ]

    async def _v4_list_threads_digest(
        self,
        *,
        channel_id: str,
        limit: int,
        order: str,
    ) -> list[dict[str, Any]]:
        convs = await self._store.list_conversations()
        if not any(c.kind == "forum" and c.id == channel_id for c in convs):
            raise ValueError("channel not found or not a forum channel")

        threads = await self._store.list_forum_threads(channel_id)
        threads_sorted = list(threads)
        if order == "new":
            threads_sorted.sort(key=lambda t: (str(t.created_at or ""), str(t.id or "")), reverse=True)
        else:
            threads_sorted.sort(
                key=lambda t: (
                    1 if t.pinned else 0,
                    str(t.last_activity_at or ""),
                ),
                reverse=True,
            )

        out: list[dict[str, Any]] = []
        for t in threads_sorted[: max(0, int(limit))]:
            title = t.title
            if t.locked and "已锁定，请勿回复" not in title:
                title = f"{title} 已锁定，请勿回复"
            out.append(
                {
                    "channel_id": t.channel_id,
                    "thread_id": t.id,
                    "title": title,
                    "reply_count": t.reply_count,
                    "last_activity_at": t.last_activity_at,
                    "pinned": t.pinned,
                    "locked": t.locked,
                }
            )
        return out

    async def _v4_list_inbox_digest(
        self,
        *,
        pc_id: str,
        limit: int,
        lines_per_peer: int,
        max_chars_per_line: int,
    ) -> list[dict[str, Any]]:
        inbox_recent_msgs = await self._store.list_messages(f"dm_to_{pc_id}", limit=160)
        pc_name_by_id = {p.id: p.name for p in self._engine.pcs}

        latest_by_peer: dict[str, dict[str, Any]] = {}

        def get_peer(m: Message) -> tuple[str, str]:
            if m.from_actor.kind == "dm":
                return ("dm", (m.from_actor.name or "DM").strip() or "DM")

            if m.from_actor.kind == "pc" and m.from_actor.id and m.from_actor.id != pc_id:
                peer_id = m.from_actor.id
                peer_name = pc_name_by_id.get(peer_id) or m.from_actor.name or peer_id
                return (peer_id, peer_name)

            if m.from_actor.kind == "pc" and m.from_actor.id == pc_id:
                to_list = m.to or []
                peer_pc = next((a for a in to_list if a.kind == "pc" and a.id and a.id != pc_id), None)
                if peer_pc is not None and peer_pc.id:
                    peer_id2 = peer_pc.id
                    peer_name2 = pc_name_by_id.get(peer_id2) or peer_pc.name or peer_id2
                    return (peer_id2, peer_name2)
                if any(a.kind == "dm" for a in to_list):
                    return ("dm", "DM")

            return ("", "")

        for m in inbox_recent_msgs:
            peer_id, peer_name = get_peer(m)
            if not peer_id:
                continue

            peer_digest = latest_by_peer.setdefault(
                peer_id,
                {
                    "_ts": m.timestamp,
                    "id": peer_id,
                    "name": peer_name or peer_id,
                    "messages": [],
                },
            )
            peer_digest["_ts"] = max(str(peer_digest.get("_ts") or ""), str(m.timestamp))
            peer_digest["name"] = peer_name or peer_digest.get("name") or peer_id

            messages = peer_digest["messages"]
            if isinstance(messages, list):
                messages.append(self._summarize_message(m, max_len=max_chars_per_line))
                if len(messages) > lines_per_peer:
                    del messages[:-lines_per_peer]

        inbox_digest = list(latest_by_peer.values())
        inbox_digest.sort(key=lambda x: str(x.get("_ts") or ""), reverse=True)
        for it in inbox_digest:
            it.pop("_ts", None)
        return inbox_digest[: max(0, int(limit))]

    def _v4_validate_direct_scope_id(self, *, pc_id: str, direct_scope_id: str) -> str | None:
        scope_id = direct_scope_id.strip()
        if not scope_id:
            return None

        if scope_id == f"dm_to_{pc_id}":
            return scope_id

        if scope_id.startswith("pc_pair:"):
            parts = scope_id.split(":")
            if len(parts) != 3:
                return None
            left = parts[1].strip()
            right = parts[2].strip()
            if not left or not right:
                return None
            if pc_id not in {left, right}:
                return None
            other = right if left == pc_id else left
            if not any(p.id == other for p in self._engine.pcs):
                return None
            left2, right2 = sorted([pc_id, other])
            return f"pc_pair:{left2}:{right2}"

        return None

    async def _v4_execute_tool(self, *, pc_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "forum_list_threads":
                channel_id = self._clean_str(args.get("channel_id"))
                limit_raw = args.get("limit")
                order_raw = self._clean_str(args.get("order")) or "active"

                if not channel_id:
                    return {"ok": False, "error": {"code": "BAD_ARGS", "message": "channel_id required"}}

                limit = 12
                if isinstance(limit_raw, (int, float)):
                    limit = int(limit_raw)
                limit = max(1, min(30, limit))

                order = order_raw if order_raw in {"active", "new"} else "active"

                threads = await self._v4_list_threads_digest(channel_id=channel_id, limit=limit, order=order)
                return {"ok": True, "data": {"threads": threads}}

            if name == "forum_get_thread_context":
                thread_id = self._clean_str(args.get("thread_id"))
                channel_id = self._clean_str(args.get("channel_id"))
                recent_n_raw = args.get("recent_n")
                max_chars_raw = args.get("max_chars_per_post")

                if not thread_id or not channel_id:
                    return {"ok": False, "error": {"code": "BAD_ARGS", "message": "thread_id/channel_id required"}}

                recent_n = 12
                if isinstance(recent_n_raw, (int, float)):
                    recent_n = int(recent_n_raw)
                recent_n = max(1, min(12, recent_n))

                max_chars_per_post = 1200
                if isinstance(max_chars_raw, (int, float)):
                    max_chars_per_post = int(max_chars_raw)
                max_chars_per_post = max(200, min(1600, max_chars_per_post))
                max_chars_per_post = min(700, max_chars_per_post)

                thread = await self._store.get_forum_thread(thread_id)
                if thread is None or thread.channel_id != channel_id:
                    return {"ok": False, "error": {"code": "NOT_FOUND", "message": "thread not found"}}

                op = await self._store.get_first_message_by_thread_in_conversation(thread_id=thread.id, conversation_id=channel_id)
                recent = await self._store.list_messages_by_thread_in_conversation(
                    thread_id=thread.id,
                    conversation_id=channel_id,
                    limit=240,
                )
                if op is not None:
                    recent = [m for m in recent if m.id != op.id]
                tail = recent[-max(0, int(recent_n)) :]

                def trim_text(text: str, *, max_len: int) -> tuple[str, bool, int]:
                    s = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
                    full_len = len(s)
                    if full_len > max_len:
                        return s[:max_len] + "…", True, full_len
                    return s, False, full_len

                def pack(m: Message, *, max_len: int) -> dict[str, object]:
                    content, truncated, content_len = trim_text(m.content, max_len=max_len)
                    return {
                        "id": m.id,
                        "timestamp": m.timestamp,
                        "from": (m.from_actor.name or m.from_actor.kind),
                        "content": content,
                        "content_len": content_len,
                        "content_truncated": truncated,
                        "start_char": 0,
                        "max_chars": max_len,
                        "next_start_char": max_len if truncated else None,
                    }

                op_post = pack(op, max_len=900) if op is not None else None
                recent_posts = [pack(m, max_len=max_chars_per_post) for m in tail]
                truncated_ids = [p["id"] for p in recent_posts if isinstance(p, dict) and p.get("content_truncated") is True]
                if isinstance(op_post, dict) and op_post.get("content_truncated") is True:
                    truncated_ids = [*(truncated_ids or []), op_post.get("id")]

                data: dict[str, object] = {
                    "thread": {
                        "thread_id": thread.id,
                        "channel_id": thread.channel_id,
                        "title": thread.title,
                        "reply_count": thread.reply_count,
                        "last_activity_at": thread.last_activity_at,
                        "pinned": thread.pinned,
                        "locked": thread.locked,
                    },
                    "op_post": op_post,
                    "recent_posts": recent_posts,
                }
                meta = {"truncated": bool(truncated_ids), "truncated_post_ids": [tid for tid in truncated_ids if isinstance(tid, str)]}
                return {"ok": True, "data": data, "meta": meta}

            if name == "forum_get_post":
                thread_id = self._clean_str(args.get("thread_id"))
                channel_id = self._clean_str(args.get("channel_id"))
                post_id = self._clean_str(args.get("post_id"))
                start_raw = args.get("start_char")
                max_chars_raw = args.get("max_chars")

                if not thread_id or not channel_id or not post_id:
                    return {"ok": False, "error": {"code": "BAD_ARGS", "message": "thread_id/channel_id/post_id required"}}

                start_char = 0
                if isinstance(start_raw, (int, float)):
                    start_char = int(start_raw)
                start_char = max(0, start_char)

                max_chars = 6000
                if isinstance(max_chars_raw, (int, float)):
                    max_chars = int(max_chars_raw)
                max_chars = max(200, min(8000, max_chars))

                msg = await self._store.get_message(post_id)
                if msg is None:
                    return {"ok": False, "error": {"code": "NOT_FOUND", "message": "post not found"}}
                if msg.conversation_id != channel_id or msg.thread_id != thread_id:
                    return {"ok": False, "error": {"code": "SCOPE", "message": "post does not belong to thread/channel"}}

                full = (msg.content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
                content_len = len(full)
                sliced = full[start_char : start_char + max_chars]
                truncated = (start_char + max_chars) < content_len
                content = sliced + ("…" if truncated else "")
                meta = {
                    "content_len": content_len,
                    "content_truncated": truncated,
                    "start_char": start_char,
                    "max_chars": max_chars,
                    "next_start_char": (start_char + max_chars) if truncated else None,
                }
                post = {
                    "id": msg.id,
                    "timestamp": msg.timestamp,
                    "from": (msg.from_actor.name or msg.from_actor.kind),
                    "content": content,
                    "content_len": content_len,
                    "content_truncated": truncated,
                }
                return {"ok": True, "data": {"post": post}, "meta": meta}

            if name == "dm_list_inbox":
                tool_pc_id = self._clean_str(args.get("pc_id"))
                limit_raw = args.get("limit")
                lines_raw = args.get("lines_per_peer")
                max_chars_raw = args.get("max_chars_per_line")

                if tool_pc_id != pc_id:
                    return {"ok": False, "error": {"code": "PC_MISMATCH", "message": "pc_id mismatch"}}

                limit = 12
                if isinstance(limit_raw, (int, float)):
                    limit = int(limit_raw)
                limit = max(1, min(24, limit))

                lines_per_peer = 2
                if isinstance(lines_raw, (int, float)):
                    lines_per_peer = int(lines_raw)
                lines_per_peer = max(1, min(4, lines_per_peer))

                max_chars_per_line = 320
                if isinstance(max_chars_raw, (int, float)):
                    max_chars_per_line = int(max_chars_raw)
                max_chars_per_line = max(120, min(800, max_chars_per_line))

                items = await self._v4_list_inbox_digest(
                    pc_id=pc_id,
                    limit=limit,
                    lines_per_peer=lines_per_peer,
                    max_chars_per_line=max_chars_per_line,
                )
                return {"ok": True, "data": {"inbox": items}}

            if name == "dm_get_peer_context":
                tool_pc_id = self._clean_str(args.get("pc_id"))
                peer_kind = self._clean_str(args.get("peer_kind"))
                peer_id = self._clean_str(args.get("peer_id"))
                recent_n_raw = args.get("recent_n")
                max_chars_raw = args.get("max_chars_per_message")

                if tool_pc_id != pc_id:
                    return {"ok": False, "error": {"code": "PC_MISMATCH", "message": "pc_id mismatch"}}
                if peer_kind not in {"pc", "dm"} or not peer_id:
                    return {"ok": False, "error": {"code": "BAD_ARGS", "message": "invalid peer_kind/peer_id"}}
                if peer_kind == "pc":
                    if peer_id == pc_id:
                        return {"ok": False, "error": {"code": "BAD_ARGS", "message": "cannot dm self"}}
                    if not any(p.id == peer_id for p in self._engine.pcs):
                        return {"ok": False, "error": {"code": "NOT_FOUND", "message": "peer pc not found"}}
                if peer_kind == "dm" and peer_id != "dm":
                    return {"ok": False, "error": {"code": "BAD_ARGS", "message": "peer_id must be 'dm' for dm"}}

                recent_n = 24
                if isinstance(recent_n_raw, (int, float)):
                    recent_n = int(recent_n_raw)
                recent_n = max(1, min(14, recent_n))

                max_chars_per_message = 800
                if isinstance(max_chars_raw, (int, float)):
                    max_chars_per_message = int(max_chars_raw)
                max_chars_per_message = max(200, min(1600, max_chars_per_message))
                max_chars_per_message = min(600, max_chars_per_message)

                data = await self._build_dm_peer_context(
                    pc_id=pc_id,
                    peer_kind=peer_kind,
                    peer_id=peer_id,
                    recent_n=recent_n,
                    max_chars_per_message=max_chars_per_message,
                )
                data = self._v4_compact_dm_context(data)
                truncated_ids: list[str] = []
                recent_messages = data.get("recent_messages")
                if isinstance(recent_messages, list):
                    for item in recent_messages:
                        if not isinstance(item, dict):
                            continue
                        if item.get("content_truncated") is True and isinstance(item.get("id"), str):
                            truncated_ids.append(item["id"])
                return {"ok": True, "data": data, "meta": {"truncated": bool(truncated_ids), "truncated_message_ids": truncated_ids}}

            if name == "dm_get_message":
                tool_pc_id = self._clean_str(args.get("pc_id"))
                peer_kind = self._clean_str(args.get("peer_kind"))
                peer_id = self._clean_str(args.get("peer_id"))
                message_id = self._clean_str(args.get("message_id"))
                start_raw = args.get("start_char")
                max_chars_raw = args.get("max_chars")

                if tool_pc_id != pc_id:
                    return {"ok": False, "error": {"code": "PC_MISMATCH", "message": "pc_id mismatch"}}
                if peer_kind not in {"pc", "dm"} or not peer_id or not message_id:
                    return {"ok": False, "error": {"code": "BAD_ARGS", "message": "invalid peer/message"}}

                start_char = 0
                if isinstance(start_raw, (int, float)):
                    start_char = int(start_raw)
                start_char = max(0, start_char)

                max_chars = 6000
                if isinstance(max_chars_raw, (int, float)):
                    max_chars = int(max_chars_raw)
                max_chars = max(200, min(8000, max_chars))

                msg = await self._store.get_message(message_id)
                if msg is None:
                    return {"ok": False, "error": {"code": "NOT_FOUND", "message": "message not found"}}

                conv_id = f"dm_to_{pc_id}"
                if msg.conversation_id != conv_id:
                    return {"ok": False, "error": {"code": "SCOPE", "message": "message not in this pc inbox"}}

                def involves_peer(m: Message) -> bool:
                    if peer_kind == "dm":
                        if m.from_actor.kind == "dm":
                            return True
                        return any(a.kind == "dm" for a in (m.to or []))
                    if m.from_actor.kind == "pc" and m.from_actor.id == peer_id:
                        return True
                    return any(a.kind == "pc" and a.id == peer_id for a in (m.to or []))

                if not involves_peer(msg):
                    return {"ok": False, "error": {"code": "SCOPE", "message": "message not involving this peer"}}

                full = (msg.content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
                content_len = len(full)
                sliced = full[start_char : start_char + max_chars]
                truncated = (start_char + max_chars) < content_len
                content = sliced + ("…" if truncated else "")
                meta = {
                    "content_len": content_len,
                    "content_truncated": truncated,
                    "start_char": start_char,
                    "max_chars": max_chars,
                    "next_start_char": (start_char + max_chars) if truncated else None,
                }
                packed = {
                    "id": msg.id,
                    "timestamp": msg.timestamp,
                    "from": (msg.from_actor.name or msg.from_actor.kind),
                    "to": [a.name or a.kind for a in (msg.to or [])],
                    "content": content,
                    "content_len": content_len,
                    "content_truncated": truncated,
                }
                return {"ok": True, "data": {"message": packed}, "meta": meta}

            if name == "doc_search":
                query_text = self._clean_str(args.get("query_text"))
                limit_raw = args.get("limit")
                text_chars_raw = args.get("text_chars")
                hops_raw = args.get("hops")

                if not query_text:
                    return {"ok": False, "error": {"code": "BAD_ARGS", "message": "query_text required"}}

                limit = 5
                if isinstance(limit_raw, (int, float)):
                    limit = int(limit_raw)
                limit = max(1, min(8, limit))

                text_chars = 600
                if isinstance(text_chars_raw, (int, float)):
                    text_chars = int(text_chars_raw)
                text_chars = max(120, min(1200, text_chars))

                hops = 0
                if isinstance(hops_raw, (int, float)):
                    hops = int(hops_raw)
                hops = max(0, min(1, hops))

                if self._doc_search is None:
                    return {"ok": True, "data": {"enabled": False, "results": []}, "meta": {"reason": "not_configured"}}

                return await self._doc_search.search(query_text=query_text, limit=limit, text_chars=text_chars, hops=hops)

            if name == "memory_search":
                tool_pc_id = self._clean_str(args.get("pc_id"))
                keywords_raw = args.get("keywords")
                include_public_raw = args.get("include_public")
                direct_scope_raw = args.get("direct_scope_id")
                limit_raw = args.get("limit")

                if tool_pc_id != pc_id:
                    return {"ok": False, "error": {"code": "PC_MISMATCH", "message": "pc_id mismatch"}}
                if not isinstance(keywords_raw, list):
                    return {"ok": False, "error": {"code": "BAD_ARGS", "message": "keywords must be an array"}}

                cleaned_keywords: list[str] = []
                seen: set[str] = set()
                for value in keywords_raw:
                    if not isinstance(value, str):
                        continue
                    text = value.strip()
                    if not text:
                        continue
                    key = text.casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    cleaned_keywords.append(text)
                    if len(cleaned_keywords) >= 12:
                        break
                if not cleaned_keywords:
                    return {"ok": False, "error": {"code": "BAD_ARGS", "message": "keywords must not be empty"}}

                include_public = True
                if isinstance(include_public_raw, bool):
                    include_public = include_public_raw

                limit = 20
                if isinstance(limit_raw, (int, float)):
                    limit = int(limit_raw)
                limit = max(1, min(60, limit))

                direct_scope_id: str | None = None
                if isinstance(direct_scope_raw, str) and direct_scope_raw.strip():
                    direct_scope_id = self._v4_validate_direct_scope_id(pc_id=pc_id, direct_scope_id=direct_scope_raw)
                    if direct_scope_id is None:
                        return {"ok": False, "error": {"code": "BAD_ARGS", "message": "invalid direct_scope_id"}}

                query_text = " ".join(cleaned_keywords).strip()

                lex_limit = int(getattr(self._settings, "memory_hybrid_lex_candidates", 40) or 40)
                lex_limit = max(10, min(200, lex_limit))
                lex_candidates = await self._store.search_memories(
                    keywords=cleaned_keywords,
                    owner_pc_id=pc_id,
                    include_public=include_public,
                    direct_scope_id=direct_scope_id,
                    limit=max(lex_limit, limit),
                )

                vector_used = False
                vector_error: str | None = None
                vector_sims: dict[str, float] = {}
                vector_model = (getattr(self._settings, "openai_embedding_model", None) or "").strip()
                min_sim = float(getattr(self._settings, "memory_vector_min_sim", 0.72) or 0.72)
                top_k = int(getattr(self._settings, "memory_vector_top_k", 30) or 30)
                scan_limit = int(getattr(self._settings, "memory_vector_scan_limit", 1200) or 1200)
                if self._memory_vector_enabled() and vector_model:
                    try:
                        url = self._embedding_url()
                        if not url:
                            raise RuntimeError("missing embedding url")
                        apikey = self._embedding_api_key()
                        if not apikey:
                            raise RuntimeError("missing embedding api key")
                        qvec = (
                            await self._llm.embeddings(
                                url=url,
                                apikey=apikey,
                                model=vector_model,
                                inputs=[query_text],
                                timeout_s=30.0,
                            )
                        )[0]
                        qnorm = math.sqrt(sum(v * v for v in qvec)) or 0.0
                        if qnorm > 0:
                            rows = await self._store.list_memory_summary_embeddings_for_vector_search(
                                owner_pc_id=pc_id,
                                include_public=include_public,
                                direct_scope_id=direct_scope_id,
                                model=vector_model,
                                scan_limit=max(50, scan_limit),
                            )
                            scored: list[tuple[str, float]] = []
                            for mid, vec in rows:
                                if not vec or len(vec) != len(qvec):
                                    continue
                                dot = 0.0
                                vnorm = 0.0
                                for i, val in enumerate(vec):
                                    dot += float(val) * float(qvec[i])
                                    vnorm += float(val) * float(val)
                                denom = qnorm * (math.sqrt(vnorm) or 0.0)
                                if denom <= 0:
                                    continue
                                sim = dot / denom
                                if sim >= min_sim:
                                    scored.append((mid, sim))
                            scored.sort(key=lambda x: x[1], reverse=True)
                            for mid, sim in scored[: max(1, min(120, top_k))]:
                                vector_sims[mid] = float(sim)
                            vector_used = True
                    except Exception as exc:  # noqa: BLE001
                        vector_error = f"{type(exc).__name__}: {exc}"

                candidate_ids: list[str] = []
                seen_ids: set[str] = set()
                for m in lex_candidates:
                    if m.id not in seen_ids:
                        seen_ids.add(m.id)
                        candidate_ids.append(m.id)
                for mid in sorted(vector_sims.keys(), key=lambda k: vector_sims[k], reverse=True):
                    if mid not in seen_ids:
                        seen_ids.add(mid)
                        candidate_ids.append(mid)

                candidates_by_id = {m.id: m for m in lex_candidates}
                missing_ids = [mid for mid in candidate_ids if mid not in candidates_by_id]
                if missing_ids:
                    fetched = await self._store.list_memories_by_ids(missing_ids)
                    for m in fetched:
                        candidates_by_id[m.id] = m

                max_kw = max(1, len(cleaned_keywords))
                w_sim = float(getattr(self._settings, "memory_hybrid_w_sim", 1.0) or 1.0)
                w_lex = float(getattr(self._settings, "memory_hybrid_w_lex", 0.35) or 0.35)
                w_score = float(getattr(self._settings, "memory_hybrid_w_score", 0.05) or 0.05)
                w_pinned = float(getattr(self._settings, "memory_hybrid_w_pinned", 0.2) or 0.2)

                def count_lex_hits(memory: MemoryEntry) -> int:
                    blob = f"{memory.summary}\n{memory.content}".casefold()
                    hits = 0
                    for kw in cleaned_keywords:
                        if kw.casefold() in blob:
                            hits += 1
                    return hits

                ranked: list[tuple[float, str]] = []
                per_id_meta: dict[str, dict[str, Any]] = {}
                for mid in candidate_ids:
                    memory = candidates_by_id.get(mid)
                    if memory is None:
                        continue
                    sim = float(vector_sims.get(mid, 0.0))
                    hits = count_lex_hits(memory)
                    lex_ratio = hits / max_kw
                    score_term = max(-3.0, min(10.0, float(memory.score))) / 10.0
                    pinned_term = 1.0 if memory.pinned else 0.0
                    final = (w_sim * sim) + (w_lex * lex_ratio) + (w_score * score_term) + (w_pinned * pinned_term)
                    ranked.append((final, mid))

                    via: list[str] = []
                    if mid in vector_sims:
                        via.append("vec")
                    if hits > 0:
                        via.append("lex")
                    per_id_meta[mid] = {
                        "final": round(final, 6),
                        "sim": round(sim, 6),
                        "lex_hits": hits,
                        "score": memory.score,
                        "via": via,
                    }

                ranked.sort(
                    key=lambda x: (
                        x[0],
                        1 if candidates_by_id.get(x[1]) and candidates_by_id[x[1]].pinned else 0,
                        candidates_by_id.get(x[1]).score if candidates_by_id.get(x[1]) else 0,
                        str(candidates_by_id.get(x[1]).updated_at if candidates_by_id.get(x[1]) else ""),
                    ),
                    reverse=True,
                )
                memories = [candidates_by_id[mid] for _, mid in ranked if mid in candidates_by_id][: max(1, limit)]

                items: list[dict[str, Any]] = []
                # Keep memory_search lightweight; the v4 executor has a tight total tool-output budget
                # shared across all tool calls in this tick.
                budget_chars = 6_000
                used_chars = 0
                omitted = 0
                for memory in memories:
                    content = memory.content
                    summary = memory.summary
                    if not isinstance(content, str) or not content.strip():
                        continue
                    if not isinstance(summary, str):
                        summary = ""

                    approx = len(content) + len(summary) + 160
                    if approx > budget_chars and not items:
                        # Avoid blowing up tool output with a single gigantic memory; skip it.
                        omitted += 1
                        continue
                    if used_chars + approx > budget_chars:
                        omitted += 1
                        continue

                    items.append(
                        {
                            "id": memory.id,
                            "scope": memory.scope,
                            "scope_id": memory.scope_id,
                            "kind": memory.kind,
                            "summary": summary,
                            "content": content,
                            "updated_at": memory.updated_at,
                            "rank": per_id_meta.get(memory.id),
                        }
                    )
                    used_chars += approx

                truncated = omitted > 0
                try:
                    await self._store.touch_memories([it["id"] for it in items if isinstance(it, dict) and it.get("id")])
                except Exception:
                    pass

                return {
                    "ok": True,
                    "data": {
                        "items": items,
                        "truncated": truncated,
                        "omitted_count": omitted,
                        "meta": {
                            "query_text": query_text,
                            "candidates": {"lex": len(lex_candidates), "vec": len(vector_sims), "union": len(candidate_ids)},
                            "vector": {
                                "enabled": self._memory_vector_enabled(),
                                "used": vector_used,
                                "model": vector_model or None,
                                "min_sim": min_sim,
                                "top_k": top_k,
                                "scan_limit": scan_limit,
                                "error": vector_error,
                            },
                            "weights": {"sim": w_sim, "lex": w_lex, "score": w_score, "pinned": w_pinned},
                        },
                    },
                }

            return {"ok": False, "error": {"code": "UNKNOWN_TOOL", "message": f"unknown tool: {name}"}}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": {"code": "EXCEPTION", "message": f"{type(exc).__name__}: {exc}"}}

    @staticmethod
    def _v4_compact_thread_context(data: dict[str, Any]) -> dict[str, Any]:
        """
        Apply a second-pass compaction to keep tool output comfortably under executor budgets.
        """
        if not isinstance(data, dict):
            return data

        def trim(v: Any, max_len: int) -> str:
            s = (str(v or "")).replace("\r\n", "\n").replace("\r", "\n").strip()
            return s[:max_len] + "…" if len(s) > max_len else s

        op_post = data.get("op_post")
        if isinstance(op_post, dict):
            if "content" in op_post:
                op_post["content"] = trim(op_post.get("content"), 900)

        recent_posts = data.get("recent_posts")
        if isinstance(recent_posts, list):
            # Keep only a short tail by default; the model can ask again if needed.
            kept = recent_posts[-10:]
            for item in kept:
                if isinstance(item, dict) and "content" in item:
                    item["content"] = trim(item.get("content"), 700)
            data["recent_posts"] = kept

        return data

    @staticmethod
    def _v4_compact_dm_context(data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            return data

        def trim(v: Any, max_len: int) -> str:
            s = (str(v or "")).replace("\r\n", "\n").replace("\r", "\n").strip()
            return s[:max_len] + "…" if len(s) > max_len else s

        recent_messages = data.get("recent_messages")
        if isinstance(recent_messages, list):
            kept = recent_messages[-18:]
            for item in kept:
                if isinstance(item, dict) and "content" in item:
                    item["content"] = trim(item.get("content"), 700)
            data["recent_messages"] = kept

        return data

    async def _llm_action(self, *, pc_id: str, pc_name: str, persona: str | None, since: str | None) -> dict[str, Any]:
        """
        v4: Run a tool-calling agent loop, then return a single final JSON action.
        """
        if self._llm is None:
            return {"type": "noop", "reason": "llm not configured"}
        if not (self._settings.openai_base_url and self._settings.openai_api_key):
            return {"type": "noop", "reason": "missing openai_base_url/api_key"}

        # context: recall
        # NOTE: `since` is the PC's last turn start timestamp, so "new since last turn" is often 0~1 item.
        # Keep both "recent" and "new" to avoid starving the model of context.
        recall_recent_items = await self._store.list_pc_activity(pc_id, since=None, limit=30)
        recall_recent = [
            {
                "kind": a.kind,
                "summary": a.summary,
                "ref_type": a.ref_type,
                "ref_id": a.ref_id,
            }
            for a in recall_recent_items
        ]
        recall_new = recall_recent
        if since:
            recall_new = [x for x in recall_recent if str(x.get("time") or "") >= since]
        recall = {"recent": recall_recent, "new": recall_new}

        # context: inbox digest (PC-aggregated)
        # Show at most the latest 2 PC↔PC DM lines per peer to keep Round 1 lightweight.
        inbox_recent_msgs = await self._store.list_messages(f"dm_to_{pc_id}", limit=160)
        pc_name_by_id = {p.id: p.name for p in self._engine.pcs}
        latest_by_peer: dict[str, dict[str, Any]] = {}
        for m in inbox_recent_msgs:
            peer_id: str | None = None
            peer_name: str | None = None

            if m.from_actor.kind == "pc" and m.from_actor.id != pc_id:
                peer_id = m.from_actor.id
                peer_name = pc_name_by_id.get(peer_id) or m.from_actor.name
            elif m.from_actor.kind == "pc" and m.from_actor.id == pc_id:
                peer = next((actor for actor in m.to if actor.kind == "pc" and actor.id and actor.id != pc_id), None)
                if peer is not None:
                    peer_id = peer.id
                    peer_name = pc_name_by_id.get(peer_id) or peer.name

            if not peer_id or peer_id == pc_id:
                continue

            peer_digest = latest_by_peer.setdefault(
                peer_id,
                {
                    "_ts": m.timestamp,
                    "id": peer_id,
                    "name": peer_name or peer_id,
                    "messages": [],
                },
            )
            peer_digest["_ts"] = max(str(peer_digest.get("_ts") or ""), str(m.timestamp))
            peer_digest["name"] = peer_name or peer_digest.get("name") or peer_id

            messages = peer_digest["messages"]
            if isinstance(messages, list):
                messages.append(self._summarize_message(m, max_len=320))
                if len(messages) > 2:
                    del messages[:-2]

        inbox_digest = list(latest_by_peer.values())
        inbox_digest.sort(key=lambda x: str(x.get("_ts") or ""), reverse=True)
        for it in inbox_digest:
            it.pop("_ts", None)
        inbox_digest = inbox_digest[:12]

        # context: active threads digest
        convs = await self._store.list_conversations()
        forum_convs = [c for c in convs if c.kind == "forum"]
        threads_all: list[dict[str, Any]] = []
        for c in forum_convs:
            threads = await self._store.list_forum_threads(c.id)
            for t in threads[:30]:
                title = t.title
                if t.locked and "已锁定，请勿回复" not in title:
                    title = f"{title} 已锁定，请勿回复"
                threads_all.append(
                    {
                        "channel_id": t.channel_id,
                        "thread_id": t.id,
                        "title": title,
                        "reply_count": t.reply_count,
                        "last_activity_at": t.last_activity_at,
                        "pinned": t.pinned,
                        "locked": t.locked,
                    }
                )
        threads_all.sort(
            key=lambda x: (
                1 if x.get("pinned") else 0,
                str(x.get("last_activity_at") or ""),
            ),
            reverse=True,
        )
        threads_digest = threads_all[:12]
        # NOTE: Intentionally do NOT embed thread posts here. We only provide a lightweight digest and
        # let the agent fetch thread context via tools (forum_get_thread_context) on demand.

        forum_channels = [{"id": c.id, "title": c.title, "description": c.description, "group": c.group} for c in forum_convs]

        persona_text = (persona or "").strip() or f"你是{pc_name}。"

        pcs = [{"id": p.id, "name": p.name} for p in self._engine.pcs]

        messages = render_prompt_messages(
            "tick_runner.v4_action",
            {
                "pc_id": pc_id,
                "pc_name": pc_name,
                "persona": persona_text,
                "pcs_json": json.dumps(pcs, ensure_ascii=False),
                "forum_channels_json": json.dumps(forum_channels, ensure_ascii=False),
                "threads_digest_json": json.dumps(threads_digest, ensure_ascii=False),
                "inbox_digest_json": json.dumps(inbox_digest, ensure_ascii=False),
                "recall_json": json.dumps(recall, ensure_ascii=False),
            },
        )

        model = getattr(next((p for p in self._engine.pcs if p.id == pc_id), None), "model", None)
        if not isinstance(model, str) or not model.strip():
            model = self._settings.openai_model
        if not isinstance(model, str) or not model.strip():
            return {"type": "noop", "reason": "missing model"}

        url = openai_chat_completions_url(self._settings.openai_base_url)
        tools = self._v4_tools()

        tool_audit: list[dict[str, Any]] = []

        async def tool_handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
            tool_audit.append({"name": tool_name, "args": tool_args})
            return await self._v4_execute_tool(pc_id=pc_id, name=tool_name, args=tool_args)

        final_action = await run_tool_calling_loop(
            llm_chat=self._llm.chat,
            url=url,
            apikey=self._settings.openai_api_key,
            model=model,
            messages=messages,
            tools=tools,
            tool_handler=tool_handler,
            limits=ToolCallLimits(
                max_tool_rounds=int(getattr(self._settings, "v4_max_tool_rounds", 3) or 3),
                max_tool_calls_per_round=int(getattr(self._settings, "v4_max_tool_calls_per_round", 2) or 2),
                max_total_tool_output_chars=int(getattr(self._settings, "v4_max_total_tool_output_chars", 60_000) or 60_000),
            ),
        )
        if tool_audit:
            return {"action": final_action, "audit": {"tools": tool_audit}}
        return final_action

    async def _build_dm_digest_context(self, *, since: str, until: str) -> dict[str, Any]:
        pcs = [{"id": p.id, "name": p.name} for p in self._engine.pcs]
        pc_name_by_id = {p["id"]: p["name"] for p in pcs}

        def is_to_dm(m: Message) -> bool:
            return any(a.kind == "dm" for a in (m.to or []))

        # direct: PC -> DM messages in dm_to_<pc> conversations
        direct_new: list[dict[str, Any]] = []
        direct_recent_by_pc: list[dict[str, Any]] = []
        for p in self._engine.pcs:
            conv_id = f"dm_to_{p.id}"
            recent_msgs = await self._store.list_messages(conv_id, limit=120)
            recent_to_dm = [m for m in recent_msgs if is_to_dm(m)]
            recent_lines = [self._summarize_message(m, max_len=800) for m in recent_to_dm[-6:]]
            if recent_lines:
                direct_recent_by_pc.append({"pc_id": p.id, "pc_name": p.name, "recent_lines": recent_lines})

            new_msgs = await self._store.list_messages_since(conv_id, since=since, limit=200)
            new_to_dm = [m for m in new_msgs if is_to_dm(m)]
            for m in new_to_dm[-30:]:
                direct_new.append(
                    {
                        "pc_id": p.id,
                        "pc_name": p.name,
                        "timestamp": m.timestamp,
                        "message_id": m.id,
                        "line": self._summarize_message(m, max_len=1200),
                    }
                )
        direct_new.sort(key=lambda x: (str(x.get("timestamp") or ""), str(x.get("message_id") or "")))
        direct_digest: dict[str, Any] = {
            "new_count": len(direct_new),
            "new": direct_new[-30:],
            "recent_by_pc": direct_recent_by_pc,
        }

        # forum: thread digests + new posts since window
        convs = await self._store.list_conversations()
        forum_convs = [c for c in convs if c.kind == "forum"]
        forum_channels = [{"id": c.id, "title": c.title, "description": c.description, "group": c.group} for c in forum_convs]
        channel_title_by_id = {c["id"]: c["title"] for c in forum_channels}

        threads_all: list[ForumThread] = []
        for c in forum_convs:
            threads_all.extend(await self._store.list_forum_threads(c.id))
        thread_by_id = {t.id: t for t in threads_all}
        threads_all.sort(
            key=lambda t: (
                1 if t.pinned else 0,
                str(t.last_activity_at or ""),
            ),
            reverse=True,
        )
        threads_digest: list[dict[str, Any]] = []
        forum_new_items: list[dict[str, Any]] = []
        for c in forum_convs:
            new_msgs = await self._store.list_messages_since(c.id, since=since, limit=500)
            for m in new_msgs:
                if m.from_actor.kind == "dm":
                    continue
                tid = m.thread_id
                if not isinstance(tid, str) or not tid.strip():
                    continue
                t = thread_by_id.get(tid)
                forum_new_items.append(
                    {
                        "channel_id": c.id,
                        "channel_title": channel_title_by_id.get(c.id) or c.id,
                        "thread_id": tid,
                        "thread_title": t.title if t else tid,
                        "timestamp": m.timestamp,
                        "message_id": m.id,
                        "line": self._summarize_message(m, max_len=1200),
                    }
                )
        forum_new_items.sort(key=lambda x: (str(x.get("timestamp") or ""), str(x.get("message_id") or "")))
        forum_new_count = len(forum_new_items)

        for t in threads_all[:12]:
            msgs = await self._store.list_messages_by_thread(t.id, limit=120)
            public_msgs = [m for m in msgs if m.conversation_id == t.channel_id]
            recent_posts = [self._summarize_message(m, max_len=1200) for m in public_msgs[-6:]]
            new_posts_msgs = [m for m in public_msgs if m.timestamp >= since and m.from_actor.kind != "dm"]
            new_posts = [self._summarize_message(m, max_len=1200) for m in new_posts_msgs[-8:]]
            threads_digest.append(
                {
                    "channel_id": t.channel_id,
                    "channel_title": channel_title_by_id.get(t.channel_id) or t.channel_id,
                    "thread_id": t.id,
                    "title": t.title,
                    "reply_count": t.reply_count,
                    "last_activity_at": t.last_activity_at,
                    "pinned": t.pinned,
                    "locked": t.locked,
                    "new_posts": new_posts,
                    "recent_posts": recent_posts,
                }
            )
        forum_digest: dict[str, Any] = {
            "new_count": forum_new_count,
            "new_items": forum_new_items[-30:],
            "threads_digest": threads_digest,
        }

        # broadcast: new messages since window (exclude DM's own messages to avoid self-loop)
        recent_broadcast = await self._store.list_messages("broadcast", limit=120)
        recent_broadcast = [m for m in recent_broadcast if m.from_actor.kind != "dm"]
        recent_lines = [self._summarize_message(m, max_len=800) for m in recent_broadcast[-12:]]

        new_broadcast = await self._store.list_messages_since("broadcast", since=since, limit=200)
        new_broadcast = [m for m in new_broadcast if m.from_actor.kind != "dm"]
        new_lines = [self._summarize_message(m, max_len=800) for m in new_broadcast[-20:]]
        broadcast_digest: dict[str, Any] = {
            "new_count": len(new_broadcast),
            "new_lines": new_lines,
            "recent_lines": recent_lines,
        }

        required_type = "noop"
        if direct_digest["new_count"] > 0:
            required_type = "dm"
        elif forum_digest["new_count"] > 0 or broadcast_digest["new_count"] > 0:
            required_type = "broadcast"

        return {
            "pcs": pcs,
            "pc_ids": [p["id"] for p in pcs],
            "pc_name_by_id": pc_name_by_id,
            "forum_channels": forum_channels,
            "direct_digest": direct_digest,
            "forum_digest": forum_digest,
            "broadcast_digest": broadcast_digest,
            "required_type": required_type,
            "since": since,
            "until": until,
        }

    async def _llm_dm_digest_action(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        """
        Ask the DM model to produce a single digest action JSON (no tool-calling).
        """
        if self._llm is None:
            return {"type": "noop", "reason": "llm not configured"}
        if not (self._settings.openai_base_url and self._settings.openai_api_key):
            return {"type": "noop", "reason": "missing openai_base_url/api_key"}

        model = self._engine.dm.model or self._settings.openai_dm_model or self._settings.openai_model
        if not isinstance(model, str) or not model.strip():
            return {"type": "noop", "reason": "missing model"}

        dm_persona = (self._engine.dm.persona or "").strip() or "你是DM。"

        messages = render_prompt_messages(
            "tick_runner.dm_digest_action",
            {
                "dm_persona": dm_persona,
                "pcs_json": json.dumps(ctx["pcs"], ensure_ascii=False),
                "forum_channels_json": json.dumps(ctx["forum_channels"], ensure_ascii=False),
                "direct_digest_json": json.dumps(ctx["direct_digest"], ensure_ascii=False),
                "forum_digest_json": json.dumps(ctx["forum_digest"], ensure_ascii=False),
                "broadcast_digest_json": json.dumps(ctx["broadcast_digest"], ensure_ascii=False),
                "since_iso": str(ctx.get("since") or ""),
                "until_iso": str(ctx.get("until") or ""),
            },
        )

        url = openai_chat_completions_url(self._settings.openai_base_url)
        res = await self._llm.chat(
            url=url,
            apikey=self._settings.openai_api_key,
            model=model,
            messages=messages,
            tools=None,
        )

        parsed = res.get("parsed") if isinstance(res, dict) else None
        if isinstance(parsed, dict) and parsed.get("kind") == "structured":
            structured = parsed.get("structured")
            if isinstance(structured, dict):
                return structured
            return {"type": "noop", "reason": "llm returned non-object structured output"}

        if isinstance(parsed, dict) and parsed.get("kind") == "markdown":
            md = parsed.get("markdown")
            if isinstance(md, str):
                j = self._try_parse_json_loose(md)
                if isinstance(j, dict):
                    return j
                return {"type": "noop", "reason": "llm returned non-json markdown"}

        raw = res.get("raw") if isinstance(res, dict) else None
        p2 = parse_llm_response(raw)
        if p2["kind"] == "structured" and isinstance(p2["structured"], dict):
            return p2["structured"]
        if p2["kind"] == "markdown" and isinstance(p2["markdown"], str):
            j = self._try_parse_json_loose(p2["markdown"])
            if isinstance(j, dict):
                return j
        return {"type": "noop", "reason": "llm output parse failed"}

    @staticmethod
    def _validate_dm_digest_action(raw: Any, *, pc_ids: set[str]) -> tuple[dict[str, Any], list[str]]:
        def clean_str(v: Any) -> str | None:
            if not isinstance(v, str):
                return None
            s = v.strip()
            return s if s else None

        if not isinstance(raw, dict):
            return {"type": "noop", "reason": "invalid action: not an object"}, ["action must be an object"]

        a_type = clean_str(raw.get("type"))
        if not a_type:
            return {"type": "noop", "reason": "invalid action: missing type"}, ["action.type is required"]

        if a_type == "dm":
            to_pc_id = clean_str(raw.get("to_pc_id"))
            content = clean_str(raw.get("content"))
            errors: list[str] = []
            if not to_pc_id:
                errors.append("dm.to_pc_id is required")
            elif to_pc_id not in pc_ids:
                errors.append("dm.to_pc_id must be an existing pc id")
            if not content:
                errors.append("dm.content is required")
            elif len(content) > 400:
                # Keep full content for DB; prompt still suggests a short reply.
                pass
            if errors:
                return {"type": "noop", "reason": "invalid dm"}, errors
            return {"type": "dm", "to_pc_id": to_pc_id, "content": content}, []

        if a_type == "broadcast":
            content = clean_str(raw.get("content"))
            if not content:
                return {"type": "noop", "reason": "invalid broadcast"}, ["broadcast.content is required"]
            if len(content) > 1200:
                pass
            return {"type": "broadcast", "content": content}, []

        if a_type == "noop":
            reason = clean_str(raw.get("reason"))
            out: dict[str, Any] = {"type": "noop"}
            if reason:
                out["reason"] = reason
            return out, []

        return {"type": "noop", "reason": f"invalid action: unknown type '{a_type}'"}, [f"unknown action.type: {a_type}"]

    def _deterministic_dm_digest_action(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        required_type = str(ctx.get("required_type") or "noop")
        direct_digest = ctx.get("direct_digest") or {}
        forum_digest = ctx.get("forum_digest") or {}
        broadcast_digest = ctx.get("broadcast_digest") or {}

        if required_type == "dm":
            items = direct_digest.get("new") if isinstance(direct_digest, dict) else None
            if isinstance(items, list) and items:
                last = items[-1] if isinstance(items[-1], dict) else {}
                to_pc_id = str(last.get("pc_id") or "").strip()
                pc_name = str(last.get("pc_name") or "").strip() or "你"
                preview = str(last.get("line") or "").strip()
                if preview:
                    preview = preview[:160] + ("…" if len(preview) > 160 else "")
                content = f"收到。你刚才说的要点我记下了：{preview}\n你希望我怎么处理：1) 仅记录 2) 帮你转达 3) 我来推进？"
                content = content[:400]
                if to_pc_id:
                    return {"type": "dm", "to_pc_id": to_pc_id, "content": f"{pc_name}，{content}"}
            return {"type": "noop", "reason": "no direct message to reply"}

        if required_type == "broadcast":
            lines: list[str] = ["【DM Digest】"]
            forum_new = int(forum_digest.get("new_count") or 0) if isinstance(forum_digest, dict) else 0
            bc_new = int(broadcast_digest.get("new_count") or 0) if isinstance(broadcast_digest, dict) else 0
            if forum_new:
                lines.append(f"- 论坛新增：{forum_new} 条")
                items = forum_digest.get("new_items") if isinstance(forum_digest, dict) else None
                if isinstance(items, list):
                    for it in items[-3:]:
                        if not isinstance(it, dict):
                            continue
                        ch = str(it.get("channel_title") or it.get("channel_id") or "").strip()
                        th = str(it.get("thread_title") or "").strip()
                        line = str(it.get("line") or "").strip()
                        if ch and th and line:
                            lines.append(f"  - {ch}《{th}》：{line[:180]}{'…' if len(line) > 180 else ''}")
            if bc_new:
                lines.append(f"- #broadcast 新增：{bc_new} 条")
                items2 = broadcast_digest.get("new_lines") if isinstance(broadcast_digest, dict) else None
                if isinstance(items2, list):
                    for l in items2[-3:]:
                        if isinstance(l, str) and l.strip():
                            s = l.strip()
                            lines.append(f"  - {s[:220]}{'…' if len(s) > 220 else ''}")
            if len(lines) == 1:
                lines.append("- 暂无新增。")
            content = "\n".join(lines)[:1200]
            return {"type": "broadcast", "content": content}

        return {"type": "noop", "reason": "no new items in digest window"}

    def _coerce_dm_digest_action(self, *, action: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        required_type = str(ctx.get("required_type") or "noop")
        if not isinstance(action, dict) or action.get("type") != required_type:
            return self._deterministic_dm_digest_action(ctx=ctx)
        return action

    async def _apply_dm_digest_action(self, *, action: dict[str, Any]) -> list[dict[str, Any]]:
        a_type = action.get("type")
        if a_type == "noop":
            reason = str(action.get("reason") or "").strip()
            summary = "DM：digest 无动作"
            if reason:
                summary = f"{summary}（{reason}）"
            await self._store.add_pc_activity(
                PcActivity(
                    pc_id="dm",
                    kind="digest_noop",
                    summary=summary,
                )
            )
            return [{"kind": "noop"}]

        dm_actor = Actor(kind="dm", id="dm", name="DM")

        if a_type == "broadcast":
            content = str(action.get("content") or "")
            msg = Message(
                conversation_id="broadcast",
                channel="broadcast",
                from_actor=dm_actor,
                to=[],
                content=content,
            )
            await self._store.append_message(msg)
            preview = content.strip().replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
            if len(preview) > 160:
                preview = preview[:160] + "…"
            await self._store.add_pc_activity(
                PcActivity(
                    pc_id="dm",
                    kind="broadcast",
                    summary=f"DM：在 #broadcast 发言（{preview}）" if preview else "DM：在 #broadcast 发言",
                    ref_type="message",
                    ref_id=msg.id,
                )
            )
            await self._write_memories_for_message(
                action_type="dm_digest_broadcast",
                actor_pc_id=None,
                actor_name="DM",
                message=msg,
                thread=None,
            )
            await self._ws.broadcast({"type": "message", "payload": msg.model_dump()})
            return [{"kind": "message", "message_id": msg.id}]

        if a_type == "dm":
            to_pc_id = str(action.get("to_pc_id") or "").strip()
            content = str(action.get("content") or "")
            pc = next((p for p in self._engine.pcs if p.id == to_pc_id), None)
            if pc is None:
                return [{"kind": "dm_failed"}]
            msg = Message(
                conversation_id=f"dm_to_{pc.id}",
                channel="direct",
                from_actor=dm_actor,
                to=[Actor(kind="pc", id=pc.id, name=pc.name)],
                content=content,
            )
            await self._store.append_message(msg)
            preview = content.strip().replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
            if len(preview) > 160:
                preview = preview[:160] + "…"
            await self._store.add_pc_activity(
                PcActivity(
                    pc_id="dm",
                    kind="dm",
                    summary=f"DM：私信 {pc.name}（{preview}）" if preview else f"DM：私信 {pc.name}",
                    ref_type="message",
                    ref_id=msg.id,
                )
            )
            await self._write_memories_for_message(
                action_type="dm_digest_dm",
                actor_pc_id=None,
                actor_name="DM",
                message=msg,
                thread=None,
            )
            await self._ws.broadcast({"type": "message", "payload": msg.model_dump()})
            return [{"kind": "message", "message_id": msg.id}]

        return [{"kind": "noop"}]

    async def _run_dm_digest_if_due(self) -> None:
        if self._dm_digest_s <= 0:
            return
        if self._engine.is_paused():
            return

        now_dt = datetime.now(timezone.utc)
        until = now_dt.isoformat()

        last = await self._store.get_latest_tick_started_at(pc_id="dm")
        since: str
        if last:
            last_dt = self._parse_iso_utc_loose(last)
            if last_dt is not None and (now_dt - last_dt).total_seconds() < self._dm_digest_s:
                return
            since = last
        else:
            since = (now_dt - timedelta(seconds=max(0.0, self._dm_bootstrap_lookback_s))).isoformat()

        tick = TickRecord(pc_id="dm", status="running", action={"since": since, "until": until})
        await self._store.upsert_tick(tick)
        t0 = time.monotonic()
        refs: list[dict[str, Any]] = []
        try:
            ctx = await self._build_dm_digest_context(since=since, until=until)
            if ctx.get("required_type") == "noop":
                raw_action: Any = {"type": "noop", "reason": "no new items in digest window"}
            elif self._settings.demo_fake:
                raw_action = self._deterministic_dm_digest_action(ctx=ctx)
            else:
                raw_action = await self._llm_dm_digest_action(ctx=ctx)
            raw_for_validation: Any = raw_action
            if isinstance(raw_action, dict) and isinstance(raw_action.get("action"), dict):
                raw_for_validation = raw_action["action"]
            action, errors = self._validate_dm_digest_action(raw_for_validation, pc_ids=set(ctx["pc_ids"]))
            action = self._coerce_dm_digest_action(action=action, ctx=ctx)
            tick.action = raw_action if isinstance(raw_action, dict) else {"_raw": str(raw_action)}
            if errors:
                tick.error = "; ".join(errors)
            await self._store.upsert_tick(tick)
            refs = await self._apply_dm_digest_action(action=action)
        except Exception as exc:  # noqa: BLE001
            tick.status = "failed"
            tick.error = f"{type(exc).__name__}: {exc}"
            tick.duration_ms = int((time.monotonic() - t0) * 1000)
            await self._store.upsert_tick(tick)
            raise
        else:
            tick.status = "done"
            tick.result_refs = refs
            tick.duration_ms = int((time.monotonic() - t0) * 1000)
            await self._store.upsert_tick(tick)

    async def _deterministic_action(self, *, pc_id: str, turn_no: int) -> dict[str, Any]:
        """
        demo_fake mode: choose a deterministic action to exercise the pipeline.

        Priority:
        - If a forum channel exists: cycle create_thread -> reply -> dm -> noop
        - Else: cycle dm -> noop
        """
        convs = await self._store.list_conversations()
        forum_channels = [c.id for c in convs if c.kind == "forum"]
        has_forum = bool(forum_channels)

        if has_forum:
            m = turn_no % 4
            if m == 1:
                now = utc_now_iso()
                return {
                    "type": "create_thread",
                    "channel_id": forum_channels[0],
                    "title": f"自动 thread #{now[:19]}",
                    "content": f"【自动行动】创建 thread（{now[:19]}）",
                }
            if m == 2:
                threads = await self._store.list_forum_threads(forum_channels[0])
                if not threads:
                    return {
                        "type": "noop",
                        "reason": "no thread to reply",
                    }
                thread = next((t for t in threads if not t.locked), None)
                if thread is None:
                    return {"type": "noop", "reason": "no unlocked thread to reply"}
                now = utc_now_iso()
                return {
                    "type": "reply",
                    "channel_id": forum_channels[0],
                    "thread_id": thread.id,
                    "content": f"【自动行动】跟帖一条（{now[:19]}）。",
                }
            if m == 3:
                now = utc_now_iso()
                idx = next((i for i, p in enumerate(self._engine.pcs) if p.id == pc_id), 0)
                target = self._engine.pcs[(idx + 1) % len(self._engine.pcs)]
                return {
                    "type": "dm",
                    "to_pc_id": target.id,
                    "content": f"【自动私信】{target.name}，我这边刚同步一下进度（{now[:19]}）。",
                }
            return {"type": "noop", "reason": "idle"}

        if turn_no % 2 == 0:
            now = utc_now_iso()
            return {"type": "dm", "content": f"【自动私信】DM，我这边先报个平安（{now[:19]}）。"}
        return {"type": "noop", "reason": "no_forum_channel"}

    async def _apply_action(self, *, pc_id: str, action: CreateThreadAction | ReplyAction | DmAction | NoopAction) -> list[dict[str, Any]]:
        pc = next((p for p in self._engine.pcs if p.id == pc_id), None)
        if pc is None:
            raise ValueError(f"unknown pc_id: {pc_id}")

        if isinstance(action, CreateThreadAction):
            return await self._apply_create_thread(
                pc_id=pc.id,
                pc_name=pc.name,
                channel_id=action.channel_id,
                title=action.title,
                content=action.content,
            )

        if isinstance(action, ReplyAction):
            return await self._apply_reply(
                pc_id=pc.id,
                pc_name=pc.name,
                channel_id=action.channel_id,
                thread_id=action.thread_id,
                content=action.content,
            )

        if isinstance(action, DmAction):
            return await self._apply_dm(
                pc_id=pc.id,
                pc_name=pc.name,
                to_pc_id=action.to_pc_id,
                content=action.content,
            )

        if isinstance(action, NoopAction):
            summary = f"{pc.name}：无动作"
            if action.reason:
                summary = f"{summary}（{action.reason}）"
            await self._store.add_pc_activity(PcActivity(pc_id=pc.id, kind="noop", summary=summary))
            return [{"kind": "noop"}]

        raise ValueError("unreachable: unknown action model")

    async def _apply_create_thread(
        self, *, pc_id: str, pc_name: str, channel_id: str, title: str, content: str
    ) -> list[dict[str, Any]]:
        now = utc_now_iso()
        thread_id = f"{channel_id}:{uuid4()}"
        thread = ForumThread(
            id=thread_id,
            channel_id=channel_id,
            title=title,
            created_at=now,
            created_by=Actor(kind="pc", id=pc_id, name=pc_name),
            last_activity_at=now,
            reply_count=0,
            pinned=False,
            locked=False,
        )
        await self._store.upsert_forum_threads([thread])

        msg = Message(
            conversation_id=channel_id,
            channel="broadcast",
            thread_id=thread_id,
            from_actor=Actor(kind="pc", id=pc_id, name=pc_name),
            to=[],
            content=content,
        )
        await self._store.append_message(msg)

        await self._store.add_pc_activity(
            PcActivity(
                pc_id=pc_id,
                kind="thread_created",
                summary=f"{pc_name}：创建 thread《{title}》",
                ref_type="thread",
                ref_id=thread_id,
            )
        )

        await self._write_memories_for_message(
            action_type="create_thread",
            actor_pc_id=pc_id,
            actor_name=pc_name,
            message=msg,
            thread=thread,
        )

        await self._ws.broadcast({"type": "message", "payload": msg.model_dump()})
        await self._ws.broadcast({"type": "forum_thread", "payload": {"thread": thread.model_dump()}})

        return [{"kind": "thread", "thread_id": thread_id}, {"kind": "message", "message_id": msg.id}]

    async def _apply_reply(
        self, *, pc_id: str, pc_name: str, channel_id: str, thread_id: str, content: str
    ) -> list[dict[str, Any]]:
        thread = await self._store.get_forum_thread(thread_id)
        if not thread or thread.channel_id != channel_id:
            raise ValueError("thread not found or does not belong to channel")
        if thread.locked:
            await self._store.add_pc_activity(
                PcActivity(
                    pc_id=pc_id,
                    kind="reply_blocked",
                    summary=f"{pc_name}：尝试回复已锁定 thread《{thread.title}》（已跳过）",
                    ref_type="thread",
                    ref_id=thread.id,
                )
            )
            return [{"kind": "reply_blocked", "thread_id": thread.id}]

        msg = Message(
            conversation_id=channel_id,
            channel="broadcast",
            thread_id=thread.id,
            from_actor=Actor(kind="pc", id=pc_id, name=pc_name),
            to=[],
            content=content,
        )
        await self._store.append_message(msg)

        await self._store.add_pc_activity(
            PcActivity(
                pc_id=pc_id,
                kind="replied",
                summary=f"{pc_name}：回复 thread《{thread.title}》",
                ref_type="thread",
                ref_id=thread.id,
            )
        )

        await self._write_memories_for_message(
            action_type="reply",
            actor_pc_id=pc_id,
            actor_name=pc_name,
            message=msg,
            thread=thread,
        )

        await self._ws.broadcast({"type": "message", "payload": msg.model_dump()})
        return [{"kind": "message", "message_id": msg.id}, {"kind": "thread", "thread_id": thread.id}]

    async def _apply_dm(self, *, pc_id: str, pc_name: str, to_pc_id: str | None, content: str) -> list[dict[str, Any]]:
        """
        Direct message semantics:
        - DM↔PC uses the per-PC inbox conversation `dm_to_<pc_id>` (one conversation per PC).
        - PC↔PC is copied into both participants' inbox conversations to avoid conversation explosion:
            - @Alice (dm_to_pc_1) shows both incoming/outgoing involving Alice
            - @Bob   (dm_to_pc_2) shows both incoming/outgoing involving Bob
        """
        now = utc_now_iso()
        from_actor = Actor(kind="pc", id=pc_id, name=pc_name)

        if to_pc_id and to_pc_id == pc_id:
            await self._store.add_pc_activity(
                PcActivity(pc_id=pc_id, kind="dm_skipped", summary=f"{pc_name}：私信自己（跳过）")
            )
            return [{"kind": "dm_skipped"}]

        # PC -> DM (default target)
        if not to_pc_id:
            conv_id = f"dm_to_{pc_id}"
            msg = Message(
                conversation_id=conv_id,
                channel="direct",
                from_actor=from_actor,
                to=[Actor(kind="dm", id="dm", name="DM")],
                content=content,
            )
            await self._store.append_message(msg)
            await self._store.add_pc_activity(
                PcActivity(
                    pc_id=pc_id,
                    kind="dm_sent",
                    summary=f"{pc_name}：私信 DM（{now[:19]}）",
                    ref_type="message",
                    ref_id=msg.id,
                )
            )
            await self._write_memories_for_message(
                action_type="dm",
                actor_pc_id=pc_id,
                actor_name=pc_name,
                message=msg,
                thread=None,
            )
            await self._ws.broadcast({"type": "message", "payload": msg.model_dump()})
            return [{"kind": "message", "message_id": msg.id}]

        # PC -> PC (replicated)
        target = next((p for p in self._engine.pcs if p.id == to_pc_id), None)
        if target is None:
            await self._store.add_pc_activity(
                PcActivity(pc_id=pc_id, kind="dm_failed", summary=f"{pc_name}：私信失败（unknown pc_id: {to_pc_id}）")
            )
            return [{"kind": "dm_failed"}]

        sbid = str(uuid4())
        to_actor = Actor(kind="pc", id=target.id, name=target.name)

        sender_conv_id = f"dm_to_{pc_id}"
        receiver_conv_id = f"dm_to_{target.id}"

        msg_sender = Message(
            conversation_id=sender_conv_id,
            channel="direct",
            from_actor=from_actor,
            to=[to_actor],
            content=content,
            send_batch_id=sbid,
        )
        msg_receiver = Message(
            conversation_id=receiver_conv_id,
            channel="direct",
            from_actor=from_actor,
            to=[to_actor],
            content=content,
            send_batch_id=sbid,
        )

        await self._store.append_message(msg_sender)
        await self._store.append_message(msg_receiver)

        await self._store.add_pc_activity(
            PcActivity(
                pc_id=pc_id,
                kind="dm_sent",
                summary=f"{pc_name}：私信 {target.name}（{now[:19]}）",
                ref_type="message",
                ref_id=msg_sender.id,
            )
        )

        await self._write_memories_for_message(
            action_type="dm",
            actor_pc_id=pc_id,
            actor_name=pc_name,
            message=msg_sender,
            thread=None,
        )

        await self._ws.broadcast({"type": "message", "payload": msg_sender.model_dump()})
        await self._ws.broadcast({"type": "message", "payload": msg_receiver.model_dump()})
        return [
            {"kind": "message", "message_id": msg_sender.id},
            {"kind": "message", "message_id": msg_receiver.id},
            {"kind": "send_batch_id", "send_batch_id": sbid},
        ]
