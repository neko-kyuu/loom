from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Iterable

import aiosqlite

from .models import Conversation, Event, Message


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
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv_time
  ON messages(conversation_id, timestamp);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  timestamp TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_time
  ON events(timestamp);

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
            await db.commit()

    async def upsert_conversations(self, conversations: Iterable[Conversation]) -> None:
        rows = [(c.id, c.model_dump_json()) for c in conversations]
        async with aiosqlite.connect(self._path) as db:
            await db.executemany(
                "INSERT INTO conversations(id, payload) VALUES(?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                rows,
            )
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
                "INSERT INTO messages(id, conversation_id, timestamp, payload) VALUES(?, ?, ?, ?)",
                (message.id, message.conversation_id, message.timestamp, message.model_dump_json()),
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

    async def add_event(self, event: Event) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO events(id, timestamp, payload) VALUES(?, ?, ?)",
                (event.id, event.timestamp, event.model_dump_json()),
            )
            await db.commit()

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
