from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

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
        dm_digest_s: float = 600.0,
        dm_bootstrap_lookback_s: float = 600.0,
        state_key: str = "tick_runner_state",
    ) -> None:
        self._store = store
        self._ws = ws
        self._engine = engine
        self._settings = settings
        self._llm = llm
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

                if isinstance(raw_for_validation, dict) and str(raw_for_validation.get("type") or "") == "reply_select":
                    refs, errors = await self._apply_reply_select_round1(
                        pc_id=pc.id,
                        pc_name=pc.name,
                        raw=raw_for_validation,
                        ctx=ctx,
                    )
                    if errors:
                        tick.error = "; ".join(errors)
                        await self._store.upsert_tick(tick)
                        refs = await self._apply_action(
                            pc_id=pc.id,
                            action=NoopAction(type="noop", reason="invalid reply_select"),
                        )
                    else:
                        round2_refs, round2_errors = await self._run_reply_write_round2(
                            pc_id=pc.id,
                            pc_name=pc.name,
                            persona=pc.persona,
                            refs=refs,
                            ctx=ctx,
                        )
                        refs.extend(round2_refs)
                        if round2_errors:
                            tick.error = "; ".join(round2_errors)
                        await self._store.upsert_tick(tick)
                elif isinstance(raw_for_validation, dict) and str(raw_for_validation.get("type") or "") == "dm_select":
                    refs, errors = await self._apply_dm_select_round1(
                        pc_id=pc.id,
                        pc_name=pc.name,
                        raw=raw_for_validation,
                        ctx=ctx,
                    )
                    if errors:
                        tick.error = "; ".join(errors)
                        await self._store.upsert_tick(tick)
                        refs = await self._apply_action(
                            pc_id=pc.id,
                            action=NoopAction(type="noop", reason="invalid dm_select"),
                        )
                    else:
                        round2_refs, round2_errors = await self._run_dm_write_round2(
                            pc_id=pc.id,
                            pc_name=pc.name,
                            persona=pc.persona,
                            refs=refs,
                            ctx=ctx,
                        )
                        refs.extend(round2_refs)
                        if round2_errors:
                            tick.error = "; ".join(round2_errors)
                        await self._store.upsert_tick(tick)
                elif isinstance(raw_for_validation, dict) and str(raw_for_validation.get("type") or "") == "reply":
                    tick.error = "reply is not allowed in round 1; use reply_select"
                    await self._store.upsert_tick(tick)
                    refs = await self._apply_action(
                        pc_id=pc.id,
                        action=NoopAction(type="noop", reason="reply not allowed in round 1"),
                    )
                elif isinstance(raw_for_validation, dict) and str(raw_for_validation.get("type") or "") == "dm":
                    tick.error = "dm is not allowed in round 1; use dm_select"
                    await self._store.upsert_tick(tick)
                    refs = await self._apply_action(
                        pc_id=pc.id,
                        action=NoopAction(type="noop", reason="dm not allowed in round 1"),
                    )
                else:
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

            await self._run_dm_digest_if_due()

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

    async def _run_reply_write_round2(
        self,
        *,
        pc_id: str,
        pc_name: str,
        persona: str | None,
        refs: list[dict[str, Any]],
        ctx: ActionValidationContext,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """
        Round 2: if Round 1 produced a `reply_select` with `thread_context`, ask the model to write the reply
        and then apply it as a normal `ReplyAction`.
        """
        sel = next((r for r in refs if isinstance(r, dict) and r.get("kind") == "reply_select"), None)
        if not isinstance(sel, dict):
            return [], []
        channel_id = self._clean_str(sel.get("channel_id"))
        thread_id = self._clean_str(sel.get("thread_id"))
        thread_context = sel.get("thread_context")
        if not channel_id or not thread_id or not isinstance(thread_context, dict):
            return [], ["reply_write missing selection context"]

        # If the thread got locked between round 1 and now, abort safely.
        thread = await self._store.get_forum_thread(thread_id)
        if thread is None or thread.channel_id != channel_id:
            return [], ["reply_write selected thread not found"]
        if thread.locked:
            await self._store.add_pc_activity(
                PcActivity(
                    pc_id=pc_id,
                    kind="reply_blocked",
                    summary=f"{pc_name}：准备回复但 thread《{thread.title}》已锁定（已跳过）",
                    ref_type="thread",
                    ref_id=thread.id,
                )
            )
            return [{"kind": "reply_blocked", "thread_id": thread.id}], []

        if self._settings.demo_fake:
            now = utc_now_iso()
            raw: dict[str, Any] = {
                "type": "reply",
                "channel_id": channel_id,
                "thread_id": thread_id,
                "content": f"【自动行动】我先跟一句（{now[:19]}）。",
            }
        else:
            raw = await self._llm_reply_write_action(
                pc_id=pc_id,
                pc_name=pc_name,
                persona=persona,
                selected_thread=dict(thread_context.get("thread") or {}),
                thread_context=thread_context,
            )

        action, errors = validate_action(raw, ctx=ctx)
        # Enforce that we only reply to the selected thread in round 2.
        if isinstance(action, ReplyAction) and (action.channel_id != channel_id or action.thread_id != thread_id):
            errors = [*errors, "reply_write must reply to the selected thread_id/channel_id"]
            action = NoopAction(type="noop", reason="reply_write mismatch selection")

        applied = await self._apply_action(pc_id=pc_id, action=action)
        return applied, errors

    async def _apply_reply_select_round1(
        self,
        *,
        pc_id: str,
        pc_name: str,
        raw: dict[str, Any],
        ctx: ActionValidationContext,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """
        Round 1 only: accept a `reply_select` intermediate output.

        - Valid selection is recorded to pc_activity for recall/debugging.
        - No forum message is posted in Round 1.
        """
        errors: list[str] = []

        channel_id = self._clean_str(raw.get("channel_id"))
        thread_id = self._clean_str(raw.get("thread_id"))
        selection_reason = self._clean_str(raw.get("selection_reason"))
        if selection_reason and len(selection_reason) > 120:
            selection_reason = selection_reason[:120] + "…"

        if not channel_id:
            errors.append("reply_select.channel_id is required")
        elif channel_id not in ctx.forum_channel_ids:
            errors.append("reply_select.channel_id must be an existing forum channel id")

        if not thread_id:
            errors.append("reply_select.thread_id is required")
        else:
            ch = ctx.thread_channel_by_id.get(thread_id)
            if not ch:
                errors.append("reply_select.thread_id must be an existing thread id")
            elif channel_id and ch != channel_id:
                errors.append("reply_select.thread_id must belong to reply_select.channel_id")

        if errors:
            return [], errors

        thread = await self._store.get_forum_thread(thread_id)
        if thread is None or thread.channel_id != channel_id:
            return [], ["reply_select.thread_id must belong to reply_select.channel_id"]
        if thread.locked:
            await self._store.add_pc_activity(
                PcActivity(
                    pc_id=pc_id,
                    kind="reply_select_blocked",
                    summary=f"{pc_name}：选择回复已锁定 thread《{thread.title}》（已跳过）",
                    ref_type="thread",
                    ref_id=thread.id,
                )
            )
            return [{"kind": "reply_select_blocked", "thread_id": thread.id}], []

        reason_part = f"（{selection_reason}）" if selection_reason else ""
        await self._store.add_pc_activity(
            PcActivity(
                pc_id=pc_id,
                kind="reply_selected",
                summary=f"{pc_name}：选择回复 thread《{thread.title}》{reason_part}（等待展开上下文）",
                ref_type="thread",
                ref_id=thread.id,
            )
        )
        thread_ctx = await self._store.get_thread_context(
            thread_id=thread.id,
            channel_id=channel_id,
            recent_n=12,
            max_chars_per_post=1200,
            op_max_chars=1600,
        )
        return [
            {
                "kind": "reply_select",
                "channel_id": channel_id,
                "thread_id": thread.id,
                "thread_context": thread_ctx,
            }
        ], []

    async def _llm_reply_write_action(
        self,
        *,
        pc_id: str,
        pc_name: str,
        persona: str | None,
        selected_thread: dict[str, Any],
        thread_context: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Ask the LLM to write the reply content for the selected thread (JSON-only).
        """
        if self._llm is None:
            return {"type": "noop", "reason": "llm not configured"}
        if not (self._settings.openai_base_url and self._settings.openai_api_key):
            return {"type": "noop", "reason": "missing openai_base_url/api_key"}

        persona_text = (persona or "").strip() or f"你是{pc_name}。"
        messages = render_prompt_messages(
            "tick_runner.reply_write",
            {
                "pc_id": pc_id,
                "pc_name": pc_name,
                "persona": persona_text,
                "selected_thread_json": json.dumps(selected_thread, ensure_ascii=False),
                "thread_context_json": json.dumps(thread_context, ensure_ascii=False),
            },
        )

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

    def _pack_dm_message(self, m: Message, *, max_chars: int = 800) -> dict[str, object]:
        def trim_text(text: str, *, max_len: int) -> str:
            s = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            if len(s) > max_len:
                return s[:max_len] + "…"
            return s

        return {
            "id": m.id,
            "timestamp": m.timestamp,
            "from": (m.from_actor.name or m.from_actor.kind),
            "to": [a.name or a.kind for a in (m.to or [])],
            "content": trim_text(m.content, max_len=max(0, int(max_chars))),
        }

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

        return {
            "peer": {"kind": peer_kind, "id": peer_id, "name": peer_name},
            "recent_messages": [self._pack_dm_message(m, max_chars=max_chars_per_message) for m in tail],
        }

    async def _run_dm_write_round2(
        self,
        *,
        pc_id: str,
        pc_name: str,
        persona: str | None,
        refs: list[dict[str, Any]],
        ctx: ActionValidationContext,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """
        Round 2: if Round 1 produced a `dm_select` with `dm_context`, ask the model to write the DM reply
        and then apply it as a normal `DmAction`.
        """
        sel = next((r for r in refs if isinstance(r, dict) and r.get("kind") == "dm_select"), None)
        if not isinstance(sel, dict):
            return [], []
        to_pc_id = self._clean_str(sel.get("to_pc_id"))
        dm_context = sel.get("dm_context")
        if to_pc_id and to_pc_id not in ctx.pc_ids:
            return [], ["dm_write selected pc_id not found"]
        if not isinstance(dm_context, dict):
            return [], ["dm_write missing selection context"]

        if self._settings.demo_fake:
            now = utc_now_iso()
            raw: dict[str, Any] = {
                "type": "dm",
                "content": f"【自动私信】（{now[:19]}）收到，我先跟进一下。",
            }
            if to_pc_id:
                raw["to_pc_id"] = to_pc_id
        else:
            raw = await self._llm_dm_write_action(
                pc_id=pc_id,
                pc_name=pc_name,
                persona=persona,
                selected_target=dict(dm_context.get("peer") or {}),
                dm_context=dm_context,
            )

        action, errors = validate_action(raw, ctx=ctx)
        # Enforce that we only DM the selected target in round 2.
        if isinstance(action, DmAction):
            if to_pc_id and action.to_pc_id != to_pc_id:
                errors = [*errors, "dm_write must dm the selected to_pc_id"]
                action = NoopAction(type="noop", reason="dm_write mismatch selection")
            if not to_pc_id and action.to_pc_id is not None:
                errors = [*errors, "dm_write must omit to_pc_id when selecting DM"]
                action = NoopAction(type="noop", reason="dm_write mismatch selection")

        applied = await self._apply_action(pc_id=pc_id, action=action)
        return applied, errors

    async def _apply_dm_select_round1(
        self,
        *,
        pc_id: str,
        pc_name: str,
        raw: dict[str, Any],
        ctx: ActionValidationContext,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """
        Round 1 only: accept a `dm_select` intermediate output.

        - Valid selection is recorded to pc_activity for recall/debugging.
        - No DM message is sent in Round 1.
        """
        errors: list[str] = []

        to_pc_id = self._clean_str(raw.get("to_pc_id"))
        selection_reason = self._clean_str(raw.get("selection_reason"))
        if selection_reason and len(selection_reason) > 120:
            selection_reason = selection_reason[:120] + "…"

        if to_pc_id:
            if to_pc_id == pc_id:
                errors.append("dm_select.to_pc_id must not equal pc_id (cannot dm self)")
            elif to_pc_id not in ctx.pc_ids:
                errors.append("dm_select.to_pc_id must be an existing pc id (or omit it for DM)")

        if errors:
            return [], errors

        if to_pc_id:
            target = next((p for p in self._engine.pcs if p.id == to_pc_id), None)
            target_name = target.name if target else to_pc_id
            reason_part = f"（{selection_reason}）" if selection_reason else ""
            await self._store.add_pc_activity(
                PcActivity(
                    pc_id=pc_id,
                    kind="dm_selected",
                    summary=f"{pc_name}：选择私信 {target_name}{reason_part}（等待展开上下文）",
                    ref_type="pc",
                    ref_id=to_pc_id,
                )
            )
            dm_context = await self._build_dm_peer_context(pc_id=pc_id, peer_kind="pc", peer_id=to_pc_id)
            return [{"kind": "dm_select", "to_pc_id": to_pc_id, "dm_context": dm_context}], []

        # default target: DM admin
        reason_part2 = f"（{selection_reason}）" if selection_reason else ""
        await self._store.add_pc_activity(
            PcActivity(
                pc_id=pc_id,
                kind="dm_selected",
                summary=f"{pc_name}：选择私信 DM{reason_part2}（等待展开上下文）",
                ref_type="pc",
                ref_id="dm",
            )
        )
        dm_context = await self._build_dm_peer_context(pc_id=pc_id, peer_kind="dm", peer_id="dm")
        return [{"kind": "dm_select", "to_pc_id": None, "dm_context": dm_context}], []

    async def _llm_dm_write_action(
        self,
        *,
        pc_id: str,
        pc_name: str,
        persona: str | None,
        selected_target: dict[str, Any],
        dm_context: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Ask the LLM to write the DM content for the selected target (JSON-only).
        """
        if self._llm is None:
            return {"type": "noop", "reason": "llm not configured"}
        if not (self._settings.openai_base_url and self._settings.openai_api_key):
            return {"type": "noop", "reason": "missing openai_base_url/api_key"}

        persona_text = (persona or "").strip() or f"你是{pc_name}。"
        messages = render_prompt_messages(
            "tick_runner.dm_write",
            {
                "pc_id": pc_id,
                "pc_name": pc_name,
                "persona": persona_text,
                "selected_target_json": json.dumps(selected_target, ensure_ascii=False),
                "dm_context_json": json.dumps(dm_context, ensure_ascii=False),
            },
        )

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

    @staticmethod
    def _summarize_message(m: Message, max_len: int = 200) -> str:
        who = m.from_actor.name or m.from_actor.kind
        content = (m.content or "").strip().replace("\r\n", "\n").replace("\r", "\n")
        if len(content) > max_len:
            content = content[:max_len] + "…"
        content = content.replace("\n", "\\n")
        return f"{who}: {content}"

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
        # Show at most one latest DM line per peer PC to keep Round 1 lightweight.
        inbox_recent_msgs = await self._store.list_messages(f"dm_to_{pc_id}", limit=160)
        pc_name_by_id = {p.id: p.name for p in self._engine.pcs}
        latest_by_peer: dict[str, dict[str, Any]] = {}
        for m in inbox_recent_msgs:
            peer_id: str | None = None
            peer_name: str | None = None

            # Only count *received* DMs from other PCs here.
            if m.from_actor.kind == "pc" and m.from_actor.id != pc_id:
                peer_id = m.from_actor.id
                peer_name = pc_name_by_id.get(peer_id) or m.from_actor.name

            if not peer_id or peer_id == pc_id:
                continue

            prev = latest_by_peer.get(peer_id)
            if prev is None or str(m.timestamp) >= str(prev.get("_ts") or ""):
                latest_by_peer[peer_id] = {
                    "_ts": m.timestamp,
                    "id": peer_id,
                    "name": peer_name or peer_id,
                    "inbox": self._summarize_message(m, max_len=320),
                }

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
        # fetch the selected thread context in a follow-up round (reply_select -> reply_write).

        forum_channels = [{"id": c.id, "title": c.title, "description": c.description, "group": c.group} for c in forum_convs]

        persona_text = (persona or "").strip() or f"你是{pc_name}。"

        pcs = [{"id": p.id, "name": p.name} for p in self._engine.pcs]

        messages = render_prompt_messages(
            "tick_runner.forum_action",
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
                content = content[:400]
            if errors:
                return {"type": "noop", "reason": "invalid dm"}, errors
            return {"type": "dm", "to_pc_id": to_pc_id, "content": content}, []

        if a_type == "broadcast":
            content = clean_str(raw.get("content"))
            if not content:
                return {"type": "noop", "reason": "invalid broadcast"}, ["broadcast.content is required"]
            if len(content) > 1200:
                content = content[:1200]
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
                return {
                    "type": "reply_select",
                    "channel_id": forum_channels[0],
                    "thread_id": thread.id,
                    "selection_reason": "demo_fake: pick first unlocked thread",
                }
            if m == 3:
                now = utc_now_iso()
                idx = next((i for i, p in enumerate(self._engine.pcs) if p.id == pc_id), 0)
                target = self._engine.pcs[(idx + 1) % len(self._engine.pcs)]
                return {
                    "type": "dm_select",
                    "to_pc_id": target.id,
                    "selection_reason": f"demo_fake: ping {target.name} ({now[:19]})",
                }
            return {"type": "noop", "reason": "idle"}

        if turn_no % 2 == 0:
            now = utc_now_iso()
            return {"type": "dm_select", "selection_reason": f"demo_fake: dm admin ({now[:19]})"}
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
