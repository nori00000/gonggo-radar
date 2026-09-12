"""Tests for alert.migrations -- schema migration idempotency and correctness."""

import sqlite3
import pytest
from alert.migrations import run_migrations


@pytest.fixture
def base_conn():
    """In-memory SQLite with base announcements table only."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
    CREATE TABLE IF NOT EXISTS announcements (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source          TEXT    NOT NULL,
        source_id       TEXT    NOT NULL,
        title           TEXT    NOT NULL,
        summary         TEXT    DEFAULT '',
        url             TEXT    NOT NULL,
        author          TEXT    DEFAULT '',
        category        TEXT    DEFAULT '',
        target          TEXT    DEFAULT '',
        period_start    TEXT,
        period_end      TEXT,
        relevance_score REAL    DEFAULT 0.0,
        relevance_reason TEXT   DEFAULT '',
        matched_keywords TEXT   DEFAULT '[]',
        is_notified     INTEGER DEFAULT 0,
        raw_data        TEXT    DEFAULT '',
        created_at      TEXT    NOT NULL,
        updated_at      TEXT    NOT NULL,
        UNIQUE(source, source_id)
    )""")
    conn.commit()
    yield conn
    conn.close()


class TestMigrations:
    """Tests for run_migrations correctness and idempotency."""

    def test_creates_new_columns(self, base_conn):
        """Migration should add knowledge layer columns to announcements."""
        run_migrations(base_conn, vec_available=False)

        cols = [r[1] for r in base_conn.execute("PRAGMA table_info(announcements)").fetchall()]
        for col in ["business_domain", "domain_confidence", "obsidian_path", "embedding_id"]:
            assert col in cols, f"Missing column: {col}"

    def test_creates_new_tables(self, base_conn):
        """Migration should create embeddings, application_history, research_documents."""
        run_migrations(base_conn, vec_available=False)

        tables = [r[0] for r in base_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        for table in ["embeddings", "application_history", "research_documents"]:
            assert table in tables, f"Missing table: {table}"

    def test_idempotent(self, base_conn):
        """Running migrations twice should not raise errors."""
        count1 = run_migrations(base_conn, vec_available=False)
        count2 = run_migrations(base_conn, vec_available=False)

        # Second run should apply 0 new migrations
        assert count2 == 0
        # First run should apply 4 migrations
        assert count1 == 5

    def test_without_vec(self, base_conn):
        """vec_available=False should NOT create vec_announcements virtual table."""
        run_migrations(base_conn, vec_available=False)

        tables = [r[0] for r in base_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "vec_announcements" not in tables

    def test_schema_version_tracking(self, base_conn):
        """Migration history should be tracked in schema_version table."""
        run_migrations(base_conn, vec_available=False)

        tables = [r[0] for r in base_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "schema_version" in tables

        rows = base_conn.execute("SELECT * FROM schema_version ORDER BY version").fetchall()
        assert len(rows) == 5  # 5 migrations
