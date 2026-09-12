"""주간 정책브리핑 다이제스트 생성 및 발송 테스트."""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock
from datetime import datetime, timedelta
from html.parser import HTMLParser

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
    markdown_sha256,
    parse_period_end,
    check_url_alive,
)
import scripts.send_digest as send_digest_module
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


def _create_announcements_table(db_path) -> None:
    """테스트용 announcements 테이블 생성."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
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
    conn.commit()
    conn.close()


def _section_body(markdown: str, heading: str) -> str:
    """마크다운에서 '## {heading}' 섹션 본문만 추출."""
    body = []
    collecting = False
    for line in markdown.split("\n"):
        if line.startswith("## "):
            collecting = line[3:].strip() == heading
            continue
        if collecting:
            body.append(line)
    return "\n".join(body)


def _insert_one(db_path, **overrides) -> None:
    """2026-W13 범위의 공고 1건 삽입."""
    row = {
        "source": "test",
        "source_id": "test_001",
        "title": "테스트 공고",
        "summary": "요약",
        "url": "https://example.com/test-001",
        "author": "기관",
        "period_end": "2026-12-31",
        "relevance_score": 0.9,
        "created_at": "2026-03-26T12:00:00",
    }
    row.update(overrides)

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO announcements
        (source, source_id, title, summary, url, author, period_end,
         relevance_score, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["source"], row["source_id"], row["title"], row["summary"],
            row["url"], row["author"], row["period_end"],
            row["relevance_score"], row["created_at"], row["created_at"],
        ),
    )
    conn.commit()
    conn.close()


class _TagCollector(HTMLParser):
    """HTML 태그와 이벤트 속성(on*) 수집기."""

    def __init__(self) -> None:
        super().__init__()
        self.tags = []
        self.event_attrs = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for name, _value in attrs:
            if name.lower().startswith("on"):
                self.event_attrs.append(name)


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
            output_path=tmp_path / "digest.md",
        )

        # 파일로 저장되었는지 확인
        assert (tmp_path / "digest.md").exists()

    def test_compose_digest_no_limit_kwarg(self, sample_announcements):
        """--limit / limit 파라미터는 완전히 제거되었다."""
        import inspect

        params = inspect.signature(compose_digest).parameters
        assert "limit" not in params

        with pytest.raises(TypeError):
            compose_digest(
                db_path=sample_announcements,
                week_str="2026-W13",
                limit=2,
            )

    def test_compose_digest_forest_not_starved_by_volume(self, tmp_path):
        """고득점 타 섹션 60건이 있어도 산림 섹션은 상한만큼 나온다 (M1 재현)."""
        db_path = tmp_path / "starve.db"
        _create_announcements_table(db_path)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        created_at = "2026-03-26T12:00:00"  # 2026-W13

        # 고득점 사회연대경제 60건
        for i in range(60):
            cursor.execute(
                """
                INSERT INTO announcements
                (source, source_id, title, url, author, period_end,
                 relevance_score, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "seis",
                    f"seis_{i}",
                    f"사회적기업 소식 {i}",
                    f"https://example.com/seis/{i}",
                    "경기도청",
                    "2026-12-31",
                    0.99,
                    created_at,
                    created_at,
                ),
            )

        # 저득점 산림 정책 5건 (공고성 키워드 없음)
        for i in range(5):
            cursor.execute(
                """
                INSERT INTO announcements
                (source, source_id, title, url, author, period_end,
                 relevance_score, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "fowi",
                    f"fowi_{i}",
                    f"국유림 경영 소식 {i}",
                    f"https://example.com/fowi/{i}",
                    "산림청",
                    "2026-12-31",
                    0.01,
                    created_at,
                    created_at,
                ),
            )

        conn.commit()
        conn.close()

        markdown = compose_digest(db_path=str(db_path), week_str="2026-W13")

        forest_section = _section_body(markdown, "산림 정책 동향")
        sse_section = _section_body(markdown, "사회연대경제 동향")
        subsidy_section = _section_body(markdown, "지원사업 공고")

        assert forest_section.count("### ") == 3, f"산림 섹션 건수: {forest_section.count('### ')}"
        assert sse_section.count("### ") == 3
        assert subsidy_section.count("### ") == 0

    def test_compose_digest_dedup_before_section_cap(self, tmp_path):
        """중복 제거를 상한 적용 전에 수행 (계약 v1.1)."""
        db_path = tmp_path / "dedup.db"
        _create_announcements_table(db_path)

        # 산림 섹션: 상위 2건이 같은 제목(중복) + 고유 3건
        titles = [
            ("dup_a", "국유림 경영 소식", 0.99),
            ("dup_b", "국유림  경영\t소식", 0.98),
            ("uniq_1", "숲가꾸기 현장 이야기", 0.50),
            ("uniq_2", "산림 탄소 흡수량 보고", 0.40),
            ("uniq_3", "임업 통계 브리프", 0.30),
        ]
        for source_id, title, score in titles:
            _insert_one(
                db_path,
                source="fowi",
                source_id=source_id,
                title=title,
                url=f"https://example.com/{source_id}",
                relevance_score=score,
            )

        markdown = compose_digest(db_path=str(db_path), week_str="2026-W13")
        forest_section = _section_body(markdown, "산림 정책 동향")

        # 중복 1건으로 접힌 뒤 상한 3건이 채워져야 함
        assert forest_section.count("### ") == 3
        assert forest_section.count("### 국유림 경영 소식") == 1

    def test_compose_digest_form_load_failure_warning(self, tmp_path):
        """깨진 폼 CSV는 경고 리스트에 기록된다."""
        db_path = tmp_path / "warn.db"
        _create_announcements_table(db_path)
        _insert_one(db_path)

        broken_csv = tmp_path / "broken.csv"
        broken_csv.write_bytes(b"\xff\xfe\x00\x00broken")

        warnings = []
        compose_digest(
            db_path=str(db_path),
            week_str="2026-W13",
            forms_csv_path=broken_csv,
            warnings_out=warnings,
        )

        assert warnings, "폼 로드 실패 경고가 수집되어야 함"
        assert "폼 로드 실패" in warnings[0]

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

    def test_check_digest_zero_urls_network_checked_false(self, tmp_path):
        """실제 검사한 URL이 0건이면 network_checked=false."""
        md_path = tmp_path / "empty.md"
        md_path.write_text("# 빈 다이제스트\n\n항목 없음")

        result = check_digest(
            db_path=":memory:",
            markdown_path=md_path,
            skip_network=False,
        )

        assert result["network_checked"] is False
        assert result["pass"] is False

    def test_check_digest_warnings_force_fail(self, sample_announcements, tmp_path):
        """생성 단계 경고가 있으면 pass=false + reason 기록."""
        md_path = tmp_path / "test.md"
        check_path = tmp_path / "test.check.json"
        md_path.write_text("# 테스트\n\n[test](https://example.com)")

        result = check_digest(
            db_path=sample_announcements,
            markdown_path=md_path,
            output_path=check_path,
            skip_network=True,
            warnings=["폼 로드 실패: 'utf-8' codec can't decode byte"],
        )

        assert result["pass"] is False
        assert "폼 로드 실패" in result["reason"]

        with open(check_path) as f:
            saved = json.load(f)
        assert saved["pass"] is False
        assert "폼 로드 실패" in saved["reason"]


class TestWeeklyDigestScript:
    """weekly_digest.py 배선 테스트."""

    def test_broken_form_csv_fails_gate(self, tmp_path, monkeypatch):
        """깨진 폼 CSV → check.json pass=false + reason에 '폼 로드 실패'."""
        import scripts.weekly_digest as weekly_digest

        db_path = tmp_path / "wd.db"
        _create_announcements_table(db_path)
        _insert_one(db_path)

        broken_csv = tmp_path / "broken.csv"
        broken_csv.write_bytes(b"\xff\xfe\x00\x00broken")

        out_dir = tmp_path / "out"

        # 네트워크 호출 차단 (URL 생존 검사는 통과로 고정)
        monkeypatch.setattr(
            "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "weekly_digest.py",
                "--db", str(db_path),
                "--week", "2026-W13",
                "--out-dir", str(out_dir),
                "--forms", str(broken_csv),
            ],
        )

        rc = weekly_digest.main()
        assert rc == 1

        with open(out_dir / "2026-W13.check.json", encoding="utf-8") as f:
            saved = json.load(f)

        assert saved["pass"] is False
        assert "폼 로드 실패" in saved["reason"]

    def test_no_limit_option(self):
        """--limit 옵션은 제거되었다."""
        script = Path(__file__).resolve().parent.parent / "scripts" / "weekly_digest.py"
        source = script.read_text(encoding="utf-8")
        assert "--limit" not in source


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
        body = "# 테스트\n\n완료"
        md_path.write_text(body)

        # check.json 파일은 markdown 파일 이름으로부터 자동 파생됨
        # 계약 W10: 검증은 그 본문의 해시를 남겨야 발송 게이트를 통과한다.
        check_path = md_path.with_suffix(".check.json")
        check_path.write_text(json.dumps({
            "items": [{"url": "https://example.com", "url_alive": True, "deadline_parsed": True, "passed": True}],
            "pass": True,
            "network_checked": True,
            "reason": "",
            "md_sha256": markdown_sha256(body),
        }))

        # dry_run=True가 기본값이므로 발송 안 함
        result = send_digest(md_path, to_email="test@example.com", dry_run=True)

        # dry_run이므로 0 (성공)
        assert result == 0

    def test_markdown_to_html_link_sentinel_no_collision(self):
        """원문의 __LINK_0__ 리터럴이 링크로 둔갑하지 않는다 (M2)."""
        md = "__LINK_0__ 라는 텍스트 [진짜](https://ok.com)"
        html = markdown_to_html(md)

        assert html.count("<a ") == 1, html
        assert "__LINK_0__" in html

    def test_markdown_to_html_javascript_no_residual_paren(self):
        """javascript: 링크는 텍스트만 남고 잔여 ')'가 없다."""
        html = markdown_to_html("[클릭](javascript:alert(1))")

        assert "javascript:" not in html
        assert "<p>클릭</p>" in html, html
        assert "클릭)" not in html

    def test_main_dry_run_with_send_never_opens_smtp(self, tmp_path, monkeypatch):
        """--dry-run --send 동시 지정 시 SMTP 연결이 생성되지 않는다."""
        md_path = tmp_path / "x.md"
        body = "# 테스트\n\n[원문](https://example.com)"
        md_path.write_text(body)
        check_path = md_path.with_suffix(".check.json")
        check_path.write_text(json.dumps({
            "items": [{"url": "https://example.com", "url_alive": True,
                       "deadline_parsed": True, "passed": True}],
            "pass": True,
            "network_checked": True,
            "reason": "",
            "md_sha256": markdown_sha256(body),
        }))

        smtp_mock = mock.MagicMock()
        monkeypatch.setattr("alert.notifiers.email_sender.smtplib.SMTP", smtp_mock)
        monkeypatch.setattr(
            sys,
            "argv",
            ["send_digest.py", str(md_path), "--to", "a@b.c", "--dry-run", "--send"],
        )

        rc = send_digest_module.main()

        assert rc == 0
        assert smtp_mock.call_count == 0

    def test_send_to_override_replaces_config_recipients(self, tmp_path, monkeypatch):
        """--to 지정 시 config 수신자 2명이 아니라 제3자에게만 발송."""
        md_path = tmp_path / "y.md"
        body = "# 테스트\n\n[원문](https://example.com)"
        md_path.write_text(body)
        check_path = md_path.with_suffix(".check.json")
        check_path.write_text(json.dumps({
            "items": [{"url": "https://example.com", "url_alive": True,
                       "deadline_parsed": True, "passed": True}],
            "pass": True,
            "network_checked": True,
            "reason": "",
            "md_sha256": markdown_sha256(body),
        }))

        monkeypatch.setenv("EMAIL_SENDER", "sender@x.com")
        monkeypatch.setenv("EMAIL_PASSWORD", "pw")
        monkeypatch.setenv("EMAIL_RECIPIENTS", "config1@x.com,config2@x.com")

        sent = {}

        class FakeSMTP:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def starttls(self):
                pass

            def login(self, *args):
                pass

            def send_message(self, msg):
                sent["to"] = msg["To"]

        monkeypatch.setattr("alert.notifiers.email_sender.smtplib.SMTP", FakeSMTP)

        # 계약 W10 사이클3 #3: 실발송에는 "사람이 본 미리보기"의 지문이 필요하다.
        from alert.digest import state as state_mod

        state_path = state_mod.state_path_for_markdown(md_path)
        state_mod.save_state(state_path, state_mod.record_preview(
            state_mod.default_state(state_mod.week_from_markdown(md_path)),
            [2014], [], markdown_sha256(body),
        ))

        rc = send_digest(md_path, to_email="third@example.com", dry_run=False,
                         approved_sha=markdown_sha256(body)[:8])

        assert rc == 0
        assert sent["to"] == "third@example.com"

    def test_html_escapes_author_and_deadline_from_db(self, tmp_path):
        """DB의 author/period_end 페이로드가 HTML에 raw로 새지 않는다."""
        db_path = tmp_path / "xss.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            author="<script>alert(1)</script>",
            period_end="<img src=x onerror=1>",
        )

        markdown = compose_digest(db_path=str(db_path), week_str="2026-W13")
        html = markdown_to_html(markdown)

        # raw 태그가 실제 태그로 파싱되지 않아야 함
        assert "<script" not in html
        assert "<img" not in html
        assert "&lt;script&gt;" in html
        assert "&lt;img src=x onerror=1&gt;" in html  # 텍스트로만 존재

        parser = _TagCollector()
        parser.feed(html)
        assert "script" not in parser.tags
        assert "img" not in parser.tags
        assert parser.event_attrs == []


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


class TestDeadUrlDrop:
    """계약 v1.2: url_alive=false 항목 자동 제외 + dropped 기록."""

    @staticmethod
    def _seed_three(db_path):
        """2026-W13 범위의 지원사업 항목 3건."""
        _create_announcements_table(db_path)
        for i in range(3):
            _insert_one(
                db_path,
                source_id=f"test_{i:03d}",
                title=f"테스트 공고 {i}",
                url=f"https://example.com/item-{i}",
                relevance_score=0.9 - i * 0.1,
            )

    @staticmethod
    def _run(tmp_path, monkeypatch, db_path, dead_urls):
        """weekly_digest.main()을 돌리고 (rc, markdown, check.json)을 돌려준다."""
        import scripts.weekly_digest as weekly_digest

        out_dir = tmp_path / "out"

        monkeypatch.setattr(
            "alert.digest.checker.check_url_alive",
            lambda url, timeout=8: url not in dead_urls,
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "weekly_digest.py",
                "--db", str(db_path),
                "--week", "2026-W13",
                "--out-dir", str(out_dir),
                "--forms", str(tmp_path / "missing.csv"),
            ],
        )

        rc = weekly_digest.main()
        markdown = (out_dir / "2026-W13.md").read_text(encoding="utf-8")
        with open(out_dir / "2026-W13.check.json", encoding="utf-8") as f:
            check = json.load(f)
        return rc, markdown, check

    def test_one_dead_url_dropped(self, tmp_path, monkeypatch):
        """죽은 URL 1건 포함 3건 → 산출물 2건, dropped 1, pass true."""
        db_path = tmp_path / "wd.db"
        self._seed_three(db_path)

        dead = "https://example.com/item-1"
        rc, markdown, check = self._run(tmp_path, monkeypatch, db_path, {dead})

        assert rc == 0
        assert check["pass"] is True
        assert check["network_checked"] is True
        # items = 최종 산출물에 실린 항목, dropped = 제외된 항목
        assert len(check["items"]) == 2
        assert all(item["url_alive"] for item in check["items"])
        assert len(check["dropped"]) == 1
        assert check["dropped"][0]["url"] == dead
        assert check["dropped"][0]["title"] == "테스트 공고 1"

        # 산출물에는 살아있는 2건만 남는다
        assert dead not in markdown
        assert "https://example.com/item-0" in markdown
        assert "https://example.com/item-2" in markdown
        assert markdown.count("### 테스트 공고") == 2

    def test_all_dead_urls_fail(self, tmp_path, monkeypatch):
        """전부 죽으면 pass false."""
        db_path = tmp_path / "wd.db"
        self._seed_three(db_path)

        dead = {f"https://example.com/item-{i}" for i in range(3)}
        rc, markdown, check = self._run(tmp_path, monkeypatch, db_path, dead)

        assert rc == 1
        assert check["pass"] is False
        assert check["items"] == []
        assert len(check["dropped"]) == 3
        assert check["reason"] == "항목 없음"
        for url in dead:
            assert url not in markdown

    def test_recompose_rounds_exhausted_fails_closed(self, tmp_path, monkeypatch):
        """라운드를 소진해도 죽은 URL이 남으면 fail-closed."""
        import scripts.weekly_digest as weekly_digest

        db_path = tmp_path / "wd.db"
        self._seed_three(db_path)

        # 1라운드만 허용 → 제외 후 재검증할 기회가 없으므로 fail-closed여야 한다
        monkeypatch.setattr(weekly_digest, "MAX_RECOMPOSE_ROUNDS", 1)

        all_dead = {f"https://example.com/item-{i}" for i in range(3)}
        rc, _markdown, check = self._run(tmp_path, monkeypatch, db_path, all_dead)

        assert rc == 1
        assert check["pass"] is False
        assert "죽은 URL 반복 검출" in check["reason"]
        assert len(check["dropped"]) == 3

    def test_missing_deadline_no_longer_gates(self, tmp_path, monkeypatch):
        """계약 v1.2: deadline_parsed는 정보 필드이므로 pass를 막지 않는다."""
        db_path = tmp_path / "wd.db"
        _create_announcements_table(db_path)
        _insert_one(db_path, period_end="")

        rc, _markdown, check = self._run(tmp_path, monkeypatch, db_path, set())

        assert rc == 0
        assert check["items"][0]["deadline_parsed"] is False
        assert check["items"][0]["url_alive"] is True
        assert check["dropped"] == []
        assert check["pass"] is True


class TestComposeExcludeUrls:
    """compose_digest(exclude_urls=...) 재조립."""

    def test_exclude_urls_backfills_up_to_section_limit(self, tmp_path):
        """제외 후 섹션 상한이 남은 후보로 다시 채워진다."""
        db_path = tmp_path / "compose.db"
        _create_announcements_table(db_path)
        # 지원사업 상한(5)을 넘는 6건
        for i in range(6):
            _insert_one(
                db_path,
                source_id=f"test_{i:03d}",
                title=f"지원사업 공고 {i}",
                url=f"https://example.com/s-{i}",
                relevance_score=0.9 - i * 0.05,
            )

        full = compose_digest(db_path=str(db_path), week_str="2026-W13")
        assert full.count("### 지원사업 공고") == 5
        assert "https://example.com/s-5" not in full

        trimmed = compose_digest(
            db_path=str(db_path),
            week_str="2026-W13",
            exclude_urls={"https://example.com/s-0"},
        )
        # 제외 1건 → 다음 순위(s-5)가 채워져 여전히 상한 5건
        assert trimmed.count("### 지원사업 공고") == 5
        assert "https://example.com/s-0" not in trimmed
        assert "https://example.com/s-5" in trimmed

    def test_exclude_urls_never_exceeds_section_limit(self, tmp_path):
        """제외 후에도 섹션 상한을 넘지 않는다."""
        db_path = tmp_path / "limit.db"
        _create_announcements_table(db_path)
        for i in range(5):
            _insert_one(
                db_path,
                source_id=f"forest_{i:03d}",
                source="fowi",
                title=f"산림 동향 {i}",
                url=f"https://example.com/f-{i}",
                relevance_score=0.9 - i * 0.05,
            )

        markdown = compose_digest(
            db_path=str(db_path),
            week_str="2026-W13",
            exclude_urls={"https://example.com/f-0"},
        )
        # 산림 상한 3, 후보 5건 중 1건 제외 → 상한대로 3건
        assert markdown.count("### 산림 동향") == 3
        assert "https://example.com/f-0" not in markdown


class TestUrlProbe:
    """check_url_alive의 HEAD→GET 폴백과 브라우저 UA."""

    @mock.patch("alert.digest.checker.requests.get")
    @mock.patch("alert.digest.checker.requests.head")
    def test_head_exception_then_get_200_is_alive(self, mock_head, mock_get):
        """HEAD 예외 + GET 200 → alive."""
        mock_head.side_effect = requests.exceptions.ConnectionError("reset by peer")
        mock_get.return_value.status_code = 200

        assert check_url_alive("https://example.com/a") is True
        assert mock_get.called

    @mock.patch("alert.digest.checker.requests.get")
    @mock.patch("alert.digest.checker.requests.head")
    def test_head_405_then_get_200_is_alive(self, mock_head, mock_get):
        """HEAD 405(미지원) + GET 200 → alive (HEAD 실패만으로 dead 아님)."""
        mock_head.return_value.status_code = 405
        mock_get.return_value.status_code = 200

        assert check_url_alive("https://example.com/b") is True

    @mock.patch("alert.digest.checker.requests.get")
    @mock.patch("alert.digest.checker.requests.head")
    def test_head_403_then_get_200_is_alive(self, mock_head, mock_get):
        """HEAD 403 + GET 200 → alive."""
        mock_head.return_value.status_code = 403
        mock_get.return_value.status_code = 200

        assert check_url_alive("https://example.com/c") is True

    @mock.patch("alert.digest.checker.requests.get")
    @mock.patch("alert.digest.checker.requests.head")
    def test_get_failure_is_dead(self, mock_head, mock_get):
        """HEAD 실패 + GET 404 → dead."""
        mock_head.side_effect = requests.exceptions.ConnectionError("reset")
        mock_get.return_value.status_code = 404

        assert check_url_alive("https://example.com/d") is False

    @mock.patch("alert.digest.checker.requests.get")
    @mock.patch("alert.digest.checker.requests.head")
    def test_both_fail_is_dead(self, mock_head, mock_get):
        """HEAD·GET 모두 예외 → dead."""
        mock_head.side_effect = requests.exceptions.ConnectionError("reset")
        mock_get.side_effect = requests.exceptions.ConnectionError("reset")

        assert check_url_alive("https://example.com/e") is False

    @mock.patch("alert.digest.checker.requests.get")
    @mock.patch("alert.digest.checker.requests.head")
    def test_browser_user_agent_sent(self, mock_head, mock_get):
        """두 요청 모두 브라우저 UA를 보낸다."""
        mock_head.side_effect = requests.exceptions.ConnectionError("reset")
        mock_get.return_value.status_code = 200

        check_url_alive("https://example.com/f")

        for call in (mock_head.call_args, mock_get.call_args):
            headers = call.kwargs["headers"]
            assert "Mozilla/5.0" in headers["User-Agent"]

    @mock.patch("alert.digest.checker.requests.get")
    @mock.patch("alert.digest.checker.requests.head")
    def test_get_streams_and_closes(self, mock_head, mock_get):
        """GET은 stream으로 열고 본문을 제한적으로 읽은 뒤 닫는다."""
        mock_head.side_effect = requests.exceptions.ConnectionError("reset")
        response = mock_get.return_value
        response.status_code = 200

        assert check_url_alive("https://example.com/g") is True
        assert mock_get.call_args.kwargs["stream"] is True
        response.iter_content.assert_called_once_with(chunk_size=64 * 1024)
        response.close.assert_called_once()
