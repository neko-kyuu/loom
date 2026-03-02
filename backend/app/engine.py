from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .db import SqliteStore
from .llm import LlmService, openai_chat_completions_url, parse_llm_response
from .models import Actor, Conversation, Message
from .settings import Settings
from .ws import ConnectionManager


@dataclass
class PC:
    id: str
    name: str
    model: str | None = None
    persona: str | None = None


@dataclass(frozen=True)
class DM:
    id: str = "dm"
    name: str = "DM"
    model: str | None = None
    persona: str | None = None


@dataclass(frozen=True)
class ForumChannel:
    id: str
    title: str
    description: str | None = None


@dataclass(frozen=True)
class Job:
    conversation_id: str
    pc: PC
    prompt: str
    thread_id: str | None = None


class DemoEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        store: SqliteStore,
        ws: ConnectionManager,
        llm: LlmService | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._ws = ws
        self._llm = llm

        self._paused = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._worker_tasks: list[asyncio.Task[None]] = []

        self.dm = DM(
            model=settings.openai_dm_model or settings.openai_model,
            persona=settings.openai_dm_persona,
        )

        self.pcs: list[PC] = [
            PC(id="pc_1", name="Alice"),
            PC(id="pc_2", name="Bob"),
            PC(id="pc_3", name="Cathy"),
            PC(id="pc_4", name="Dylan"),
        ]
        self._apply_llm_profiles()

    def _apply_llm_profiles(self) -> None:
        for pc in self.pcs:
            model = self._settings.openai_pc_models.get(pc.id)
            pc.model = model if isinstance(model, str) and model.strip() else self._settings.openai_model
            persona = self._settings.openai_pc_personas.get(pc.id)
            pc.persona = (
                persona if isinstance(persona, str) and persona.strip() else f"你是{pc.name}。回复简短明确。"
            )

    @staticmethod
    def _history_speaker_name(m: Message) -> str:
        if m.from_actor.kind == "user":
            return "用户"
        if m.from_actor.kind == "dm":
            return "DM"
        return m.from_actor.name or m.from_actor.id or m.from_actor.kind

    @classmethod
    def _format_history_as_table(cls, history: list[Message], limit: int = 40) -> str:
        lines = ['|发言人name（用户固定“用户”，DM固定“DM”|发言内容']
        for m in history[-limit:]:
            speaker = cls._history_speaker_name(m)
            content = (m.content or "").strip()
            if not content:
                continue
            content = content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
            content = content.replace("|", "\\|")
            speaker = speaker.replace("|", "\\|")
            if len(content) > 800:
                content = content[:800] + "…"
            lines.append(f"|{speaker}|{content}")
        return "\n".join(lines)

    def apply_profiles_state(self, profiles_state: Any) -> None:
        """
        Sync PC display names from frontend profiles_state payload.

        Expected shape:
          { "profiles": [ { "id": "pc_1", "kind": "pc", "displayName": "..." }, ... ] }
        """
        if not isinstance(profiles_state, dict):
            return
        raw_profiles = profiles_state.get("profiles")
        if not isinstance(raw_profiles, list):
            return

        display_name_by_id: dict[str, str] = {}
        for item in raw_profiles:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            if kind != "pc":
                continue
            pid = item.get("id")
            name = item.get("displayName")
            if not isinstance(pid, str) or not pid.strip():
                continue
            if not isinstance(name, str) or not name.strip():
                continue
            display_name_by_id[pid.strip()] = name.strip()

        if not display_name_by_id:
            return

        for pc in self.pcs:
            new_name = display_name_by_id.get(pc.id)
            if new_name:
                pc.name = new_name
        self._apply_llm_profiles()

    def build_conversations(self, *, forum_channels: list[ForumChannel], broadcast_description: str | None = None) -> list[Conversation]:
        broadcast = Conversation(
            id="broadcast",
            kind="broadcast",
            title="#broadcast",
            description=broadcast_description,
            participants=[Actor(kind="dm", id="dm", name="DM")]
            + [Actor(kind="pc", id=p.id, name=p.name) for p in self.pcs],
        )
        forums = [
            Conversation(
                id=ch.id,
                kind="forum",
                title=ch.title,
                description=ch.description,
                participants=[Actor(kind="dm", id="dm", name="DM")]
                + [Actor(kind="pc", id=p.id, name=p.name) for p in self.pcs],
            )
            for ch in forum_channels
        ]
        dm_to_pc = [
            Conversation(
                id=f"dm_to_{p.id}",
                kind="dm_to_pc",
                title=f"DM → {p.name}",
                description=f"DM 与 {p.name} 的私聊会话",
                participants=[Actor(kind="dm", id="dm", name="DM"), Actor(kind="pc", id=p.id, name=p.name)],
            )
            for p in self.pcs
        ]
        return [broadcast, *forums, *dm_to_pc]

    def build_default_conversations(self) -> list[Conversation]:
        return self.build_conversations(forum_channels=[], broadcast_description=None)

    async def start(self) -> None:
        if not self._worker_tasks:
            n = max(1, len(self.pcs))
            self._worker_tasks = [
                asyncio.create_task(self._worker(i), name=f"demo-engine-worker-{i}") for i in range(n)
            ]

    async def set_paused(self, paused: bool) -> None:
        self._paused = paused
        if paused:
            self._pause_event.clear()
        else:
            self._pause_event.set()
        await self._broadcast_queue_state()

    async def enqueue_pc_reaction(
        self, *, conversation_id: str, pc_id: str, prompt: str, thread_id: str | None = None
    ) -> None:
        pc = next((p for p in self.pcs if p.id == pc_id), None)
        if pc is None:
            raise ValueError(f"unknown pc_id: {pc_id}")
        await self._queue.put(Job(conversation_id=conversation_id, pc=pc, prompt=prompt, thread_id=thread_id))
        await self._broadcast_queue_state()

    async def _broadcast_queue_state(self) -> None:
        await self._ws.broadcast(
            {
                "type": "queue",
                "payload": {"paused": self._paused, "queued": self._queue.qsize()},
            }
        )

    async def _worker(self, _worker_id: int) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._pause_event.wait()
                await self._ws.broadcast(
                    {
                        "type": "typing",
                        "payload": {
                            "conversation_id": job.conversation_id,
                            "pc_id": job.pc.id,
                            "value": True,
                        },
                    }
                )
                if self._settings.demo_fake:
                    await asyncio.sleep(max(0, self._settings.demo_fake_latency_ms) / 1000)

                reply = await self._pc_reply(job)
                message = Message(
                    conversation_id=job.conversation_id,
                    channel="direct" if job.conversation_id != "broadcast" else "broadcast",
                    thread_id=job.thread_id,
                    from_actor=Actor(kind="pc", id=job.pc.id, name=job.pc.name),
                    to=[Actor(kind="dm", id="dm", name="DM")],
                    content=reply,
                )
                await self._store.add_message(message)
                await self._ws.broadcast({"type": "message", "payload": message.model_dump()})
            finally:
                await self._ws.broadcast(
                    {
                        "type": "typing",
                        "payload": {
                            "conversation_id": job.conversation_id,
                            "pc_id": job.pc.id,
                            "value": False,
                        },
                    }
                )
                self._queue.task_done()
                await self._broadcast_queue_state()

    def _fake_pc_reply(self, pc: PC, prompt: str) -> str:
        return f"{pc.name}：收到。({prompt})"

    async def dm_forward(
        self,
        *,
        content: str,
        pc: PC | None,
        conversation_id: str,
        thread_id: str | None,
    ) -> str:
        """
        Convert user's message into a DM message (optionally tailored to a specific PC).
        """
        if self._settings.demo_fake or self._llm is None:
            return content
        if not (self._settings.openai_base_url and self._settings.openai_api_key and self.dm.model):
            return content

        target_hint = f"面向 {pc.name}" if pc is not None else "面向所有PC"
        system = (self.dm.persona or "").strip() or "你是DM。"
        if thread_id:
            history = await self._store.list_messages_by_thread(thread_id, limit=200)
        else:
            history = await self._store.list_messages(conversation_id, limit=200)
        history_text = self._format_history_as_table(history, limit=60)
        messages = [
            {
                "role": "system",
                "content": (
                    f"{system}\n\n"
                    f"以下为最近对话记录：\n{history_text}\n\n"
                    f"{target_hint}。把用户的话转述/整理成你要对PC说的话；简短明确，不要复述提示词。"
                ),
            },
            {"role": "user", "content": content},
        ]

        url = openai_chat_completions_url(self._settings.openai_base_url)
        res = await self._llm.chat(
            url=url,
            apikey=self._settings.openai_api_key,
            model=self.dm.model,
            messages=messages,
            tools=None,
        )
        parsed = res.get("parsed") if isinstance(res, dict) else None
        if isinstance(parsed, dict) and parsed.get("kind") == "markdown":
            out = parsed.get("markdown")
            if isinstance(out, str) and out.strip():
                return out.strip()

        raw = res.get("raw") if isinstance(res, dict) else None
        p2 = parse_llm_response(raw)
        if p2["kind"] == "markdown" and isinstance(p2["markdown"], str):
            return p2["markdown"].strip()
        try:
            return json.dumps(p2["structured"], ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return content

    async def _pc_reply(self, job: Job) -> str:
        if self._settings.demo_fake or self._llm is None:
            return self._fake_pc_reply(job.pc, job.prompt)
        if not (self._settings.openai_base_url and self._settings.openai_api_key and job.pc.model):
            return self._fake_pc_reply(job.pc, job.prompt)

        if job.thread_id:
            history = await self._store.list_messages_by_thread(job.thread_id, limit=200)
        else:
            history = await self._store.list_messages(job.conversation_id, limit=200)
        history = await self._filter_private_history_for_pc(history, pc_id=job.pc.id)
        system = (job.pc.persona or "").strip() or "你是一个PC角色。"
        history_text = self._format_history_as_table(history, limit=40)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": f"{system}\n\n以下为对话记录：\n{history_text}\n\n你只需用一段简短中文回复。",
            },
            {"role": "user", "content": job.prompt},
        ]

        url = openai_chat_completions_url(self._settings.openai_base_url)
        res = await self._llm.chat(
            url=url,
            apikey=self._settings.openai_api_key,
            model=job.pc.model,
            messages=messages,
            tools=None,
        )
        parsed = res.get("parsed") if isinstance(res, dict) else None
        if isinstance(parsed, dict) and parsed.get("kind") == "markdown":
            out = parsed.get("markdown")
            if isinstance(out, str) and out.strip():
                return out.strip()

        raw = res.get("raw") if isinstance(res, dict) else None
        p2 = parse_llm_response(raw)
        if p2["kind"] == "markdown" and isinstance(p2["markdown"], str):
            return p2["markdown"].strip()
        try:
            return json.dumps(p2["structured"], ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return self._fake_pc_reply(job.pc, job.prompt)

    async def _filter_private_history_for_pc(self, history: list[Message], *, pc_id: str) -> list[Message]:
        """
        Privacy rule:
        - Any message with channel="direct" may only appear in the LLM request body for its private target PC(s).
        - Targets are resolved by looking up all DB rows with the same send_batch_id and collecting payload.to pc ids.
        """
        private_conv_id = f"dm_to_{pc_id}"
        targets_by_batch: dict[str, set[str]] = {}
        out: list[Message] = []
        for m in history:
            if m.channel != "direct":
                out.append(m)
                continue

            if m.conversation_id == private_conv_id:
                out.append(m)
                continue

            sbid = m.send_batch_id
            if not isinstance(sbid, str) or not sbid.strip():
                continue

            targets = targets_by_batch.get(sbid)
            if targets is None:
                rows = await self._store.list_messages_by_send_batch_id(sbid, limit=200)
                targets = set()
                for r in rows:
                    for a in r.to or []:
                        if a.kind == "pc" and isinstance(a.id, str) and a.id.strip():
                            targets.add(a.id.strip())
                targets_by_batch[sbid] = targets

            if pc_id in targets:
                out.append(m)
        return out

    @staticmethod
    def new_send_batch_id() -> str:
        return str(uuid4())
