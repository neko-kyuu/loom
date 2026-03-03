from __future__ import annotations

from typing import Any

from .db import SqliteStore


async def build_state_message(*, store: SqliteStore) -> dict[str, Any]:
    conversations = await store.list_conversations()
    messages_by_conv: dict[str, list[dict[str, Any]]] = {}
    for conv in conversations:
        msgs = await store.list_messages(conv.id, limit=200)
        messages_by_conv[conv.id] = [m.model_dump() for m in msgs]

    forum_threads_by_channel: dict[str, list[dict[str, Any]]] = {}
    forum_posts_by_thread: dict[str, list[dict[str, Any]]] = {}
    for conv in conversations:
        if conv.kind != "forum":
            continue
        threads = await store.list_forum_threads(conv.id)
        forum_threads_by_channel[conv.id] = [t.model_dump() for t in threads]
        for t in threads:
            posts = await store.list_messages_by_thread(t.id, limit=200)
            posts = [p for p in posts if p.conversation_id == conv.id]
            forum_posts_by_thread[t.id] = [p.model_dump() for p in posts]

    return {
        "type": "state",
        "payload": {
            "conversations": [c.model_dump() for c in conversations],
            "messages_by_conversation": messages_by_conv,
            "forum_threads_by_channel": forum_threads_by_channel,
            "forum_posts_by_thread": forum_posts_by_thread,
        },
    }

