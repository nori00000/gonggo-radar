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
    load_form_responses,
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
        {
            "source": "fowi",
            "source_id": "fowi_003",
            "title": "산림 정보 공개",
            "summary": "산림 관리 현황 공개",
            "url": "https://example.com/fowi/003",
            "author": "산림청",
            "period_end": None,
            "relevance_score": 0.6,
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

    def test_categorize_forest_policy(self):
        """산림 정책 분류."""
        assert categorize_item("fowi", "산림 정책 발표") == "산림"
        assert categorize_item("forest_service", "숲 보호") == "산림"
        assert categorize_item("kofpi", "산림 교육") == "산림"

    def test_categorize_forest_subsidy(self):
        """산림 보조금 공고 분류."""
        assert categorize_item("fowi", "산림 보조금 공고") == "지원사업"
        assert categorize_item("fowi", "산림 보조금 모집") == "지원사업"

    def test_categorize_sse(self):
        """사회연대경제 분류."""
        assert categorize_item("seis", "사회적기업 지원") == "사회연대경제"
        assert categorize_item("socialenterprise", "협동조합") == "사회연대경제"

    def test_categorize_default(self):
        """기본 분류 (지원사업)."""
        assert categorize_item("nongsaro", "지원금") == "지원사업"

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

    def test_compose_digest_limit(self, sample_announcements):
        """항목 개수 제한."""
        markdown = compose_digest(
            db_path=sample_announcements,
            week_str="2026-W13",
            limit=3,
        )

        # 최대 3개 항목
        url_count = markdown.count("**원문:**")
        assert url_count <= 3

    def test_compose_digest_file_output(self, sample_announcements, tmp_path):
        """파일 출력."""
        output_path = tmp_path / "test_digest.md"
        markdown = compose_digest(
            db_path=sample_announcements,
            week_str="2026-W13",
            output_path=output_path,
        )

        assert output_path.exists()
        with open(output_path) as f:
            content = f.read()
        assert content == markdown


class TestChecker:
    """Checker 테스트."""

    def test_parse_period_end_iso_date(self):
        """ISO 날짜 파싱."""
        assert parse_period_end("2026-12-31") is True
        assert parse_period_end("2026-01-01") is True

    def test_parse_period_end_ko_format(self):
        """한국식 날짜 파싱."""
        assert parse_period_end("2026년 12월 31일") is True
        assert parse_period_end("2026년 1월 1일") is True

    def test_parse_period_end_invalid(self):
        """유효하지 않은 날짜."""
        assert parse_period_end("") is False
        assert parse_period_end(None) is False
        assert parse_period_end("invalid") is False

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

    @mock.patch("alert.digest.checker.requests.get")
    @mock.patch("alert.digest.checker.requests.head")
    def test_check_url_dead(self, mock_head, mock_get):
        """URL 실패."""
        mock_head.side_effect = requests.exceptions.RequestException("Connection failed")
        mock_get.side_effect = requests.exceptions.RequestException("Connection failed")
        assert check_url_alive("https://dead.example.com") is False

    def test_check_digest_no_file(self, tmp_path):
        """파일 없음."""
        result = check_digest(
            db_path=":memory:",
            markdown_path=tmp_path / "nonexistent.md",
            skip_network=True,
        )
        assert result["pass"] is False

    def test_check_digest_with_urls(self, sample_announcements, tmp_path):
        """URL 검증."""
        # 다이제스트 생성
        markdown_path = tmp_path / "digest.md"
        compose_digest(
            db_path=sample_announcements,
            week_str="2026-W13",
            output_path=markdown_path,
        )

        # 검증 (네트워크 건너뛰기)
        result = check_digest(
            db_path=sample_announcements,
            markdown_path=markdown_path,
            skip_network=True,
        )

        assert "items" in result
        assert "pass" in result
        # 네트워크 없으므로 모두 alive=True, 파싱 가능한지는 DB에서 확인


class TestSendDigest:
    """SendDigest 테스트."""

    def test_markdown_to_html_basic(self):
        """마크다운 HTML 변환."""
        markdown = "# 제목\n\n**굵은** 텍스트\n\n[링크](https://example.com)"
        html = markdown_to_html(markdown)

        assert "<h1>제목</h1>" in html
        assert "<strong>굵은</strong>" in html
        assert '<a href="https://example.com">링크</a>' in html

    def test_markdown_to_html_list(self):
        """마크다운 목록 변환."""
        markdown = "- 항목1\n- 항목2"
        html = markdown_to_html(markdown)

        assert "<ul>" in html
        assert "<li>항목1</li>" in html
        assert "<li>항목2</li>" in html

    def test_fail_closed_with_marker(self, tmp_path):
        """마커 있으면 실패."""
        markdown_path = tmp_path / "digest.md"
        markdown_path.write_text("# 제목\n\n<!-- 상민 확정 필요 -->")

        passed, msg = check_fail_closed(markdown_path, tmp_path / "nocheck.json")
        assert passed is False
        assert "미확정" in msg

    def test_fail_closed_no_marker(self, tmp_path):
        """마커 없으면 통과."""
        markdown_path = tmp_path / "digest.md"
        markdown_path.write_text("# 제목\n\n완료된 다이제스트")

        passed, msg = check_fail_closed(markdown_path, tmp_path / "nocheck.json")
        assert passed is True

    def test_fail_closed_check_json_fail(self, tmp_path):
        """check.json pass=false면 실패."""
        markdown_path = tmp_path / "digest.md"
        markdown_path.write_text("# 제목\n\n완료된 다이제스트")

        check_json_path = tmp_path / "digest.check.json"
        check_json_path.write_text(json.dumps({
            "items": [{"url": "https://dead.com", "passed": False}],
            "pass": False
        }))

        passed, msg = check_fail_closed(markdown_path, check_json_path)
        assert passed is False
        assert "검증 실패" in msg

    def test_send_digest_dry_run_no_marker(self, tmp_path):
        """드라이런 (마커 없음)."""
        markdown_path = tmp_path / "digest.md"
        markdown_path.write_text("# 협의회 주간 정책브리핑\n\n다이제스트 내용")

        with mock.patch("alert.config.get_config"):
            result = send_digest(markdown_path, to_email="test@example.com", dry_run=True)

        assert result == 0

    def test_send_digest_fail_with_marker(self, tmp_path):
        """발송 거부 (마커 있음)."""
        markdown_path = tmp_path / "digest.md"
        markdown_path.write_text("# 협의회 주간 정책브리핑\n\n<!-- 상민 확정 필요 -->")

        with mock.patch("alert.config.get_config"):
            result = send_digest(markdown_path, to_email="test@example.com", dry_run=True)

        assert result == 2

    def test_send_digest_file_not_found(self, tmp_path):
        """파일 없음."""
        result = send_digest(tmp_path / "nonexistent.md", dry_run=True)
        assert result == 2


class TestIntegration:
    """통합 테스트."""

    def test_full_workflow(self, sample_announcements, tmp_path):
        """전체 워크플로우: 생성 -> 검증 -> 발송."""
        # 1. 다이제스트 생성
        markdown_path = tmp_path / "digest.md"
        markdown = compose_digest(
            db_path=sample_announcements,
            week_str="2026-W13",
            limit=5,
            output_path=markdown_path,
        )

        assert markdown_path.exists()
        assert "<!-- 상민 확정 필요 -->" in markdown

        # 2. 검증
        check_json_path = markdown_path.with_suffix(".check.json")
        result = check_digest(
            db_path=sample_announcements,
            markdown_path=markdown_path,
            output_path=check_json_path,
            skip_network=True,
        )

        assert "items" in result
        assert "pass" in result

        # 3. 발송 실패 (마커 때문에)
        with mock.patch("alert.config.get_config"):
            exit_code = send_digest(
                markdown_path,
                to_email="test@example.com",
                dry_run=True,
            )

        assert exit_code == 2  # 미확정 마커 때문에 실패

        # 4. 마커 제거 후 발송 성공
        cleaned_markdown = markdown.replace(
            "<!-- 상민 확정 필요 -->",
            ""
        )
        markdown_path.write_text(cleaned_markdown)

        with mock.patch("alert.config.get_config"):
            exit_code = send_digest(
                markdown_path,
                to_email="test@example.com",
                dry_run=True,
            )

        assert exit_code == 0  # 성공
