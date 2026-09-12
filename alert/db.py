"""데이터베이스 -- 공고, 키워드, 실행 이력 관리 (SQLite / PostgreSQL 듀얼 백엔드)."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .models import (
    AnalyzedAnnouncement,
    ApplicationRecord,
    Keyword,
    RawAnnouncement,
    ResearchDocument,
)

# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------
_DATABASE_URL = os.getenv("DATABASE_URL", "")

if _DATABASE_URL.startswith(("postgresql://", "postgres://")):
    import psycopg2
    import psycopg2.extras
    _BACKEND = "postgresql"
    _IntegrityError: type = psycopg2.IntegrityError
    _OperationalError: type = psycopg2.OperationalError
else:
    _BACKEND = "sqlite"
    _IntegrityError = sqlite3.IntegrityError
    _OperationalError = sqlite3.OperationalError


def _sql(query: str) -> str:
    """Convert SQLite placeholders to PostgreSQL if needed."""
    if _BACKEND == "postgresql":
        return query.replace("?", "%s")
    return query

# ---------------------------------------------------------------------------
# SQL Definitions
# ---------------------------------------------------------------------------

_CREATE_ANNOUNCEMENTS = """
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
);
"""

_CREATE_KEYWORDS = """
CREATE TABLE IF NOT EXISTS keywords (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword   TEXT    NOT NULL UNIQUE,
    category  TEXT    NOT NULL DEFAULT 'boost',
    weight    REAL    NOT NULL DEFAULT 1.0,
    is_active INTEGER NOT NULL DEFAULT 1
);
"""

_CREATE_RUN_HISTORY = """
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
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ann_source ON announcements(source);",
    "CREATE INDEX IF NOT EXISTS idx_ann_notified ON announcements(is_notified);",
    "CREATE INDEX IF NOT EXISTS idx_ann_score ON announcements(relevance_score);",
    "CREATE INDEX IF NOT EXISTS idx_ann_created ON announcements(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_kw_category ON keywords(category);",
]

# ---------------------------------------------------------------------------
# PostgreSQL SQL Definitions
# ---------------------------------------------------------------------------

_PG_CREATE_ANNOUNCEMENTS = """
CREATE TABLE IF NOT EXISTS announcements (
    id              SERIAL PRIMARY KEY,
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
    relevance_score DOUBLE PRECISION DEFAULT 0.0,
    relevance_reason TEXT   DEFAULT '',
    matched_keywords TEXT   DEFAULT '[]',
    is_notified     INTEGER DEFAULT 0,
    raw_data        TEXT    DEFAULT '',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE(source, source_id)
);
"""

_PG_CREATE_KEYWORDS = """
CREATE TABLE IF NOT EXISTS keywords (
    id        SERIAL PRIMARY KEY,
    keyword   TEXT    NOT NULL UNIQUE,
    category  TEXT    NOT NULL DEFAULT 'boost',
    weight    DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    is_active INTEGER NOT NULL DEFAULT 1
);
"""

_PG_CREATE_RUN_HISTORY = """
CREATE TABLE IF NOT EXISTS run_history (
    id            SERIAL PRIMARY KEY,
    started_at    TEXT    NOT NULL,
    finished_at   TEXT,
    source        TEXT    NOT NULL,
    total_fetched INTEGER DEFAULT 0,
    new_count     INTEGER DEFAULT 0,
    relevant_count INTEGER DEFAULT 0,
    notified_count INTEGER DEFAULT 0,
    status        TEXT    DEFAULT 'running',
    error_message TEXT    DEFAULT ''
);
"""

_PG_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ann_source ON announcements(source);",
    "CREATE INDEX IF NOT EXISTS idx_ann_notified ON announcements(is_notified);",
    "CREATE INDEX IF NOT EXISTS idx_ann_score ON announcements(relevance_score);",
    "CREATE INDEX IF NOT EXISTS idx_ann_created ON announcements(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_kw_category ON keywords(category);",
]


class Database:
    """Persistence layer for the alert bot (SQLite / PostgreSQL)."""

    def __init__(self, db_path: str | Path | None = None, database_url: str = "") -> None:
        """Open (or create) the database.

        Args:
            db_path: Path to the SQLite file. Ignored when using PostgreSQL.
            database_url: PostgreSQL connection URL. If empty, falls back to
                          DATABASE_URL env var, then SQLite.
        """
        url = database_url or _DATABASE_URL
        self._backend = "postgresql" if url.startswith(("postgresql://", "postgres://")) else "sqlite"

        if self._backend == "postgresql":
            import psycopg2
            import psycopg2.extras
            self._conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
            self._conn.autocommit = True
            self.db_path = None
            self._vec_available = False  # pgvector handled separately
        else:
            if db_path is None:
                db_path = Path(__file__).resolve().parent / "data" / "announcements.db"
            else:
                db_path = Path(db_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = db_path
            self._conn = sqlite3.connect(str(db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._vec_available = False
            try:
                import sqlite_vec
                sqlite_vec.load(self._conn)
                self._vec_available = True
            except Exception as e:
                logging.getLogger(__name__).debug("sqlite-vec not loaded: %s", e)

        self._create_tables()

        # Run knowledge layer migrations
        from .migrations import run_migrations
        run_migrations(self._conn, vec_available=self._vec_available, backend=self._backend)

    @property
    def conn(self) -> Any:
        """Public access to the underlying connection (for vector operations)."""
        return self._conn

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_tables(self) -> None:
        """Create tables and indexes if they do not already exist."""
        cur = self._conn.cursor()
        if self._backend == "postgresql":
            cur.execute(_PG_CREATE_ANNOUNCEMENTS)
            cur.execute(_PG_CREATE_KEYWORDS)
            cur.execute(_PG_CREATE_RUN_HISTORY)
            for idx_sql in _PG_CREATE_INDEXES:
                cur.execute(idx_sql)
        else:
            cur.execute(_CREATE_ANNOUNCEMENTS)
            cur.execute(_CREATE_KEYWORDS)
            cur.execute(_CREATE_RUN_HISTORY)
            for idx_sql in _CREATE_INDEXES:
                cur.execute(idx_sql)
            self._conn.commit()

    # ------------------------------------------------------------------
    # Insert helpers
    # ------------------------------------------------------------------

    def _insert_returning_id(self, sql: str, params: tuple) -> int:
        """Execute INSERT and return the new row ID.

        PostgreSQL: appends RETURNING id to the query.
        SQLite: uses cursor.lastrowid.
        """
        if self._backend == "postgresql":
            cur = self._conn.cursor()
            cur.execute(sql + " RETURNING id", params)
            row = cur.fetchone()
            return row["id"]
        else:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid

    def _row_get(self, row: Any, key: str, default: Any = None) -> Any:
        """Get value from a row (works with both sqlite3.Row and RealDictRow)."""
        if isinstance(row, dict):
            return row.get(key, default)
        try:
            val = row[key]
            return val if val is not None else default
        except (KeyError, IndexError):
            return default

    # ------------------------------------------------------------------
    # Announcements
    # ------------------------------------------------------------------

    def insert_announcement(self, ann: AnalyzedAnnouncement, return_existing: bool = False) -> Optional[int]:
        """Insert an announcement.

        Args:
            ann: The announcement to insert.
            return_existing: If True, return existing row ID on duplicate (for re-processing).

        Returns:
            The new row ID if inserted, or ``None`` if it was a duplicate
            (unless *return_existing* is True, in which case returns existing row ID).
            Backward-compatible: ``bool(int)`` is ``True``, ``bool(None)`` is ``False``.
        """
        now = datetime.now().isoformat()
        try:
            sql = _sql("""
                INSERT INTO announcements
                    (source, source_id, title, summary, url, author, category,
                     target, period_start, period_end, relevance_score,
                     relevance_reason, matched_keywords, is_notified,
                     raw_data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """)
            params = (
                ann.source, ann.source_id, ann.title, ann.summary, ann.url,
                ann.author, ann.category, ann.target, ann.period_start,
                ann.period_end, ann.relevance_score, ann.relevance_reason,
                json.dumps(ann.matched_keywords, ensure_ascii=False),
                ann.raw_data, now, now,
            )
            if self._backend == "postgresql":
                cur = self._conn.cursor()
                cur.execute(sql + " RETURNING id", params)
                row = cur.fetchone()
                return row["id"]
            else:
                cur = self._conn.execute(sql, params)
                self._conn.commit()
                return cur.lastrowid
        except _IntegrityError:
            # Duplicate (source, source_id) -- update score/reason and return existing ID.
            self._conn.execute(
                _sql("""
                UPDATE announcements
                   SET relevance_score  = ?,
                       relevance_reason = ?,
                       matched_keywords = ?,
                       updated_at       = ?
                 WHERE source = ? AND source_id = ?
                """),
                (
                    ann.relevance_score,
                    ann.relevance_reason,
                    json.dumps(ann.matched_keywords, ensure_ascii=False),
                    now,
                    ann.source,
                    ann.source_id,
                ),
            )
            if self._backend == "sqlite":
                self._conn.commit()
            if return_existing:
                row = self._conn.execute(
                    _sql("SELECT id FROM announcements WHERE source = ? AND source_id = ?"),
                    (ann.source, ann.source_id),
                ).fetchone()
                if row:
                    return row["id"] if isinstance(row, dict) else row[0]
                return None
            return None

    def get_unnotified(self) -> List[AnalyzedAnnouncement]:
        """Return all announcements that have not been notified yet."""
        rows = self._conn.execute(
            """
            SELECT * FROM announcements
             WHERE is_notified = 0
               AND (period_end IS NULL OR period_end = '' OR period_end >= date('now'))
             ORDER BY relevance_score DESC, created_at DESC
            """
        ).fetchall()
        return [self._row_to_announcement(r) for r in rows]

    def mark_notified(self, source_id: str) -> None:
        """Mark an announcement as notified by its *source_id*."""
        self._conn.execute(
            _sql("UPDATE announcements SET is_notified = 1, updated_at = ? WHERE source_id = ?"),
            (datetime.now().isoformat(), source_id),
        )
        if self._backend == "sqlite":
            self._conn.commit()

    def is_duplicate(self, source: str, source_id: str) -> bool:
        """Check whether an announcement with *source* + *source_id* already exists."""
        row = self._conn.execute(
            _sql("SELECT 1 FROM announcements WHERE source = ? AND source_id = ?"),
            (source, source_id),
        ).fetchone()
        return row is not None

    def get_quoted_source_ids(self, source: str) -> set:
        """이미 상세 인용을 받은 공고의 source_id 집합.

        크롤러가 상세 요청 예산을 이미 채운 항목에 낭비하지 않도록 알려
        준다(Codex 재검토 #11). 이것이 없으면 목록이 요청 상한보다 길 때
        뒤쪽 항목이 매 실행 영구히 미수집으로 남는다.

        Args:
            source: 크롤러 소스 이름

        Returns:
            ``raw_data`` 에 인용 키가 들어 있는 공고의 source_id 집합
        """
        rows = self._conn.execute(
            _sql(
                "SELECT source_id FROM announcements"
                " WHERE source = ?"
                "   AND (raw_data LIKE '%\"quote_deadline\"%'"
                "     OR raw_data LIKE '%\"quote_eligibility\"%'"
                "     OR raw_data LIKE '%\"quote_amount\"%')"
            ),
            (source,),
        ).fetchall()
        return {str(row["source_id"]) for row in rows}

    QUOTE_FIELDS = (
        "quote_deadline", "quote_eligibility", "quote_amount",
        "quote_period_start", "quote_period_end",
        "always_open", "early_close", "detail_truncated",
        "quotes_attempted_at",
    )

    def merge_quote_fields(self, announcement: RawAnnouncement) -> bool:
        """이미 저장된 공고에 **새로 얻은 인용**을 합쳐 넣는다.

        상세 인용은 목록보다 늦게 도착한다(요청 상한 때문에 다음 실행에서
        오기도 한다). 중복 필터가 그 공고를 걸러 버리면 인용이 영구히
        버려진다. 그래서 인용 관련 필드만 upsert 한다.

        **빈 값으로는 지우지 않는다** (4차 게이트 #7): ``{"quote_deadline": ""}``
        같은 결과가 정상 인용을 덮어써 사라지게 했고, 자격 인용만 새로
        들어와도 기존 마감 인용·근거·플래그가 함께 지워졌다. 이제 값이 있는
        키만 덮어쓰고, 없는 키는 **손대지 않는다**.

        인용을 못 얻은 항목도 ``quotes_attempted_at`` 만 기록해 다음 실행이
        아직 안 본 항목을 먼저 보게 한다 (4차 게이트 #6).

        Args:
            announcement: 인용(또는 시도 기록)을 담은 새 수집 결과

        Returns:
            실제로 갱신했으면 True
        """
        try:
            incoming = json.loads(announcement.raw_data or "{}")
        except (ValueError, TypeError):
            return False
        if not isinstance(incoming, dict):
            return False

        fresh = {
            key: incoming[key]
            for key in self.QUOTE_FIELDS
            if key in incoming and incoming[key] not in ("", None, {}, [])
        }
        if not fresh:
            return False

        row = self._find_row("id, raw_data", announcement)
        if row is None:
            return False

        try:
            stored = json.loads(row["raw_data"] or "{}")
        except (ValueError, TypeError):
            stored = {}
        if not isinstance(stored, dict):
            stored = {}

        # 인용 **본문**이 새로 왔을 때만 그 계열의 파생값을 정리한다.
        # 시도 기록만 왔다면 기존 인용을 건드리지 않는다.
        has_new_quote = any(
            key in fresh
            for key in ("quote_deadline", "quote_eligibility", "quote_amount")
        )
        if has_new_quote and "quote_deadline" in fresh:
            for key in ("quote_period_start", "quote_period_end",
                        "always_open", "early_close"):
                stored.pop(key, None)
        stored.update(fresh)

        # 기간은 **여기서 쓰지 않는다** (13차). 인용에서 뽑은
        # ``quote_period_*`` 는 raw_data 증거로만 남고, 기간 두 필드는
        # 관문(``alert.main._finalize_periods``)이 정한 값을
        # ``overwrite_periods`` 가 쓴다. 예전에는 ``or row["period_end"]`` 가
        # 기존 값을 보존해, 재수집해도 가짜 마감이 영구히 남았다
        # (9차 게이트 MEDIUM).
        self._conn.execute(
            _sql(
                "UPDATE announcements SET raw_data = ?, updated_at = ?"
                " WHERE id = ?"
            ),
            (
                json.dumps(stored, ensure_ascii=False),
                datetime.now().isoformat(),
                row["id"],
            ),
        )
        if self._backend == "sqlite":
            self._conn.commit()
        return True

    def _find_row(self, columns: str, announcement: RawAnnouncement) -> Optional[Any]:
        """``(source, source_id)`` 로 찾고, 없으면 **같은 URL** 로 찾는다.

        source_id 체계가 바뀌어도(예: seis 가 ``42`` -> ``fnc:42``) 예전에
        저장된 행을 같은 공고로 인식해야 한다 - 그러지 않으면 같은 공고가
        두 행이 되고, 예전 행의 기간은 영원히 정리되지 않는다 (11차 게이트).

        Args:
            columns: 읽을 컬럼 목록 (내부 상수만 넘긴다)
            announcement: 조회 기준이 되는 수집 결과

        Returns:
            찾은 행 또는 None
        """
        url = (announcement.url or "").strip()
        row = self._conn.execute(
            _sql(
                f"SELECT {columns}, url FROM announcements"
                " WHERE source = ? AND source_id = ?"
            ),
            (announcement.source, announcement.source_id),
        ).fetchone()
        if row is not None:
            stored_url = (row["url"] or "").strip()
            # 같은 ID 인데 URL 이 다르면 **다른 공고**다 - ID 체계가 특정에
            # 실패한 경우이므로 남의 행을 덮어쓰지 않는다 (12차 게이트).
            if not url or not stored_url or stored_url == url:
                return row

        if not url:
            return None
        return self._conn.execute(
            _sql(
                f"SELECT {columns}, url FROM announcements"
                " WHERE source = ? AND url = ?"
            ),
            (announcement.source, url),
        ).fetchone()

    def exists(self, announcement: RawAnnouncement) -> bool:
        """이 공고가 이미 저장돼 있는가 (source_id 또는 같은 URL)."""
        return self._find_row("id", announcement) is not None

    def revalidate_periods(self, source: str, recompute) -> int:
        """저장된 행의 기간을 **raw_data 근거로 다시 산출**한다 (멱등).

        전용 추출기가 있는 소스라도, 목록에서 내려간 공고는 재수집되지
        않아 관문을 다시 지나지 않는다. 그래서 예전 규칙으로 심긴 기간이
        알림까지 살아남았다 (11차 게이트 MEDIUM). 근거가 없으면 NULL 이다.

        Args:
            source: 소스 이름
            recompute: ``raw_data`` 문자열 -> ``(start, end)`` 순수 함수

        Returns:
            실제로 값이 바뀐 행 수
        """
        rows = self._conn.execute(
            _sql(
                "SELECT id, raw_data, period_start, period_end FROM announcements"
                " WHERE source = ?"
            ),
            (source,),
        ).fetchall()

        changed = 0
        for row in rows:
            try:
                start, end = recompute(row["raw_data"])
            except Exception:                       # noqa: BLE001
                start, end = None, None             # 근거를 못 읽으면 기간 없음
            if (row["period_start"] or None) == (start or None) and (
                row["period_end"] or None
            ) == (end or None):
                continue
            self._conn.execute(
                _sql(
                    "UPDATE announcements SET period_start = ?, period_end = ?,"
                    " updated_at = ? WHERE id = ?"
                ),
                (start or None, end or None, datetime.now().isoformat(), row["id"]),
            )
            changed += 1

        if changed and self._backend == "sqlite":
            self._conn.commit()
        return changed

    def clear_periods_except(self, keep_sources: Sequence[str]) -> int:
        """**추출기가 있는 소스를 뺀 전부**의 기간을 NULL 로 만든다 (멱등).

        재수집되지 않은 행(목록에서 내려간 공고)은 관문을 다시 지나지
        않으므로, 예전 실행이 심은 마감이 그대로 남아 알림·브리핑까지
        갔다. 12차 게이트에서 정규화 범위가 허용목록과 달라 ``bizinfo``
        같은 소스의 오염이 살아남는 것이 확인됐다 - 그래서 **허용목록의
        여집합 전체**를 대상으로 한다. 이 소스들은 설계상 기간을 만들 수
        없으므로 남아 있는 값은 전부 날조값이다.

        이미 비어 있는 행은 건드리지 않으므로 두 번째 호출은 0을 돌려준다.

        Args:
            keep_sources: 전용 추출기가 있는 소스 이름들 (건드리지 않는다)

        Returns:
            실제로 비운 행 수
        """
        names = [name for name in keep_sources if name]
        keep_clause = ""
        if names:
            placeholders = ", ".join("?" for _ in names)
            keep_clause = f" AND source NOT IN ({placeholders})"

        cursor = self._conn.execute(
            _sql(
                "UPDATE announcements SET period_start = NULL,"
                " period_end = NULL, updated_at = ?"
                " WHERE (period_start IS NOT NULL OR period_end IS NOT NULL)"
                + keep_clause
            ),
            (datetime.now().isoformat(), *names),
        )
        affected = cursor.rowcount or 0
        if self._backend == "sqlite":
            self._conn.commit()
        return affected

    def overwrite_periods(
        self,
        announcement: RawAnnouncement,
        evidence_keys: Sequence[str] = (),
    ) -> bool:
        """기간과 **그 근거**를 재수집 값으로 덮어쓴다 (빈 값 포함).

        기간만 갱신하고 ``raw_data`` 의 추출 근거를 옛 값으로 두면, 나중에
        기존 행 재검증이 **철회된 기간을 되살린다** (12차 게이트 HIGH:
        10월 범위 → 재수집 NULL → 다음 실행에서 10월 범위 부활). 그래서
        근거 키는 새 수집 결과의 값으로 교체하고, 새 값이 없으면 지운다.

        기간 값 자체는 관문(``alert.main._finalize_periods``)이 이미
        정했다 - 이 메서드는 그 결정을 그대로 쓴다.

        Args:
            announcement: 관문을 지난 새 수집 결과
            evidence_keys: 추출기가 근거로 읽는 ``raw_data`` 키들

        Returns:
            실제로 값이 바뀌었으면 True (같으면 건드리지 않는다)
        """
        row = self._find_row("id, period_start, period_end, raw_data", announcement)
        if row is None:
            return False

        start = announcement.period_start or None
        end = announcement.period_end or None
        period_changed = (
            (row["period_start"] or None) != start
            or (row["period_end"] or None) != end
        )

        stored = self._load_json(row["raw_data"])
        incoming = self._load_json(announcement.raw_data)
        evidence_changed = False
        for key in evidence_keys:
            fresh = incoming.get(key)
            if fresh in (None, "", [], {}):
                if key in stored:
                    stored.pop(key)
                    evidence_changed = True
            elif stored.get(key) != fresh:
                stored[key] = fresh
                evidence_changed = True

        if not period_changed and not evidence_changed:
            return False               # 같으면 updated_at 도 건드리지 않는다

        self._conn.execute(
            _sql(
                "UPDATE announcements SET period_start = ?, period_end = ?,"
                " raw_data = ?, updated_at = ? WHERE id = ?"
            ),
            (
                start,
                end,
                json.dumps(stored, ensure_ascii=False),
                datetime.now().isoformat(),
                row["id"],
            ),
        )
        if self._backend == "sqlite":
            self._conn.commit()
        return True

    @staticmethod
    def _load_json(payload: Optional[str]) -> Dict[str, Any]:
        """``raw_data`` 를 딕셔너리로 읽는다 (못 읽으면 빈 딕셔너리)."""
        try:
            loaded = json.loads(payload or "{}")
        except (ValueError, TypeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def get_quote_attempts(self, source: str) -> Dict[str, str]:
        """source_id -> 마지막 상세 시도 시각.

        인용을 얻지 못한 항목도 시도 시각이 남으므로, 다음 실행이 **아직 안
        본 항목부터** 볼 수 있다. 이것이 없으면 목록이 요청 상한보다 길 때
        같은 앞쪽 20건만 영원히 다시 요청한다 (4차 게이트 #6).
        """
        rows = self._conn.execute(
            _sql(
                "SELECT source_id, raw_data FROM announcements"
                " WHERE source = ? AND raw_data LIKE '%quotes_attempted_at%'"
            ),
            (source,),
        ).fetchall()
        attempts: Dict[str, str] = {}
        for row in rows:
            try:
                payload = json.loads(row["raw_data"] or "{}")
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("quotes_attempted_at"):
                attempts[str(row["source_id"])] = str(payload["quotes_attempted_at"])
        return attempts

    def search_announcements(self, query: str, limit: int = 20) -> List[AnalyzedAnnouncement]:
        """Full-text search across title and summary.

        Uses ``LIKE`` for simplicity; upgrade to FTS5 / tsvector if needed.
        """
        pattern = f"%{query}%"
        rows = self._conn.execute(
            _sql("""
            SELECT * FROM announcements
             WHERE title LIKE ? OR summary LIKE ?
             ORDER BY relevance_score DESC, created_at DESC
             LIMIT ?
            """),
            (pattern, pattern, limit),
        ).fetchall()
        return [self._row_to_announcement(r) for r in rows]

    # ------------------------------------------------------------------
    # Keywords
    # ------------------------------------------------------------------

    def insert_keyword(self, kw: Keyword) -> bool:
        """Insert a keyword. Returns ``True`` on success, ``False`` on duplicate."""
        try:
            self._conn.execute(
                _sql("INSERT INTO keywords (keyword, category, weight, is_active) VALUES (?, ?, ?, ?)"),
                (kw.keyword, kw.category, kw.weight, int(kw.is_active)),
            )
            if self._backend == "sqlite":
                self._conn.commit()
            return True
        except _IntegrityError:
            return False

    def get_keywords(self) -> List[Keyword]:
        """Return all keywords."""
        rows = self._conn.execute("SELECT * FROM keywords ORDER BY category, keyword").fetchall()
        return [
            Keyword(
                keyword=r["keyword"],
                category=r["category"],
                weight=r["weight"],
                is_active=bool(r["is_active"]),
            )
            for r in rows
        ]

    def remove_keyword(self, keyword: str) -> bool:
        """Delete a keyword by value. Returns ``True`` if it existed."""
        cur = self._conn.execute(_sql("DELETE FROM keywords WHERE keyword = ?"), (keyword,))
        if self._backend == "sqlite":
            self._conn.commit()
        return cur.rowcount > 0

    def init_default_keywords(self, keywords_cfg: Optional[Any] = None) -> int:
        """Populate keywords table from config if empty.

        Args:
            keywords_cfg: A :class:`KeywordsConfig` (or compatible object with
                          ``must_match``, ``boost``, ``exclude`` list attrs).
                          When *None*, loads from :func:`get_config`.

        Returns:
            Number of keywords inserted.
        """
        # Only seed when the table is empty.
        count_row = self._conn.execute("SELECT COUNT(*) AS cnt FROM keywords").fetchone()
        if count_row["cnt"] > 0:
            return 0

        if keywords_cfg is None:
            from .config import get_config
            keywords_cfg = get_config().keywords

        inserted = 0
        category_map = {
            "must_match": (keywords_cfg.must_match, 2.0),
            "boost": (keywords_cfg.boost, 1.0),
            "exclude": (keywords_cfg.exclude, 1.0),
        }
        for category, (words, default_weight) in category_map.items():
            for word in words:
                kw = Keyword(keyword=word, category=category, weight=default_weight)
                if self.insert_keyword(kw):
                    inserted += 1
        return inserted

    def sync_keywords_from_config(self, keywords_cfg: Optional[Any] = None) -> int:
        """Sync keywords from config to database (additive only).

        This method adds new keywords from config that don't exist in the database.
        It never removes or modifies existing keywords.

        Args:
            keywords_cfg: A :class:`KeywordsConfig` (or compatible object with
                          ``must_match``, ``boost``, ``exclude`` list attrs).
                          When *None*, loads from :func:`get_config`.

        Returns:
            Number of new keywords inserted.
        """
        if keywords_cfg is None:
            from .config import get_config
            keywords_cfg = get_config().keywords

        inserted = 0
        category_weight_map = {
            "must_match": (keywords_cfg.must_match, 0.5),
            "boost": (keywords_cfg.boost, 0.05),
            "exclude": (keywords_cfg.exclude, 0.0),
        }

        for category, (words, weight) in category_weight_map.items():
            for word in words:
                kw = Keyword(keyword=word, category=category, weight=weight, is_active=True)
                if self.insert_keyword(kw):
                    inserted += 1

        return inserted

    # ------------------------------------------------------------------
    # Run history
    # ------------------------------------------------------------------

    def insert_run(
        self,
        source: str,
        total: int,
        new: int,
        relevant: int,
        notified: int,
        status: str,
        error_msg: str = "",
    ) -> int:
        """Record a crawler run and return the new row id."""
        now = datetime.now().isoformat()
        sql = _sql("""
            INSERT INTO run_history
                (started_at, finished_at, source, total_fetched, new_count,
                 relevant_count, notified_count, status, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """)
        params = (now, now, source, total, new, relevant, notified, status, error_msg)
        return self._insert_returning_id(sql, params)

    def get_recent_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return the most recent run history entries."""
        rows = self._conn.execute(
            _sql("SELECT * FROM run_history ORDER BY id DESC LIMIT ?"), (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Aggregate statistics for a quick dashboard view.

        Returns:
            Dict with keys ``total``, ``by_source``, ``by_month``,
            ``notified``, ``unnotified``.
        """
        total = self._conn.execute("SELECT COUNT(*) AS cnt FROM announcements").fetchone()["cnt"]
        notified = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM announcements WHERE is_notified = 1"
        ).fetchone()["cnt"]

        by_source_rows = self._conn.execute(
            "SELECT source, COUNT(*) AS cnt FROM announcements GROUP BY source ORDER BY cnt DESC"
        ).fetchall()
        by_source = {r["source"]: r["cnt"] for r in by_source_rows}

        if self._backend == "postgresql":
            by_month_rows = self._conn.execute(
                """
                SELECT TO_CHAR(created_at::timestamp, 'YYYY-MM') AS month, COUNT(*) AS cnt
                  FROM announcements
                 GROUP BY month
                 ORDER BY month DESC
                 LIMIT 12
                """
            ).fetchall()
        else:
            by_month_rows = self._conn.execute(
                """
                SELECT strftime('%Y-%m', created_at) AS month, COUNT(*) AS cnt
                  FROM announcements
                 GROUP BY month
                 ORDER BY month DESC
                 LIMIT 12
                """
            ).fetchall()
        by_month = {r["month"]: r["cnt"] for r in by_month_rows}

        return {
            "total": total,
            "notified": notified,
            "unnotified": total - notified,
            "by_source": by_source,
            "by_month": by_month,
        }

    # ------------------------------------------------------------------
    # Knowledge Layer: Domain Classification
    # ------------------------------------------------------------------

    def update_announcement_domain(self, ann_id: int, domain: str, confidence: float) -> None:
        """Update the business domain classification for an announcement."""
        self._conn.execute(
            _sql("UPDATE announcements SET business_domain = ?, domain_confidence = ?, updated_at = ? WHERE id = ?"),
            (domain, confidence, datetime.now().isoformat(), ann_id),
        )
        if self._backend == "sqlite":
            self._conn.commit()

    def update_announcement_obsidian_path(self, ann_id: int, path: str) -> None:
        """Update the Obsidian vault path for an announcement."""
        self._conn.execute(
            _sql("UPDATE announcements SET obsidian_path = ?, updated_at = ? WHERE id = ?"),
            (path, datetime.now().isoformat(), ann_id),
        )
        if self._backend == "sqlite":
            self._conn.commit()

    def get_announcement_by_id(self, ann_id: int) -> Optional[AnalyzedAnnouncement]:
        """Fetch a single announcement by its row ID."""
        row = self._conn.execute(
            _sql("SELECT * FROM announcements WHERE id = ?"), (ann_id,)
        ).fetchone()
        return self._row_to_announcement(row) if row else None

    def get_announcement_domain(self, ann_id: int) -> tuple:
        """Get business_domain and domain_confidence for an announcement.

        Returns:
            Tuple of (domain_str, confidence_float). Empty string and 0.0 if not found.
        """
        try:
            if self._backend == "postgresql":
                cur = self.conn.cursor()
                cur.execute(
                    "SELECT business_domain, domain_confidence FROM announcements WHERE id = %s",
                    (ann_id,),
                )
                row = cur.fetchone()
            else:
                row = self._conn.execute(
                    "SELECT business_domain, domain_confidence FROM announcements WHERE id = ?",
                    (ann_id,),
                ).fetchone()
            if row:
                return (row["business_domain"] or "", row["domain_confidence"] or 0.0)
        except Exception:
            pass
        return ("", 0.0)

    # ------------------------------------------------------------------
    # Knowledge Layer: Embeddings
    # ------------------------------------------------------------------

    def insert_embedding(self, announcement_id: int, vector_bytes: bytes, model: str = "text-embedding-3-small", dims: int = 256) -> int:
        """Store an embedding vector. Returns the embedding row ID."""
        now = datetime.now().isoformat()
        sql = _sql("""
            INSERT INTO embeddings (announcement_id, model_name, embedding, dimensions, created_at)
            VALUES (?, ?, ?, ?, ?)
            """)
        params = (announcement_id, model, vector_bytes, dims, now)
        emb_id = self._insert_returning_id(sql, params)
        # Update announcement's embedding_id reference
        self._conn.execute(
            _sql("UPDATE announcements SET embedding_id = ?, updated_at = ? WHERE id = ?"),
            (emb_id, now, announcement_id),
        )
        if self._backend == "sqlite":
            self._conn.commit()
        return emb_id

    # ------------------------------------------------------------------
    # Knowledge Layer: Application History
    # ------------------------------------------------------------------

    def insert_application_record(self, record: ApplicationRecord) -> int:
        """Insert an application history record. Returns the row ID."""
        now = datetime.now().isoformat()
        sql = _sql("""
            INSERT INTO application_history
                (announcement_id, status, applied_date, result_date, result,
                 prepared_docs, notes, assigned_domain, priority, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """)
        params = (
            record.announcement_id, record.status, record.applied_date,
            record.result_date, record.result,
            json.dumps(record.prepared_docs, ensure_ascii=False),
            record.notes, record.assigned_domain, record.priority,
            now, now,
        )
        return self._insert_returning_id(sql, params)

    _ALLOWED_APP_FIELDS = frozenset({
        "applied_date", "result_date", "result", "notes", "priority",
    })

    def update_application_status(self, record_id: int, status: str, **kwargs: Any) -> bool:
        """Update an application record's status and optional fields.

        Returns True if record was found and updated, False otherwise.
        Raises ValueError for disallowed field names.
        """
        now = datetime.now().isoformat()
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now]

        for key, value in kwargs.items():
            if key == "prepared_docs":
                updates.append("prepared_docs = ?")
                params.append(json.dumps(value, ensure_ascii=False))
            elif key in self._ALLOWED_APP_FIELDS:
                updates.append(f"{key} = ?")
                params.append(value)
            else:
                raise ValueError(f"Disallowed field in update_application_status: {key}")

        params.append(record_id)
        cur = self._conn.execute(
            _sql(f"UPDATE application_history SET {', '.join(updates)} WHERE id = ?"),
            params,
        )
        if self._backend == "sqlite":
            self._conn.commit()
        return cur.rowcount > 0

    def get_application_history(self, announcement_id: int) -> List[ApplicationRecord]:
        """Get all application records for an announcement."""
        rows = self._conn.execute(
            _sql("SELECT * FROM application_history WHERE announcement_id = ? ORDER BY created_at DESC"),
            (announcement_id,),
        ).fetchall()
        return [self._row_to_application_record(r) for r in rows]

    def get_applications_by_status(self, status: str, limit: int = 20) -> List[ApplicationRecord]:
        """Get application records filtered by status."""
        rows = self._conn.execute(
            _sql("SELECT * FROM application_history WHERE status = ? ORDER BY updated_at DESC LIMIT ?"),
            (status, limit),
        ).fetchall()
        return [self._row_to_application_record(r) for r in rows]

    def get_applications_by_domain(self, domain: str, limit: int = 20) -> List[ApplicationRecord]:
        """Get application records filtered by assigned domain."""
        rows = self._conn.execute(
            _sql("SELECT * FROM application_history WHERE assigned_domain = ? ORDER BY updated_at DESC LIMIT ?"),
            (domain, limit),
        ).fetchall()
        return [self._row_to_application_record(r) for r in rows]

    def get_recent_applications(self, limit: int = 10) -> List[ApplicationRecord]:
        """Get the most recent application records."""
        rows = self._conn.execute(
            _sql("SELECT * FROM application_history ORDER BY updated_at DESC LIMIT ?"),
            (limit,),
        ).fetchall()
        return [self._row_to_application_record(r) for r in rows]

    # ------------------------------------------------------------------
    # Knowledge Layer: Research Documents
    # ------------------------------------------------------------------

    def insert_research_document(self, doc: ResearchDocument) -> int:
        """Insert a research document. Returns the row ID."""
        now = datetime.now().isoformat()
        sql = _sql("""
            INSERT INTO research_documents
                (title, doc_type, period_start, period_end, content,
                 metadata, obsidian_path, version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """)
        params = (
            doc.title, doc.doc_type, doc.period_start, doc.period_end,
            doc.content, json.dumps(doc.metadata, ensure_ascii=False),
            doc.obsidian_path, doc.version, now, now,
        )
        return self._insert_returning_id(sql, params)

    def update_research_document(self, doc_id: int, content: str, version: int, obsidian_path: str = "") -> None:
        """Update a research document's content, version, and optionally obsidian_path."""
        now = datetime.now().isoformat()
        if obsidian_path:
            self._conn.execute(
                _sql("UPDATE research_documents SET content = ?, version = ?, obsidian_path = ?, updated_at = ? WHERE id = ?"),
                (content, version, obsidian_path, now, doc_id),
            )
            if self._backend == "sqlite":
                self._conn.commit()
            return
        self._conn.execute(
            _sql("UPDATE research_documents SET content = ?, version = ?, updated_at = ? WHERE id = ?"),
            (content, version, now, doc_id),
        )
        if self._backend == "sqlite":
            self._conn.commit()

    # ------------------------------------------------------------------
    # Knowledge Layer: Queries
    # ------------------------------------------------------------------

    def get_announcements_by_period(self, start: str, end: str) -> List[AnalyzedAnnouncement]:
        """Get announcements created within a date range."""
        rows = self._conn.execute(
            _sql("""
            SELECT * FROM announcements
             WHERE created_at >= ? AND created_at <= ?
             ORDER BY relevance_score DESC, created_at DESC
            """),
            (start, end),
        ).fetchall()
        return [self._row_to_announcement(r) for r in rows]

    def get_domain_stats(self, start: str, end: str) -> Dict[str, int]:
        """Get announcement counts by business domain for a period."""
        rows = self._conn.execute(
            _sql("""
            SELECT business_domain, COUNT(*) AS cnt
              FROM announcements
             WHERE created_at >= ? AND created_at <= ?
               AND business_domain != ''
             GROUP BY business_domain
             ORDER BY cnt DESC
            """),
            (start, end),
        ).fetchall()
        return {r["business_domain"]: r["cnt"] for r in rows}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_announcement(row: Any) -> AnalyzedAnnouncement:
        """Convert a database row into an :class:`AnalyzedAnnouncement`."""
        # Works with both sqlite3.Row and dict (RealDictRow)
        matched = row["matched_keywords"]
        try:
            matched_list = json.loads(matched) if matched else []
        except (json.JSONDecodeError, TypeError):
            matched_list = []

        return AnalyzedAnnouncement(
            id=row["id"],
            source=row["source"],
            source_id=row["source_id"],
            title=row["title"],
            url=row["url"],
            summary=row["summary"] or "",
            author=row["author"] or "",
            category=row["category"] or "",
            target=row["target"] or "",
            period_start=row["period_start"],
            period_end=row["period_end"],
            raw_data=row["raw_data"] or "",
            fetched_at=row["created_at"],
            relevance_score=row["relevance_score"] or 0.0,
            relevance_reason=row["relevance_reason"] or "",
            matched_keywords=matched_list,
        )

    @staticmethod
    def _row_to_application_record(row: Any) -> ApplicationRecord:
        """Convert a database row into an ApplicationRecord."""
        docs = row["prepared_docs"]
        try:
            docs_list = json.loads(docs) if docs else []
        except (json.JSONDecodeError, TypeError):
            docs_list = []

        return ApplicationRecord(
            id=row["id"],
            announcement_id=row["announcement_id"],
            status=row["status"],
            applied_date=row["applied_date"] or "",
            result_date=row["result_date"] or "",
            result=row["result"] or "",
            prepared_docs=docs_list,
            notes=row["notes"] or "",
            assigned_domain=row["assigned_domain"] or "",
            priority=row["priority"],
        )

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
