from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .db import SqliteStore
from .engine import DemoEngine
from .models import Actor, ForumThread, Message, PcActivity, TickRecord, utc_now_iso
from .ws import ConnectionManager


@dataclass
class TickRunnerState:
    next_pc_index: int = 0
    turn_no: int = 0


class TickRunner:
    def __init__(
        self,
        *,
        store: SqliteStore,
        ws: ConnectionManager,
        engine: DemoEngine,
        tick_s: float = 60.0,
        state_key: str = "tick_runner_state",
    ) -> None:
        self._store = store
        self._ws = ws
        self._engine = engine
        self._tick_s = tick_s
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
        out = TickRunnerState()
        if isinstance(idx, int) and idx >= 0:
            out.next_pc_index = idx
        if isinstance(turn_no, int) and turn_no >= 0:
            out.turn_no = turn_no
        return out

    async def _save_state(self, state: TickRunnerState) -> None:
        await self._store.set_setting_json(
            self._state_key,
            json.dumps({"next_pc_index": state.next_pc_index, "turn_no": state.turn_no}, ensure_ascii=False),
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
            state.next_pc_index = (state.next_pc_index + 1) % max(1, len(self._engine.pcs))
            state.turn_no += 1
            await self._save_state(state)

            tick = TickRecord(pc_id=pc.id, status="running", action={})
            await self._store.upsert_tick(tick)

            t0 = time.monotonic()
            refs: list[dict[str, Any]] = []
            try:
                action = await self._deterministic_action(pc_id=pc.id, turn_no=state.turn_no)
                tick.action = action
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
                return {"type": "create_thread", "channel_id": forum_channels[0]}
            if m == 2:
                return {"type": "reply", "channel_id": forum_channels[0]}
            if m == 3:
                return {"type": "dm"}
            return {"type": "noop", "reason": "idle"}

        if turn_no % 2 == 0:
            return {"type": "dm"}
        return {"type": "noop", "reason": "no_forum_channel"}

    async def _apply_action(self, *, pc_id: str, action: dict[str, Any]) -> list[dict[str, Any]]:
        pc = next((p for p in self._engine.pcs if p.id == pc_id), None)
        if pc is None:
            raise ValueError(f"unknown pc_id: {pc_id}")

        a_type = action.get("type")
        if a_type == "create_thread":
            channel_id = action.get("channel_id")
            if not isinstance(channel_id, str) or not channel_id.strip():
                raise ValueError("create_thread.channel_id is required")
            return await self._apply_create_thread(pc_id=pc.id, pc_name=pc.name, channel_id=channel_id.strip())

        if a_type == "reply":
            channel_id = action.get("channel_id")
            if not isinstance(channel_id, str) or not channel_id.strip():
                raise ValueError("reply.channel_id is required")
            return await self._apply_reply(pc_id=pc.id, pc_name=pc.name, channel_id=channel_id.strip())

        if a_type == "dm":
            return await self._apply_dm_to_dm(pc_id=pc.id, pc_name=pc.name)

        if a_type == "noop":
            reason = action.get("reason")
            summary = f"{pc.name}：无动作"
            if isinstance(reason, str) and reason.strip():
                summary = f"{summary}（{reason.strip()}）"
            await self._store.add_pc_activity(PcActivity(pc_id=pc.id, kind="noop", summary=summary))
            return [{"kind": "noop"}]

        raise ValueError(f"unknown action.type: {a_type}")

    async def _apply_create_thread(self, *, pc_id: str, pc_name: str, channel_id: str) -> list[dict[str, Any]]:
        now = utc_now_iso()
        thread_id = f"{channel_id}:{uuid4()}"
        title = f"{pc_name} 的记录 #{now[:19]}"
        thread = ForumThread(
            id=thread_id,
            channel_id=channel_id,
            title=title,
            created_at=now,
            created_by=Actor(kind="pc", id=pc_id, name=pc_name),
            last_activity_at=now,
            reply_count=0,
        )
        await self._store.upsert_forum_threads([thread])

        content = f"【自动行动】{pc_name} 在 {now[:19]} 创建了一个新 thread，用于记录进展。"
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

        await self._ws.broadcast({"type": "message", "payload": msg.model_dump()})
        await self._ws.broadcast({"type": "forum_thread", "payload": {"thread": thread.model_dump()}})

        return [{"kind": "thread", "thread_id": thread_id}, {"kind": "message", "message_id": msg.id}]

    async def _apply_reply(self, *, pc_id: str, pc_name: str, channel_id: str) -> list[dict[str, Any]]:
        threads = await self._store.list_forum_threads(channel_id)
        if not threads:
            # fallback: create a thread instead
            return await self._apply_create_thread(pc_id=pc_id, pc_name=pc_name, channel_id=channel_id)

        thread = threads[0]
        now = utc_now_iso()
        content = f"【自动行动】{pc_name}：回顾一下，目前我倾向于把信息整理在这个 thread。({now[:19]})"
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

        await self._ws.broadcast({"type": "message", "payload": msg.model_dump()})
        return [{"kind": "message", "message_id": msg.id}, {"kind": "thread", "thread_id": thread.id}]

    async def _apply_dm_to_dm(self, *, pc_id: str, pc_name: str) -> list[dict[str, Any]]:
        now = utc_now_iso()
        conv_id = f"dm_to_{pc_id}"
        content = f"【自动行动】{pc_name}：我准备在下一次行动里推进一个小目标。({now[:19]})"
        msg = Message(
            conversation_id=conv_id,
            channel="direct",
            from_actor=Actor(kind="pc", id=pc_id, name=pc_name),
            to=[Actor(kind="dm", id="dm", name="DM")],
            content=content,
        )
        await self._store.append_message(msg)
        await self._store.add_pc_activity(
            PcActivity(
                pc_id=pc_id,
                kind="dm_sent",
                summary=f"{pc_name}：私信 DM",
                ref_type="message",
                ref_id=msg.id,
            )
        )
        await self._ws.broadcast({"type": "message", "payload": msg.model_dump()})
        return [{"kind": "message", "message_id": msg.id}]
