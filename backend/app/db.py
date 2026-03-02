from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Iterable

import aiosqlite

from .models import Conversation, Event, ForumThread, Message


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

    async def delete_forum_thread(self, thread_id: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("DELETE FROM forum_threads WHERE id=?", (thread_id,))
            await db.execute("DELETE FROM messages WHERE thread_id=?", (thread_id,))
            await db.commit()

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
