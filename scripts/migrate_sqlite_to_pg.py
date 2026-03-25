#!/usr/bin/env python3
"""SQLite → PostgreSQL 데이터 이관 스크립트.

사용법:
    DATABASE_URL=postgresql://agrion:pass@localhost:5432/agrion_db \
    python scripts/migrate_sqlite_to_pg.py [--sqlite-path alert/data/announcements.db]

멱등성 보장: ON CONFLICT DO NOTHING으로 중복 실행 안전.
"""

import argparse
import json
import os
import sqlite3
import struct
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def get_pg_connection(database_url: str):
    """Create PostgreSQL connection."""
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False  # Use transactions for data migration
    return conn


def get_sqlite_connection(db_path: str) -> sqlite3.Connection:
    """Create SQLite connection."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def migrate_table(sqlite_conn, pg_conn, table_name: str, columns: list[str], conflict_column: str = "id"):
    """Migrate a single table from SQLite to PostgreSQL.

    Args:
        sqlite_conn: SQLite connection
        pg_conn: PostgreSQL connection
        table_name: Table name
        columns: Column names to migrate
        conflict_column: Column to use for ON CONFLICT
    """
    rows = sqlite_conn.execute(f"SELECT * FROM {table_name} ORDER BY id").fetchall()
    if not rows:
        print(f"  {table_name}: 0 rows (empty)")
        return 0

    col_list = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))

    cur = pg_conn.cursor()
    count = 0
    for row in rows:
        values = tuple(row[col] for col in columns)
        try:
            cur.execute(
                f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders}) ON CONFLICT ({conflict_column}) DO NOTHING",
                values,
            )
            if cur.rowcount > 0:
                count += 1
        except Exception as e:
            print(f"  WARNING: Failed to insert {table_name} row {row['id']}: {e}")
            pg_conn.rollback()
            continue

    print(f"  {table_name}: {count}/{len(rows)} rows migrated")
    return count


def reset_sequence(pg_conn, table_name: str):
    """Reset PostgreSQL SERIAL sequence to max(id) + 1."""
    cur = pg_conn.cursor()
    cur.execute(f"SELECT COALESCE(MAX(id), 0) AS max_id FROM {table_name}")
    max_id = cur.fetchone()["max_id"]
    if max_id > 0:
        cur.execute(f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), {max_id})")
        print(f"  {table_name}: sequence reset to {max_id}")


def migrate_vec_announcements(sqlite_conn, pg_conn):
    """Migrate vec_announcements: sqlite-vec BLOB → pgvector vector(256)."""
    # Check if sqlite has vec_announcements
    tables = [r[0] for r in sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' OR type='virtual table'"
    ).fetchall()]

    if "vec_announcements" not in [t.lower() for t in tables]:
        print("  vec_announcements: not found in SQLite (skipping)")
        return

    try:
        rows = sqlite_conn.execute("SELECT rowid, embedding FROM vec_announcements").fetchall()
    except Exception as e:
        print(f"  vec_announcements: cannot read ({e})")
        return

    if not rows:
        print("  vec_announcements: 0 rows (empty)")
        return

    cur = pg_conn.cursor()
    count = 0
    for row in rows:
        rowid = row["rowid"] if "rowid" in row.keys() else row[0]
        emb_bytes = row["embedding"] if "embedding" in row.keys() else row[1]

        # Convert float32 bytes to float list
        n = len(emb_bytes) // 4
        floats = list(struct.unpack(f"<{n}f", emb_bytes))
        vec_literal = "[" + ",".join(str(f) for f in floats) + "]"

        try:
            cur.execute(
                "INSERT INTO vec_announcements (id, embedding) VALUES (%s, %s::vector) ON CONFLICT (id) DO NOTHING",
                (rowid, vec_literal),
            )
            if cur.rowcount > 0:
                count += 1
        except Exception as e:
            print(f"  WARNING: vec row {rowid}: {e}")
            pg_conn.rollback()
            continue

    print(f"  vec_announcements: {count}/{len(rows)} rows migrated")
    return count


def main():
    parser = argparse.ArgumentParser(description="Migrate SQLite to PostgreSQL")
    parser.add_argument(
        "--sqlite-path",
        default="alert/data/announcements.db",
        help="Path to SQLite database (default: alert/data/announcements.db)",
    )
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable not set")
        print("Usage: DATABASE_URL=postgresql://... python scripts/migrate_sqlite_to_pg.py")
        sys.exit(1)

    sqlite_path = Path(args.sqlite_path)
    if not sqlite_path.exists():
        print(f"ERROR: SQLite database not found: {sqlite_path}")
        sys.exit(1)

    print(f"Source: {sqlite_path}")
    print(f"Target: {database_url.split('@')[1] if '@' in database_url else database_url}")
    print()

    sqlite_conn = get_sqlite_connection(str(sqlite_path))
    pg_conn = get_pg_connection(database_url)

    try:
        # Migrate tables in FK-safe order
        print("=== Migrating tables ===")

        # 1. announcements (no FK dependencies)
        ann_cols = [
            "id", "source", "source_id", "title", "summary", "url", "author",
            "category", "target", "period_start", "period_end", "relevance_score",
            "relevance_reason", "matched_keywords", "is_notified", "raw_data",
            "created_at", "updated_at",
        ]
        # Check for knowledge layer columns
        sqlite_cols = [r[1] for r in sqlite_conn.execute("PRAGMA table_info(announcements)").fetchall()]
        for extra_col in ["business_domain", "domain_confidence", "obsidian_path", "embedding_id"]:
            if extra_col in sqlite_cols:
                ann_cols.append(extra_col)

        migrate_table(sqlite_conn, pg_conn, "announcements", ann_cols, "id")
        pg_conn.commit()

        # 2. keywords
        kw_cols = ["id", "keyword", "category", "weight", "is_active"]
        migrate_table(sqlite_conn, pg_conn, "keywords", kw_cols, "id")
        pg_conn.commit()

        # 3. run_history
        rh_cols = [
            "id", "started_at", "finished_at", "source", "total_fetched",
            "new_count", "relevant_count", "notified_count", "status", "error_message",
        ]
        migrate_table(sqlite_conn, pg_conn, "run_history", rh_cols, "id")
        pg_conn.commit()

        # 4. embeddings (FK → announcements)
        emb_tables = [r[0] for r in sqlite_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        if "embeddings" in emb_tables:
            emb_cols = ["id", "announcement_id", "model_name", "embedding", "dimensions", "created_at"]
            migrate_table(sqlite_conn, pg_conn, "embeddings", emb_cols, "id")
            pg_conn.commit()

        # 5. application_history (FK → announcements)
        if "application_history" in emb_tables:
            ah_cols = [
                "id", "announcement_id", "status", "applied_date", "result_date",
                "result", "prepared_docs", "notes", "assigned_domain", "priority",
                "created_at", "updated_at",
            ]
            migrate_table(sqlite_conn, pg_conn, "application_history", ah_cols, "id")
            pg_conn.commit()

        # 6. research_documents
        if "research_documents" in emb_tables:
            rd_cols = [
                "id", "title", "doc_type", "period_start", "period_end",
                "content", "metadata", "obsidian_path", "version",
                "created_at", "updated_at",
            ]
            migrate_table(sqlite_conn, pg_conn, "research_documents", rd_cols, "id")
            pg_conn.commit()

        # 7. vec_announcements (special handling)
        migrate_vec_announcements(sqlite_conn, pg_conn)
        pg_conn.commit()

        # 8. schema_version
        sv_tables = [r[0] for r in sqlite_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        if "schema_version" in sv_tables:
            sv_cols = ["version", "applied_at", "description"]
            migrate_table(sqlite_conn, pg_conn, "schema_version", sv_cols, "version")
            pg_conn.commit()

        # Reset sequences
        print("\n=== Resetting sequences ===")
        for table in ["announcements", "keywords", "run_history", "embeddings",
                       "application_history", "research_documents", "vec_announcements"]:
            try:
                reset_sequence(pg_conn, table)
                pg_conn.commit()
            except Exception:
                pg_conn.rollback()

        print("\n=== Migration complete ===")

    except Exception as e:
        pg_conn.rollback()
        print(f"\nERROR: Migration failed: {e}")
        raise
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    main()
