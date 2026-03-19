from __future__ import annotations

import json
import array
import sys
import hashlib
from datetime import datetime, timezone
from collections.abc import Iterable

import aiosqlite

from .models import Conversation, Event, ForumThread, MemoryEntry, Message, PcActivity, TickRecord
from .text_utils import clean_keywords


def _pack_embedding_f32(values: list[float]) -> bytes | None:
    if not isinstance(values, list) or not values:
        return None
    arr = array.array("f")
    try:
        for v in values:
            if not isinstance(v, (int, float)):
                return None
            arr.append(float(v))
    except Exception:
        return None
    if sys.byteorder == "big":
        arr.byteswap()
    return arr.tobytes()


def _unpack_embedding_f32(blob: bytes, *, dims: int) -> list[float] | None:
    if not isinstance(blob, (bytes, bytearray)) or dims <= 0:
        return None
    if len(blob) != dims * 4:
        return None
    arr = array.array("f")
    try:
        arr.frombytes(blob)
    except Exception:
        return None
    if sys.byteorder == "big":
        arr.byteswap()
    return [float(x) for x in arr]


_SQLITE_VEC_MODULE = None
_SQLITE_VEC_IMPORT_ERROR: str | None = None


def _get_sqlite_vec_module():
    global _SQLITE_VEC_MODULE, _SQLITE_VEC_IMPORT_ERROR
    if _SQLITE_VEC_MODULE is not None:
        return _SQLITE_VEC_MODULE
    if _SQLITE_VEC_IMPORT_ERROR is not None:
        return None
    try:
        import sqlite_vec  # type: ignore

        _SQLITE_VEC_MODULE = sqlite_vec
        return _SQLITE_VEC_MODULE
    except Exception as exc:  # noqa: BLE001
        _SQLITE_VEC_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
        return None


def _vec_table_name(*, model: str, dims: int) -> str:
    m = (model or "").strip()
    h = hashlib.sha256(m.encode("utf-8")).hexdigest()[:12] if m else "nomodel"
    d = max(1, int(dims))
    # Versioned table name (virtual tables are awkward to ALTER).
    return f"memory_summary_vec0_v1_{d}_{h}"


def _meta_req_text(v: str | None) -> str:
    if isinstance(v, str) and v.strip():
        return v.strip()
    return ""


def _meta_opt_text(v: str | None) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


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
  pinned INTEGER NOT NULL DEFAULT 0,
  access_count INTEGER NOT NULL DEFAULT 0,
  last_accessed_at TEXT,
  deleted_at TEXT,
  edit_state TEXT NOT NULL DEFAULT 'normal',
  source_type TEXT,
  source_memory_id TEXT,
  revision INTEGER NOT NULL DEFAULT 0,
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
CREATE INDEX IF NOT EXISTS idx_memories_deleted_at
  ON memories(deleted_at);
CREATE INDEX IF NOT EXISTS idx_memories_edit_state
  ON memories(edit_state);

-- v5: summary-only embeddings for memory items (hybrid recall)
CREATE TABLE IF NOT EXISTS memory_summary_embeddings (
  memory_id TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  dims INTEGER NOT NULL,
  summary_sha256 TEXT NOT NULL,
  embedding_blob BLOB NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_summary_embeddings_model_updated
  ON memory_summary_embeddings(model, updated_at);

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

    async def _try_load_sqlite_vec(self, db: aiosqlite.Connection) -> tuple[bool, str | None]:
        mod = _get_sqlite_vec_module()
        if mod is None:
            return False, _SQLITE_VEC_IMPORT_ERROR or "sqlite_vec_not_installed"

        conn = getattr(db, "_conn", None)
        if conn is None:
            return False, "missing_sqlite3_connection"
        try:
            conn.enable_load_extension(True)
            mod.load(conn)
            conn.enable_load_extension(False)
            return True, None
        except Exception as exc:  # noqa: BLE001
            try:
                conn.enable_load_extension(False)
            except Exception:
                pass
            return False, f"{type(exc).__name__}: {exc}"

    async def _ensure_memory_summary_vec_table(self, db: aiosqlite.Connection, *, table: str, dims: int) -> None:
        dims = max(1, int(dims))
        # NOTE: vec0 requires fixed dimensions at table creation time.
        await db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0("
            "memory_id TEXT PRIMARY KEY, "
            "model TEXT, "
            "scope TEXT, "
            "owner_pc_id TEXT, "
            "scope_id TEXT, "
            "kind TEXT, "
            "subject_id TEXT, "
            "updated_at TEXT, "
            f"embedding float[{dims}] distance_metric=cosine"
            ")"
        )

    @staticmethod
    async def _sqlite_table_exists(db: aiosqlite.Connection, *, table: str) -> bool:
        try:
            cur = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (table,),
            )
            row = await cur.fetchone()
            return bool(row)
        except Exception:
            return False

    @staticmethod
    async def _sqlite_table_has_any_rows(db: aiosqlite.Connection, *, table: str) -> bool:
        try:
            cur = await db.execute(f"SELECT 1 FROM {table} LIMIT 1")
            row = await cur.fetchone()
            return bool(row)
        except Exception:
            return False

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

        cur = await db.execute("PRAGMA table_info(memories)")
        cols = await cur.fetchall()
        col_names = {r["name"] for r in cols}
        if cols and "pinned" not in col_names:
            await db.execute("ALTER TABLE memories ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        if cols and "deleted_at" not in col_names:
            await db.execute("ALTER TABLE memories ADD COLUMN deleted_at TEXT")
        if cols and "edit_state" not in col_names:
            await db.execute("ALTER TABLE memories ADD COLUMN edit_state TEXT NOT NULL DEFAULT 'normal'")
        if cols and "source_type" not in col_names:
            await db.execute("ALTER TABLE memories ADD COLUMN source_type TEXT")
        if cols and "source_memory_id" not in col_names:
            await db.execute("ALTER TABLE memories ADD COLUMN source_memory_id TEXT")
        if cols and "revision" not in col_names:
            await db.execute("ALTER TABLE memories ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memories_deleted_at ON memories(deleted_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memories_edit_state ON memories(edit_state)")

        await db.execute(
            "CREATE TABLE IF NOT EXISTS memory_summary_embeddings ("
            "memory_id TEXT PRIMARY KEY, "
            "model TEXT NOT NULL, "
            "dims INTEGER NOT NULL, "
            "summary_sha256 TEXT NOT NULL, "
            "embedding_blob BLOB NOT NULL, "
            "updated_at TEXT NOT NULL"
            ")"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_summary_embeddings_model_updated "
            "ON memory_summary_embeddings(model, updated_at)"
        )

        # v5 migration: switch from JSON embeddings to float32 BLOB storage for performance.
        cur = await db.execute("PRAGMA table_info(memory_summary_embeddings)")
        cols = await cur.fetchall()
        col_names = {r["name"] for r in cols}
        if cols and "embedding_json" in col_names and "embedding_blob" not in col_names:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS memory_summary_embeddings_v2 ("
                "memory_id TEXT PRIMARY KEY, "
                "model TEXT NOT NULL, "
                "dims INTEGER NOT NULL, "
                "summary_sha256 TEXT NOT NULL, "
                "embedding_blob BLOB NOT NULL, "
                "updated_at TEXT NOT NULL"
                ")"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_summary_embeddings_v2_model_updated "
                "ON memory_summary_embeddings_v2(model, updated_at)"
            )

            cur = await db.execute(
                "SELECT memory_id, model, dims, summary_sha256, embedding_json, updated_at "
                "FROM memory_summary_embeddings"
            )
            rows = await cur.fetchall()
            for row in rows:
                mid = str(row["memory_id"] or "").strip()
                model = str(row["model"] or "").strip()
                summary_sha = str(row["summary_sha256"] or "").strip()
                updated_at = str(row["updated_at"] or "").strip()
                try:
                    dims = int(row["dims"])
                except Exception:
                    dims = 0
                raw_json = row["embedding_json"]
                if not mid or not model or not summary_sha or not updated_at or dims <= 0:
                    continue
                try:
                    vec = json.loads(raw_json) if isinstance(raw_json, str) else None
                except Exception:  # noqa: BLE001
                    vec = None
                if not isinstance(vec, list) or len(vec) != dims:
                    continue
                packed = _pack_embedding_f32([float(v) for v in vec if isinstance(v, (int, float))])
                if packed is None or len(packed) != dims * 4:
                    continue

                await db.execute(
                    "INSERT INTO memory_summary_embeddings_v2(memory_id, model, dims, summary_sha256, embedding_blob, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(memory_id) DO UPDATE SET "
                    "model=excluded.model, dims=excluded.dims, summary_sha256=excluded.summary_sha256, "
                    "embedding_blob=excluded.embedding_blob, updated_at=excluded.updated_at",
                    (mid, model, dims, summary_sha, packed, updated_at),
                )

            await db.execute("DROP TABLE memory_summary_embeddings")
            await db.execute("ALTER TABLE memory_summary_embeddings_v2 RENAME TO memory_summary_embeddings")
            await db.execute("DROP INDEX IF EXISTS idx_memory_summary_embeddings_v2_model_updated")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_summary_embeddings_model_updated "
                "ON memory_summary_embeddings(model, updated_at)"
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

    async def list_ticks_page(
        self,
        *,
        pc_id: str | None = None,
        cursor: tuple[str, str] | None = None,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """
        Returns newest-first tick rows for UI infinite scrolling.

        Cursor is keyset-based: (started_at, id) of the last item in the previous page.
        """
        import json

        where: list[str] = []
        params: list[str | int] = []

        if pc_id:
            where.append("pc_id=?")
            params.append(pc_id)

        if cursor:
            cursor_ts, cursor_id = cursor
            where.append("(started_at < ? OR (started_at = ? AND id < ?))")
            params.extend([cursor_ts, cursor_ts, cursor_id])

        where_sql = f" WHERE {' AND '.join(where)}" if where else ""

        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, started_at, pc_id, status, action_json, result_refs_json, duration_ms, error "
                "FROM ticks"
                f"{where_sql} "
                "ORDER BY started_at DESC, id DESC LIMIT ?",
                (*params, limit),
            )
            rows = await cur.fetchall()

        items: list[dict[str, Any]] = []
        for r in rows:
            items.append(
                {
                    "id": r["id"],
                    "started_at": r["started_at"],
                    "pc_id": r["pc_id"],
                    "status": r["status"],
                    "action": json.loads(r["action_json"] or "{}"),
                    "result_refs": json.loads(r["result_refs_json"] or "[]"),
                    "duration_ms": r["duration_ms"],
                    "error": r["error"],
                }
            )

        next_cursor = f"{items[-1]['started_at']}|{items[-1]['id']}" if len(items) == limit else None
        return items, next_cursor

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
        items = list(memories)
        if not items:
            return

        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            existing_by_id: dict[str, MemoryEntry] = {}
            ids = [memory.id for memory in items if isinstance(memory.id, str) and memory.id.strip()]
            if ids:
                placeholders = ", ".join("?" for _ in ids)
                cur = await db.execute(
                    "SELECT id, scope, scope_id, owner_pc_id, kind, created_at, updated_at, content, summary, "
                    "subject_type, subject_id, importance, score, pinned, access_count, last_accessed_at, "
                    "deleted_at, edit_state, source_type, source_memory_id, revision, meta_json "
                    f"FROM memories WHERE id IN ({placeholders})",
                    ids,
                )
                rows = await cur.fetchall()
                existing_by_id = {row["id"]: self._memory_from_row(row) for row in rows}

            merged_items = [self._merge_memory_upsert(existing_by_id.get(memory.id), memory) for memory in items]
            merged_by_id = {m.id: m for m in merged_items if isinstance(m.id, str) and m.id.strip()}
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
                    1 if memory.pinned else 0,
                    memory.access_count,
                    memory.last_accessed_at,
                    memory.deleted_at,
                    memory.edit_state,
                    memory.source_type,
                    memory.source_memory_id,
                    memory.revision,
                    json.dumps(memory.meta, ensure_ascii=False),
                )
                for memory in merged_items
            ]
            await db.executemany(
                "INSERT INTO memories("
                "id, scope, scope_id, owner_pc_id, kind, created_at, updated_at, content, summary, "
                "subject_type, subject_id, importance, score, pinned, access_count, last_accessed_at, "
                "deleted_at, edit_state, source_type, source_memory_id, revision, meta_json"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "scope=excluded.scope, scope_id=excluded.scope_id, owner_pc_id=excluded.owner_pc_id, "
                "kind=excluded.kind, updated_at=excluded.updated_at, content=excluded.content, "
                "summary=excluded.summary, subject_type=excluded.subject_type, subject_id=excluded.subject_id, "
                "importance=excluded.importance, score=excluded.score, pinned=excluded.pinned, access_count=excluded.access_count, "
                "last_accessed_at=excluded.last_accessed_at, deleted_at=excluded.deleted_at, "
                "edit_state=excluded.edit_state, source_type=excluded.source_type, "
                "source_memory_id=excluded.source_memory_id, revision=excluded.revision, meta_json=excluded.meta_json",
                rows,
            )
            await db.commit()

            # Keep sqlite-vec metadata in sync when memory attributes change (scope/kind/subject_id/updated_at/deleted_at).
            try:
                loaded, _ = await self._try_load_sqlite_vec(db)
                if loaded and ids:
                    placeholders = ", ".join("?" for _ in ids)
                    cur = await db.execute(
                        f"SELECT memory_id, model, dims, embedding_blob "
                        f"FROM memory_summary_embeddings WHERE memory_id IN ({placeholders})",
                        ids,
                    )
                    emb_rows = await cur.fetchall()

                    upserts_by_table: dict[
                        tuple[str, int],
                        list[tuple[str, str, str, str | None, str | None, str, str | None, str, bytes]],
                    ] = {}
                    deletes_by_table: dict[tuple[str, int], list[tuple[str]]] = {}

                    for r in emb_rows:
                        mid = str(r["memory_id"] or "").strip()
                        if not mid:
                            continue
                        mem = merged_by_id.get(mid)
                        if mem is None:
                            continue
                        try:
                            dims = int(r["dims"])
                        except Exception:
                            continue
                        row_model = str(r["model"] or "").strip()
                        raw_blob = r["embedding_blob"]
                        if not row_model or dims <= 0 or not isinstance(raw_blob, (bytes, bytearray, memoryview)):
                            continue
                        blob = bytes(raw_blob)
                        if len(blob) != dims * 4:
                            continue

                        table = _vec_table_name(model=row_model, dims=dims)
                        key = (table, dims)

                        if isinstance(mem.deleted_at, str) and mem.deleted_at.strip():
                            deletes_by_table.setdefault(key, []).append((mid,))
                            continue

                        upserts_by_table.setdefault(key, []).append(
                            (
                                mid,
                                row_model,
                                _meta_req_text(mem.scope),
                                _meta_opt_text(mem.owner_pc_id),
                                _meta_opt_text(mem.scope_id),
                                _meta_req_text(mem.kind),
                                _meta_opt_text(mem.subject_id),
                                _meta_req_text(mem.updated_at),
                                blob,
                            )
                        )

                    for (table, dims), vec_rows in upserts_by_table.items():
                        await self._ensure_memory_summary_vec_table(db, table=table, dims=dims)
                        await db.executemany(
                            f"INSERT INTO {table}(memory_id, model, scope, owner_pc_id, scope_id, kind, subject_id, updated_at, embedding) "
                            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(memory_id) DO UPDATE SET "
                            "model=excluded.model, scope=excluded.scope, owner_pc_id=excluded.owner_pc_id, scope_id=excluded.scope_id, "
                            "kind=excluded.kind, subject_id=excluded.subject_id, updated_at=excluded.updated_at, embedding=excluded.embedding",
                            vec_rows,
                        )
                    for (table, dims), del_rows in deletes_by_table.items():
                        if not await self._sqlite_table_exists(db, table=table):
                            continue
                        await db.executemany(
                            f"DELETE FROM {table} WHERE memory_id = ?",
                            del_rows,
                        )
                    await db.commit()
            except Exception:
                pass

    async def get_memory(self, memory_id: str) -> MemoryEntry | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, scope, scope_id, owner_pc_id, kind, created_at, updated_at, content, summary, "
                "subject_type, subject_id, importance, score, pinned, access_count, last_accessed_at, "
                "deleted_at, edit_state, source_type, source_memory_id, revision, meta_json "
                "FROM memories WHERE id=?",
                (memory_id,),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return self._memory_from_row(row)

    async def list_memories_by_ids(
        self,
        memory_ids: Iterable[str],
        *,
        include_deleted: bool = False,
    ) -> list[MemoryEntry]:
        ids = [mid for mid in memory_ids if isinstance(mid, str) and mid.strip()]
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        where = f"WHERE id IN ({placeholders})"
        if not include_deleted:
            where += " AND deleted_at IS NULL"
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, scope, scope_id, owner_pc_id, kind, created_at, updated_at, content, summary, "
                "subject_type, subject_id, importance, score, pinned, access_count, last_accessed_at, "
                "deleted_at, edit_state, source_type, source_memory_id, revision, meta_json "
                "FROM memories "
                f"{where}",
                ids,
            )
            rows = await cur.fetchall()
        by_id = {row["id"]: self._memory_from_row(row) for row in rows}
        return [by_id[memory_id] for memory_id in ids if memory_id in by_id]

    async def list_memories(
        self,
        *,
        scope: str | None = None,
        owner_pc_id: str | None = None,
        scope_id: str | None = None,
        kind: str | None = None,
        subject_id: str | None = None,
        pinned: bool | None = None,
        include_deleted: bool = False,
        edit_state: str | None = None,
        source_type: str | None = None,
        limit: int = 200,
    ) -> list[MemoryEntry]:
        where: list[str] = []
        params: list[str | int] = []

        if not include_deleted:
            where.append("deleted_at IS NULL")

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
        if pinned is not None:
            where.append("pinned=?")
            params.append(1 if pinned else 0)
        if edit_state is not None:
            where.append("edit_state=?")
            params.append(edit_state)
        if source_type is not None:
            where.append("source_type=?")
            params.append(source_type)

        where_sql = f" WHERE {' AND '.join(where)}" if where else ""

        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id, scope, scope_id, owner_pc_id, kind, created_at, updated_at, content, summary, "
                "subject_type, subject_id, importance, score, pinned, access_count, last_accessed_at, "
                "deleted_at, edit_state, source_type, source_memory_id, revision, meta_json "
                "FROM memories"
                f"{where_sql} "
                "ORDER BY score DESC, updated_at DESC LIMIT ?",
                (*params, limit),
            )
            rows = await cur.fetchall()
        return [self._memory_from_row(row) for row in rows]

    async def get_memory_summary_embedding_hashes(
        self,
        *,
        memory_ids: Iterable[str],
        model: str,
    ) -> dict[str, str]:
        ids = [mid for mid in memory_ids if isinstance(mid, str) and mid.strip()]
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"SELECT memory_id, summary_sha256 FROM memory_summary_embeddings WHERE model=? AND memory_id IN ({placeholders})",
                (model, *ids),
            )
            rows = await cur.fetchall()
        return {str(r["memory_id"]): str(r["summary_sha256"] or "") for r in rows}

    async def upsert_memory_summary_embeddings(
        self,
        *,
        model: str,
        items: Iterable[tuple[str, str, list[float]]],
        updated_at: str,
    ) -> None:
        rows: list[tuple[str, str, int, str, bytes, str]] = []
        for memory_id, summary_sha256, vector in items:
            if not isinstance(memory_id, str) or not memory_id.strip():
                continue
            if not isinstance(summary_sha256, str) or not summary_sha256.strip():
                continue
            if not isinstance(vector, list) or not vector:
                continue
            dims = len(vector)
            packed = _pack_embedding_f32(vector)
            if packed is None or len(packed) != dims * 4:
                continue
            rows.append((memory_id.strip(), model, dims, summary_sha256.strip(), packed, updated_at))
        if not rows:
            return
        async with aiosqlite.connect(self._path) as db:
            await db.executemany(
                "INSERT INTO memory_summary_embeddings(memory_id, model, dims, summary_sha256, embedding_blob, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(memory_id) DO UPDATE SET "
                "model=excluded.model, dims=excluded.dims, summary_sha256=excluded.summary_sha256, "
                "embedding_blob=excluded.embedding_blob, updated_at=excluded.updated_at",
                rows,
            )
            await db.commit()

            try:
                loaded, _ = await self._try_load_sqlite_vec(db)
                if loaded:
                    ids = [r[0] for r in rows]
                    placeholders = ", ".join("?" for _ in ids)
                    db.row_factory = aiosqlite.Row
                    cur = await db.execute(
                        f"SELECT id, scope, owner_pc_id, scope_id, kind, subject_id, updated_at "
                        f"FROM memories WHERE id IN ({placeholders})",
                        ids,
                    )
                    meta_rows = await cur.fetchall()
                    meta_by_id = {str(r["id"]): r for r in meta_rows if r and r.get("id")}

                    vec_rows_by_table: dict[
                        tuple[str, int],
                        list[tuple[str, str, str, str | None, str | None, str, str | None, str, bytes]],
                    ] = {}
                    for memory_id, row_model, dims, _sha, packed, _ua in rows:
                        meta = meta_by_id.get(memory_id)
                        if meta is None:
                            continue
                        table = _vec_table_name(model=row_model, dims=dims)
                        key = (table, int(dims))
                        vec_rows_by_table.setdefault(key, []).append(
                            (
                                memory_id,
                                row_model,
                                _meta_req_text(meta["scope"]),
                                _meta_opt_text(meta["owner_pc_id"]),
                                _meta_opt_text(meta["scope_id"]),
                                _meta_req_text(meta["kind"]),
                                _meta_opt_text(meta["subject_id"]),
                                _meta_req_text(meta["updated_at"]),
                                packed,
                            )
                        )

                    for (table, dims), vec_rows in vec_rows_by_table.items():
                        await self._ensure_memory_summary_vec_table(db, table=table, dims=dims)
                        await db.executemany(
                            f"INSERT INTO {table}(memory_id, model, scope, owner_pc_id, scope_id, kind, subject_id, updated_at, embedding) "
                            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(memory_id) DO UPDATE SET "
                            "model=excluded.model, scope=excluded.scope, owner_pc_id=excluded.owner_pc_id, scope_id=excluded.scope_id, "
                            "kind=excluded.kind, subject_id=excluded.subject_id, updated_at=excluded.updated_at, embedding=excluded.embedding",
                            vec_rows,
                        )
                    await db.commit()
            except Exception:
                pass

    async def knn_memory_summary_similarities(
        self,
        *,
        owner_pc_id: str | None,
        include_public: bool,
        direct_scope_id: str | None,
        model: str,
        query_vector: list[float],
        k: int,
    ) -> list[tuple[str, float]] | None:
        model = (model or "").strip()
        if not model:
            return None
        if not isinstance(query_vector, list) or not query_vector:
            return None
        mod = _get_sqlite_vec_module()
        if mod is not None and hasattr(mod, "serialize_float32"):
            packed = mod.serialize_float32(query_vector)
        else:
            packed = _pack_embedding_f32(query_vector)
        if packed is None:
            return None
        dims = len(query_vector)
        table = _vec_table_name(model=model, dims=dims)
        k = max(1, min(500, int(k)))

        async with aiosqlite.connect(self._path) as db:
            loaded, _ = await self._try_load_sqlite_vec(db)
            if not loaded:
                return None
            if not await self._sqlite_table_exists(db, table=table):
                return None
            if not await self._sqlite_table_has_any_rows(db, table=table):
                return None

            queries: list[tuple[str, tuple[object, ...]]] = []
            if owner_pc_id:
                queries.append(
                    (
                        f"SELECT memory_id, distance FROM {table} "
                        "WHERE embedding MATCH ? AND k = ? AND model = ? "
                        "AND scope = 'pc' AND owner_pc_id = ? AND scope_id IS NULL "
                        "ORDER BY distance",
                        (packed, k, model, owner_pc_id),
                    )
                )
            if include_public:
                queries.append(
                    (
                        f"SELECT memory_id, distance FROM {table} "
                        "WHERE embedding MATCH ? AND k = ? AND model = ? "
                        "AND scope = 'public' AND owner_pc_id IS NULL AND scope_id IS NULL "
                        "ORDER BY distance",
                        (packed, k, model),
                    )
                )
            if direct_scope_id:
                queries.append(
                    (
                        f"SELECT memory_id, distance FROM {table} "
                        "WHERE embedding MATCH ? AND k = ? AND model = ? "
                        "AND scope = 'direct' AND owner_pc_id IS NULL AND scope_id = ? "
                        "ORDER BY distance",
                        (packed, k, model, direct_scope_id),
                    )
                )

            best_dist: dict[str, float] = {}
            db.row_factory = aiosqlite.Row
            for sql, params in queries:
                cur = await db.execute(sql, params)
                rows = await cur.fetchall()
                for r in rows:
                    mid = str(r["memory_id"] or "").strip()
                    if not mid:
                        continue
                    try:
                        dist = float(r["distance"])
                    except Exception:
                        continue
                    prev = best_dist.get(mid)
                    if prev is None or dist < prev:
                        best_dist[mid] = dist

        # cosine distance -> cosine similarity (approx): sim = 1 - dist
        out: list[tuple[str, float]] = []
        for mid, dist in best_dist.items():
            out.append((mid, 1.0 - float(dist)))
        out.sort(key=lambda x: x[1], reverse=True)
        return out[:k]

    async def knn_memory_summary_candidates_for_write_dedup(
        self,
        *,
        scope: str,
        owner_pc_id: str | None,
        scope_id: str | None,
        kind: str,
        subject_id: str | None,
        model: str,
        query_vector: list[float],
        scan_limit: int,
        updated_after: str | None,
    ) -> list[tuple[str, float, str]] | None:
        model = (model or "").strip()
        if not model:
            return None
        if not isinstance(query_vector, list) or not query_vector:
            return None
        mod = _get_sqlite_vec_module()
        if mod is not None and hasattr(mod, "serialize_float32"):
            packed = mod.serialize_float32(query_vector)
        else:
            packed = _pack_embedding_f32(query_vector)
        if packed is None:
            return None
        dims = len(query_vector)
        table = _vec_table_name(model=model, dims=dims)
        scan_limit = max(1, min(2000, int(scan_limit)))

        scope = (scope or "").strip()
        kind = (kind or "").strip()
        if scope not in {"pc", "public", "direct"}:
            return None
        if not kind:
            return None

        where_parts = [
            "embedding MATCH ?",
            "k = ?",
            "model = ?",
            "kind = ?",
        ]
        params: list[object] = [packed, scan_limit, model, kind]

        if scope == "pc":
            if not owner_pc_id:
                return None
            where_parts.append("scope = 'pc'")
            where_parts.append("owner_pc_id = ?")
            where_parts.append("scope_id IS NULL")
            params.append(owner_pc_id)
        elif scope == "public":
            where_parts.append("scope = 'public'")
            where_parts.append("owner_pc_id IS NULL")
            where_parts.append("scope_id IS NULL")
        else:
            if not scope_id:
                return None
            where_parts.append("scope = 'direct'")
            where_parts.append("owner_pc_id IS NULL")
            where_parts.append("scope_id = ?")
            params.append(scope_id)

        if subject_id is None:
            where_parts.append("subject_id IS NULL")
        else:
            where_parts.append("subject_id = ?")
            params.append(subject_id)

        if isinstance(updated_after, str) and updated_after.strip():
            where_parts.append("updated_at >= ?")
            params.append(updated_after.strip())

        where_sql = " AND ".join(where_parts)

        async with aiosqlite.connect(self._path) as db:
            loaded, _ = await self._try_load_sqlite_vec(db)
            if not loaded:
                return None
            if not await self._sqlite_table_exists(db, table=table):
                return None
            if not await self._sqlite_table_has_any_rows(db, table=table):
                return None

            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"SELECT memory_id, distance FROM {table} WHERE {where_sql} ORDER BY distance",
                tuple(params),
            )
            rows = await cur.fetchall()

            ids: list[str] = []
            sim_by_id: dict[str, float] = {}
            for r in rows:
                mid = str(r["memory_id"] or "").strip()
                if not mid:
                    continue
                try:
                    dist = float(r["distance"])
                except Exception:
                    continue
                if mid not in sim_by_id:
                    ids.append(mid)
                    sim_by_id[mid] = 1.0 - float(dist)

            if not ids:
                return []

            placeholders = ", ".join("?" for _ in ids)
            cur = await db.execute(
                f"SELECT id, meta_json FROM memories WHERE deleted_at IS NULL AND id IN ({placeholders})",
                ids,
            )
            meta_rows = await cur.fetchall()
            meta_by_id = {str(r["id"]): str(r["meta_json"] or "") for r in meta_rows if r and r.get("id")}

        out: list[tuple[str, float, str]] = []
        for mid in ids:
            if mid not in meta_by_id:
                continue
            out.append((mid, float(sim_by_id.get(mid, 0.0)), meta_by_id.get(mid, "")))
        return out

    async def list_memory_summary_embeddings_for_vector_search(
        self,
        *,
        owner_pc_id: str | None,
        include_public: bool,
        direct_scope_id: str | None,
        model: str,
        scan_limit: int,
    ) -> list[tuple[str, list[float]]]:
        scope_clauses: list[str] = []
        scope_params: list[str] = []
        if owner_pc_id:
            scope_clauses.append("(m.scope='pc' AND m.owner_pc_id=?)")
            scope_params.append(owner_pc_id)
        if include_public:
            scope_clauses.append("(m.scope='public')")
        if direct_scope_id:
            scope_clauses.append("(m.scope='direct' AND m.scope_id=?)")
            scope_params.append(direct_scope_id)
        if not scope_clauses:
            return []

        where_parts = [f"({' OR '.join(scope_clauses)})", "m.deleted_at IS NULL", "e.model=?"]
        params: list[str | int] = [*scope_params, model, max(1, int(scan_limit))]
        where_sql = " WHERE " + " AND ".join(where_parts)

        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT m.id AS id, e.dims AS dims, e.embedding_blob AS embedding_blob "
                "FROM memories m JOIN memory_summary_embeddings e ON e.memory_id=m.id "
                f"{where_sql} "
                "ORDER BY m.score DESC, m.updated_at DESC LIMIT ?",
                params,
            )
            rows = await cur.fetchall()

        out: list[tuple[str, list[float]]] = []
        for row in rows:
            mid = str(row["id"] or "").strip()
            if not mid:
                continue
            raw_blob = row["embedding_blob"]
            try:
                dims = int(row["dims"])
            except Exception:
                dims = 0
            if not isinstance(raw_blob, (bytes, bytearray, memoryview)) or dims <= 0:
                continue
            vec = _unpack_embedding_f32(bytes(raw_blob), dims=dims)
            if vec is None:
                continue
            out.append((mid, vec))
        return out

    async def list_memory_summary_embeddings_for_write_dedup(
        self,
        *,
        scope: str,
        owner_pc_id: str | None,
        scope_id: str | None,
        kind: str,
        subject_id: str | None,
        model: str,
        scan_limit: int,
        updated_after: str | None,
    ) -> list[tuple[str, list[float], str]]:
        scope = str(scope or "").strip()
        kind = str(kind or "").strip()
        if scope not in {"pc", "public", "direct"}:
            return []
        if not kind:
            return []
        model = str(model or "").strip()
        if not model:
            return []

        where_parts: list[str] = ["m.deleted_at IS NULL", "m.edit_state='normal'", "m.pinned=0", "e.model=?", "m.scope=?", "m.kind=?"]
        params: list[str | int] = [model, scope, kind]

        if scope == "pc":
            if not owner_pc_id:
                return []
            where_parts.append("m.owner_pc_id=?")
            params.append(owner_pc_id)
            where_parts.append("m.scope_id IS NULL")
        elif scope == "public":
            where_parts.append("m.owner_pc_id IS NULL")
            where_parts.append("m.scope_id IS NULL")
        else:
            if not scope_id:
                return []
            where_parts.append("m.owner_pc_id IS NULL")
            where_parts.append("m.scope_id=?")
            params.append(scope_id)

        if subject_id is None:
            where_parts.append("m.subject_id IS NULL")
        else:
            where_parts.append("m.subject_id=?")
            params.append(subject_id)

        if isinstance(updated_after, str) and updated_after.strip():
            where_parts.append("m.updated_at>=?")
            params.append(updated_after.strip())

        limit = max(1, int(scan_limit))
        where_sql = " WHERE " + " AND ".join(where_parts)

        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT m.id AS id, e.dims AS dims, e.embedding_blob AS embedding_blob, m.meta_json AS meta_json "
                "FROM memories m JOIN memory_summary_embeddings e ON e.memory_id=m.id "
                f"{where_sql} "
                "ORDER BY m.updated_at DESC, m.score DESC LIMIT ?",
                (*params, limit),
            )
            rows = await cur.fetchall()

        out: list[tuple[str, list[float], str]] = []
        for row in rows:
            mid = str(row["id"] or "").strip()
            if not mid:
                continue
            raw_blob = row["embedding_blob"]
            try:
                dims = int(row["dims"])
            except Exception:
                dims = 0
            if not isinstance(raw_blob, (bytes, bytearray, memoryview)) or dims <= 0:
                continue
            vec = _unpack_embedding_f32(bytes(raw_blob), dims=dims)
            if vec is None:
                continue
            meta_json = row["meta_json"]
            out.append((mid, vec, meta_json if isinstance(meta_json, str) else ""))
        return out

    async def search_memories(
        self,
        *,
        keywords: Iterable[str],
        owner_pc_id: str | None = None,
        include_public: bool = False,
        direct_scope_id: str | None = None,
        kind: str | None = None,
        include_deleted: bool = False,
        limit: int = 200,
    ) -> list[MemoryEntry]:
        cleaned_keywords = clean_keywords(keywords)

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
        if not include_deleted:
            where_parts.append("deleted_at IS NULL")
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
                "subject_type, subject_id, importance, score, pinned, access_count, last_accessed_at, "
                "deleted_at, edit_state, source_type, source_memory_id, revision, meta_json "
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
                "WHERE id=? AND deleted_at IS NULL",
                [(now, now, memory_id) for memory_id in ids],
            )
            await db.commit()

    async def decay_memories(self, *, k: int = 1, threshold: int = -3) -> dict[str, int]:
        decay_k = max(0, int(k))
        delete_threshold = int(threshold)

        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row

            cur = await db.execute(
                "SELECT COUNT(1) AS c FROM memories "
                "WHERE deleted_at IS NULL AND pinned=0 AND kind != 'autobiography'"
            )
            row = await cur.fetchone()
            candidates = int(row["c"] if row else 0)

            decayed = 0
            if decay_k > 0 and candidates > 0:
                now = self._utc_now_iso()
                cur = await db.execute(
                    "UPDATE memories SET score=score-?, updated_at=? "
                    "WHERE deleted_at IS NULL AND pinned=0 AND kind != 'autobiography'",
                    (decay_k, now),
                )
                decayed = int(cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else candidates)

            deleted_at = self._utc_now_iso()
            cur = await db.execute(
                "UPDATE memories SET deleted_at=?, edit_state='deleted', updated_at=? "
                "WHERE deleted_at IS NULL AND pinned=0 AND kind != 'autobiography' AND score < ?",
                (deleted_at, deleted_at, delete_threshold),
            )
            deleted = int(cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0)

            cur = await db.execute("SELECT COUNT(1) AS c FROM memories WHERE deleted_at IS NULL")
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
    def _merge_memory_upsert(existing: MemoryEntry | None, incoming: MemoryEntry) -> MemoryEntry:
        if existing is None:
            return incoming

        incoming_source = (incoming.source_type or "").strip()
        is_auto_write = incoming_source in {"llm_write", "deterministic_write"}

        if is_auto_write and (existing.deleted_at or existing.edit_state == "deleted"):
            return existing

        if is_auto_write and existing.edit_state == "user_locked":
            return existing

        if is_auto_write and existing.edit_state == "user_edited":
            score = max(existing.score, incoming.score)
            importance = max(existing.importance, incoming.importance)
            access_count = max(existing.access_count, incoming.access_count)
            last_accessed_at = incoming.last_accessed_at or existing.last_accessed_at
            pinned = existing.pinned or incoming.pinned
            changed = (
                score != existing.score
                or importance != existing.importance
                or access_count != existing.access_count
                or last_accessed_at != existing.last_accessed_at
                or pinned != existing.pinned
            )
            return existing.model_copy(
                update={
                    "score": score,
                    "importance": importance,
                    "access_count": access_count,
                    "last_accessed_at": last_accessed_at,
                    "pinned": pinned,
                    "updated_at": incoming.updated_at if changed else existing.updated_at,
                }
            )

        next_edit_state = incoming.edit_state
        if next_edit_state == "normal" and existing.edit_state != "normal":
            next_edit_state = existing.edit_state

        return incoming.model_copy(
            update={
                "deleted_at": incoming.deleted_at if incoming.deleted_at is not None else existing.deleted_at,
                "edit_state": next_edit_state,
                "source_memory_id": incoming.source_memory_id or existing.source_memory_id,
                "revision": max(existing.revision, incoming.revision),
            }
        )

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
            pinned=bool(row["pinned"]),
            access_count=row["access_count"],
            last_accessed_at=row["last_accessed_at"],
            deleted_at=row["deleted_at"],
            edit_state=row["edit_state"],
            source_type=row["source_type"],
            source_memory_id=row["source_memory_id"],
            revision=row["revision"],
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

    async def get_event(self, event_id: str) -> Event | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT payload FROM events WHERE id=?",
                (event_id,),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return Event.model_validate_json(row["payload"])

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
