"""
migrations.py

Knowledge Layer의 스키마 마이그레이션 시스템.

각 마이그레이션은 버전 번호와 함께 관리되며,
schema_version 테이블에 적용된 마이그레이션을 추적합니다.
run_migrations()는 멱등성을 보장하여 여러 번 실행해도 안전합니다.
"""

import sqlite3
import logging
from datetime import datetime
from typing import List, Tuple

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
]


def _create_schema_version_table(conn: sqlite3.Connection) -> None:
    """schema_version 테이블 생성 (존재하지 않는 경우)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version      INTEGER PRIMARY KEY,
            applied_at   TEXT    NOT NULL,
            description  TEXT    NOT NULL DEFAULT ''
        );
    """)
    conn.commit()


def _get_applied_versions(conn: sqlite3.Connection) -> set:
    """이미 적용된 마이그레이션 버전 집합 반환."""
    cursor = conn.execute("SELECT version FROM schema_version ORDER BY version;")
    return {row[0] for row in cursor.fetchall()}


def _apply_migration(
    conn: sqlite3.Connection,
    version: int,
    description: str,
    sql_statements: List[str],
    vec_available: bool = False
) -> None:
    """단일 마이그레이션 적용."""
    logger.info(f"Applying migration {version}: {description}")

    try:
        for sql in sql_statements:
            # ALTER TABLE 구문은 컬럼이 이미 존재할 수 있으므로 try/except
            if sql.strip().upper().startswith("ALTER TABLE"):
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError as e:
                    if "duplicate column name" in str(e).lower():
                        logger.debug(f"Column already exists, skipping: {sql}")
                    else:
                        raise
            else:
                conn.execute(sql)
    except Exception:
        conn.rollback()
        raise

    # Migration 2의 경우 vec_announcements 가상 테이블 추가 (옵션)
    if version == 2 and vec_available:
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_announcements USING vec0(
                    embedding float[256]
                );
            """)
            logger.info("Created vec_announcements virtual table (vec0 available)")
        except sqlite3.OperationalError as e:
            logger.warning(f"Failed to create vec_announcements: {e}")

    # schema_version 테이블에 기록
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (version, applied_at, description) VALUES (?, ?, ?);",
        (version, now, description)
    )
    conn.commit()
    logger.info(f"Migration {version} applied successfully")


def run_migrations(conn: sqlite3.Connection, vec_available: bool = False) -> int:
    """
    모든 대기 중인 마이그레이션을 순서대로 실행.

    Args:
        conn: SQLite 연결 객체
        vec_available: sqlite-vec 확장 사용 가능 여부 (기본값: False)

    Returns:
        적용된 마이그레이션 개수

    멱등성 보장: 여러 번 실행해도 안전.
    """
    _create_schema_version_table(conn)
    applied_versions = _get_applied_versions(conn)

    count = 0
    for version, description, sql_statements in MIGRATIONS:
        if version not in applied_versions:
            _apply_migration(conn, version, description, sql_statements, vec_available)
            count += 1
        else:
            logger.debug(f"Migration {version} already applied, skipping")

    if count == 0:
        logger.info("No pending migrations. Database is up to date.")
    else:
        logger.info(f"Applied {count} migration(s)")

    return count
