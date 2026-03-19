from __future__ import annotations

import argparse
import sqlite3


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def main() -> int:
    ap = argparse.ArgumentParser(description="Drop sqlite-vec vec0 tables created by loom.")
    ap.add_argument("--db", default="loom.sqlite3", help="SQLite DB path (default: loom.sqlite3)")
    ap.add_argument(
        "--prefix",
        default="memory_summary_vec0_v1_",
        help="Table name prefix to drop (default: memory_summary_vec0_v1_)",
    )
    args = ap.parse_args()

    prefix = str(args.prefix or "").strip()
    if not prefix:
        print("ERROR: --prefix must not be empty")
        return 1

    conn = sqlite3.connect(args.db)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ORDER BY name",
            (f"{prefix}%",),
        )
        tables = [str(r[0]) for r in cur.fetchall() if r and r[0]]
        if not tables:
            print("No vec tables found; nothing to drop.")
            return 0

        for t in tables:
            print(f"Dropping {t} ...")
            conn.execute(f"DROP TABLE IF EXISTS {_quote_ident(t)}")
        conn.commit()
        print(f"Done. Dropped {len(tables)} tables.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
