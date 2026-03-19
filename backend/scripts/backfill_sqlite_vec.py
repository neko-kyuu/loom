from __future__ import annotations

import argparse
import array
import hashlib
import sqlite3
import sys
from typing import Iterable


def _vec_table_name(*, model: str, dims: int) -> str:
    m = (model or "").strip()
    h = hashlib.sha256(m.encode("utf-8")).hexdigest()[:12] if m else "nomodel"
    d = max(1, int(dims))
    return f"memory_summary_vec0_v1_{d}_{h}"


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


def _chunks(it: Iterable[tuple], *, size: int) -> Iterable[list[tuple]]:
    buf: list[tuple] = []
    for x in it:
        buf.append(x)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill sqlite-vec vec0 tables from memory_summary_embeddings.")
    ap.add_argument("--db", default="loom.sqlite3", help="SQLite DB path (default: loom.sqlite3)")
    ap.add_argument("--batch", type=int, default=500, help="Row batch size for fallback inserts")
    args = ap.parse_args()

    try:
        import sqlite_vec  # type: ignore
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: sqlite-vec not installed or import failed: {type(exc).__name__}: {exc}")
        return 1

    conn = sqlite3.connect(args.db)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as exc:  # noqa: BLE001
        try:
            conn.enable_load_extension(False)
        except Exception:
            pass
        print(f"ERROR: failed to load sqlite-vec extension: {type(exc).__name__}: {exc}")
        return 1

    conn.row_factory = sqlite3.Row

    pairs = conn.execute("SELECT DISTINCT model, dims FROM memory_summary_embeddings").fetchall()
    if not pairs:
        print("No rows in memory_summary_embeddings; nothing to backfill.")
        return 0

    total = 0
    for p in pairs:
        model = str(p["model"] or "").strip()
        dims = int(p["dims"] or 0)
        if not model or dims <= 0:
            continue

        table = _vec_table_name(model=model, dims=dims)
        conn.execute(
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

        print(f"Backfilling {table} (model={model}, dims={dims}) ...")

        try:
            cur = conn.execute(
                f"INSERT INTO {table}(memory_id, model, scope, owner_pc_id, scope_id, kind, subject_id, updated_at, embedding) "
                "SELECT m.id, e.model, m.scope, m.owner_pc_id, m.scope_id, m.kind, m.subject_id, m.updated_at, e.embedding_blob "
                "FROM memories m JOIN memory_summary_embeddings e ON e.memory_id=m.id "
                "WHERE m.deleted_at IS NULL AND e.model=? AND e.dims=? "
                "ON CONFLICT(memory_id) DO UPDATE SET "
                "model=excluded.model, scope=excluded.scope, owner_pc_id=excluded.owner_pc_id, scope_id=excluded.scope_id, "
                "kind=excluded.kind, subject_id=excluded.subject_id, updated_at=excluded.updated_at, embedding=excluded.embedding",
                (model, dims),
            )
            inserted = int(cur.rowcount or 0)
            total += max(0, inserted)
            conn.commit()
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"Bulk insert failed for {table}, falling back to row inserts: {type(exc).__name__}: {exc}")
            conn.rollback()

        rows = conn.execute(
            "SELECT m.id AS memory_id, e.model AS model, m.scope AS scope, m.owner_pc_id AS owner_pc_id, "
            "m.scope_id AS scope_id, m.kind AS kind, m.subject_id AS subject_id, m.updated_at AS updated_at, "
            "e.embedding_blob AS embedding_blob "
            "FROM memories m JOIN memory_summary_embeddings e ON e.memory_id=m.id "
            "WHERE m.deleted_at IS NULL AND e.model=? AND e.dims=?",
            (model, dims),
        )
        to_insert: list[tuple] = []
        for r in rows:
            blob = r["embedding_blob"]
            if not isinstance(blob, (bytes, bytearray, memoryview)):
                continue
            vec = _unpack_embedding_f32(bytes(blob), dims=dims)
            if vec is None:
                continue
            if hasattr(sqlite_vec, "serialize_float32"):
                packed = sqlite_vec.serialize_float32(vec)
            else:
                packed = _pack_embedding_f32(vec)
            if packed is None:
                continue
            to_insert.append(
                (
                    str(r["memory_id"]),
                    str(r["model"]),
                    str(r["scope"]),
                    r["owner_pc_id"],
                    r["scope_id"],
                    str(r["kind"]),
                    r["subject_id"],
                    str(r["updated_at"]),
                    packed,
                )
            )

        for chunk in _chunks(to_insert, size=max(1, int(args.batch))):
            conn.executemany(
                f"INSERT INTO {table}(memory_id, model, scope, owner_pc_id, scope_id, kind, subject_id, updated_at, embedding) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(memory_id) DO UPDATE SET "
                "model=excluded.model, scope=excluded.scope, owner_pc_id=excluded.owner_pc_id, scope_id=excluded.scope_id, "
                "kind=excluded.kind, subject_id=excluded.subject_id, updated_at=excluded.updated_at, embedding=excluded.embedding",
                chunk,
            )
            conn.commit()
            total += len(chunk)

    print(f"Done. Upserted ~{total} vec rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
