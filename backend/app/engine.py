from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .db import SqliteStore
from .models import Actor, Conversation, Message
from .settings import Settings
from .ws import ConnectionManager


@dataclass(frozen=True)
class PC:
    id: str
    name: str


@dataclass(frozen=True)
class Job:
    conversation_id: str
    pc: PC
    prompt: str


class DemoEngine:
    def __init__(self, *, settings: Settings, store: SqliteStore, ws: ConnectionManager) -> None:
        self._settings = settings
        self._store = store
        self._ws = ws

        self._paused = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._runner_task: asyncio.Task | None = None

        self.pcs: list[PC] = [
            PC(id="pc_1", name="Alice"),
            PC(id="pc_2", name="Bob"),
            PC(id="pc_3", name="Cathy"),
            PC(id="pc_4", name="Dylan"),
        ]

    def build_default_conversations(self) -> list[Conversation]:
        broadcast = Conversation(
            id="broadcast",
            kind="broadcast",
            title="#broadcast",
            participants=[Actor(kind="dm", id="dm", name="DM")]
            + [Actor(kind="pc", id=p.id, name=p.name) for p in self.pcs],
        )
        dm_to_pc = [
            Conversation(
                id=f"dm_to_{p.id}",
                kind="dm_to_pc",
                title=f"DM → {p.name}",
                participants=[Actor(kind="dm", id="dm", name="DM"), Actor(kind="pc", id=p.id, name=p.name)],
            )
            for p in self.pcs
        ]
        return [broadcast, *dm_to_pc]

    async def start(self) -> None:
        if self._runner_task is None:
            self._runner_task = asyncio.create_task(self._runner(), name="demo-engine-runner")

    async def set_paused(self, paused: bool) -> None:
        self._paused = paused
        if paused:
            self._pause_event.clear()
        else:
            self._pause_event.set()
        await self._broadcast_queue_state()

    async def enqueue_pc_reaction(self, *, conversation_id: str, pc_id: str, prompt: str) -> None:
        pc = next((p for p in self.pcs if p.id == pc_id), None)
        if pc is None:
            raise ValueError(f"unknown pc_id: {pc_id}")
        await self._queue.put(Job(conversation_id=conversation_id, pc=pc, prompt=prompt))
        await self._broadcast_queue_state()

    async def _broadcast_queue_state(self) -> None:
        await self._ws.broadcast(
            {
                "type": "queue",
                "payload": {"paused": self._paused, "queued": self._queue.qsize()},
            }
        )

    async def _runner(self) -> None:
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
                await asyncio.sleep(max(0, self._settings.demo_fake_latency_ms) / 1000)

                reply = self._fake_pc_reply(job.pc, job.prompt)
                message = Message(
                    conversation_id=job.conversation_id,
                    channel="direct" if job.conversation_id != "broadcast" else "broadcast",
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

    @staticmethod
    def new_send_batch_id() -> str:
        return str(uuid4())

