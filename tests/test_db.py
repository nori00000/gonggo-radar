"""Tests for alert.db -- insert, duplicate handling, field validation."""


import pytest
from alert.db import Database


@pytest.fixture
def tmp_db(tmp_path):
    """Create a Database instance with a temp file."""
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    yield db
    db.close()


class TestInsertAnnouncement:
    """Tests for insert_announcement return values."""

    def test_insert_returns_int_id(self, tmp_db, sample_announcement):
        """New announcement → returns integer row ID."""
        result = tmp_db.insert_announcement(sample_announcement)
        assert isinstance(result, int)
        assert result > 0

    def test_insert_duplicate_returns_none(self, tmp_db, sample_announcement):
        """Duplicate (same source + source_id) → returns None."""
        tmp_db.insert_announcement(sample_announcement)
        result = tmp_db.insert_announcement(sample_announcement)
        assert result is None

    def test_insert_duplicate_return_existing(self, tmp_db, sample_announcement):
        """Duplicate with return_existing=True → returns existing row ID."""
        first_id = tmp_db.insert_announcement(sample_announcement)
        second_id = tmp_db.insert_announcement(sample_announcement, return_existing=True)
        assert second_id == first_id

    def test_backward_compatible_bool(self, tmp_db, sample_announcement):
        """bool(int) is True, bool(None) is False -- backward compat."""
        new_id = tmp_db.insert_announcement(sample_announcement)
        assert bool(new_id) is True

        dup_result = tmp_db.insert_announcement(sample_announcement)
        assert bool(dup_result) is False

    def test_duplicate_updates_score(self, tmp_db, sample_announcement):
        """Duplicate insert should update relevance_score."""
        tmp_db.insert_announcement(sample_announcement)

        # Modify and re-insert
        sample_announcement.relevance_score = 0.95
        sample_announcement.relevance_reason = "updated reason"
        tmp_db.insert_announcement(sample_announcement)

        # Check the stored value was updated
        row = tmp_db.conn.execute(
            "SELECT relevance_score, relevance_reason FROM announcements WHERE source_id = ?",
            (sample_announcement.source_id,),
        ).fetchone()
        assert row["relevance_score"] == 0.95
        assert row["relevance_reason"] == "updated reason"


class TestUpdateApplicationStatus:
    """Tests for update_application_status field validation."""

    def _insert_app_record(self, db, announcement_id=None):
        """Helper to insert an application record and return its ID."""
        # First, insert a parent announcement to satisfy FK constraint
        if announcement_id is None:
            from datetime import datetime
            now = datetime.now().isoformat()
            cur = db.conn.execute(
                """INSERT INTO announcements
                   (source, source_id, title, url, created_at, updated_at)
                   VALUES ('test', ?, 'FK test', 'http://test', ?, ?)""",
                (f"fk-test-{id(self)}-{now}", now, now),
            )
            db.conn.commit()
            announcement_id = cur.lastrowid
        db.conn.execute(
            """INSERT INTO application_history
               (announcement_id, status, assigned_domain, priority, created_at, updated_at)
               VALUES (?, 'discovered', 'moss_agriculture', 2, datetime('now'), datetime('now'))""",
            (announcement_id,),
        )
        db.conn.commit()
        return db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def test_valid_field_update(self, tmp_db):
        """Allowed fields should update successfully."""
        record_id = self._insert_app_record(tmp_db)
        result = tmp_db.update_application_status(
            record_id, "applied", notes="신청서 제출 완료"
        )
        assert result is True

    def test_invalid_field_raises_valueerror(self, tmp_db):
        """Disallowed field names should raise ValueError (SQL injection prevention)."""
        record_id = self._insert_app_record(tmp_db)
        with pytest.raises(ValueError, match="Disallowed field"):
            tmp_db.update_application_status(
                record_id, "applied", malicious_field="DROP TABLE"
            )

    def test_nonexistent_record_returns_false(self, tmp_db):
        """Non-existent record ID → returns False."""
        result = tmp_db.update_application_status(99999, "applied")
        assert result is False

    def test_allowed_fields(self, tmp_db):
        """Each allowed field should work without error."""
        record_id = self._insert_app_record(tmp_db)
        for field_name in ["applied_date", "result_date", "result", "notes", "priority"]:
            result = tmp_db.update_application_status(
                record_id, "in_progress", **{field_name: "test_value"}
            )
            assert result is True


class TestSyncKeywordsFromConfig:
    """Tests for sync_keywords_from_config -- additive-only keyword sync."""

    def _create_mock_config(self):
        """Create a mock KeywordsConfig for testing."""
        from dataclasses import dataclass, field

        @dataclass
        class MockKeywordsConfig:
            must_match: list = field(default_factory=list)
            boost: list = field(default_factory=list)
            exclude: list = field(default_factory=list)

        return MockKeywordsConfig(
            must_match=["스마트팜", "시설원예", "농업"],
            boost=["IoT", "자동화", "센서"],
            exclude=["개발", "건설"]
        )

    def test_sync_adds_new_keywords(self, tmp_db):
        """Sync should add new keywords from config."""
        mock_cfg = self._create_mock_config()
        count = tmp_db.sync_keywords_from_config(mock_cfg)

        # Should insert all keywords (3 must_match + 3 boost + 2 exclude = 8)
        assert count == 8

        # Verify they exist in database
        keywords = tmp_db.get_keywords()
        assert len(keywords) == 8

        # Verify category and weight mapping
        must_match_kws = [k for k in keywords if k.category == "must_match"]
        assert len(must_match_kws) == 3
        assert all(k.weight == 0.5 for k in must_match_kws)

        boost_kws = [k for k in keywords if k.category == "boost"]
        assert len(boost_kws) == 3
        assert all(k.weight == 0.05 for k in boost_kws)

        exclude_kws = [k for k in keywords if k.category == "exclude"]
        assert len(exclude_kws) == 2
        assert all(k.weight == 0.0 for k in exclude_kws)

    def test_sync_does_not_overwrite_existing(self, tmp_db):
        """Sync should not modify existing keywords."""
        # Insert a keyword with custom weight
        from alert.models import Keyword
        existing = Keyword(keyword="스마트팜", category="must_match", weight=0.99, is_active=True)
        tmp_db.insert_keyword(existing)

        # Sync with config that includes the same keyword
        mock_cfg = self._create_mock_config()
        count = tmp_db.sync_keywords_from_config(mock_cfg)

        # Should only add new keywords (7 new, 1 skipped)
        assert count == 7

        # Verify existing keyword was not modified
        keywords = tmp_db.get_keywords()
        existing_kw = next(k for k in keywords if k.keyword == "스마트팜")
        assert existing_kw.weight == 0.99  # Original weight preserved

    def test_sync_is_idempotent(self, tmp_db):
        """Running sync twice should not create duplicates."""
        mock_cfg = self._create_mock_config()

        # First sync
        count1 = tmp_db.sync_keywords_from_config(mock_cfg)
        assert count1 == 8

        # Second sync (should add 0 new keywords)
        count2 = tmp_db.sync_keywords_from_config(mock_cfg)
        assert count2 == 0

        # Total count should still be 8
        keywords = tmp_db.get_keywords()
        assert len(keywords) == 8

    def test_sync_works_with_empty_db(self, tmp_db):
        """Sync should work on fresh database."""
        # Verify DB is empty
        keywords = tmp_db.get_keywords()
        assert len(keywords) == 0

        # Run sync
        mock_cfg = self._create_mock_config()
        count = tmp_db.sync_keywords_from_config(mock_cfg)

        assert count == 8
        keywords = tmp_db.get_keywords()
        assert len(keywords) == 8

    def test_sync_partial_overlap(self, tmp_db):
        """Sync should only add keywords that don't exist."""
        from alert.models import Keyword

        # Pre-populate with 2 keywords
        tmp_db.insert_keyword(Keyword(keyword="스마트팜", category="must_match", weight=0.5))
        tmp_db.insert_keyword(Keyword(keyword="IoT", category="boost", weight=0.05))

        # Sync with config
        mock_cfg = self._create_mock_config()
        count = tmp_db.sync_keywords_from_config(mock_cfg)

        # Should add 6 new keywords (8 total - 2 existing)
        assert count == 6

        keywords = tmp_db.get_keywords()
        assert len(keywords) == 8

    def test_sync_uses_correct_weight_mapping(self, tmp_db):
        """Sync should use correct weight for each category."""
        mock_cfg = self._create_mock_config()
        tmp_db.sync_keywords_from_config(mock_cfg)

        keywords = tmp_db.get_keywords()

        # Check must_match keywords have weight 0.5
        must_match = [k for k in keywords if k.keyword in ["스마트팜", "시설원예", "농업"]]
        assert all(k.weight == 0.5 for k in must_match)
        assert all(k.category == "must_match" for k in must_match)

        # Check boost keywords have weight 0.05
        boost = [k for k in keywords if k.keyword in ["IoT", "자동화", "센서"]]
        assert all(k.weight == 0.05 for k in boost)
        assert all(k.category == "boost" for k in boost)

        # Check exclude keywords have weight 0.0
        exclude = [k for k in keywords if k.keyword in ["개발", "건설"]]
        assert all(k.weight == 0.0 for k in exclude)
        assert all(k.category == "exclude" for k in exclude)
