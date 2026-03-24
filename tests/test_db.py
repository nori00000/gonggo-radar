"""Tests for alert.db -- insert, duplicate handling, field validation."""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest
from alert.db import Database
from alert.models import AnalyzedAnnouncement, ApplicationRecord


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

    def _insert_app_record(self, db, announcement_id=1):
        """Helper to insert an application record and return its ID."""
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
