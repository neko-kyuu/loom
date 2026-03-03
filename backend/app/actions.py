from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Union

from pydantic import BaseModel, Field


class _ActionBase(BaseModel):
    model_config = {"extra": "forbid"}


class CreateThreadAction(_ActionBase):
    type: Literal["create_thread"]
    channel_id: str
    title: str
    content: str


class ReplyAction(_ActionBase):
    type: Literal["reply"]
    channel_id: str
    thread_id: str
    content: str


class DmAction(_ActionBase):
    """
    Direct message action.

    Semantics:
    - If `to_pc_id` is provided: send to that PC (PC↔PC), and backend may replicate copies for inbox views.
    - If omitted: treat as DM (PC↔DM) message.
    """

    type: Literal["dm"]
    to_pc_id: str | None = None
    content: str


class NoopAction(_ActionBase):
    type: Literal["noop"]
    reason: str | None = None


Action = Union[CreateThreadAction, ReplyAction, DmAction, NoopAction]


class ActionEnvelope(_ActionBase):
    action: Action = Field(discriminator="type")


@dataclass(frozen=True)
class ActionValidationContext:
    forum_channel_ids: set[str]
    pc_ids: set[str]
    thread_channel_by_id: dict[str, str]


def action_json_schema() -> dict[str, Any]:
    """
    JSON schema for a single action object (discriminated by `type`).
    """
    return ActionEnvelope.model_json_schema()


def _clean_str(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s if s else None


def validate_action(raw: Any, *, ctx: ActionValidationContext) -> tuple[Action, list[str]]:
    """
    Strictly validate a single action.

    - Unknown or invalid actions become a `noop` with reasons.
    - `dm` is accepted as an action type but considered not executable yet.
    """
    errors: list[str] = []
    notes: list[str] = []

    if not isinstance(raw, dict):
        return NoopAction(type="noop", reason="invalid action: not an object"), ["action must be an object"]

    a_type = _clean_str(raw.get("type"))
    if not a_type:
        return NoopAction(type="noop", reason="invalid action: missing type"), ["action.type is required"]

    # Parse by explicit type to keep error messages stable.
    if a_type == "create_thread":
        channel_id = _clean_str(raw.get("channel_id"))
        title = _clean_str(raw.get("title"))
        content = _clean_str(raw.get("content"))
        if not channel_id:
            errors.append("create_thread.channel_id is required")
        elif channel_id not in ctx.forum_channel_ids:
            errors.append("create_thread.channel_id must be an existing forum channel id")
        if not title:
            errors.append("create_thread.title is required")
        elif len(title) > 80:
            title = title[:80]
            notes.append("create_thread.title truncated to 80 chars")
        if not content:
            errors.append("create_thread.content is required")
        elif len(content) > 1200:
            content = content[:1200]
            notes.append("create_thread.content truncated to 1200 chars")
        if errors:
            return NoopAction(type="noop", reason="invalid create_thread"), errors
        return CreateThreadAction(type="create_thread", channel_id=channel_id, title=title, content=content), notes

    if a_type == "reply":
        channel_id = _clean_str(raw.get("channel_id"))
        thread_id = _clean_str(raw.get("thread_id"))
        content = _clean_str(raw.get("content"))
        if not channel_id:
            errors.append("reply.channel_id is required")
        elif channel_id not in ctx.forum_channel_ids:
            errors.append("reply.channel_id must be an existing forum channel id")
        if not thread_id:
            errors.append("reply.thread_id is required")
        else:
            ch = ctx.thread_channel_by_id.get(thread_id)
            if not ch:
                errors.append("reply.thread_id must be an existing thread id")
            elif channel_id and ch != channel_id:
                errors.append("reply.thread_id must belong to reply.channel_id")
        if not content:
            errors.append("reply.content is required")
        elif len(content) > 1200:
            content = content[:1200]
            notes.append("reply.content truncated to 1200 chars")
        if errors:
            return NoopAction(type="noop", reason="invalid reply"), errors
        return ReplyAction(type="reply", channel_id=channel_id, thread_id=thread_id, content=content), notes

    if a_type == "dm":
        to_pc_id = _clean_str(raw.get("to_pc_id"))
        content = _clean_str(raw.get("content"))
        if to_pc_id and to_pc_id not in ctx.pc_ids:
            errors.append("dm.to_pc_id must be an existing pc id (or omit it)")
        if not content:
            errors.append("dm.content is required")
        elif len(content) > 800:
            content = content[:800]
            notes.append("dm.content truncated to 800 chars")
        if errors:
            return NoopAction(type="noop", reason="invalid dm"), errors
        return DmAction(type="dm", to_pc_id=to_pc_id, content=content), notes

    if a_type == "noop":
        reason = _clean_str(raw.get("reason"))
        return NoopAction(type="noop", reason=reason), []

    return NoopAction(type="noop", reason=f"invalid action: unknown type '{a_type}'"), [f"unknown action.type: {a_type}"]
