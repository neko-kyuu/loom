from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


Channel = Literal["broadcast", "direct"]


class Actor(BaseModel):
    kind: Literal["user", "dm", "pc"]
    id: str | None = None
    name: str | None = None


class Conversation(BaseModel):
    id: str
    kind: Literal["broadcast", "dm_to_pc", "pc_to_pc", "forum"]
    title: str
    description: str | None = None
    participants: list[Actor]


class ForumThread(BaseModel):
    id: str
    channel_id: str
    title: str
    created_at: str
    created_by: Actor
    last_activity_at: str
    reply_count: int = 0


class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: str = Field(default_factory=utc_now_iso)
    conversation_id: str
    channel: Channel
    thread_id: str | None = None
    from_actor: Actor
    to: list[Actor] = Field(default_factory=list)
    content: str
    send_batch_id: str | None = None


class RelationshipDelta(BaseModel):
    from_pc_id: str
    to_pc_id: str
    delta: int
    reason: str | None = None


class PCResult(BaseModel):
    pc_id: str
    relationship_delta: list[RelationshipDelta] = Field(default_factory=list)
    info_shared: list[str] = Field(default_factory=list)


class Event(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: str = Field(default_factory=utc_now_iso)
    location: str | None = None
    pc_id: str | None = None
    type: str
    summary: str
    visibility: Literal["public", "private"] = "public"
    consequences: dict = Field(default_factory=dict)


class WsClientToServer(BaseModel):
    type: Literal[
        "hello",
        "request_state",
        "user_inject",
        "forum_post",
        "pause",
        "resume",
    ]
    content: str | None = None
    target: dict | None = None
    channel_id: str | None = None
    thread_id: str | None = None
    value: bool | None = None


class WsServerToClient(BaseModel):
    type: Literal["state", "message", "typing", "queue", "error"]
    payload: dict
