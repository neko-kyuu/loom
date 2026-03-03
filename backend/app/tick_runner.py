from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .actions import (
    ActionValidationContext,
    CreateThreadAction,
    DmAction,
    NoopAction,
    ReplyAction,
    validate_action,
)
from .db import SqliteStore
from .engine import DemoEngine
from .llm import LlmService, openai_chat_completions_url, parse_llm_response
from .models import Actor, ForumThread, Message, PcActivity, TickRecord, utc_now_iso
from .settings import Settings
from .ws import ConnectionManager


@dataclass
class TickRunnerState:
    next_pc_index: int = 0
    turn_no: int = 0
    last_turn_by_pc: dict[str, str] | None = None


class TickRunner:
    def __init__(
        self,
        *,
        store: SqliteStore,
        ws: ConnectionManager,
        engine: DemoEngine,
        settings: Settings,
        llm: LlmService | None = None,
        tick_s: float = 60.0,
        state_key: str = "tick_runner_state",
    ) -> None:
        self._store = store
        self._ws = ws
        self._engine = engine
        self._settings = settings
        self._llm = llm
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
                action, errors = validate_action(raw_for_validation, ctx=ctx)
                tick.action = raw_action if isinstance(raw_action, dict) else {"_raw": str(raw_action)}
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
    def _summarize_message(m: Message, max_len: int = 200) -> str:
        who = m.from_actor.name or m.from_actor.kind
        content = (m.content or "").strip().replace("\r\n", "\n").replace("\r", "\n")
        if len(content) > max_len:
            content = content[:max_len] + "…"
        content = content.replace("\n", "\\n")
        return f"{m.timestamp[:19]} {who}: {content}"

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

    async def _llm_action(self, *, pc_id: str, pc_name: str, persona: str | None, since: str | None) -> dict[str, Any]:
        """
        Ask the LLM to output a single JSON action (no tool-calling).
        """
        if self._llm is None:
            return {"type": "noop", "reason": "llm not configured"}
        if not (self._settings.openai_base_url and self._settings.openai_api_key):
            return {"type": "noop", "reason": "missing openai_base_url/api_key"}

        # context: recall
        recall_items = await self._store.list_pc_activity(pc_id, since=since, limit=30)
        recall = [
            {
                "time": a.timestamp,
                "kind": a.kind,
                "summary": a.summary,
                "ref_type": a.ref_type,
                "ref_id": a.ref_id,
            }
            for a in recall_items
        ]

        # context: inbox digest (DM<->PC)
        inbox_msgs = await self._store.list_messages(f"dm_to_{pc_id}", limit=80)
        if since:
            inbox_msgs = [m for m in inbox_msgs if m.timestamp >= since]
        inbox_lines = [self._summarize_message(m) for m in inbox_msgs[-12:]]

        # context: active threads digest
        convs = await self._store.list_conversations()
        forum_convs = [c for c in convs if c.kind == "forum"]
        threads_all: list[dict[str, Any]] = []
        for c in forum_convs:
            threads = await self._store.list_forum_threads(c.id)
            for t in threads[:30]:
                threads_all.append(
                    {
                        "channel_id": t.channel_id,
                        "thread_id": t.id,
                        "title": t.title,
                        "last_activity_at": t.last_activity_at,
                        "reply_count": t.reply_count,
                    }
                )
        threads_all.sort(key=lambda x: str(x.get("last_activity_at") or ""), reverse=True)
        threads_digest = threads_all[:12]

        forum_channels = [{"id": c.id, "title": c.title, "description": c.description} for c in forum_convs]

        system = (persona or "").strip() or f"你是{pc_name}。"
        rules = {
            "output": "你必须只输出 1 个 JSON object（不是数组）。禁止输出 Markdown/代码块/解释文字。",
            "setting": "你正在一个虚拟的论坛里“上网”。你可以在论坛里发帖（thread）、回复、私信其他人，或者选择暂不行动（noop）。",
            "bias": [
                "你可以采取的行动如下，无优先级区分：",
                "- create_thread ：没找到想聊的帖子？新建主题贴，展示你的表达欲",
                "- reply ：回复现有的帖子",
                "- dm ：有悄悄话想说？私信 DM管理员 或与其他 PC 进行私密的社交吧",
                "只有在确实没有可做的事、或缺少必要信息时才选择 noop，并在 reason 里说明你缺少什么。",
            ],
            "actions": [
                {"type": "create_thread", "required_fields": ["type", "channel_id", "title", "content"]},
                {"type": "reply", "required_fields": ["type", "channel_id", "thread_id", "content"]},
                {"type": "dm", "required_fields": ["type", "content"], "optional_fields": ["to_pc_id"]},
                {"type": "noop", "required_fields": ["type"], "optional_fields": ["reason"]},
            ],
            "hard_constraints": [
                "channel_id 必须来自 forum_channels[].id",
                "thread_id 必须来自 threads_digest[].thread_id，且必须属于所选 channel_id",
                "create_thread.title <= 80 chars；create_thread/reply.content <= 1200 chars；dm.content <= 800 chars",
                "dm: 省略 to_pc_id 表示发给 DM；填写 to_pc_id 表示发给某个 PC（必须是 pcs[].id 且不能等于 pc_id）",
            ],
            "writing_style": [
                "用符合你的人设的语气发言，论坛很自由，没有硬性的格式规定。",
                "避免空泛表态（如“收到/好的/我会努力”）。",
            ],
        }

        user_payload = {
            "pc_id": pc_id,
            "pc_name": pc_name,
            "since": since,
            "pcs": [{"id": p.id, "name": p.name} for p in self._engine.pcs],
            "forum_channels": forum_channels,
            "threads_digest": threads_digest,
            "inbox_digest": inbox_lines,
            "recall": recall,
        }

        messages = [
            {"role": "system", "content": f"{system}\n\nYou must follow the rules strictly:\n{json.dumps(rules, ensure_ascii=False)}"},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]

        model = getattr(next((p for p in self._engine.pcs if p.id == pc_id), None), "model", None)
        if not isinstance(model, str) or not model.strip():
            model = self._settings.openai_model
        if not isinstance(model, str) or not model.strip():
            return {"type": "noop", "reason": "missing model"}

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
                now = utc_now_iso()
                return {
                    "type": "reply",
                    "channel_id": forum_channels[0],
                    "thread_id": threads[0].id,
                    "content": f"【自动行动】回复一下（{now[:19]}）",
                }
            if m == 3:
                now = utc_now_iso()
                idx = next((i for i, p in enumerate(self._engine.pcs) if p.id == pc_id), 0)
                target = self._engine.pcs[(idx + 1) % len(self._engine.pcs)]
                return {
                    "type": "dm",
                    "to_pc_id": target.id,
                    "content": f"【自动私信】{now[:19]} 你方便同步一下进展吗？",
                }
            return {"type": "noop", "reason": "idle"}

        if turn_no % 2 == 0:
            now = utc_now_iso()
            return {"type": "dm", "content": f"【自动私信】{now[:19]} 我准备推进一个小目标。"}
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

        await self._ws.broadcast({"type": "message", "payload": msg.model_dump()})
        await self._ws.broadcast({"type": "forum_thread", "payload": {"thread": thread.model_dump()}})

        return [{"kind": "thread", "thread_id": thread_id}, {"kind": "message", "message_id": msg.id}]

    async def _apply_reply(
        self, *, pc_id: str, pc_name: str, channel_id: str, thread_id: str, content: str
    ) -> list[dict[str, Any]]:
        thread = await self._store.get_forum_thread(thread_id)
        if not thread or thread.channel_id != channel_id:
            raise ValueError("thread not found or does not belong to channel")

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

        await self._ws.broadcast({"type": "message", "payload": msg_sender.model_dump()})
        await self._ws.broadcast({"type": "message", "payload": msg_receiver.model_dump()})
        return [
            {"kind": "message", "message_id": msg_sender.id},
            {"kind": "message", "message_id": msg_receiver.id},
            {"kind": "send_batch_id", "send_batch_id": sbid},
        ]
