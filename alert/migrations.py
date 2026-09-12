"""
migrations.py

Knowledge Layer의 스키마 마이그레이션 시스템.

각 마이그레이션은 버전 번호와 함께 관리되며,
schema_version 테이블에 적용된 마이그레이션을 추적합니다.
run_migrations()는 멱등성을 보장하여 여러 번 실행해도 안전합니다.
"""

import logging
from datetime import datetime
from typing import Any, List, Tuple

import sqlite3

try:
    import psycopg2  # noqa: F401
    _PG_AVAILABLE = True
except ImportError:
    _PG_AVAILABLE = False

__all__ = ["run_migrations"]

logger = logging.getLogger(__name__)

# 마이그레이션 목록: (버전, 설명, SQL 문장 리스트)
MIGRATIONS: List[Tuple[int, str, List[str]]] = [
    (
        1,
        "Add business_domain, domain_confidence, obsidian_path, embedding_id to announcements",
        [
            "ALTER TABLE announcements ADD COLUMN business_domain TEXT DEFAULT '';",
            "ALTER TABLE announcements ADD COLUMN domain_confidence REAL DEFAULT 0.0;",
            "ALTER TABLE announcements ADD COLUMN obsidian_path TEXT DEFAULT '';",
            "ALTER TABLE announcements ADD COLUMN embedding_id INTEGER DEFAULT NULL;",
        ]
    ),
    (
        2,
        "Create embeddings table and optional vec_announcements virtual table",
        [
            """
            CREATE TABLE IF NOT EXISTS embeddings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                announcement_id INTEGER NOT NULL,
                model_name      TEXT    NOT NULL DEFAULT 'text-embedding-3-small',
                embedding       BLOB    NOT NULL,
                dimensions      INTEGER NOT NULL DEFAULT 256,
                created_at      TEXT    NOT NULL,
                FOREIGN KEY (announcement_id) REFERENCES announcements(id)
            );
            """,
            "CREATE INDEX IF NOT EXISTS idx_emb_ann ON embeddings(announcement_id);",
        ]
    ),
    (
        3,
        "Create application_history table",
        [
            """
            CREATE TABLE IF NOT EXISTS application_history (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                announcement_id   INTEGER NOT NULL,
                status            TEXT    NOT NULL DEFAULT 'discovered',
                applied_date      TEXT,
                result_date       TEXT,
                result            TEXT    DEFAULT '',
                prepared_docs     TEXT    DEFAULT '[]',
                notes             TEXT    DEFAULT '',
                assigned_domain   TEXT    DEFAULT '',
                priority          INTEGER DEFAULT 0,
                created_at        TEXT    NOT NULL,
                updated_at        TEXT    NOT NULL,
                FOREIGN KEY (announcement_id) REFERENCES announcements(id)
            );
            """,
            "CREATE INDEX IF NOT EXISTS idx_apphist_ann ON application_history(announcement_id);",
            "CREATE INDEX IF NOT EXISTS idx_apphist_status ON application_history(status);",
        ]
    ),
    (
        4,
        "Create research_documents table",
        [
            """
            CREATE TABLE IF NOT EXISTS research_documents (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT    NOT NULL,
                doc_type        TEXT    NOT NULL DEFAULT 'quarterly',
                period_start    TEXT,
                period_end      TEXT,
                content         TEXT    NOT NULL DEFAULT '',
                metadata        TEXT    DEFAULT '{}',
                obsidian_path   TEXT    DEFAULT '',
                version         INTEGER DEFAULT 1,
                created_at      TEXT    NOT NULL,
                updated_at      TEXT    NOT NULL
            );
            """
        ]
    ),
    (
        5,
        "Add duplicate_of to announcements (identity migration conflicts)",
        [
            "ALTER TABLE announcements ADD COLUMN duplicate_of INTEGER DEFAULT NULL;",
            "CREATE INDEX IF NOT EXISTS idx_ann_duplicate_of"
            " ON announcements(duplicate_of);",
        ]
    ),
    (
        6,
        "Add legacy flag to announcements (rows stored under old id rules)",
        [
            "ALTER TABLE announcements ADD COLUMN legacy INTEGER DEFAULT 0;",
            "CREATE INDEX IF NOT EXISTS idx_ann_legacy ON announcements(legacy);",
        ]
    ),
]


def _create_schema_version_table(conn: Any, backend: str = "sqlite") -> None:
    """schema_version 테이블 생성 (존재하지 않는 경우)."""
    if backend == "postgresql":
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version      INTEGER PRIMARY KEY,
                applied_at   TEXT    NOT NULL,
                description  TEXT    NOT NULL DEFAULT ''
            );
        """)
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version      INTEGER PRIMARY KEY,
                applied_at   TEXT    NOT NULL,
                description  TEXT    NOT NULL DEFAULT ''
            );
        """)
        conn.commit()


def _get_applied_versions(conn: Any, backend: str = "sqlite") -> set:
    """이미 적용된 마이그레이션 버전 집합 반환."""
    if backend == "postgresql":
        cursor = conn.cursor()
        cursor.execute("SELECT version FROM schema_version ORDER BY version;")
        return {row["version"] for row in cursor.fetchall()}
    else:
        cursor = conn.execute("SELECT version FROM schema_version ORDER BY version;")
        return {row[0] for row in cursor.fetchall()}


def _apply_migration(
    conn: Any,
    version: int,
    description: str,
    sql_statements: List[str],
    vec_available: bool = False,
    backend: str = "sqlite",
) -> None:
    """단일 마이그레이션 적용."""
    logger.info(f"Applying migration {version}: {description}")

    def _exec(sql: str) -> None:
        """Execute SQL with backend-appropriate cursor handling."""
        if backend == "postgresql":
            sql = sql.replace("AUTOINCREMENT", "")
            # REAL -> DOUBLE PRECISION already handled in PG DDL, but handle dynamic SQL
            conn.cursor().execute(sql)
        else:
            conn.execute(sql)

    try:
        for sql in sql_statements:
            if sql.strip().upper().startswith("ALTER TABLE"):
                try:
                    _exec(sql)
                except (sqlite3.OperationalError if backend == "sqlite" else Exception) as e:
                    err_msg = str(e).lower()
                    if "duplicate column" in err_msg or "already exists" in err_msg:
                        logger.debug(f"Column already exists, skipping: {sql}")
                    else:
                        raise
            else:
                _exec(sql)
    except Exception:
        if backend == "sqlite":
            conn.rollback()
        raise

    # Migration 2: vector table creation
    if version == 2:
        if backend == "postgresql":
            # Create pgvector extension and table (if pgvector is available)
            try:
                cur = conn.cursor()
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS vec_announcements (
                        id        SERIAL PRIMARY KEY,
                        embedding vector(256)
                    );
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_vec_ann_embedding
                    ON vec_announcements USING ivfflat (embedding vector_cosine_ops)
                    WITH (lists = 10);
                """)
                logger.info("Created pgvector vec_announcements table")
            except Exception as e:
                logger.warning(f"Failed to create pgvector table (non-fatal): {e}")
        elif vec_available:
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS vec_announcements USING vec0(
                        embedding float[256]
                    );
                """)
                logger.info("Created vec_announcements virtual table (vec0 available)")
            except sqlite3.OperationalError as e:
                logger.warning(f"Failed to create vec_announcements: {e}")

    # Record migration in schema_version
    now = datetime.now().isoformat()
    if backend == "postgresql":
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO schema_version (version, applied_at, description)
               VALUES (%s, %s, %s)
               ON CONFLICT (version) DO UPDATE SET applied_at = EXCLUDED.applied_at, description = EXCLUDED.description;""",
            (version, now, description),
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version, applied_at, description) VALUES (?, ?, ?);",
            (version, now, description),
        )
        conn.commit()
    logger.info(f"Migration {version} applied successfully")


def run_migrations(conn: Any, vec_available: bool = False, backend: str = "sqlite") -> int:
    """
    모든 대기 중인 마이그레이션을 순서대로 실행.

    Args:
        conn: SQLite 연결 객체
        vec_available: sqlite-vec 확장 사용 가능 여부 (기본값: False)
        backend: "sqlite" or "postgresql"

    Returns:
        적용된 마이그레이션 개수

    멱등성 보장: 여러 번 실행해도 안전.
    """
    _create_schema_version_table(conn, backend=backend)
    applied_versions = _get_applied_versions(conn, backend=backend)

    count = 0
    for version, description, sql_statements in MIGRATIONS:
        if version not in applied_versions:
            _apply_migration(conn, version, description, sql_statements, vec_available, backend=backend)
            count += 1
        else:
            logger.debug(f"Migration {version} already applied, skipping")

    if count == 0:
        logger.info("No pending migrations. Database is up to date.")
    else:
        logger.info(f"Applied {count} migration(s)")

    return count
