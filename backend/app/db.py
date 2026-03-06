from __future__ import annotations

import json
from datetime import datetime, timezone
from collections.abc import Iterable

import aiosqlite

from .models import Conversation, Event, ForumThread, MemoryEntry, Message, PcActivity, TickRecord


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  thread_id TEXT,
  send_batch_id TEXT,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv_time
  ON messages(conversation_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_thread_time
  ON messages(thread_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_send_batch
  ON messages(send_batch_id, timestamp);

CREATE TABLE IF NOT EXISTS llm_logs (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  model TEXT,
  request_json TEXT NOT NULL,
  response_json TEXT,
  status_code INTEGER,
  error TEXT,
  duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_llm_logs_created_at
  ON llm_logs(created_at);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  timestamp TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_time
  ON events(timestamp);

CREATE TABLE IF NOT EXISTS forum_threads (
  id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forum_threads_channel
  ON forum_threads(channel_id, created_at);

CREATE TABLE IF NOT EXISTS ticks (
  id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  pc_id TEXT NOT NULL,
  status TEXT NOT NULL,
  action_json TEXT NOT NULL,
  result_refs_json TEXT NOT NULL,
  duration_ms INTEGER,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_ticks_started_at
  ON ticks(started_at);
CREATE INDEX IF NOT EXISTS idx_ticks_pc_started_at
  ON ticks(pc_id, started_at);

CREATE TABLE IF NOT EXISTS pc_activity (
  id TEXT PRIMARY KEY,
  pc_id TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  kind TEXT NOT NULL,
  summary TEXT NOT NULL,
  ref_type TEXT,
  ref_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_pc_activity_pc_time
  ON pc_activity(pc_id, timestamp);

CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  scope_id TEXT,
  owner_pc_id TEXT,
  kind TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  content TEXT NOT NULL,
  summary TEXT NOT NULL,
  subject_type TEXT,
  subject_id TEXT,
  importance INTEGER NOT NULL DEFAULT 0,
  score INTEGER NOT NULL DEFAULT 0,
  access_count INTEGER NOT NULL DEFAULT 0,
  last_accessed_at TEXT,
  meta_json TEXT NOT NULL DEFAULT '{}',
  CHECK (scope IN ('pc', 'public', 'direct')),
  CHECK (
    (scope = 'pc' AND owner_pc_id IS NOT NULL AND scope_id IS NULL) OR
    (scope = 'public' AND owner_pc_id IS NULL AND scope_id IS NULL) OR
    (scope = 'direct' AND owner_pc_id IS NULL AND scope_id IS NOT NULL)
  ),
  CHECK (kind != 'secret' OR scope = 'pc')
);
CREATE INDEX IF NOT EXISTS idx_memories_scope_owner_kind_score
  ON memories(scope, owner_pc_id, kind, score DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_scope_kind_score
  ON memories(scope, kind, score DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_scope_scope_id_kind_score
  ON memories(scope, scope_id, kind, score DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_scope_owner_last_accessed
  ON memories(scope, owner_pc_id, last_accessed_at);
CREATE INDEX IF NOT EXISTS idx_memories_scope_scope_id_last_accessed
  ON memories(scope, scope_id, last_accessed_at);
CREATE INDEX IF NOT EXISTS idx_memories_scope_owner_subject
  ON memories(scope, owner_pc_id, subject_id);
CREATE INDEX IF NOT EXISTS idx_memories_scope_scope_id_subject
  ON memories(scope, scope_id, subject_id);

CREATE TABLE IF NOT EXISTS kv_settings (
  key TEXT PRIMARY KEY,
  json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY,
  mime TEXT NOT NULL,
  data BLOB NOT NULL,
  created_at TEXT NOT NULL
);
"""


class SqliteStore:
    def __init__(self, path: str) -> None:
        self._path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(SCHEMA_SQL)
            await self._migrate(db)
            await db.commit()

    async def _migrate(self, db: aiosqlite.Connection) -> None:
        db.row_factory = aiosqlite.Row

        cur = await db.execute("PRAGMA table_info(messages)")
        cols = await cur.fetchall()
        col_names = {r["name"] for r in cols}
        if "thread_id" not in col_names:
            await db.execute("ALTER TABLE messages ADD COLUMN thread_id TEXT")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_thread_time ON messages(thread_id, timestamp)")
        if "send_batch_id" not in col_names:
            await db.execute("ALTER TABLE messages ADD COLUMN send_batch_id TEXT")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_send_batch ON messages(send_batch_id, timestamp)"
            )

    async def upsert_conversations(self, conversations: Iterable[Conversation]) -> None:
        rows = [(c.id, c.model_dump_json()) for c in conversations]
        async with aiosqlite.connect(self._path) as db:
            await db.executemany(
                "INSERT INTO conversations(id, payload) VALUES(?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                rows,
            )
            await db.commit()

    async def sync_conversations(self, conversations: Iterable[Conversation]) -> None:
        """
        Upserts provided conversations and deletes any existing conversations that are not present.
        """
        desired = list(conversations)
        desired_ids = {c.id for c in desired}
        await self.upsert_conversations(desired)

        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT id FROM conversations")
            rows = await cur.fetchall()
            existing_ids = [r["id"] for r in rows]
            to_delete = [cid for cid in existing_ids if cid not in desired_ids]
            if to_delete:
                await db.executemany("DELETE FROM conversations WHERE id=?", [(cid,) for cid in to_delete])
                await db.commit()

    async def list_conversations(self) -> list[Conversation]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT payload FROM conversations ORDER BY id")
            rows = await cur.fetchall()
        return [Conversation.model_validate_json(r["payload"]) for r in rows]

    async def get_message(self, message_id: str) -> Message | None:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute("SELECT payload FROM messages WHERE id=?", (message_id,))
            row = await cur.fetchone()
        if not row:
            return None
        return Message.model_validate_json(row[0])

    async def update_messages_payload(self, messages: Iterable[Message]) -> None:
        rows = [(m.model_dump_json(), m.id) for m in messages]
        if not rows:
            return
        async with aiosqlite.connect(self._path) as db:
            await db.executemany("UPDATE messages SET payload=? WHERE id=?", rows)
            await db.commit()

    async def add_message(self, message: Message) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO messages(id, conversation_id, timestamp, thread_id, send_batch_id, payload) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    message.conversation_id,
                    message.timestamp,
                    message.thread_id,
                    message.send_batch_id,
                    message.model_dump_json(),
                ),
            )
            await db.commit()

    async def add_message_ignore(self, message: Message) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO messages(id, conversation_id, timestamp, thread_id, send_batch_id, payload) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    message.conversation_id,
                    message.timestamp,
                    message.thread_id,
                    message.send_batch_id,
                    message.model_dump_json(),
                ),
            )
            await db.commit()

    async def list_messages(self, conversation_id: str, limit: int = 200) -> list[Message]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT payload FROM messages WHERE conversation_id=? "
                "ORDER BY timestamp DESC LIMIT ?",
                (conversation_id, limit),
            )
            rows = await cur.fetchall()
        msgs = [Message.model_validate_json(r["payload"]) for r in rows]
        msgs.reverse()
        return msgs

    async def list_messages_since(self, conversation_id: str, *, since: str, limit: int = 200) -> list[Message]:
        """
        List messages in a conversation with timestamp >= since (ascending).
        """
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT payload FROM messages WHERE conversation_id=? AND timestamp>=? "
                "ORDER BY timestamp DESC LIMIT ?",
                (conversation_id, since, limit),
            )
            rows = await cur.fetchall()
        msgs = [Message.model_validate_json(r["payload"]) for r in rows]
        msgs.reverse()
        return msgs

    async def list_messages_by_thread(self, thread_id: str, limit: int = 200) -> list[Message]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT payload FROM messages WHERE thread_id=? "
                "ORDER BY timestamp DESC LIMIT ?",
                (thread_id, limit),
            )
            rows = await cur.fetchall()
        msgs = [Message.model_validate_json(r["payload"]) for r in rows]
        msgs.reverse()
        return msgs

    async def list_messages_by_thread_in_conversation(
        self, *, thread_id: str, conversation_id: str, limit: int = 200
    ) -> list[Message]:
        """
        List messages in a specific (conversation_id, thread_id) pair (ascending).

        This is useful because `thread_id` can appear across different conversations (e.g. DM copies),
        but forum thread context should only include the public posts in its forum channel.
        """
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT payload FROM messages WHERE conversation_id=? AND thread_id=? "
                "ORDER BY timestamp DESC LIMIT ?",
                (conversation_id, thread_id, limit),
            )
            rows = await cur.fetchall()
        msgs = [Message.model_validate_json(r["payload"]) for r in rows]
        msgs.reverse()
        return msgs

    async def get_first_message_by_thread_in_conversation(
        self, *, thread_id: str, conversation_id: str
    ) -> Message | None:
        """
        Fetch the earliest message in a specific (conversation_id, thread_id) pair.
        """
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT payload FROM messages WHERE conversation_id=? AND thread_id=? "
                "ORDER BY timestamp ASC LIMIT 1",
                (conversation_id, thread_id),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return Message.model_validate_json(row["payload"])

    async def get_thread_context(
        self,
        *,
        thread_id: str,
        channel_id: str,
        recent_n: int = 12,
        max_chars_per_post: int = 1200,
        op_max_chars: int = 1600,
    ) -> dict[str, object]:
        """
        Fetch a compact forum thread context for LLM prompting.

        - Filters to public posts in the given forum channel (conversation_id == channel_id).
        - Returns bounded per-post content to keep token usage predictable.
        """

        def trim_text(text: str, *, max_len: int) -> str:
            s = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            if len(s) > max_len:
                return s[:max_len] + "…"
            return s

        thread = await self.get_forum_thread(thread_id)
        if thread is None or thread.channel_id != channel_id:
            raise ValueError("thread not found or does not belong to channel")

        op = await self.get_first_message_by_thread_in_conversation(
            thread_id=thread.id,
            conversation_id=channel_id,
        )
        recent = await self.list_messages_by_thread_in_conversation(
            thread_id=thread.id,
            conversation_id=channel_id,
            limit=max(60, int(recent_n) + 10),
        )
        if op is not None:
            recent = [m for m in recent if m.id != op.id]
        tail = recent[-max(0, int(recent_n)) :]

        def pack(m: Message, *, max_len: int) -> dict[str, object]:
            return {
                "id": m.id,
                "timestamp": m.timestamp,
                "from": (m.from_actor.name or m.from_actor.kind),
                "content": trim_text(m.content, max_len=max_len),
            }

        return {
            "thread": {
                "thread_id": thread.id,
                "channel_id": thread.channel_id,
                "title": thread.title,
                "reply_count": thread.reply_count,
                "last_activity_at": thread.last_activity_at,
                "pinned": thread.pinned,
                "locked": thread.locked,
            },
            "op_post": pack(op, max_len=op_max_chars) if op is not None else None,
            "recent_posts": [pack(m, max_len=max_chars_per_post) for m in tail],
        }

    async def count_messages_by_thread_in_conversation(self, *, thread_id: str, conversation_id: str) -> int:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "SELECT COUNT(1) AS c FROM messages WHERE thread_id=? AND conversation_id=?",
                (thread_id, conversation_id),
            )
            row = await cur.fetchone()
        return int(row[0] if row else 0)

    async def list_messages_by_send_batch_id(self, send_batch_id: str, limit: int = 500) -> list[Message]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT payload FROM messages WHERE send_batch_id=? ORDER BY timestamp DESC LIMIT ?",
                (send_batch_id, limit),
            )
            rows = await cur.fetchall()
        msgs = [Message.model_validate_json(r["payload"]) for r in rows]
        msgs.reverse()
        return msgs

    async def list_recent_messages(self, limit: int = 200) -> list[Message]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT payload FROM messages ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
        msgs = [Message.model_validate_json(r["payload"]) for r in rows]
        msgs.reverse()
        return msgs

    async def delete_messages_by_ids(self, message_ids: Iterable[str]) -> None:
        ids = [mid for mid in message_ids if isinstance(mid, str) and mid.strip()]
        if not ids:
            return
        async with aiosqlite.connect(self._path) as db:
            await db.executemany("DELETE FROM messages WHERE id=?", [(mid,) for mid in ids])
            await db.commit()

    async def delete_pc_activity_by_message_ids(self, message_ids: Iterable[str]) -> None:
        ids = [mid for mid in message_ids if isinstance(mid, str) and mid.strip()]
        if not ids:
            return
        async with aiosqlite.connect(self._path) as db:
            await db.executemany(
                "DELETE FROM pc_activity WHERE ref_type='message' AND ref_id=?",
                [(mid,) for mid in ids],
            )
            await db.commit()

    async def upsert_forum_threads(self, threads: Iterable[ForumThread]) -> None:
        rows = [(t.id, t.channel_id, t.created_at, t.model_dump_json()) for t in threads]
        async with aiosqlite.connect(self._path) as db:
            await db.executemany(
                "INSERT INTO forum_threads(id, channel_id, created_at, payload) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, channel_id=excluded.channel_id, created_at=excluded.created_at",
                rows,
            )
            await db.commit()

    async def list_forum_threads(self, channel_id: str) -> list[ForumThread]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT payload FROM forum_threads WHERE channel_id=? ORDER BY created_at DESC",
                (channel_id,),
            )
            rows = await cur.fetchall()
        return [ForumThread.model_validate_json(r["payload"]) for r in rows]

    async def get_forum_thread(self, thread_id: str) -> ForumThread | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT payload FROM forum_threads WHERE id=?", (thread_id,))
            row = await cur.fetchone()
        if not row:
            return None
        return ForumThread.model_validate_json(row["payload"])

    async def count_messages_by_thread(self, thread_id: str) -> int:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute("SELECT COUNT(1) AS c FROM messages WHERE thread_id=?", (thread_id,))
            row = await cur.fetchone()
        return int(row[0] if row else 0)

    async def touch_forum_thread_from_message(self, message: Message) -> None:
        """
        If `message` is a public post in a forum thread (i.e. message.conversation_id matches thread.channel_id),
        update forum thread metadata (last_activity_at, reply_count).
        """
        tid = message.thread_id
        if not isinstance(tid, str) or not tid.strip():
            return

        thread = await self.get_forum_thread(tid)
        if not thread:
            return
        if message.conversation_id != thread.channel_id:
            return

        total = await self.count_messages_by_thread_in_conversation(thread_id=tid, conversation_id=thread.channel_id)
        thread.last_activity_at = message.timestamp
        thread.reply_count = max(0, total - 1)
        await self.upsert_forum_threads([thread])

    async def append_message(self, message: Message) -> None:
        """
        Centralized append path:
        - insert message
        - if it belongs to a forum thread's public conversation, touch the thread metadata
        """
        await self.add_message(message)
        await self.touch_forum_thread_from_message(message)

    async def delete_forum_thread(self, thread_id: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("DELETE FROM forum_threads WHERE id=?", (thread_id,))
            await db.execute("DELETE FROM messages WHERE thread_id=?", (thread_id,))
            await db.commit()

    async def rebuild_forum_thread_meta(self, thread_id: str) -> None:
        """
        Recomputes a forum thread's derived metadata (last_activity_at/reply_count)
        from remaining messages in its public conversation.
        """
        thread = await self.get_forum_thread(thread_id)
        if not thread:
            return

        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "SELECT COUNT(1) AS c, MAX(timestamp) AS last_ts "
                "FROM messages WHERE thread_id=? AND conversation_id=?",
                (thread_id, thread.channel_id),
            )
            row = await cur.fetchone()
        total = int(row[0] if row and row[0] is not None else 0)
        last_ts = row[1] if row and isinstance(row[1], str) and row[1].strip() else None

        thread.last_activity_at = last_ts or thread.created_at
        thread.reply_count = max(0, total - 1)
        await self.upsert_forum_threads([thread])

    async def upsert_tick(self, tick: TickRecord) -> None:
        """
        Insert/update a tick execution record.
        """
        import json

        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO ticks(id, started_at, pc_id, status, action_json, result_refs_json, duration_ms, error) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "started_at=excluded.started_at, pc_id=excluded.pc_id, status=excluded.status, "
                "action_json=excluded.action_json, result_refs_json=excluded.result_refs_json, "
                "duration_ms=excluded.duration_ms, error=excluded.error",
                (
                    tick.id,
                    tick.started_at,
                    tick.pc_id,
                    tick.status,
                    json.dumps(tick.action, ensure_ascii=False),
                    json.dumps(tick.result_refs, ensure_ascii=False),
                    tick.duration_ms,
                    tick.error,
                ),
            )
            await db.commit()

    async def get_latest_tick_started_at(self, *, pc_id: str) -> str | None:
        """
        Return the latest tick started_at for a given pc_id (newest started_at).
        """
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "SELECT started_at FROM ticks WHERE pc_id=? ORDER BY started_at DESC LIMIT 1",
                (pc_id,),
            )
            row = await cur.fetchone()
        if not row:
            return None
        started_at = row[0]
        return started_at if isinstance(started_at, str) and started_at.strip() else None

    async def get_tick(self, tick_id: str) -> TickRecord | None:
        import json

        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, started_at, pc_id, status, action_json, result_refs_json, duration_ms, error "
                "FROM ticks WHERE id=?",
                (tick_id,),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return TickRecord(
            id=row["id"],
            started_at=row["started_at"],
            pc_id=row["pc_id"],
            status=row["status"],
            action=json.loads(row["action_json"] or "{}"),
            result_refs=json.loads(row["result_refs_json"] or "[]"),
            duration_ms=row["duration_ms"],
            error=row["error"],
        )

    async def list_ticks(self, limit: int = 200) -> list[TickRecord]:
        import json

        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, started_at, pc_id, status, action_json, result_refs_json, duration_ms, error "
                "FROM ticks ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
        out = [
            TickRecord(
                id=r["id"],
                started_at=r["started_at"],
                pc_id=r["pc_id"],
                status=r["status"],
                action=json.loads(r["action_json"] or "{}"),
                result_refs=json.loads(r["result_refs_json"] or "[]"),
                duration_ms=r["duration_ms"],
                error=r["error"],
            )
            for r in rows
        ]
        out.reverse()
        return out

    async def add_pc_activity(self, activity: PcActivity) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO pc_activity(id, pc_id, timestamp, kind, summary, ref_type, ref_id) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    activity.id,
                    activity.pc_id,
                    activity.timestamp,
                    activity.kind,
                    activity.summary,
                    activity.ref_type,
                    activity.ref_id,
                ),
            )
            await db.commit()

    async def list_pc_activity(self, pc_id: str, *, since: str | None = None, limit: int = 200) -> list[PcActivity]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            if since:
                cur = await db.execute(
                    "SELECT id, pc_id, timestamp, kind, summary, ref_type, ref_id "
                    "FROM pc_activity WHERE pc_id=? AND timestamp>=? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (pc_id, since, limit),
                )
            else:
                cur = await db.execute(
                    "SELECT id, pc_id, timestamp, kind, summary, ref_type, ref_id "
                    "FROM pc_activity WHERE pc_id=? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (pc_id, limit),
                )
            rows = await cur.fetchall()
        out = [
            PcActivity(
                id=r["id"],
                pc_id=r["pc_id"],
                timestamp=r["timestamp"],
                kind=r["kind"],
                summary=r["summary"],
                ref_type=r["ref_type"],
                ref_id=r["ref_id"],
            )
            for r in rows
        ]
        out.reverse()
        return out

    async def list_pc_activity_log_page(
        self,
        *,
        pc_id: str | None = None,
        cursor: tuple[str, str] | None = None,
        limit: int = 50,
    ) -> tuple[list[dict[str, str]], str | None]:
        """
        Returns newest-first activity log rows for UI infinite scrolling.

        Cursor is keyset-based: (timestamp, id) of the last item in the previous page.
        """
        where: list[str] = []
        params: list[str | int] = []

        if pc_id:
            where.append("pc_id=?")
            params.append(pc_id)

        if cursor:
            cursor_ts, cursor_id = cursor
            where.append("(timestamp < ? OR (timestamp = ? AND id < ?))")
            params.extend([cursor_ts, cursor_ts, cursor_id])

        where_sql = f" WHERE {' AND '.join(where)}" if where else ""

        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, pc_id, timestamp, summary "
                "FROM pc_activity"
                f"{where_sql} "
                "ORDER BY timestamp DESC, id DESC LIMIT ?",
                (*params, limit),
            )
            rows = await cur.fetchall()

        items = [
            {"id": r["id"], "pc_id": r["pc_id"], "timestamp": r["timestamp"], "summary": r["summary"]}
            for r in rows
        ]
        next_cursor = f"{items[-1]['timestamp']}|{items[-1]['id']}" if len(items) == limit else None
        return items, next_cursor

    async def upsert_memory(self, memory: MemoryEntry) -> None:
        await self.upsert_memories([memory])

    async def upsert_memories(self, memories: Iterable[MemoryEntry]) -> None:
        rows = [
            (
                memory.id,
                memory.scope,
                memory.scope_id,
                memory.owner_pc_id,
                memory.kind,
                memory.created_at,
                memory.updated_at,
                memory.content,
                memory.summary,
                memory.subject_type,
                memory.subject_id,
                memory.importance,
                memory.score,
                memory.access_count,
                memory.last_accessed_at,
                json.dumps(memory.meta, ensure_ascii=False),
            )
            for memory in memories
        ]
        if not rows:
            return

        async with aiosqlite.connect(self._path) as db:
            await db.executemany(
                "INSERT INTO memories("
                "id, scope, scope_id, owner_pc_id, kind, created_at, updated_at, content, summary, "
                "subject_type, subject_id, importance, score, access_count, last_accessed_at, meta_json"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "scope=excluded.scope, scope_id=excluded.scope_id, owner_pc_id=excluded.owner_pc_id, "
                "kind=excluded.kind, updated_at=excluded.updated_at, content=excluded.content, "
                "summary=excluded.summary, subject_type=excluded.subject_type, subject_id=excluded.subject_id, "
                "importance=excluded.importance, score=excluded.score, access_count=excluded.access_count, "
                "last_accessed_at=excluded.last_accessed_at, meta_json=excluded.meta_json",
                rows,
            )
            await db.commit()

    async def get_memory(self, memory_id: str) -> MemoryEntry | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, scope, scope_id, owner_pc_id, kind, created_at, updated_at, content, summary, "
                "subject_type, subject_id, importance, score, access_count, last_accessed_at, meta_json "
                "FROM memories WHERE id=?",
                (memory_id,),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return self._memory_from_row(row)

    async def list_memories(
        self,
        *,
        scope: str | None = None,
        owner_pc_id: str | None = None,
        scope_id: str | None = None,
        kind: str | None = None,
        subject_id: str | None = None,
        limit: int = 200,
    ) -> list[MemoryEntry]:
        where: list[str] = []
        params: list[str | int] = []

        if scope is not None:
            where.append("scope=?")
            params.append(scope)
        if owner_pc_id is not None:
            where.append("owner_pc_id=?")
            params.append(owner_pc_id)
        if scope_id is not None:
            where.append("scope_id=?")
            params.append(scope_id)
        if kind is not None:
            where.append("kind=?")
            params.append(kind)
        if subject_id is not None:
            where.append("subject_id=?")
            params.append(subject_id)

        where_sql = f" WHERE {' AND '.join(where)}" if where else ""

        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, scope, scope_id, owner_pc_id, kind, created_at, updated_at, content, summary, "
                "subject_type, subject_id, importance, score, access_count, last_accessed_at, meta_json "
                "FROM memories"
                f"{where_sql} "
                "ORDER BY score DESC, updated_at DESC LIMIT ?",
                (*params, limit),
            )
            rows = await cur.fetchall()
        return [self._memory_from_row(row) for row in rows]

    async def search_memories(
        self,
        *,
        keywords: Iterable[str],
        owner_pc_id: str | None = None,
        include_public: bool = False,
        direct_scope_id: str | None = None,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[MemoryEntry]:
        cleaned_keywords: list[str] = []
        seen_keywords: set[str] = set()
        for keyword in keywords:
            if not isinstance(keyword, str):
                continue
            text = keyword.strip()
            if not text:
                continue
            dedupe_key = text.casefold()
            if dedupe_key in seen_keywords:
                continue
            seen_keywords.add(dedupe_key)
            cleaned_keywords.append(text)

        scope_clauses: list[str] = []
        scope_params: list[str] = []
        if owner_pc_id:
            scope_clauses.append("(scope='pc' AND owner_pc_id=?)")
            scope_params.append(owner_pc_id)
        if include_public:
            scope_clauses.append("(scope='public')")
        if direct_scope_id:
            scope_clauses.append("(scope='direct' AND scope_id=?)")
            scope_params.append(direct_scope_id)

        if not scope_clauses:
            return []
        if not cleaned_keywords:
            return []

        where_parts = [f"({' OR '.join(scope_clauses)})"]
        params: list[str | int] = list(scope_params)

        if kind is not None:
            where_parts.append("kind=?")
            params.append(kind)

        like_parts: list[str] = []
        for keyword in cleaned_keywords:
            like_parts.append("(summary LIKE ? OR content LIKE ?)")
            like_value = f"%{keyword}%"
            params.extend([like_value, like_value])
        where_parts.append(f"({' OR '.join(like_parts)})")

        where_sql = " WHERE " + " AND ".join(where_parts)

        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, scope, scope_id, owner_pc_id, kind, created_at, updated_at, content, summary, "
                "subject_type, subject_id, importance, score, access_count, last_accessed_at, meta_json "
                "FROM memories"
                f"{where_sql} "
                "ORDER BY score DESC, updated_at DESC LIMIT ?",
                (*params, limit),
            )
            rows = await cur.fetchall()
        return [self._memory_from_row(row) for row in rows]

    async def touch_memories(self, memory_ids: Iterable[str]) -> None:
        ids = [memory_id for memory_id in memory_ids if isinstance(memory_id, str) and memory_id.strip()]
        if not ids:
            return

        now = self._utc_now_iso()
        async with aiosqlite.connect(self._path) as db:
            await db.executemany(
                "UPDATE memories SET access_count=access_count+1, score=score+1, last_accessed_at=?, updated_at=? "
                "WHERE id=?",
                [(now, now, memory_id) for memory_id in ids],
            )
            await db.commit()

    async def decay_memories(self, *, k: int = 1, threshold: int = -3) -> dict[str, int]:
        decay_k = max(0, int(k))
        delete_threshold = int(threshold)

        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row

            cur = await db.execute(
                "SELECT COUNT(1) AS c FROM memories WHERE kind != 'autobiography'"
            )
            row = await cur.fetchone()
            candidates = int(row["c"] if row else 0)

            decayed = 0
            if decay_k > 0 and candidates > 0:
                cur = await db.execute(
                    "UPDATE memories SET score=score-?, updated_at=? WHERE kind != 'autobiography'",
                    (decay_k, self._utc_now_iso()),
                )
                decayed = int(cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else candidates)

            cur = await db.execute(
                "DELETE FROM memories WHERE kind != 'autobiography' AND score < ?",
                (delete_threshold,),
            )
            deleted = int(cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0)

            cur = await db.execute("SELECT COUNT(1) AS c FROM memories")
            row = await cur.fetchone()
            remaining = int(row["c"] if row else 0)

            await db.commit()

        return {
            "candidates": candidates,
            "decayed": decayed,
            "deleted": deleted,
            "remaining": remaining,
            "k": decay_k,
            "threshold": delete_threshold,
        }

    @staticmethod
    def _memory_from_row(row: aiosqlite.Row) -> MemoryEntry:
        return MemoryEntry(
            id=row["id"],
            scope=row["scope"],
            scope_id=row["scope_id"],
            owner_pc_id=row["owner_pc_id"],
            kind=row["kind"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            content=row["content"],
            summary=row["summary"],
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            importance=row["importance"],
            score=row["score"],
            access_count=row["access_count"],
            last_accessed_at=row["last_accessed_at"],
            meta=json.loads(row["meta_json"] or "{}"),
        )

    async def add_event(self, event: Event) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO events(id, timestamp, payload) VALUES(?, ?, ?)",
                (event.id, event.timestamp, event.model_dump_json()),
            )
            await db.commit()

    async def add_llm_log(
        self,
        *,
        log_id: str,
        created_at: str,
        model: str | None,
        request_json: str,
        response_json: str | None,
        status_code: int | None,
        error: str | None,
        duration_ms: int | None,
    ) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO llm_logs(id, created_at, model, request_json, response_json, status_code, error, duration_ms) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (log_id, created_at, model, request_json, response_json, status_code, error, duration_ms),
                )
            await db.commit()

    async def list_llm_logs_meta(self, *, limit: int = 200) -> list[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, created_at, model, status_code, error, duration_ms "
                "FROM llm_logs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_llm_log(self, log_id: str) -> dict | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, created_at, model, request_json, response_json, status_code, error, duration_ms "
                "FROM llm_logs WHERE id=?",
                (log_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_llm_logs(self, limit: int = 200) -> list[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, created_at, model, request_json, response_json, status_code, error, duration_ms "
                "FROM llm_logs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
        logs = [dict(r) for r in rows]
        logs.reverse()
        return logs

    async def list_events(self, limit: int = 200) -> list[Event]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT payload FROM events ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
        events = [Event.model_validate_json(r["payload"]) for r in rows]
        events.reverse()
        return events

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def get_setting_json(self, key: str) -> str | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT json FROM kv_settings WHERE key=?", (key,))
            row = await cur.fetchone()
        return row["json"] if row else None

    async def set_setting_json(self, key: str, value_json: str) -> None:
        now = self._utc_now_iso()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO kv_settings(key, json, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET json=excluded.json, updated_at=excluded.updated_at",
                (key, value_json, now),
            )
            await db.commit()

    async def put_asset(self, asset_id: str, mime: str, data: bytes) -> None:
        now = self._utc_now_iso()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO assets(id, mime, data, created_at) VALUES(?, ?, ?, ?)",
                (asset_id, mime, data, now),
            )
            await db.commit()

    async def get_asset(self, asset_id: str) -> tuple[str, bytes] | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT mime, data FROM assets WHERE id=?", (asset_id,))
            row = await cur.fetchone()
        if not row:
            return None
        return row["mime"], row["data"]
