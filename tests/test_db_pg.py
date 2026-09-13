"""Tests for PostgreSQL backend -- skipped when DATABASE_URL is not set.

Run with:
    DATABASE_URL=postgresql://gonggo_user:change_me@localhost:5432/gonggo_test \
    pytest tests/test_db_pg.py -v
"""

import os

import pytest

# Skip entire module if no PostgreSQL available
pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith(("postgresql://", "postgres://")),
    reason="DATABASE_URL not set or not PostgreSQL",
)


def _pg_available() -> bool:
    """Check if psycopg2 is importable and DATABASE_URL is set."""
    try:
        import psycopg2  # noqa: F401
        url = os.getenv("DATABASE_URL", "")
        return url.startswith(("postgresql://", "postgres://"))
    except ImportError:
        return False


@pytest.fixture
def pg_db():
    """Create a Database instance connected to PostgreSQL test DB.

    WARNING: This uses a real PostgreSQL database. Use a dedicated test database.
    Tables are cleaned after each test.
    """
    from alert.db import Database

    url = os.getenv("DATABASE_URL")
    db = Database(database_url=url)
    yield db

    # Cleanup: truncate all tables (reverse FK order)
    try:
        cur = db.conn.cursor()
        for table in [
            "vec_announcements", "research_documents", "application_history",
            "embeddings", "run_history", "keywords", "announcements",
        ]:
            try:
                cur.execute(f"TRUNCATE TABLE {table} CASCADE")
            except Exception:
                pass
    finally:
        db.close()


@pytest.fixture
def pg_sample_announcement():
    """Sample AnalyzedAnnouncement for PostgreSQL tests."""
    from alert.models import AnalyzedAnnouncement
    return AnalyzedAnnouncement(
        source="test_pg",
        source_id="pg-001",
        title="PostgreSQL 테스트 공고",
        url="https://example.com/pg-001",
        summary="PostgreSQL 백엔드 테스트용 공고",
        author="테스트",
        category="테스트",
        target="테스트 대상",
        period_start="2026-04-01",
        period_end="2026-04-30",
        relevance_score=0.9,
        relevance_reason="테스트 매칭",
        matched_keywords=["PostgreSQL", "테스트"],
    )


class TestPostgreSQLConnection:
    """Test basic PostgreSQL connectivity."""

    def test_connect_and_backend(self, pg_db):
        """Database should detect PostgreSQL backend."""
        assert pg_db._backend == "postgresql"

    def test_tables_created(self, pg_db):
        """All required tables should exist."""
        cur = pg_db.conn.cursor()
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
        """)
        tables = {row["table_name"] for row in cur.fetchall()}
        expected = {
            "announcements", "keywords", "run_history",
            "embeddings", "application_history", "research_documents",
            "schema_version",
        }
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"


class TestPostgreSQLCRUD:
    """Test CRUD operations on PostgreSQL."""

    def test_insert_announcement(self, pg_db, pg_sample_announcement):
        """Insert should return integer ID."""
        result = pg_db.insert_announcement(pg_sample_announcement)
        assert isinstance(result, int)
        assert result > 0

    def test_duplicate_returns_none(self, pg_db, pg_sample_announcement):
        """Duplicate insert should return None."""
        pg_db.insert_announcement(pg_sample_announcement)
        result = pg_db.insert_announcement(pg_sample_announcement)
        assert result is None

    def test_duplicate_return_existing(self, pg_db, pg_sample_announcement):
        """Duplicate with return_existing=True should return existing ID."""
        first_id = pg_db.insert_announcement(pg_sample_announcement)
        second_id = pg_db.insert_announcement(pg_sample_announcement, return_existing=True)
        assert second_id == first_id

    def test_get_unnotified(self, pg_db, pg_sample_announcement):
        """Get unnotified should return inserted announcements."""
        pg_db.insert_announcement(pg_sample_announcement)
        unnotified = pg_db.get_unnotified()
        assert len(unnotified) == 1
        assert unnotified[0].source_id == "pg-001"

    def test_mark_notified(self, pg_db, pg_sample_announcement):
        """Mark notified should update is_notified flag."""
        pg_db.insert_announcement(pg_sample_announcement)
        pg_db.mark_notified(pg_sample_announcement)
        unnotified = pg_db.get_unnotified()
        assert len(unnotified) == 0

    def test_is_duplicate(self, pg_db, pg_sample_announcement):
        """is_duplicate should detect existing records."""
        assert pg_db.is_duplicate("test_pg", "pg-001") is False
        pg_db.insert_announcement(pg_sample_announcement)
        assert pg_db.is_duplicate("test_pg", "pg-001") is True

    def test_search_announcements(self, pg_db, pg_sample_announcement):
        """Search should find matching announcements."""
        pg_db.insert_announcement(pg_sample_announcement)
        results = pg_db.search_announcements("PostgreSQL")
        assert len(results) >= 1

    def test_insert_keyword(self, pg_db):
        """Insert keyword should work."""
        from alert.models import Keyword
        kw = Keyword(keyword="스마트팜", category="boost", weight=1.0)
        assert pg_db.insert_keyword(kw) is True
        # Duplicate should return False
        assert pg_db.insert_keyword(kw) is False

    def test_get_keywords(self, pg_db):
        """Get keywords should return inserted keywords."""
        from alert.models import Keyword
        pg_db.insert_keyword(Keyword(keyword="테스트키워드", category="boost", weight=1.5))
        keywords = pg_db.get_keywords()
        assert len(keywords) >= 1
        assert keywords[0].keyword == "테스트키워드"

    def test_insert_run(self, pg_db):
        """Insert run should return integer ID."""
        run_id = pg_db.insert_run("test_source", 10, 5, 3, 2, "success")
        assert isinstance(run_id, int)
        assert run_id > 0

    def test_get_recent_runs(self, pg_db):
        """Get recent runs should return inserted runs."""
        pg_db.insert_run("test_source", 10, 5, 3, 2, "success")
        runs = pg_db.get_recent_runs()
        assert len(runs) >= 1

    def test_get_stats(self, pg_db, pg_sample_announcement):
        """Stats should aggregate correctly."""
        pg_db.insert_announcement(pg_sample_announcement)
        stats = pg_db.get_stats()
        assert stats["total"] == 1
        assert stats["unnotified"] == 1

    def test_application_history(self, pg_db, pg_sample_announcement):
        """Application history CRUD should work."""
        from alert.models import ApplicationRecord
        ann_id = pg_db.insert_announcement(pg_sample_announcement)
        record = ApplicationRecord(
            announcement_id=ann_id,
            status="discovered",
            assigned_domain="test_domain",
            priority=2,
        )
        rec_id = pg_db.insert_application_record(record)
        assert isinstance(rec_id, int)

        # Update status
        result = pg_db.update_application_status(rec_id, "applied", notes="테스트 메모")
        assert result is True

        # Get history
        history = pg_db.get_application_history(ann_id)
        assert len(history) == 1
        assert history[0].status == "applied"

    def test_research_document(self, pg_db):
        """Research document CRUD should work."""
        from alert.models import ResearchDocument
        doc = ResearchDocument(
            title="2026 Q1 분석",
            doc_type="quarterly",
            period_start="2026-01-01",
            period_end="2026-03-31",
            content="테스트 내용",
        )
        doc_id = pg_db.insert_research_document(doc)
        assert isinstance(doc_id, int)

        # Update
        pg_db.update_research_document(doc_id, "수정된 내용", 2)


class TestPostgreSQLDomainFeatures:
    """Test domain-specific features on PostgreSQL."""

    def test_update_domain(self, pg_db, pg_sample_announcement):
        """Domain update should work."""
        ann_id = pg_db.insert_announcement(pg_sample_announcement)
        pg_db.update_announcement_domain(ann_id, "smart_farm", 0.95)

        ann = pg_db.get_announcement_by_id(ann_id)
        assert ann is not None

    def test_get_announcements_by_period(self, pg_db, pg_sample_announcement):
        """Period query should work."""
        pg_db.insert_announcement(pg_sample_announcement)
        results = pg_db.get_announcements_by_period("2020-01-01", "2030-12-31")
        assert len(results) >= 1
