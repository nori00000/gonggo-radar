"""주간 정책브리핑 다이제스트 생성 및 발송 테스트."""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock
from datetime import datetime, timedelta

import pytest
import requests.exceptions

from alert.digest.composer import (
    compose_digest,
    get_week_date_range,
    categorize_item,
    normalize_title,
)
from alert.digest.checker import (
    check_digest,
    parse_period_end,
    check_url_alive,
)
from scripts.send_digest import check_fail_closed, markdown_to_html, send_digest


@pytest.fixture
def temp_db():
    """테스트용 임시 SQLite 데이터베이스."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    # 스키마 생성
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE announcements (
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
            business_domain TEXT DEFAULT '',
            domain_confidence REAL DEFAULT 0.0,
            obsidian_path   TEXT DEFAULT '',
            embedding_id    INTEGER DEFAULT NULL,
            UNIQUE(source, source_id)
        )
        """
    )
    conn.commit()
    conn.close()

    yield db_path

    # 정리
    Path(db_path).unlink()


@pytest.fixture
def sample_announcements(temp_db):
    """샘플 공고 데이터."""
    conn = sqlite3.connect(temp_db)
    cursor = conn.cursor()

    now = datetime.now()
    three_days_ago = (now - timedelta(days=3)).isoformat()

    announcements = [
        {
            "source": "fowi",
            "source_id": "fowi_001",
            "title": "산림 정책 동향 발표",
            "summary": "최근 산림청 정책 방향을 안내합니다",
            "url": "https://example.com/fowi/001",
            "author": "산림청",
            "period_end": "2026-12-31",
            "relevance_score": 0.9,
            "created_at": three_days_ago,
            "updated_at": three_days_ago,
        },
        {
            "source": "fowi",
            "source_id": "fowi_002",
            "title": "산림 보조금 공고",
            "summary": "숲 복원 사업 보조금 모집합니다",
            "url": "https://example.com/fowi/002",
            "author": "산림청",
            "period_end": "2026-06-30",
            "relevance_score": 0.85,
            "created_at": three_days_ago,
            "updated_at": three_days_ago,
        },
        {
            "source": "seis",
            "source_id": "seis_001",
            "title": "사회적기업 모집 공고",
            "summary": "경기도 사회적기업 지원사업 참여 기업 모집",
            "url": "https://example.com/seis/001",
            "author": "경기도청",
            "period_end": "2026-05-15",
            "relevance_score": 0.8,
            "created_at": three_days_ago,
            "updated_at": three_days_ago,
        },
        {
            "source": "nongsaro",
            "source_id": "nongsaro_001",
            "title": "농업 지원금 신청",
            "summary": "농가 소득 향상을 위한 지원금 안내",
            "url": "https://example.com/nongsaro/001",
            "author": "농림축산식품부",
            "period_end": "2026-08-20",
            "relevance_score": 0.75,
            "created_at": three_days_ago,
            "updated_at": three_days_ago,
        },
        {
            "source": "socialenterprise",
            "source_id": "se_001",
            "title": "사회연대경제 네트워킹",
            "summary": "사회연대경제 기업 간 협력 프로그램",
            "url": "https://example.com/se/001",
            "author": "사회적경제원",
            "period_end": "2026-07-01",
            "relevance_score": 0.7,
            "created_at": three_days_ago,
            "updated_at": three_days_ago,
        },
    ]

    for ann in announcements:
        cursor.execute(
            """
            INSERT INTO announcements (
                source, source_id, title, summary, url, author, period_end,
                relevance_score, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ann["source"],
                ann["source_id"],
                ann["title"],
                ann["summary"],
                ann["url"],
                ann["author"],
                ann["period_end"],
                ann["relevance_score"],
                ann["created_at"],
                ann["updated_at"],
            ),
        )

    conn.commit()
    conn.close()

    return temp_db


class TestComposer:
    """Composer 테스트."""

    def test_get_week_date_range(self):
        """주 날짜 범위 계산."""
        start, end = get_week_date_range("2026-W13")
        assert start == "2026-03-23"
        assert end == "2026-03-29"

    def test_normalize_title(self):
        """제목 정규화 (개행·탭 제거)."""
        title = "산림치유지도사\n\t\t자격증발급현황"
        normalized = normalize_title(title)
        assert normalized == "산림치유지도사 자격증발급현황"

    def test_categorize_forest_policy(self):
        """산림 정책 분류."""
        assert categorize_item("fowi", "산림 정책 발표") == "산림"

    def test_categorize_forest_subsidy(self):
        """산림 보조금 공고 분류."""
        assert categorize_item("fowi", "산림 보조금 공고") == "지원사업"

    def test_categorize_sse(self):
        """사회연대경제 분류."""
        assert categorize_item("seis", "사회적기업 지원") == "사회연대경제"

    def test_compose_digest_basic(self, sample_announcements):
        """기본 다이제스트 생성."""
        markdown = compose_digest(
            db_path=sample_announcements,
            week_str="2026-W13",
            limit=5,
        )

        # 섹션 존재 확인
        assert "## 산림 정책 동향" in markdown
        assert "## 지원사업 공고" in markdown
        assert "## 사회연대경제 동향" in markdown
        assert "## 회원사 동정" in markdown
        assert "## 협의회 의견" in markdown

        # 마커 확인
        assert "<!-- 상민 확정 필요 -->" in markdown

    def test_compose_digest_lane_comment(self, sample_announcements):
        """레인 표기가 항상 있어야 함."""
        markdown = compose_digest(
            db_path=sample_announcements,
            week_str="2026-W13",
        )
        assert "<!-- lane: Codex(gpt-5.6) -->" in markdown

    def test_compose_digest_per_section_limits(self, sample_announcements, tmp_path):
        """섹션별 항목 개수 제한."""
        compose_digest(
            db_path=sample_announcements,
            week_str="2026-W13",
            limit=5,
            output_path=tmp_path / "digest.md",
        )

        # 파일로 저장되었는지 확인
        assert (tmp_path / "digest.md").exists()

    def test_compose_digest_deadline_missing(self, tmp_path):
        """마감일이 없으면 명시."""
        # DB 생성: period_end 없는 항목
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE announcements (
                id INTEGER PRIMARY KEY, source TEXT, source_id TEXT,
                title TEXT, summary TEXT, url TEXT, author TEXT,
                period_end TEXT, relevance_score REAL, created_at TEXT,
                updated_at TEXT, category TEXT DEFAULT '', target TEXT DEFAULT '',
                period_start TEXT, relevance_reason TEXT DEFAULT '',
                matched_keywords TEXT DEFAULT '[]', is_notified INTEGER DEFAULT 0,
                raw_data TEXT DEFAULT '', business_domain TEXT DEFAULT '',
                domain_confidence REAL DEFAULT 0.0, obsidian_path TEXT DEFAULT '',
                embedding_id INTEGER DEFAULT NULL,
                UNIQUE(source, source_id)
            )
            """
        )

        # 2026-W13 범위 내 (03-23 ~ 03-29)
        created_at = "2026-03-26T12:00:00"
        cursor.execute(
            """
            INSERT INTO announcements
            (source, source_id, title, url, author, relevance_score, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("test", "id_1", "테스트 공고", "https://example.com/1", "기관", 0.9, created_at, created_at),
        )

        conn.commit()
        conn.close()

        markdown = compose_digest(
            db_path=str(db_path),
            week_str="2026-W13",
        )

        # "미정" 텍스트 확인
        assert "**마감:** 미정" in markdown

    def test_compose_digest_opinions_separate_file(self, tmp_path):
        """의견 파일 별도 저장."""
        # CSV 생성 with 의견 행
        csv_path = tmp_path / "responses.csv"
        csv_path.write_text(
            "접수일,회원사,유형,내용,관련정책\n"
            "2026-01-01,테스트,의견,의견1,정책1\n"
        )

        # DB 준비
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE announcements (
                id INTEGER PRIMARY KEY, source TEXT, source_id TEXT,
                title TEXT, summary TEXT DEFAULT '', url TEXT, author TEXT,
                category TEXT DEFAULT '', target TEXT DEFAULT '',
                period_start TEXT, period_end TEXT,
                relevance_score REAL DEFAULT 0.0,
                relevance_reason TEXT DEFAULT '',
                matched_keywords TEXT DEFAULT '[]',
                is_notified INTEGER DEFAULT 0,
                raw_data TEXT DEFAULT '', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, business_domain TEXT DEFAULT '',
                domain_confidence REAL DEFAULT 0.0,
                obsidian_path TEXT DEFAULT '', embedding_id INTEGER DEFAULT NULL,
                UNIQUE(source, source_id)
            )
            """
        )
        cursor.execute(
            "INSERT INTO announcements (source, source_id, title, url, created_at, updated_at) "
            "VALUES ('test', 'test_001', 'Test', 'https://example.com/test', '2026-03-26', '2026-03-26')"
        )
        conn.commit()
        conn.close()

        digest_path = tmp_path / "digest.md"
        compose_digest(
            db_path=str(db_path),
            week_str="2026-W13",
            output_path=digest_path,
            forms_csv_path=csv_path,
        )

        # 의견 파일 확인
        opinions_path = tmp_path / "digest.opinions.md"
        assert opinions_path.exists(), "의견 파일이 생성되어야 함"


class TestChecker:
    """Checker 테스트."""

    def test_parse_period_end_iso_date(self):
        """ISO 날짜 파싱."""
        assert parse_period_end("2026-12-31") is True

    def test_parse_period_end_ko_format(self):
        """한국식 날짜 파싱."""
        assert parse_period_end("2026년 12월 31일") is True

    def test_parse_period_end_invalid(self):
        """유효하지 않은 날짜."""
        assert parse_period_end("") is False
        assert parse_period_end(None) is False

    @mock.patch("alert.digest.checker.requests.get")
    @mock.patch("alert.digest.checker.requests.head")
    def test_check_url_alive_head(self, mock_head, mock_get):
        """URL HEAD 요청."""
        mock_head.return_value.status_code = 200
        assert check_url_alive("https://example.com") is True

    @mock.patch("alert.digest.checker.requests.get")
    @mock.patch("alert.digest.checker.requests.head")
    def test_check_url_alive_get_fallback(self, mock_head, mock_get):
        """URL GET 폴백."""
        mock_head.side_effect = requests.exceptions.RequestException("HEAD failed")
        mock_get.return_value.status_code = 200
        assert check_url_alive("https://example.com") is True

    def test_check_digest_empty_items_rejected(self, tmp_path):
        """항목 0건은 거부 (fail-closed)."""
        # 빈 마크다운 생성
        md_path = tmp_path / "empty.md"
        md_path.write_text("# 빈 다이제스트\n\n항목 없음")

        result = check_digest(
            db_path=":memory:",
            markdown_path=md_path,
            skip_network=True,
        )

        assert result["pass"] is False
        assert result["reason"] == "항목 없음"

    def test_check_digest_missing_file(self, tmp_path):
        """파일 없음."""
        result = check_digest(
            db_path=":memory:",
            markdown_path=tmp_path / "nonexistent.md",
            skip_network=True,
        )
        assert result["pass"] is False

    def test_check_digest_network_checked_field(self, sample_announcements, tmp_path):
        """network_checked 필드 포함."""
        md_path = tmp_path / "test.md"
        md_path.write_text("# 테스트\n\n[test](https://example.com)")

        result = check_digest(
            db_path=sample_announcements,
            markdown_path=md_path,
            skip_network=True,
        )

        assert "network_checked" in result
        assert result["network_checked"] is False  # skip_network=True이므로

    def test_check_json_has_reason_field(self, sample_announcements, tmp_path):
        """check.json에 reason 필드."""
        md_path = tmp_path / "test.md"
        check_path = tmp_path / "test.check.json"
        md_path.write_text("# 테스트\n\n[test](https://example.com)")

        check_digest(
            db_path=sample_announcements,
            markdown_path=md_path,
            output_path=check_path,
            skip_network=True,
        )

        with open(check_path) as f:
            saved = json.load(f)

        assert "reason" in saved


class TestSendDigest:
    """SendDigest 테스트."""

    def test_markdown_to_html_escaping(self):
        """HTML 이스케이프."""
        md = "# <script>alert(1)</script>"
        html = markdown_to_html(md)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_markdown_to_html_https_only(self):
        """http/https만 허용."""
        md = "[test](https://example.com)"
        html = markdown_to_html(md)
        assert "https://example.com" in html

        md_js = "[test](javascript:alert(1))"
        html_js = markdown_to_html(md_js)
        # javascript: URL은 제거되고 텍스트만 남음
        assert "javascript:" not in html_js

    def test_fail_closed_marker(self, tmp_path):
        """마커 있으면 실패."""
        md_path = tmp_path / "digest.md"
        md_path.write_text("# 테스트\n\n<!-- 상민 확정 필요 -->")

        passed, msg = check_fail_closed(md_path, tmp_path / "nocheck.json")
        assert passed is False
        assert "미확정" in msg

    def test_fail_closed_check_missing(self, tmp_path):
        """check.json 부재는 실패 (fail-closed)."""
        md_path = tmp_path / "digest.md"
        md_path.write_text("# 테스트\n\n완료")

        check_path = tmp_path / "nonexistent.check.json"
        passed, msg = check_fail_closed(md_path, check_path)
        assert passed is False
        assert "검증" in msg or "파일" in msg

    def test_fail_closed_check_pass_false(self, tmp_path):
        """check.json pass=false는 실패."""
        md_path = tmp_path / "digest.md"
        md_path.write_text("# 테스트\n\n완료")

        check_path = tmp_path / "test.check.json"
        check_path.write_text(json.dumps({
            "items": [],
            "pass": False,
            "network_checked": True,
            "reason": "테스트"
        }))

        passed, msg = check_fail_closed(md_path, check_path)
        assert passed is False

    def test_dry_run_default(self, tmp_path):
        """기본값은 dry_run (발송 안 함)."""
        md_path = tmp_path / "digest.md"
        md_path.write_text("# 테스트\n\n완료")

        # check.json 파일은 markdown 파일 이름으로부터 자동 파생됨
        check_path = md_path.with_suffix(".check.json")
        check_path.write_text(json.dumps({
            "items": [{"url": "https://example.com", "url_alive": True, "deadline_parsed": True, "passed": True}],
            "pass": True,
            "network_checked": True,
            "reason": ""
        }))

        # dry_run=True가 기본값이므로 발송 안 함
        result = send_digest(md_path, to_email="test@example.com", dry_run=True)

        # dry_run이므로 0 (성공)
        assert result == 0


class TestIntegration:
    """통합 테스트."""

    def test_full_workflow(self, sample_announcements, tmp_path):
        """전체 워크플로우."""
        md_path = tmp_path / "digest.md"
        check_path = tmp_path / "digest.check.json"

        # 1. 다이제스트 생성
        markdown = compose_digest(
            db_path=sample_announcements,
            week_str="2026-W13",
            output_path=md_path,
        )

        assert "<!-- 상민 확정 필요 -->" in markdown

        # 2. 검증
        check_digest(
            db_path=sample_announcements,
            markdown_path=md_path,
            output_path=check_path,
            skip_network=True,
        )

        # 마커 때문에 게이트 실패 예상
        passed, msg = check_fail_closed(md_path, check_path)
        assert passed is False

    def test_duplicates_removed(self, tmp_path):
        """중복 제거 (정규화된 제목 기준)."""
        # DB 생성: 정규화하면 같은 제목
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE announcements (
                id INTEGER PRIMARY KEY, source TEXT, source_id TEXT,
                title TEXT, summary TEXT, url TEXT, author TEXT,
                period_end TEXT, relevance_score REAL, created_at TEXT,
                updated_at TEXT, category TEXT DEFAULT '', target TEXT DEFAULT '',
                period_start TEXT, relevance_reason TEXT DEFAULT '',
                matched_keywords TEXT DEFAULT '[]', is_notified INTEGER DEFAULT 0,
                raw_data TEXT DEFAULT '', business_domain TEXT DEFAULT '',
                domain_confidence REAL DEFAULT 0.0, obsidian_path TEXT DEFAULT '',
                embedding_id INTEGER DEFAULT NULL,
                UNIQUE(source, source_id)
            )
            """
        )

        # 정규화하면 같은 제목 3개 (다른 URL) - 2026-W13 범위 내 (03-23 ~ 03-29)
        created_at = "2026-03-26T12:00:00"  # W13 내
        for i in range(3):
            cursor.execute(
                """
                INSERT INTO announcements
                (source, source_id, title, url, author, period_end, relevance_score, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "test",
                    f"id_{i}",
                    "공고  \n\t  안내",  # 정규화하면 "공고 안내"
                    f"https://example.com/{i}",
                    "기관",
                    "2026-12-31",
                    0.9 - i * 0.01,
                    created_at,
                    created_at,
                ),
            )

        conn.commit()
        conn.close()

        md = compose_digest(
            db_path=str(db_path),
            week_str="2026-W13",
        )

        # "공고 안내"는 1번만 나타나야 함
        count = md.count("### 공고 안내")
        assert count == 1, f"중복 제거 실패: {count}번 나타남"
