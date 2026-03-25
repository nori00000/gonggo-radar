"""Shared pytest fixtures for gonggo-radar tests."""

import sqlite3
import pytest
from alert.models import AnalyzedAnnouncement, ApplicationRecord
from alert.migrations import run_migrations


@pytest.fixture
def in_memory_conn():
    """Raw SQLite connection with base tables + migrations applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Create base tables (same as db.py _create_tables)
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
    conn.execute("""
    CREATE TABLE IF NOT EXISTS keywords (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword   TEXT    NOT NULL UNIQUE,
        category  TEXT    NOT NULL DEFAULT 'boost',
        weight    REAL    NOT NULL DEFAULT 1.0,
        is_active INTEGER NOT NULL DEFAULT 1
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS run_history (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at    TEXT    NOT NULL,
        finished_at   TEXT,
        source        TEXT    NOT NULL,
        total_fetched INTEGER DEFAULT 0,
        new_count     INTEGER DEFAULT 0,
        relevant_count INTEGER DEFAULT 0,
        notified_count INTEGER DEFAULT 0,
        status        TEXT    DEFAULT 'running',
        error_message TEXT    DEFAULT ''
    )""")
    conn.commit()

    # Apply knowledge layer migrations
    run_migrations(conn, vec_available=False)

    yield conn
    conn.close()


@pytest.fixture
def sample_announcement():
    """Sample AnalyzedAnnouncement for testing."""
    return AnalyzedAnnouncement(
        source="test",
        source_id="test-001",
        title="2026년 스마트팜 혁신 지원사업",
        url="https://example.com/test-001",
        summary="스마트팜 시설원예 농업인 지원",
        author="농림축산식품부",
        category="농업지원",
        target="시설원예농가",
        period_start="2026-04-01",
        period_end="2026-04-30",
        relevance_score=0.85,
        relevance_reason="스마트팜 + 시설원예 매칭",
        matched_keywords=["스마트팜", "시설원예"],
    )


@pytest.fixture
def sample_application_record():
    """Sample ApplicationRecord for testing."""
    return ApplicationRecord(
        announcement_id=1,
        status="discovered",
        assigned_domain="moss_agriculture",
        priority=2,
    )
