"""주간 정책브리핑 다이제스트 생성 및 발송 테스트."""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock
from datetime import date
from html.parser import HTMLParser

import pytest
import requests.exceptions

from alert.digest.composer import (
    MARKER,
    SECTION_HEADINGS,
    HOLD_REASON_DIVERSITY,
    HOLD_REASON_SECTION_CAP,
    ITEM_SECTIONS,
    KAKAO_CHUNK_SEPARATOR,
    KAKAO_HEADLINE_PLACEHOLDER,
    KAKAO_HEADLINE_PREFIX,
    SECTION_LIMITS,
    HOLD_REASON_URL_TOO_LONG,
    KAKAO_CHUNK_LIMIT,
    prefix_signature,
    SOURCE_DIVERSITY_LIMIT,
    URL_TOO_LONG_NOTICE,
    VERDICT_APPLY,
    VERDICT_EXCLUDE,
    VERDICT_HOLD,
    VERDICT_NOTICE,
    chunk_plaintext,
    classify_item,
    compose_digest,
    compose_digest_data,
    extract_quotes,
    get_week_date_range,
    infer_target_tags,
    hold_comment,
    jaccard,
    kakao_blocks,
    kakao_blocks_from_markdown,
    kakao_file_text,
    kakao_file_text_from_markdown,
    kst_date,
    normalize_title,
    parse_deadline,
    render_kakao_chunks,
    render_kakao,
    render_markdown,
    bracket_regions,
    dedup_key,
    infer_region,
    merge_title_key,
    prefix_bracket,
    prefix_brackets,
    sanitize_title,
    source_display_name,
    target_display,
    title_ngrams,
    week_bounds,
    window_prefilter_bounds,
)
from alert.digest import blocks as blocks_mod
from alert.digest.checker import (
    body_links,
    check_digest,
    extract_item_urls,
    markdown_sha256,
    parse_period_end,
    check_url_alive,
)
import scripts.send_digest as send_digest_module
from scripts.send_digest import check_fail_closed, markdown_to_html, send_digest

# 테스트가 고정으로 쓰는 주차와 기준일 (2026-W13 = 03-23 ~ 03-29)
W13 = "2026-W13"
W13_CREATED_AT = "2026-03-26T12:00:00"
W13_TODAY = date(2026, 3, 26)

APPLY_HEADING = SECTION_HEADINGS[VERDICT_APPLY]
NOTICE_HEADING = SECTION_HEADINGS[VERDICT_NOTICE]

# 소스 다양성 상한(같은 소스 2건)에 걸리지 않게 3소스 x 2건으로 펼친 신청 후보.
# 전부 사업자 신호(기업·업체·사업자·입점·조달)를 갖고 있어 B2C 보류에도 걸리지 않는다.
SPREAD_APPLY_ROWS = (
    ("kofpi", "산림분야 오픈이노베이션 참여기업 모집 공고"),
    ("kofpi", "임산물 가공유통 지원사업 신청 안내"),
    ("fowi", "산촌 목재 이용 설명회 참여업체 모집"),
    ("fowi", "임업 경영 컨설팅 참여기업 모집"),
    ("coop", "산림복지전문업 플랫폼 입점 지원사업"),
    ("coop", "숲가꾸기 사업자 조달 계약 공모"),
)


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

    created_at = W13_CREATED_AT

    announcements = [
        {
            "source": "lawmaking",
            "source_id": "law_001",
            "title": "산림재난방지법 시행령 일부개정령안 입법예고",
            "summary": "임도·산사태 취약지 의무 관리 확대",
            "url": "https://example.com/law/001",
            "author": "산림청",
            "period_end": "2026-12-31",
            "relevance_score": 0.9,
            "created_at": created_at,
            "updated_at": created_at,
        },
        {
            "source": "kofpi",
            "source_id": "kofpi_001",
            "title": "산림분야 오픈이노베이션 참여기업 모집 공고",
            "summary": "임업 기업 대상 기술 협업 과제 공고",
            "url": "https://example.com/kofpi/001",
            "author": "한국임업진흥원",
            "period_end": "2026-12-31",
            "relevance_score": 0.85,
            "created_at": created_at,
            "updated_at": created_at,
        },
        {
            "source": "seis",
            "source_id": "seis_001",
            "title": "경기도 사회적기업 사회보험료 지원사업 참여기업 모집 공고",
            "summary": "경기도 사회적기업 사회보험료 지원",
            "url": "https://example.com/seis/001",
            "author": "한국사회적기업진흥원",
            "period_end": "2026-12-30",
            "relevance_score": 0.8,
            "created_at": created_at,
            "updated_at": created_at,
        },
        {
            "source": "nongsaro",
            "source_id": "nongsaro_001",
            "title": "농업 지원금 신청 안내",
            "summary": "농가 소득 향상을 위한 지원금 안내",
            "url": "https://example.com/nongsaro/001",
            "author": "농림축산식품부",
            "period_end": "2026-08-20",
            "relevance_score": 0.75,
            "created_at": created_at,
            "updated_at": created_at,
        },
        {
            "source": "coop",
            "source_id": "coop_001",
            "title": "사회적협동조합 공공조달 1:1 컨설팅 참여기업 모집",
            "summary": "조달·민간위탁 컨설팅",
            "url": "https://example.com/coop/001",
            "author": "기획재정부 협동조합 포털",
            "period_end": "2026-12-29",
            "relevance_score": 0.7,
            "created_at": created_at,
            "updated_at": created_at,
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


def _count_items(body: str) -> int:
    """형식 v2.1의 항목 수 = `  [원문](…)` 줄 수."""
    return sum(1 for line in body.split("\n") if line.startswith("  [원문]("))


def _insert_one(db_path, **overrides) -> None:
    """2026-W13 범위의 공고 1건 삽입 (기본값은 룰 v2.1을 통과하는 신청 항목)."""
    row = {
        "source": "kofpi",
        "source_id": "test_001",
        "title": "산림 분야 지원사업 참여기업 모집 공고",
        "summary": "요약",
        "url": "https://example.com/test-001",
        "author": "기관",
        "period_start": None,
        "period_end": "2026-12-31",
        "raw_data": "",
        "relevance_score": 0.9,
        "created_at": W13_CREATED_AT,
    }
    row.update(overrides)

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO announcements
        (source, source_id, title, summary, url, author, period_start,
         period_end, raw_data, relevance_score, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["source"], row["source_id"], row["title"], row["summary"],
            row["url"], row["author"], row["period_start"],
            row["period_end"], row["raw_data"],
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

    def test_compose_digest_basic(self, sample_announcements):
        """기본 다이제스트 생성 (형식 v2.1의 섹션 구성)."""
        markdown = compose_digest(
            db_path=sample_announcements,
            week_str=W13,
            today=W13_TODAY,
        )

        assert f"## {APPLY_HEADING}" in markdown
        assert f"## {NOTICE_HEADING}" in markdown
        # 개정 v2.4 (c): 내용 없는 협의회에서·회원사 소식은 섹션 자체를 생략
        assert SECTION_HEADINGS["협의회에서"] not in markdown
        assert SECTION_HEADINGS["회원사 소식"] not in markdown
        assert "이번 주 기록 없음" not in markdown
        # v2.1에서 소스 축 섹션은 폐기됐다
        assert "산림 정책 동향" not in markdown
        assert "사회연대경제 동향" not in markdown

        # 마커는 "이번 주 한 줄" 자리에 정확히 한 줄
        assert markdown.count(MARKER) == 1
        assert f"이번 주 한 줄: {MARKER}" in markdown

    def test_compose_digest_lane_comment(self, sample_announcements):
        """레인 표기가 항상 있어야 함."""
        markdown = compose_digest(
            db_path=sample_announcements,
            week_str=W13,
            today=W13_TODAY,
        )
        assert "<!-- lane: Claude opus executor -->" in markdown

    def test_compose_digest_author_is_source_display_name(self, sample_announcements):
        """판정 ⑤: 기관 = 소스 표시명. author(게시자 이름)는 표시하지 않는다."""
        markdown = compose_digest(
            db_path=sample_announcements,
            week_str=W13,
            today=W13_TODAY,
        )
        assert "한국임업진흥원" in markdown
        assert "국민참여입법센터(산림청 소관)" in markdown
        # DB author 값은 그대로 새지 않는다
        assert "기획재정부 협동조합 포털" not in markdown
        assert "한국사회적기업진흥원" not in markdown

    def test_compose_digest_per_section_limits(self, sample_announcements, tmp_path):
        """파일 저장 + 카톡 평문 파일이 함께 생성된다."""
        compose_digest(
            db_path=sample_announcements,
            week_str=W13,
            output_path=tmp_path / "digest.md",
            today=W13_TODAY,
        )

        assert (tmp_path / "digest.md").exists()
        assert (tmp_path / "digest.kakao.txt").exists()

    def test_compose_digest_no_limit_kwarg(self, sample_announcements):
        """--limit / limit 파라미터는 완전히 제거되었다."""
        import inspect

        params = inspect.signature(compose_digest).parameters
        assert "limit" not in params

        with pytest.raises(TypeError):
            compose_digest(
                db_path=sample_announcements,
                week_str=W13,
                limit=2,
            )

    def test_compose_digest_apply_cap_is_five(self, tmp_path):
        """신청하세요 상한 5 (판정 ⑩). 대량의 저관련 항목이 상한을 뚫지 못한다."""
        db_path = tmp_path / "cap.db"
        _create_announcements_table(db_path)

        for index, (source, title) in enumerate(SPREAD_APPLY_ROWS):
            _insert_one(
                db_path,
                source=source,
                source_id=f"cap_{index}",
                title=title,
                url=f"https://example.com/cap-{index}",
                period_end=f"2026-12-{20 + index}",
            )

        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )
        assert _count_items(_section_body(markdown, APPLY_HEADING)) == (
            SECTION_LIMITS[VERDICT_APPLY]
        )

    def test_compose_digest_notice_cap_is_three(self, tmp_path):
        """알아두세요 상한 3 (판정 ⑩)."""
        db_path = tmp_path / "notice.db"
        _create_announcements_table(db_path)

        titles = [
            "산림재난방지법 시행령 일부개정령안 입법예고",
            "산지관리법 시행령 일부개정령안 행정예고",
            "임업·산림 공익직접지불제도 고시 개정",
            "산림복지 진흥에 관한 법률 시행규칙 개정 예고",
        ]
        for index, title in enumerate(titles):
            _insert_one(
                db_path,
                source="lawmaking",
                source_id=f"law_{index}",
                title=title,
                url=f"https://example.com/law-{index}",
                period_end=None,
                period_start=f"2026-03-2{index}",
            )

        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )
        assert _count_items(_section_body(markdown, NOTICE_HEADING)) == (
            SECTION_LIMITS[VERDICT_NOTICE]
        )

    def test_compose_digest_dedup_same_source_merges(self, tmp_path):
        """판정 ⑥ (사이클 10 #1): **완전 동일** 제목만 대표 1건으로 병합한다.

        공백·구두점 차이는 같은 제목으로 본다. 유사도(3-gram Jaccard) 병합은
        폐기됐다 — 괄호·회차 토큰이 정규화에서 사라져 서로 다른 공고를 지웠다.
        """
        db_path = tmp_path / "dedup.db"
        _create_announcements_table(db_path)

        rows = [
            ("dup_a", "임업 경영 컨설팅 참여기업 모집 공고"),
            ("dup_b", "임업 경영 컨설팅 참여기업 모집  \t공고"),
            ("uniq_1", "숲가꾸기 사업자 조달 계약 공모"),
        ]
        for source_id, title in rows:
            _insert_one(
                db_path,
                source="kofpi",
                source_id=source_id,
                title=title,
                url=f"https://example.com/{source_id}",
            )

        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )
        body = _section_body(markdown, APPLY_HEADING)

        assert _count_items(body) == 2
        assert body.count("임업 경영 컨설팅 참여기업 모집") == 1

    def test_near_duplicate_is_not_merged_but_flagged(self, tmp_path):
        """근사 중복은 **합치지 않고** "유사 항목 n" 으로만 표시 (사이클 10 #1).

        Codex 6차 HIGH #1 재현 입력의 일반형: 괄호·회차 토큰만 다른 제목은
        유사도로는 0.6~1.0 이지만 서로 다른 공고일 수 있다. 판단은 편집자 몫이다.
        """
        db_path = tmp_path / "near.db"
        _create_announcements_table(db_path)
        for source_id, title in (
            ("near_a", "임업 경영 컨설팅 참여기업 모집 공고"),
            ("near_b", "임업 경영 컨설팅 참여기업 모집 공고(~9.30)"),
        ):
            _insert_one(
                db_path, source="kofpi", source_id=source_id, title=title,
                url=f"https://example.com/{source_id}",
            )
        data = compose_digest_data(
            str(db_path), week_str=W13, today=W13_TODAY
        )
        published = data["sections"][VERDICT_APPLY]
        assert len(published) == 2
        assert data["merged_ids"] == []
        # 편집자에게는 "유사 항목" 으로 알린다
        assert all(item["similar_count"] >= 1 for item in published)
        markdown = render_markdown(data)
        assert "유사 항목" in markdown

    @pytest.mark.parametrize("shape", [
        "사회적기업 사업개발비 지원사업 [{}] 모집",       # 중간 괄호
        "사회적기업 사업개발비 지원사업 모집 [{}]",       # 후미 괄호
        "[{}] 사회적기업 사업개발비 지원사업 모집",       # 선두 괄호
    ])
    def test_region_bracket_anywhere_is_never_merged(self, tmp_path, shape):
        """지역 괄호가 제목 어디에 있어도 병합되지 않는다 (사이클 10 #1).

        Codex 6차 HIGH #1: 후미는 고쳤지만 **중간 괄호**로 옮기면 지역이 양쪽
        None 이 되고 유사도 0.6 으로 병합돼 게시 1·보류 0 이 됐다.
        완전 동일 제목만 병합하므로 위치와 무관하게 갈라진다.
        """
        db_path = tmp_path / f"c10_mid_{abs(hash(shape))}.db"
        _create_announcements_table(db_path)
        for index, region in enumerate(["경기", "강원"]):
            _insert_one(
                db_path, source="seis", source_id=f"mid_{index}",
                title=shape.format(region),
                url=f"https://example.com/mid-{index}",
                period_start="2026-09-08", period_end="2026-09-30",
                created_at=W37_CREATED_AT,
            )
        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        assert len(data["sections"][VERDICT_APPLY]) == 2
        assert data["merged_ids"] == []

    def test_round_token_is_never_merged(self, tmp_path):
        """`(3차)`/`(4차)` 는 다른 회차다 — 마감이 NULL 이어도 병합 금지 (#1)."""
        db_path = tmp_path / "c10_round.db"
        _create_announcements_table(db_path)
        for index, label in enumerate(["3차", "4차"]):
            _insert_one(
                db_path, source="kofpi", source_id=f"rd_{index}",
                title=f"산림 지원사업 참여기업 모집 ({label})",
                url=f"https://example.com/rd-{index}",
                period_start="2026-09-08", period_end=None,
                created_at=W37_CREATED_AT,
            )
        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        assert len(data["sections"][VERDICT_APPLY]) == 2
        assert data["merged_ids"] == []

    @pytest.mark.parametrize("labels", [
        ("Ⅲ차", "Ⅳ차"),            # 로마 숫자 (Nl)
        ("３차", "４차"),            # 전각 숫자 (Nd)
        ("3차", "4차"),              # ASCII
        ("제Ⅲ기", "제Ⅳ기"),
    ])
    def test_unicode_round_token_is_never_merged(self, tmp_path, labels):
        """어떤 스크립트의 회차 토큰도 병합 키에서 사라지지 않는다 (사이클 11 #1).

        Codex 7차 HIGH #1 재현 입력: `[^0-9A-Za-z가-힣…]` 문자 클래스가 ASCII·한글
        밖의 글자를 전부 지워서 `(Ⅲ차)`/`(Ⅳ차)`·`(３차)`/`(４차)` 가 둘 다
        `…모집차` 로 같아졌다 — 게시 1·보류 0·merged_ids=[2].
        """
        db_path = tmp_path / f"c11_round_{abs(hash(labels))}.db"
        _create_announcements_table(db_path)
        for index, label in enumerate(labels):
            _insert_one(
                db_path, source="kofpi", source_id=f"u_{index}",
                title=f"사회적기업 지원사업 모집 ({label})",
                url=f"https://example.com/u-{index}",
                period_start="2026-09-08", period_end=None,
                created_at=W37_CREATED_AT,
            )
        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        published = data["sections"][VERDICT_APPLY]
        assert len(published) == 2, [item["title"] for item in published]
        assert data["merged_ids"] == []

    def test_merge_title_key_removes_only_whitespace(self):
        """병합 키는 **공백만** 지운다 — 구두점·숫자·기호는 남는다 (사이클 12 #1).

        구두점 하나가 금액·회차를 가른다: `1.5억원` vs `15억원`,
        `(1·3차)` vs `(13차)`. 구두점을 지우면 서로 다른 공고가 합쳐졌다.
        """
        for left, right in (
            ("모집 (Ⅲ차)", "모집 (Ⅳ차)"),          # 로마 숫자 (Nl)
            ("모집 (３차)", "모집 (４차)"),          # 전각 숫자 (Nd)
            ("모집 (三次)", "모집 (四次)"),          # 한자
            ("최대 1.5억원 지원", "최대 15억원 지원"),
            ("모집 (1·3차)", "모집 (13차)"),
            ("산림 공고!", "산림 공고"),
        ):
            assert merge_title_key(left) != merge_title_key(right), (left, right)
        # 공백 차이만 같은 제목이다
        assert merge_title_key("사업개발비 지원사업") == merge_title_key(
            "사업개발비지원사업"
        )
        assert merge_title_key("산림  \t공고") == merge_title_key("산림공고")

    @pytest.mark.parametrize("titles", [
        ("사회적기업 최대 1.5억원 지원사업 모집", "사회적기업 최대 15억원 지원사업 모집"),
        ("사회적기업 지원사업 모집 (1·3차)", "사회적기업 지원사업 모집 (13차)"),
    ])
    def test_punctuation_difference_is_never_merged(self, tmp_path, titles):
        """구두점만 다른 제목은 병합하지 않는다 (Codex 8차 HIGH #1)."""
        db_path = tmp_path / f"c12_punct_{abs(hash(titles))}.db"
        _create_announcements_table(db_path)
        for index, title in enumerate(titles):
            _insert_one(
                db_path, source="seis", source_id=f"p_{index}", title=title,
                url=f"https://example.com/p-{index}",
                period_start="2026-09-08", period_end=None,
                created_at=W37_CREATED_AT,
            )
        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        published = data["sections"][VERDICT_APPLY]
        assert len(published) == 2, [item["title"] for item in published]
        assert data["merged_ids"] == []

    def test_space_variant_duplicate_is_merged(self, tmp_path):
        """공백만 다른 같은 제목은 병합된다 (Codex 7차 LOW #6)."""
        db_path = tmp_path / "c11_space.db"
        _create_announcements_table(db_path)
        for index, title in enumerate((
            "사회적기업 사업개발비 지원사업 참여기업 모집",
            "사회적기업 사업개발비지원사업 참여기업 모집",
        )):
            _insert_one(
                db_path, source="seis", source_id=f"sp_{index}", title=title,
                url=f"https://example.com/sp-{index}",
                period_start="2026-09-08", period_end="2026-09-30",
                created_at=W37_CREATED_AT,
            )
        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        assert len(data["sections"][VERDICT_APPLY]) == 1
        assert len(data["merged_ids"]) == 1

    @pytest.mark.parametrize("group", [
        "설명회 장소 서울", "설명회 장소: 서울", "서울 개최", "개최 장소 대전",
        "서울에서 개최", "서울에서 진행", "서울 회장", "서울 행사장",
    ])
    def test_venue_group_without_colon_is_not_a_region(self, group):
        """콜론이 없어도 장소 문맥이면 자격 지역이 아니다 (사이클 11 #5)."""
        from alert.digest.composer import is_venue_group

        assert is_venue_group(group), group
        for title in (f"[{group}] 사회적기업 지원사업 모집",
                      f"사회적기업 지원사업 모집 [{group}]",
                      f"사회적기업 지원사업 모집({group})"):
            assert infer_region(title) is None, title

    def test_qualification_region_still_wins(self):
        """장소 필터가 자격 지역을 삼키지 않는다 (사이클 11 #5 + 12 #6).

        Codex 8차 MEDIUM #6 재현 입력: 단독 `장` 을 장소 신호로 본 탓에
        `[경기 사업장 보유 기업]` 의 지역 제한이 사라져 대상이 넓어졌다 —
        자격 지역을 잃는 것은 행사 장소를 오인하는 것보다 나쁘다(오배포).
        """
        assert infer_region("[경기] 사회적기업 지원사업 모집") == "경기"
        assert infer_region("[세종대전충청센터] 설명회 안내") == "충청"
        assert infer_region("사회적기업 지원사업 모집 [강원]") == "강원"
        # `장` 이 자격 조건에 든 괄호는 장소가 아니다
        assert infer_region("[경기 사업장 보유 기업] 사회적기업 지원사업 모집") == "경기"
        assert infer_region("[강원 교육장 운영 기업] 지원사업 모집") == "강원"
        # 사이클 14 #2: 지역 탐색은 선두·중간·후미 순으로 **모든** 괄호를 본다
        assert infer_region("사회적기업 지원사업 [경기 소재 기업] 모집") == "경기"
        assert infer_region("2026년 [강원 소재 기업] 지원사업 모집") == "강원"
        # 중간 괄호에도 장소 문맥 필터는 똑같이 적용된다
        assert infer_region("사회적기업 지원사업 [설명회 장소: 서울] 모집") is None
        assert infer_region("사회적기업 지원사업 [서울에서 개최] 모집") is None
        # 선두가 먼저다 — 중간·후미보다 앞선 그룹에서 확정한다
        assert infer_region("[경기] 지원사업 [강원 설명회] 모집") == "경기"
        # 사이클 13 #4: `에서` 는 **장소 동사가 따를 때만** 장소 신호다
        assert infer_region("[경기에서 사업하는 기업] 사회적기업 지원사업 모집") == "경기"
        assert infer_region("[강원에서 창업한 기업] 지원사업 모집") == "강원"
        assert infer_region("[서울에서 개최] 지원사업 모집") is None
        assert infer_region("[서울에서 진행되는 설명회] 지원사업 모집") is None

    def test_merge_title_key_is_conservative(self):
        """병합 키는 공백·구두점만 지운다 (괄호·날짜·회차 전부 보존 — #1)."""
        assert merge_title_key("산림 공고") == merge_title_key("산림  \t공고")
        for other in ("산림 공고(~9.30)", "산림 공고 [경기]", "산림 공고 (3차)"):
            assert merge_title_key("산림 공고") != merge_title_key(other), other

    def test_compose_digest_cross_source_similar_is_annotated_not_merged(
        self, tmp_path
    ):
        """판정 ⑥③: 다른 소스 간 의미 중복은 병합하지 않고 "유사 항목 n"만 표기."""
        db_path = tmp_path / "cross.db"
        _create_announcements_table(db_path)

        _insert_one(
            db_path,
            source="forest_service",
            source_id="fs_1",
            title="2026년도 제2차 산림형 예비사회적기업 지정 계획 공고",
            url="https://example.com/fs-1",
        )
        _insert_one(
            db_path,
            source="socialenterprise",
            source_id="se_1",
            title="2026년도 제2차 산림형 예비사회적기업 지정 계획 공고",
            url="https://example.com/se-1",
        )

        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )
        body = _section_body(markdown, APPLY_HEADING)

        assert _count_items(body) == 2
        assert body.count("유사 항목 1") == 2

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

    def test_compose_digest_deadline_labels(self, tmp_path):
        """판정 ④: 마감 없음 → 게시일을 아는 7일 이내는 "새 소식", 모르거나 오래되면 "상시"."""
        db_path = tmp_path / "labels.db"
        _create_announcements_table(db_path)

        _insert_one(
            db_path,
            source_id="fresh",
            title="산촌 목재 이용 설명회 참여업체 모집",
            url="https://example.com/fresh",
            period_end=None,
            period_start="2026-03-24",
        )
        _insert_one(
            db_path,
            source_id="standing",
            title="임업 경영 컨설팅 참여기업 모집",
            url="https://example.com/standing",
            period_end=None,
            period_start="2025-10-29",
        )
        _insert_one(
            db_path,
            source="seis",
            source_id="unknown",
            title="사회적기업 사무공간 신규 입주기업 모집 공고",
            url="https://example.com/unknown",
            period_end=None,
            period_start=None,
        )

        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )

        assert "[새 소식] 산촌 목재 이용 설명회 참여업체 모집" in markdown
        assert "[상시] 임업 경영 컨설팅 참여기업 모집" in markdown
        # 게시일을 모르는 항목에 "새 소식"을 붙이지 않는다 (fail-closed)
        assert "[상시] 사회적기업 사무공간 신규 입주기업 모집 공고" in markdown
        # 사이클 12 #3: 마감 근거는 DB period_end 뿐이다 —
        # 게시일만 알면 "접수 M/D부터, 마감 미정"
        assert "접수 3/24부터, 마감 미정" in markdown
        assert "접수 10/29부터, 마감 미정" in markdown
        # 둘 다 모르면 "마감 미정"
        assert "· 마감 미정" in markdown
        # 텍스트에서 유도한 마감 표기는 더 이상 나오지 않는다
        assert "원문 확인" not in markdown

    def test_compose_digest_expired_deadline_dropped(self, tmp_path):
        """판정 ④: 마감이 지난 신청 항목은 배제된다."""
        db_path = tmp_path / "expired.db"
        _create_announcements_table(db_path)

        _insert_one(
            db_path,
            source_id="expired",
            title="산림분야 오픈이노베이션 참여기업 모집 공고",
            url="https://example.com/expired",
            period_end="2026-03-20",
        )
        _insert_one(
            db_path,
            source_id="alive",
            title="임산물 가공유통 지원사업 신청 안내",
            url="https://example.com/alive",
            period_end="2026-04-10",
        )

        data = compose_digest_data(
            "%s" % db_path, week_str=W13, today=W13_TODAY
        )
        published = [item["url"] for item in data["sections"][VERDICT_APPLY]]
        expired = [
            item for item in data["excluded"] if item["reason"] == "마감 경과"
        ]

        assert published == ["https://example.com/alive"]
        assert [item["url"] for item in expired] == ["https://example.com/expired"]
        assert "[D-15] 임산물 가공유통 지원사업 신청 안내" in compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )

    def test_compose_digest_holds_are_comment_only(self, tmp_path):
        """판정 ⑨: 보류는 맨 아래 HTML 주석으로만 남고 발송 HTML에는 실리지 않는다."""
        db_path = tmp_path / "hold.db"
        _create_announcements_table(db_path)

        _insert_one(
            db_path,
            source_id="hold_1",
            # 관련성은 통과하지만 기회·제도 키워드가 없다 → 보류
            title="국유림 산림경영 현장 이야기",
            url="https://example.com/hold-1",
            period_end=None,
        )
        _insert_one(
            db_path,
            source_id="apply_1",
            title="임산물 가공유통 지원사업 신청 안내",
            url="https://example.com/apply-1",
        )

        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )

        assert "<!-- 보류: 1. 국유림 산림경영 현장 이야기 | 섹션 판정 불명 | id=" in markdown
        # 발송 HTML 변환은 주석 줄을 건너뛴다
        html = markdown_to_html(markdown)
        assert "국유림 산림경영 현장 이야기" not in html
        assert "임산물 가공유통 지원사업 신청 안내" in html

    def test_render_kakao_is_plaintext(self, sample_announcements):
        """⑧ 카톡 평문 렌더: 마크다운 링크·HTML 주석 없이 같은 틀."""
        data = compose_digest_data(
            sample_announcements, week_str=W13, today=W13_TODAY
        )
        text = render_kakao(data)

        assert text.startswith("📋 협의회 주간 정책브리핑 2026-W13 (3/23~3/29)")
        assert "이번 주 한 줄: (확정 필요)" in text
        assert "[원문](" not in text
        assert "<!--" not in text
        assert "https://example.com/kofpi/001" in text

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
        md_path.write_text("# 테스트\n\n[원문](https://example.com)")

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
        md_path.write_text("# 테스트\n\n[원문](https://example.com)")

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
        md_path.write_text("# 테스트\n\n[원문](https://example.com)")

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



def _bind_send_fixture(md_path):
    """발송 픽스처의 정본 사슬 — items.json + 대조 DB (통합 1 #1).

    통합 사이클 1 부터 발송기는 잠금 안에서 checker 와 **같은 정본 대조**를 다시
    돌린다. 그래서 발송 픽스처도 md·check.json 만으로는 부족하다.
    """
    from tests.test_digest_gate import _bind

    return str(_bind(md_path))


class TestSendDigest:
    """SendDigest 테스트."""

    def test_markdown_to_html_escaping(self):
        """HTML 이스케이프."""
        md = "# <script>alert(1)</script>"
        html = markdown_to_html(md)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_markdown_to_html_https_only(self):
        """앵커는 http/https 스킴일 때만 만든다."""
        md = "[test](https://example.com)"
        html = markdown_to_html(md)
        assert "https://example.com" in html

        md_js = "[test](javascript:alert(1))"
        html_js = markdown_to_html(md_js)
        # 개정: LINK_PATTERN 이 URL 스킴(http/https/www)을 요구하므로 javascript:
        # 는 링크 문법으로 읽히지 않는다. 안전 속성은 "앵커·href 가 생기지 않는다"
        # 이며, 남은 문자열은 이스케이프된 평문이다.
        assert "<a " not in html_js
        assert "href=" not in html_js

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
        md_path = tmp_path / "2026-W13.md"
        # 계약 v2.1 발송본 형식 — composer 가 만드는 형식만 항목으로 센다
        # (사이클 6 #1: `###`·`**원문:**` 는 산문으로 보고 fail-closed).
        body = (
            "# 테스트\n\n## ✅ 신청하세요 (마감순)\n\n"
            "<!-- item id=1 -->\n"
            "[상시] 공고 하나 — 기관 · 대상: 산림사업자 · 마감 연중 상시 모집\n"
            "  [원문](https://example.com)\n"
        )
        md_path.write_text(body)

        # check.json 파일은 markdown 파일 이름으로부터 자동 파생됨
        # 계약 W10: 검증은 그 본문의 해시를 남겨야 발송 게이트를 통과한다.
        check_path = md_path.with_suffix(".check.json")
        check_path.write_text(json.dumps({
            "items": [{"url": "https://example.com", "url_alive": True, "deadline_parsed": True, "passed": True}],
            "item_blocks": 1,
            "pass": True,
            "network_checked": True,
            "reason": "",
            "markdown_sha256": markdown_sha256(md_path.read_bytes()),
        }))
        db_path = _bind_send_fixture(md_path)

        # dry_run=True가 기본값이므로 발송 안 함
        result = send_digest(md_path, to_email="test@example.com", dry_run=True, db_path=db_path)

        # dry_run이므로 0 (성공)
        assert result == 0

    def test_markdown_to_html_link_sentinel_no_collision(self):
        """원문의 __LINK_0__ 리터럴이 링크로 둔갑하지 않는다 (M2)."""
        md = "__LINK_0__ 라는 텍스트 [진짜](https://ok.com)"
        html = markdown_to_html(md)

        assert html.count("<a ") == 1, html
        assert "__LINK_0__" in html

    def test_markdown_to_html_javascript_is_never_linked(self):
        """javascript: 는 앵커가 되지 않는다 (링크 문법으로 읽지 않는다)."""
        html = markdown_to_html("[클릭](javascript:alert(1))")

        assert "<a " not in html
        assert "href=" not in html
        # 링크가 아니므로 잔여 ')' 없이 원문 그대로 이스케이프되어 남는다
        assert "[클릭](javascript:alert(1))" in html

    def test_markdown_to_html_keeps_balanced_parens_in_url(self):
        """사이클 6 #4: URL 안의 괄호를 잘라먹지 않는다 (`…/report(2026)`)."""
        url = "https://example.com/report(2026)"
        html = markdown_to_html(f"[원문]({url})")

        assert f'href="{url}"' in html
        assert html.count("<a ") == 1

    def test_markdown_to_html_never_marks_up_inside_href(self):
        """사이클 6 #4: href 안에서는 `**` 를 마크업으로 해석하지 않는다."""
        url = "https://example.com/**bold**/x"
        html = markdown_to_html(f"[원문]({url}) 그리고 **강조**")

        assert f'href="{url}"' in html
        assert "<strong>bold</strong>" not in html
        # 링크 밖의 강조는 그대로 렌더된다
        assert "<strong>강조</strong>" in html

    def test_markdown_to_html_keeps_non_url_parenthesis(self):
        """`[모집](~9.30)` 같은 비URL 괄호는 링크가 아니라 본문 그대로 남는다.

        V1 사이클 5 잔여: 링크로 오인하면 발송 HTML에서 마감 표기가 조용히 사라졌다.
        """
        html = markdown_to_html("[모집](~9.30) 산림 공고 [원문](https://ok.example/x)")

        assert "[모집](~9.30)" in html
        assert html.count("<a ") == 1
        assert 'href="https://ok.example/x"' in html

    def test_main_dry_run_with_send_never_opens_smtp(self, tmp_path, monkeypatch):
        """--dry-run --send 동시 지정 시 SMTP 연결이 생성되지 않는다."""
        md_path = tmp_path / "2026-W14.md"
        # 계약 v2.1 발송본 형식 — composer 가 만드는 형식만 항목으로 센다
        # (사이클 6 #1: `###`·`**원문:**` 는 산문으로 보고 fail-closed).
        body = (
            "# 테스트\n\n## ✅ 신청하세요 (마감순)\n\n"
            "<!-- item id=1 -->\n"
            "[상시] 공고 하나 — 기관 · 대상: 산림사업자 · 마감 연중 상시 모집\n"
            "  [원문](https://example.com)\n"
        )
        md_path.write_text(body)
        check_path = md_path.with_suffix(".check.json")
        check_path.write_text(json.dumps({
            "items": [{"url": "https://example.com", "url_alive": True,
                       "deadline_parsed": True, "passed": True}],
            "item_blocks": 1,
            "pass": True,
            "network_checked": True,
            "reason": "",
            "markdown_sha256": markdown_sha256(md_path.read_bytes()),
        }))
        db_path = _bind_send_fixture(md_path)

        smtp_mock = mock.MagicMock()
        monkeypatch.setattr("alert.notifiers.email_sender.smtplib.SMTP", smtp_mock)
        monkeypatch.setattr(
            sys,
            "argv",
            ["send_digest.py", str(md_path), "--to", "a@b.c",
             "--db", db_path, "--dry-run", "--send"],
        )

        rc = send_digest_module.main()

        assert rc == 0
        assert smtp_mock.call_count == 0

    def test_send_to_override_replaces_config_recipients(self, tmp_path, monkeypatch):
        """--to 지정 시 config 수신자 2명이 아니라 제3자에게만 발송."""
        md_path = tmp_path / "2026-W15.md"
        # 계약 v2.1 발송본 형식 — composer 가 만드는 형식만 항목으로 센다
        # (사이클 6 #1: `###`·`**원문:**` 는 산문으로 보고 fail-closed).
        body = (
            "# 테스트\n\n## ✅ 신청하세요 (마감순)\n\n"
            "<!-- item id=1 -->\n"
            "[상시] 공고 하나 — 기관 · 대상: 산림사업자 · 마감 연중 상시 모집\n"
            "  [원문](https://example.com)\n"
        )
        md_path.write_text(body)
        check_path = md_path.with_suffix(".check.json")
        check_path.write_text(json.dumps({
            "items": [{"url": "https://example.com", "url_alive": True,
                       "deadline_parsed": True, "passed": True}],
            "item_blocks": 1,
            "pass": True,
            "network_checked": True,
            "reason": "",
            "markdown_sha256": markdown_sha256(md_path.read_bytes()),
        }))
        db_path = _bind_send_fixture(md_path)

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
        week = state_mod.week_from_markdown(md_path)
        seeded = state_mod.record_preview(
            state_mod.default_state(week), [2014], [],
            markdown_sha256(md_path.read_bytes()),
            markdown_sha256(check_path.read_bytes()),
        )
        state_mod.save_state(state_path, seeded)

        rc = send_digest(
            md_path, to_email="third@example.com", dry_run=False,
            approval_id=state_mod.approval_of(seeded)["id"],
            db_path=db_path,
        )

        assert rc == 0
        assert sent["to"] == "third@example.com"

    def test_html_escapes_db_payloads(self, tmp_path):
        """DB 페이로드가 HTML에 raw로 새지 않는다.

        v2.1에서 author는 아예 렌더되지 않고(판정 ⑤), 파싱 불가한 period_end도
        표시되지 않는다 — 그래도 제목 경로의 이스케이프는 살아 있어야 한다.
        """
        db_path = tmp_path / "xss.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            title="<script>alert(1)</script> 산림 지원사업 모집 공고",
            author="<img src=x onerror=1>",
            period_end="<svg onload=1>",
        )

        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )
        html = markdown_to_html(markdown)

        # raw 태그가 실제 태그로 파싱되지 않아야 함
        assert "<script" not in html
        # 개정 v2.5 (#2): 제목의 꺾쇠는 전각으로 바뀌어 태그가 만들어지지 않는다
        assert "＜script＞" in html
        # author·파싱 불가 마감은 렌더 경로에 아예 들어오지 않는다
        assert "onerror" not in html
        assert "onload" not in html

        parser = _TagCollector()
        parser.feed(html)
        assert "script" not in parser.tags
        assert "img" not in parser.tags
        assert "svg" not in parser.tags
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
            week_str=W13,
            output_path=md_path,
            today=W13_TODAY,
        )

        assert MARKER in markdown

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

    def test_seis_nine_duplicate_rows_collapse_to_one(self, tmp_path):
        """W37 실측 결함: seis가 한 공고의 링크를 9행으로 저장한다 → 발송본은 1건."""
        db_path = tmp_path / "seis.db"
        _create_announcements_table(db_path)

        for i in range(9):
            _insert_one(
                db_path,
                source="seis",
                source_id=f"seis_{i}",
                title="2026년 경기도 사회적기업 사회보험료 지원사업 참여기업 모집 공고",
                url=f"https://www.seis.or.kr/subPage.do?fncPbofrSn={8371 + i}",
                period_end=None,
            )

        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )
        body = _section_body(markdown, APPLY_HEADING)

        assert _count_items(body) == 1
        assert body.count("사회보험료 지원사업 참여기업 모집 공고") == 1
        # 대표 링크는 1개만 남는다
        assert markdown.count("https://www.seis.or.kr/subPage.do") == 1


class TestDeadUrlDrop:
    """계약 v1.2: url_alive=false 항목 자동 제외 + dropped 기록."""

    # 소스 다양성 상한(2)에 걸리지 않도록 소스를 나눈 신청 항목 3건
    SEED_ROWS = (
        ("kofpi", "산림분야 오픈이노베이션 참여기업 모집 공고"),
        ("fowi", "임산물 가공유통 지원사업 신청 안내"),
        ("coop", "산촌 목재 이용 설명회 참여업체 모집"),
    )
    SEED_TITLES = tuple(title for _source, title in SEED_ROWS)

    @classmethod
    def _seed_three(cls, db_path):
        """2026-W13 범위의 신청 항목 3건."""
        _create_announcements_table(db_path)
        for i, (source, title) in enumerate(cls.SEED_ROWS):
            _insert_one(
                db_path,
                source=source,
                source_id=f"test_{i:03d}",
                title=title,
                url=f"https://example.com/item-{i}",
                period_end=f"2026-12-2{i}",
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
        assert check["dropped"][0]["title"] == self.SEED_TITLES[1]

        # 산출물에는 살아있는 2건만 남는다
        assert dead not in markdown
        assert "https://example.com/item-0" in markdown
        assert "https://example.com/item-2" in markdown
        assert _count_items(_section_body(markdown, APPLY_HEADING)) == 2

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

    def _seed_six(self, db_path):
        _create_announcements_table(db_path)
        for i, (source, title) in enumerate(SPREAD_APPLY_ROWS):
            _insert_one(
                db_path,
                source=source,
                source_id=f"test_{i:03d}",
                title=title,
                url=f"https://example.com/s-{i}",
                period_end=f"2026-12-2{i}",
            )

    def test_exclude_urls_backfills_up_to_section_limit(self, tmp_path):
        """제외 후 섹션 상한이 남은 후보로 다시 채워진다."""
        db_path = tmp_path / "compose.db"
        self._seed_six(db_path)

        full = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )
        assert _count_items(_section_body(full, APPLY_HEADING)) == 5
        assert "https://example.com/s-5" not in full

        trimmed = compose_digest(
            db_path=str(db_path),
            week_str=W13,
            exclude_urls={"https://example.com/s-0"},
            today=W13_TODAY,
        )
        # 제외 1건 → 다음 순위(s-5)가 채워져 여전히 상한 5건
        assert _count_items(_section_body(trimmed, APPLY_HEADING)) == 5
        assert "https://example.com/s-0" not in trimmed
        assert "https://example.com/s-5" in trimmed

    def test_exclude_urls_never_exceeds_section_limit(self, tmp_path):
        """제외 후에도 섹션 상한을 넘지 않는다."""
        db_path = tmp_path / "limit.db"
        _create_announcements_table(db_path)
        notice_titles = (
            "산림재난방지법 시행령 일부개정령안 입법예고",
            "산지관리법 시행령 일부개정령안 행정예고",
            "임업·산림 공익직접지불제도 고시 개정",
            "산림복지 진흥에 관한 법률 시행규칙 개정 예고",
        )
        for i, title in enumerate(notice_titles):
            _insert_one(
                db_path,
                source_id=f"forest_{i:03d}",
                source="lawmaking",
                title=title,
                url=f"https://example.com/f-{i}",
                period_end=None,
                period_start=f"2026-03-2{i}",
            )

        markdown = compose_digest(
            db_path=str(db_path),
            week_str=W13,
            exclude_urls={"https://example.com/f-0"},
            today=W13_TODAY,
        )
        # 알아두세요 상한 3, 후보 4건 중 1건 제외 → 상한대로 3건
        assert _count_items(_section_body(markdown, NOTICE_HEADING)) == 3
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


class TestRulesV21:
    """룰 v2.1 순수 함수 — 2026-W37 실데이터 사례 (확정안 §4 + 판정 ①②③⑤⑦)."""

    @pytest.mark.parametrize(
        "source,title,summary,reason_head",
        [
            # 협의회 소스지만 노이즈 사전에 걸린다
            (
                "forest_press",
                "청양산림항공관리소, 논산시 꿈빛나래 페스티벌 진로체험 부스 운영",
                "청소년 진로체험 박람회에 참가해 진로체험 부스를 운영했다",
                "노이즈",
            ),
            ("fowi", "추석 명절 청탁금지법 선물 바로알기", "", "노이즈"),
            ("kofpi", "2026년 상반기 채용 최종합격자 발표", "", "노이즈"),
            ("smartfarm", "스마트팜코리아 시스템 작업 안내", "", "협의회 소스 풀 외"),
            # 협의회 소스 풀 밖 + 제목에 사회적경제 정체성 키워드 없음
            (
                "ipet",
                "스마트팜 다부처 패키지 혁신기술개발 후속사업 기획 및 "
                "스마트팜 고도화를 위한 현장 의견 조사",
                "",
                "협의회 소스 풀 외",
            ),
            (
                "nongup_gg",
                "2026 치유농업센터 운영 시설물관리원(기간제근로자) 채용 최종합격자 결정 공고",
                "",
                "협의회 소스 풀 외",
            ),
            # 판정 ③ 단서: "치유"는 산림치유로만, "조경"은 회사 축이라 제외
            ("kofpi", "치유농업 프로그램 참가자 모집", "", "관련성 없음"),
            ("kofpi", "조경 공사 입찰 공고", "", "관련성 없음"),
        ],
    )
    def test_excluded_cases(self, source, title, summary, reason_head):
        result = classify_item(title, summary, source)
        assert result.verdict == VERDICT_EXCLUDE, result
        assert result.reason.startswith(reason_head), result

    def test_lawmaking_goes_to_notice(self):
        """산림재난방지법 입법예고 → 알아두세요 + 산림사업자."""
        result = classify_item(
            "산림재난방지법 시행령 일부개정령안 입법예고", "", "lawmaking"
        )
        assert result.verdict == VERDICT_NOTICE
        assert result.tags == ("산림사업자",)
        assert "입법예고" in result.matched

    def test_coop_consulting_goes_to_apply_with_tags(self):
        """협동조합 컨설팅 모집 → 신청하세요 + 대상 사협·사회적기업."""
        result = classify_item(
            "2026 사회적협동조합·사회적기업 대상 공공조달 및 민간위탁 "
            "1:1 맞춤형 컨설팅」 참여기업 모집 공고",
            "",
            "coop",
        )
        assert result.verdict == VERDICT_APPLY
        assert result.tags == ("사협", "사회적기업")
        assert result.region is None

    def test_non_council_source_passes_only_with_identity_in_title(self):
        """판정 ②의 예외: 제목에 사회적경제 정체성 키워드가 있으면 통과."""
        result = classify_item(
            "2026년 농업농촌형 예비사회적기업 모집 공고", "", "mafra"
        )
        assert result.verdict == VERDICT_APPLY
        assert "사회적기업" in result.tags

    def test_relevant_but_untaggable_goes_to_hold(self):
        """대상 태그를 못 붙이면 보류 (판정 ①)."""
        result = classify_item("자활기업 성장 지원 안내", "", "coop")
        assert result.verdict == VERDICT_HOLD
        assert result.reason == "대상 태그 없음"
        assert result.tags == ()

    def test_relevant_without_section_signal_goes_to_hold(self):
        """관련성은 통과했으나 기회·제도 신호가 없으면 보류."""
        result = classify_item(
            "국립새만금수목원, 지역민과 함께 만드는 산림 이야기", "", "forest_press"
        )
        assert result.verdict == VERDICT_HOLD
        assert result.reason == "섹션 판정 불명"

    def test_region_prefers_title_body(self):
        """제목 본문의 시·도가 1순위 (판정 ① + 개정 v2.4 (b))."""
        gyeonggi = classify_item(
            "2026년 경기도 사회적기업 사회보험료 지원사업 참여기업 모집 공고",
            "",
            "seis",
        )
        assert gyeonggi.region == "경기"

        gangwon = classify_item(
            "[경기강원센터] 강원(춘천 권역) 사회적경제 성장사다리 프로그램 "
            "참여기업 모집",
            "",
            "coop",
        )
        assert gangwon.region == "강원"

    def test_infer_target_tags_separates_coop_kinds(self):
        assert infer_target_tags("사회적협동조합 설립인가 교육") == ("사협",)
        assert infer_target_tags("일반협동조합 설립신고 안내") == ("협동조합",)
        assert infer_target_tags("마을기업 지정 공고") == ("마을기업",)
        assert infer_target_tags("사회적경제 성장사다리 프로그램") == ("전체",)
        assert infer_target_tags("임산물 유통 지원") == ("산림사업자",)

    def test_source_display_name_replaces_author(self):
        assert source_display_name("kofpi") == "한국임업진흥원"
        assert source_display_name("lawmaking") == "국민참여입법센터(산림청 소관)"
        # 미등록 소스는 조용히 비우지 않는다
        assert source_display_name("unknown_source") == "unknown_source"

    def test_extract_quotes_only_literal_spans(self):
        """판정 ⑦: 원문에서 문자 그대로 찾을 수 있을 때만 값, 아니면 "원문 확인"."""
        raw = (
            '{"title": "산림재난방지법 시행령 일부개정령안 입법예고", '
            '"link": "/gcom/ogLmPp/88388?isOgYn=Y", '
            '"period": "2026. 9. 7. ~2026. 10. 19."}'
        )
        quotes = extract_quotes("", raw)
        # 사이클 12 #3: 마감은 인용에서 뽑지 않는다 (근거는 DB period_end 뿐)
        assert "quote_deadline" not in quotes
        assert quotes["quote_eligibility"] == "원문 확인"
        assert quotes["quote_amount"] == "원문 확인"

        empty = extract_quotes("", "")
        assert set(empty.values()) == {"원문 확인"}

    def test_extract_quotes_ignores_link_fields(self):
        """링크 안의 숫자가 금액·마감으로 둔갑하지 않는다."""
        raw = '{"link": "subPage.do?fncPbofrSn=8371&page=9/30", "title": "모집 공고"}'
        quotes = extract_quotes("", raw)
        assert "quote_deadline" not in quotes
        assert quotes["quote_amount"] == "원문 확인"

    def test_jaccard_merges_identical_and_keeps_distinct(self):
        """판정 ⑥의 임계값 0.6."""
        same = jaccard(
            title_ngrams("산림재난방지법 시행령 일부개정령안 입법예고"),
            title_ngrams("산림재난방지법 시행령 일부개정령안 입법예고"),
        )
        assert same == 1.0

        different = jaccard(
            title_ngrams("산림재난방지법 시행령 일부개정령안 입법예고"),
            title_ngrams("산지관리법 시행령 일부개정령안 입법예고"),
        )
        assert different < 0.6, different

    def test_dedup_key_drops_brackets_dates_symbols(self):
        """괄호·날짜·기호를 지운 뒤 비교한다 (판정 ⑥)."""
        assert jaccard(
            title_ngrams("「2026년 배출권거래제 외부사업 등록·인증 지원」 참여자 추가모집 공고"),
            title_ngrams("2026년 배출권거래제 외부사업 등록 인증 지원 참여자 추가모집 공고(~9.7)"),
        ) >= 0.6


class TestFixCycle1:
    """개정 v2.2 (판단 티어 판정 F1·F2·F4)."""

    # ─── F1: 사업자 모집 vs 참가자 모집 ─────────────────────────────────
    @pytest.mark.parametrize(
        "title",
        [
            "2026년「건강출산 행복가정 지원사업」사업 수시모집 공고문(숲동행-10월)",
            "2026년 4분기 2차 국립횡성숲체원 「나눔의 숲 캠프」모집 공고",
            "2026년 산림복지 취업아카데미 참가 모집(~9.16.)",
        ],
    )
    def test_b2c_recruitment_goes_to_hold(self, title):
        result = classify_item(title, "", "fowi")
        assert result.verdict == VERDICT_HOLD, result
        assert result.reason.startswith("참가자 모집(B2C)"), result

    @pytest.mark.parametrize(
        "source,title",
        [
            ("kofpi", "[모집] 2026 산림분야 오픈이노베이션 참여기업 모집(~9.30)"),
            (
                "fowi",
                "[모집]「2026년 산림복지전문업 통합 온라인 플랫폼 입점 지원사업」"
                "참가기업 모집 연장(~9.14.)",
            ),
            ("coop", "사회적협동조합 공공조달 1:1 컨설팅 참여기업 모집 공고"),
        ],
    )
    def test_business_recruitment_stays_in_apply(self, source, title):
        """사업자 신호가 하나라도 있으면 B2C 신호와 공존해도 신청하세요."""
        result = classify_item(title, "", source)
        assert result.verdict == VERDICT_APPLY, result

    def test_b2c_hold_is_recoverable_not_excluded(self):
        """보류는 배제가 아니다 — 미리보기 보류 목록에 남아 `핀 n`으로 복구된다."""
        result = classify_item(
            "2026년 4분기 국립춘천숲체원 「나눔의 숲」 캠프 모집 공고", "", "fowi"
        )
        assert result.verdict != VERDICT_EXCLUDE
        assert result.tags == ("산림사업자",)

    # ─── F2: 정체성 우선 정렬 + 소스 다양성 상한 ────────────────────────
    def test_identity_announcement_survives_apply_cap(self, tmp_path):
        """정체성 공고가 게시일 며칠 차이로 상한에서 밀려나지 않는다."""
        db_path = tmp_path / "identity.db"
        _create_announcements_table(db_path)

        # 게시일이 더 최신인 비정체성 공고 3건 (다른 소스로 분산)
        fresher = (
            ("fowi", "숲체험교육사업 참여업체 정기모집 공고", "2026-03-25"),
            ("fowi", "임산물 가공유통 지원 참여기업 모집", "2026-03-25"),
            ("kofpi", "목재산업 시설 개선 지원사업 참여업체 모집", "2026-03-24"),
            ("kofpi", "산촌 자원 활용 시제품 개발 지원 참여기업 모집", "2026-03-24"),
            ("coop", "숲가꾸기 사업자 조달 계약 공모", "2026-03-24"),
        )
        for index, (source, title, posted) in enumerate(fresher):
            _insert_one(
                db_path,
                source=source,
                source_id=f"fresh_{index}",
                title=title,
                url=f"https://example.com/fresh-{index}",
                period_end=None,
                period_start=posted,
            )

        # 게시일이 더 이른 정체성 공고
        _insert_one(
            db_path,
            source="forest_service",
            source_id="identity_1",
            title="2026년도 제2차 산림형 예비사회적기업 지정 계획 공고",
            url="https://example.com/identity-1",
            period_end=None,
            period_start="2026-03-23",
        )

        data = compose_digest_data(
            str(db_path), week_str=W13, today=W13_TODAY
        )
        published = data["sections"][VERDICT_APPLY]

        assert published[0]["url"] == "https://example.com/identity-1"
        assert published[0]["identity_priority"] == 0

    def test_source_diversity_limit_pushes_overflow_to_hold(self, tmp_path):
        """같은 소스는 신청 섹션에 최대 2건, 초과분은 보류."""
        db_path = tmp_path / "diversity.db"
        _create_announcements_table(db_path)

        titles = (
            "산림분야 오픈이노베이션 참여기업 모집 공고",
            "임산물 가공유통 지원사업 참여업체 모집",
            "목재산업 시설 개선 지원 참여기업 모집",
            "숲가꾸기 사업자 조달 계약 공모",
        )
        for index, title in enumerate(titles):
            _insert_one(
                db_path,
                source="kofpi",
                source_id=f"div_{index}",
                title=title,
                url=f"https://example.com/div-{index}",
                period_end=f"2026-12-2{index}",
            )

        data = compose_digest_data(
            str(db_path), week_str=W13, today=W13_TODAY
        )

        assert len(data["sections"][VERDICT_APPLY]) == SOURCE_DIVERSITY_LIMIT
        overflow = [
            item
            for item in data["holds"]
            if item["reason"] == HOLD_REASON_DIVERSITY
        ]
        assert len(overflow) == 2
        assert [item["url"] for item in overflow] == [
            "https://example.com/div-2",
            "https://example.com/div-3",
        ]

    def test_diversity_overflow_appears_in_hold_comments(self, tmp_path):
        db_path = tmp_path / "diversity_md.db"
        _create_announcements_table(db_path)
        for index in range(3):
            _insert_one(
                db_path,
                source="kofpi",
                source_id=f"div_{index}",
                title=(
                    "산림분야 오픈이노베이션 참여기업 모집 공고",
                    "임산물 가공유통 지원사업 참여업체 모집",
                    "목재산업 시설 개선 지원 참여기업 모집",
                )[index],
                url=f"https://example.com/divmd-{index}",
                period_end=f"2026-12-2{index}",
            )

        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )
        assert "소스 다양성 상한(같은 소스 2건) | id=" in markdown

    # ─── F4: 같은 제목 다른 회차 ────────────────────────────────────────
    def test_same_title_different_deadline_not_merged(self, tmp_path):
        """같은 제목이라도 인정된 마감이 다르면 다른 회차 — 병합하지 않는다."""
        db_path = tmp_path / "rounds.db"
        _create_announcements_table(db_path)

        for index, (period_end, posted) in enumerate(
            [("2026-10-19", "2026-03-26"), ("2026-09-16", "2026-03-24")]
        ):
            _insert_one(
                db_path,
                source="lawmaking",
                source_id=f"round_{index}",
                title="산림재난방지법 시행령 일부개정령안 입법예고",
                url=f"https://example.com/round-{index}",
                period_end=period_end,
                period_start=posted,
            )

        data = compose_digest_data(
            str(db_path), week_str=W13, today=W13_TODAY
        )
        notices = data["sections"][VERDICT_NOTICE]

        assert len(notices) == 2
        assert {item["url"] for item in notices} == {
            "https://example.com/round-0",
            "https://example.com/round-1",
        }

    def test_same_title_same_deadline_merges_into_one(self, tmp_path):
        """제목·마감이 완전히 같으면 병합하고, 다르면 병합하지 않는다 (사이클 10 #1)."""
        db_path = tmp_path / "rep.db"
        _create_announcements_table(db_path)

        for source_id in ("rep_a", "rep_b"):
            _insert_one(
                db_path,
                source="kofpi",
                source_id=source_id,
                title="산림분야 오픈이노베이션 참여기업 모집 공고",
                url=f"https://example.com/{source_id}",
                period_end="2026-12-20",
                period_start="2026-03-23",
            )

        data = compose_digest_data(
            str(db_path), week_str=W13, today=W13_TODAY
        )
        published = data["sections"][VERDICT_APPLY]
        assert len(published) == 1
        assert published[0]["deadline"] == "2026-12-20"
        assert len(data["merged_ids"]) == 1

    def test_one_sided_deadline_is_a_different_round(self, tmp_path):
        """한쪽만 마감이 있으면 **다른 회차**다 — 병합하지 않는다 (사이클 10 #1).

        예전에는 그룹 마감을 최대값으로 합쳐 한 건으로 만들었다. 마감이 다르면
        서로 다른 회차일 수 있으므로 둘 다 싣고 편집자가 판단한다.
        """
        db_path = tmp_path / "rep2.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path, source="kofpi", source_id="rep_none",
            title="산림분야 오픈이노베이션 참여기업 모집 공고",
            url="https://example.com/rep-none",
            period_end=None, period_start="2026-03-24",
        )
        _insert_one(
            db_path, source="kofpi", source_id="rep_open",
            title="산림분야 오픈이노베이션 참여기업 모집 공고",
            url="https://example.com/rep-open",
            period_end="2026-12-20", period_start="2026-03-23",
        )
        data = compose_digest_data(
            str(db_path), week_str=W13, today=W13_TODAY
        )
        published = data["sections"][VERDICT_APPLY]
        assert len(published) == 2
        assert data["merged_ids"] == []

    def test_posting_date_artifact_does_not_fake_a_new_round(self, tmp_path):
        """period_start == period_end(게시일 위장)는 "다른 회차"가 되지 못한다."""
        db_path = tmp_path / "artifact.db"
        _create_announcements_table(db_path)

        for index, posted in enumerate(["2026-03-25", "2026-03-24"]):
            _insert_one(
                db_path,
                source="fowi",
                source_id=f"art_{index}",
                title="숲체험교육사업 참여업체 정기모집 공고",
                url=f"https://example.com/art-{index}",
                period_start=posted,
                period_end=posted,
            )

        data = compose_digest_data(
            str(db_path), week_str=W13, today=W13_TODAY
        )
        assert len(data["sections"][VERDICT_APPLY]) == 1


class TestFixCycle2:
    """개정 v2.3 (판단 티어 판정 G1·G2)."""

    # ─── G1: "참여자"는 중립어 ──────────────────────────────────────────
    @pytest.mark.parametrize(
        "title",
        [
            "「2026년 산림부문 배출권거래제 외부사업 등록·인증 지원」 참여자 추가모집 공고",
            "[모집] 산양삼 등 산촌자원 활용 시제품 개발 지원 참여자 모집(연중 상시 모집)",
        ],
    )
    def test_participant_wording_no_longer_blocks_business_notices(self, title):
        result = classify_item(title, "", "kofpi")
        assert result.verdict == VERDICT_APPLY, result

    @pytest.mark.parametrize(
        "title",
        [
            "2026년「건강출산 행복가정 지원사업」사업 수시모집 공고문(숲동행-10월)",
            "2026년 4분기 2차 국립횡성숲체원 「나눔의 숲 캠프」모집 공고",
            "2026년 산림복지 취업아카데미 참가 모집(~9.16.)",
        ],
    )
    def test_b2c_recruitment_still_held(self, title):
        result = classify_item(title, "", "fowi")
        assert result.verdict == VERDICT_HOLD, result
        assert result.reason.startswith("참가자 모집(B2C)"), result

    # ─── G2: 다양성 사유 vs 상한 초과 사유 ──────────────────────────────
    DIVERSITY_ROWS = (
        ("kofpi", "산림분야 오픈이노베이션 참여기업 모집 공고"),
        ("kofpi", "임산물 가공유통 지원사업 참여업체 모집"),
        ("kofpi", "목재산업 시설 개선 지원 참여기업 모집"),
        ("fowi", "산림복지전문업 플랫폼 입점 지원사업 모집"),
        ("fowi", "숲길 운영 위탁 사업자 공모"),
        ("coop", "사회적협동조합 공공조달 컨설팅 참여기업 모집"),
        ("coop", "협동조합 유통채널 입점 설명회 참여업체 모집"),
        ("forest_service", "산림형 예비사회적기업 지정 계획 공고"),
    )

    def _seed(self, db_path):
        _create_announcements_table(db_path)
        for index, (source, title) in enumerate(self.DIVERSITY_ROWS):
            _insert_one(
                db_path,
                source=source,
                source_id=f"g2_{index}",
                title=title,
                url=f"https://example.com/g2-{index}",
                period_end=f"2026-12-{20 + index}",
            )

    def test_diversity_reason_only_for_items_pushed_out_by_diversity(
        self, tmp_path
    ):
        db_path = tmp_path / "g2.db"
        self._seed(db_path)

        data = compose_digest_data(
            str(db_path), week_str=W13, today=W13_TODAY
        )
        published = [item["url"] for item in data["sections"][VERDICT_APPLY]]
        reasons = {
            item["url"]: item["reason"]
            for item in data["holds"]
            if item["reason"]
            in (HOLD_REASON_DIVERSITY, HOLD_REASON_SECTION_CAP)
        }

        # 상한 5칸: kofpi 2 + fowi 2 + coop 1
        assert published == [
            "https://example.com/g2-0",
            "https://example.com/g2-1",
            "https://example.com/g2-3",
            "https://example.com/g2-4",
            "https://example.com/g2-5",
        ]
        # kofpi 3번째만 다양성 때문에 밀렸다
        assert reasons["https://example.com/g2-2"] == HOLD_REASON_DIVERSITY
        # 상한이 찬 뒤에 남은 항목은 다양성 사유를 달지 않는다
        assert reasons["https://example.com/g2-6"] == HOLD_REASON_SECTION_CAP
        assert reasons["https://example.com/g2-7"] == HOLD_REASON_SECTION_CAP

    def test_cap_overflow_is_recorded_not_silently_dropped(self, tmp_path):
        db_path = tmp_path / "g2md.db"
        self._seed(db_path)

        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )
        assert _count_items(_section_body(markdown, APPLY_HEADING)) == 5
        assert f"| {HOLD_REASON_DIVERSITY} | id=" in markdown
        assert f"| {HOLD_REASON_SECTION_CAP} | id=" in markdown
        assert markdown.count(f"| {HOLD_REASON_SECTION_CAP} | id=") == 2


class TestFixCycle3:
    """개정 v2.4 (콜드리드 r2 판정 a·b·c)."""

    # ─── (a) 마감 표기 3단계 ────────────────────────────────────────────
    def test_deadline_display_three_tiers(self, tmp_path):
        db_path = tmp_path / "c3.db"
        _create_announcements_table(db_path)

        _insert_one(
            db_path,
            source="kofpi",
            source_id="tier1",
            title="산림분야 오픈이노베이션 참여기업 모집 공고",
            url="https://example.com/tier1",
            period_start="2026-03-20",
            period_end="2026-09-30",
        )
        _insert_one(
            db_path,
            source="fowi",
            source_id="tier2",
            title="산림복지전문업 플랫폼 입점 지원사업 모집",
            url="https://example.com/tier2",
            period_start="2026-03-24",
            period_end=None,
        )
        _insert_one(
            db_path,
            source="coop",
            source_id="tier3",
            title="사회적협동조합 공공조달 컨설팅 참여기업 모집",
            url="https://example.com/tier3",
            period_start=None,
            period_end=None,
        )

        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )

        assert "· 마감 9/30" in markdown
        assert "[D-188]" in markdown
        assert "· 접수 3/24부터, 마감 미정" in markdown
        assert "· 마감 미정" in markdown
        assert "원문 확인" not in markdown

    def test_text_derived_deadline_is_never_rendered(self, tmp_path):
        """summary·raw_data 의 날짜는 마감으로 쓰지 않는다 (사이클 12 #3).

        Codex 8차 HIGH #3: 텍스트에서 유도한 마감은 정본 대조가 검증할 수 없다 —
        DB 를 `~9.12` 로 바꿔도 `9/30` 렌더가 그대로 통과했다. 근거는 DB
        `period_end` 뿐이고, 모르면 "미정" 이라고 쓴다(창작 금지).
        """
        db_path = tmp_path / "c3quote.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="fowi",
            source_id="quoted",
            title="산림복지전문업 플랫폼 입점 지원사업 참가기업 모집 연장",
            url="https://example.com/quoted",
            period_start="2026-03-24",
            period_end=None,
            summary="접수기간: 2026-03-24 ~2026-09-30",
            raw_data=(
                '{"title": "산림복지전문업 플랫폼 입점 지원사업 '
                '참가기업 모집 연장(~9.14.)"}'
            ),
        )

        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )
        assert "· 접수 3/24부터, 마감 미정" in markdown
        for derived in ("~9.14.", "9/30", "2026-09-30", "원문 확인"):
            assert derived not in markdown, derived

    def test_notice_keeps_opinion_deadline_form(self, tmp_path):
        db_path = tmp_path / "c3notice.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="lawmaking",
            source_id="notice",
            title="산림재난방지법 시행령 일부개정령안 입법예고",
            url="https://example.com/notice",
            period_start="2026-03-24",
            period_end="2026-10-19",
        )

        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )
        assert "· 의견 10/19까지" in markdown

    # ─── (b) 지역 태그 ──────────────────────────────────────────────────
    def test_center_prefix_yields_broad_region(self):
        """`[세종대전충청센터]`처럼 여러 시·도가 나열되면 권역명이 붙는다."""
        result = classify_item(
            "[세종대전충청센터] (예비)사회적기업 인·지정 설명회 안내", "", "socialenterprise"
        )
        assert result.region == "충청"
        assert target_display(result.tags, result.region) == "사회적기업(충청)"

    def test_broad_region_in_title_body_wins(self):
        result = classify_item(
            "[세종대전충청센터] 2026년 9월 충청권역 (예비)사회적기업 인 · 지정 "
            "설명회 안내('26.09.30.(수) 14:00, 대전)",
            "",
            "socialenterprise",
        )
        assert result.region == "충청"

    def test_multiple_regions_without_broad_name_is_unassigned(self):
        """여러 시·도가 섞였고 권역명이 없으면 붙이지 않는다 (틀린 지역 주장 금지)."""
        result = classify_item(
            "[부산울산경남센터] 사회적협동조합 설립인가 및 경영공시 교육 참여기업 모집",
            "",
            "coop",
        )
        assert result.region is None

    def test_region_appears_in_target_field(self, tmp_path):
        db_path = tmp_path / "c3region.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="seis",
            source_id="region",
            title="2026년 경기도 사회적기업 사회보험료 지원사업 참여기업 모집 공고",
            url="https://example.com/region",
            period_end="2026-12-31",
        )
        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )
        assert "대상: 사회적기업(경기)" in markdown

    # ─── (c) 빈 섹션 생략 ───────────────────────────────────────────────
    def test_empty_council_section_is_omitted(self, sample_announcements):
        markdown = compose_digest(
            db_path=sample_announcements, week_str=W13, today=W13_TODAY
        )
        kakao = render_kakao(
            compose_digest_data(
                sample_announcements, week_str=W13, today=W13_TODAY
            )
        )
        for text in (markdown, kakao):
            assert "협의회에서" not in text
            assert "회원사 소식" not in text
            assert "이번 주 기록 없음" not in text

    def test_council_section_appears_when_filled(self, sample_announcements):
        data = compose_digest_data(
            sample_announcements, week_str=W13, today=W13_TODAY
        )
        data["council_notes"] = ["산림청 면담 1회 (9/10)"]
        markdown = render_markdown(data)
        kakao = render_kakao(data)
        for text in (markdown, kakao):
            assert "협의회에서" in text
            assert "· 산림청 면담 1회 (9/10)" in text

    # ─── (d) 마커 불변 ──────────────────────────────────────────────────
    def test_headline_marker_unchanged(self, sample_announcements):
        markdown = compose_digest(
            db_path=sample_announcements, week_str=W13, today=W13_TODAY
        )
        assert markdown.count(MARKER) == 1
        assert f"이번 주 한 줄: {MARKER}" in markdown


W37 = "2026-W37"
W37_CREATED_AT = "2026-09-10T12:00:00"
W37_TODAY = date(2026, 9, 13)


class TestFixCycle4:
    """개정 v2.5 — Codex 실크리틱 14건의 재현 입력 고정 (#1은 main 레인 몫)."""

    # ─── #2 제목 링크 주입 / 죽은 링크 잔존 ─────────────────────────────
    def test_title_markdown_link_is_not_rendered(self, tmp_path):
        db_path = tmp_path / "c4_2.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="kofpi",
            source_id="inject",
            title="산림 지원사업 [신청](https://example.com/dead) 모집",
            url="https://example.com/live",
            period_end="2026-12-31",
        )

        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )

        assert "https://example.com/dead" not in markdown
        assert "산림 지원사업 신청 모집" in markdown
        assert extract_item_urls(markdown) == ["https://example.com/live"]

    def test_sanitize_title_neutralizes_links_and_tags(self):
        assert sanitize_title("A [신청](https://x/y) B") == "A 신청 B"
        assert sanitize_title("<https://x/y> 공고") == "https://x/y 공고"
        assert sanitize_title("<img src=x onerror=1> 공고") == (
            "＜img src=x onerror=1＞ 공고"
        )
        # 사이클 9 #6: 선두 대괄호는 **원문 그대로** 둔다 — 링크 판정은 괄호 내용이
        # URL 일 때만이고, 라벨 흉내는 items.json 제목 대조가 잡는다.
        assert sanitize_title("[모집] 산림 공고(~9.30)") == "[모집] 산림 공고(~9.30)"

    def test_sanitize_title_neutralizes_leading_structure(self):
        """DB 제목이 문서 구조를 바꾸지 못한다 (사이클 8 #2 / Codex 4차 HIGH #3)."""
        assert sanitize_title("## 산림 제도 개정") == "＃＃ 산림 제도 개정"
        assert sanitize_title("*산림 제도 개정") == "＊산림 제도 개정"
        assert sanitize_title("- 산림 제도 개정") == "－ 산림 제도 개정"
        assert sanitize_title("> 인용 제목") == "＞ 인용 제목"
        assert sanitize_title("+ 더하기 제목") == "＋ 더하기 제목"
        assert sanitize_title("1. 산림 제도 개정") == "1． 산림 제도 개정"
        # 선두 대괄호는 중화하지 않는다 (사이클 9 #6) — 라벨 흉내는 정본 대조가 잡는다
        assert sanitize_title("[D-9] 위조 라벨 제목") == "[D-9] 위조 라벨 제목"
        # 항목 줄 구분자와 겹치는 em dash 는 en dash 로 (제목/꼬리 경계 보호)
        assert sanitize_title("산림 — 제도 개정") == "산림 – 제도 개정"
        # 선두가 아닌 기호는 손대지 않는다
        assert sanitize_title("산림 제도 개정 * 추가") == "산림 제도 개정 * 추가"

    def test_extract_item_urls_only_reads_origin_links(self):
        markdown = (
            "## ✅ 신청하세요 (마감순)\n\n"
            "<!-- item id=1 -->\n"
            "산림 지원사업 [신청](https://example.com/dead) 모집 — 기관\n"
            "  [원문](https://example.com/live)\n"
        )
        assert extract_item_urls(markdown) == ["https://example.com/live"]
        assert "https://example.com/dead" in body_links(markdown)

    def test_item_needs_the_structural_marker(self):
        """사이클 7: 마커가 없으면 모양이 맞아도 항목이 아니다 (Codex 3차 #2)."""
        markdown = (
            "## ✅ 신청하세요 (마감순)\n\n"
            "산림 지원사업 참여기업 모집 — 기관 · 마감 9/22\n"
            "  [원문](https://example.com/live)\n"
        )
        assert extract_item_urls(markdown) == []
        assert blocks_mod.item_block_count(markdown) == 0
        # 그 줄은 "항목 섹션의 산문"으로 잡힌다
        assert blocks_mod.prose_lines_in_item_sections(markdown)

    def test_residual_dropped_url_in_body_fails_closed(
        self, tmp_path, monkeypatch
    ):
        """재검사 루프가 못 본 죽은 링크가 본문에 남으면 pass=false."""
        import scripts.weekly_digest as weekly_digest

        db_path = tmp_path / "c4_residual.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="kofpi",
            source_id="live",
            title="산림 지원사업 참여기업 모집 공고",
            url="https://example.com/live",
        )
        out_dir = tmp_path / "out"

        def fake_compose(**kwargs):
            path = Path(kwargs["output_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "# 테스트\n\n"
                "## ✅ 신청하세요 (마감순)\n\n"
                "산림 지원사업 참여기업 모집 — 한국임업진흥원 · 마감 미정\n"
                "  [원문](https://example.com/live)\n\n"
                "남은 링크 [보기](https://example.com/dead)\n",
                encoding="utf-8",
            )
            return ""

        monkeypatch.setattr(weekly_digest, "compose_digest", fake_compose)
        monkeypatch.setattr(
            "alert.digest.checker.check_url_alive",
            lambda url, timeout=8: url != "https://example.com/dead",
        )
        monkeypatch.setattr(
            "alert.digest.checker.extract_item_urls",
            lambda text, item_sections=None: [
                "https://example.com/live",
                "https://example.com/dead",
            ],
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "weekly_digest.py",
                "--db", str(db_path),
                "--week", W13,
                "--out-dir", str(out_dir),
                "--forms", str(tmp_path / "none.csv"),
            ],
        )

        assert weekly_digest.main() == 1
        with open(out_dir / f"{W13}.check.json", encoding="utf-8") as fh:
            check = json.load(fh)
        assert check["pass"] is False
        assert "제외된 URL이 본문에 남아 있음" in check["reason"]
        assert check["residual_urls"] == ["https://example.com/dead"]

    # ─── #3 마감 경과 복제본 ────────────────────────────────────────────
    def test_expired_row_is_excluded_and_open_row_survives(self, tmp_path):
        """만료 행은 배제되고 마감 없는 행은 남는다 (사이클 10 #1).

        마감이 다르면 병합하지 않으므로 각 행이 제 판정을 받는다 — 예전에는
        그룹 마감을 합쳐 둘 다 배제했고, 살아 있는 쪽이 사라질 수 있었다.
        """
        db_path = tmp_path / "c4_3.db"
        _create_announcements_table(db_path)
        for index, period_end in enumerate(["2026-09-12", None]):
            _insert_one(
                db_path,
                source="kofpi",
                source_id=f"exp_{index}",
                title="산림 지원사업 참여기업 모집 공고",
                url=f"https://example.com/exp-{index}",
                period_start="2026-09-08",
                period_end=period_end,
                created_at=W37_CREATED_AT,
            )

        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        published = data["sections"][VERDICT_APPLY]
        assert [item["url"] for item in published] == [
            "https://example.com/exp-1"
        ]
        assert [item["reason"] for item in data["excluded"]] == ["마감 경과"]

    # ─── #4 상한 초과 무기록 삭제 금지 + 불변식 ─────────────────────────
    def test_notice_cap_overflow_is_recorded(self, tmp_path):
        db_path = tmp_path / "c4_4.db"
        _create_announcements_table(db_path)
        for index, source in enumerate(
            ["kofpi", "fowi", "forest_service", "lawmaking"]
        ):
            _insert_one(
                db_path,
                source=source,
                source_id=f"n_{index}",
                title="산림 정책 개정 안내",
                url=f"https://example.com/n-{index}",
                period_end=None,
                period_start=f"2026-09-0{index + 5}",
                created_at=W37_CREATED_AT,
            )

        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        assert len(data["sections"][VERDICT_NOTICE]) == 3
        overflow = [
            item
            for item in data["holds"]
            if item["reason"] == HOLD_REASON_SECTION_CAP
        ]
        assert len(overflow) == 1

    def test_candidate_partition_invariant(self, tmp_path):
        """창 내 모든 후보 = 섹션 ∪ 보류 ∪ 배제 ∪ 병합됨."""
        db_path = tmp_path / "c4_inv.db"
        _create_announcements_table(db_path)
        rows = (
            ("kofpi", "산림분야 오픈이노베이션 참여기업 모집 공고", "2026-09-30"),
            ("kofpi", "산림분야 오픈이노베이션 참여기업 모집 공고", "2026-09-30"),
            ("fowi", "나눔의 숲 캠프 모집 공고", None),
            ("lawmaking", "산지관리법 시행령 일부개정령안 입법예고", "2026-10-19"),
            ("smartfarm", "스마트팜 시스템 작업 안내", None),
            ("coop", "사회적협동조합 공공조달 컨설팅 참여기업 모집", "2026-09-25"),
            ("seis", "사회적기업 사무공간 신규 입주기업 모집 공고", None),
            ("forest_press", "산림청, 국제산림협력 해법 모색", None),
        )
        for index, (source, title, period_end) in enumerate(rows):
            _insert_one(
                db_path,
                source=source,
                source_id=f"inv_{index}",
                title=title,
                url=f"https://example.com/inv-{index}",
                period_end=period_end,
                period_start="2026-09-08",
                created_at=W37_CREATED_AT,
            )

        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        placed = set()
        for section in ITEM_SECTIONS:
            placed.update(item["id"] for item in data["sections"][section])
        placed.update(item["id"] for item in data["holds"])
        placed.update(item["id"] for item in data["excluded"])
        placed.update(data["merged_ids"])

        assert placed == set(data["candidate_ids"])

    # ─── #5 판정·대상이 다르면 병합 금지 ────────────────────────────────
    def test_participant_notice_does_not_swallow_business_notice(self, tmp_path):
        db_path = tmp_path / "c4_5.db"
        _create_announcements_table(db_path)
        for index, title in enumerate(
            [
                "2026년 산림복지서비스 활성화 지원사업 참가자 모집",
                "2026년 산림복지서비스 활성화 지원사업 참가기업 모집",
            ]
        ):
            _insert_one(
                db_path,
                source="fowi",
                source_id=f"m_{index}",
                title=title,
                url=f"https://example.com/m-{index}",
                period_start="2026-09-08",
                period_end="2026-09-30",
                created_at=W37_CREATED_AT,
            )

        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        published = [item["url"] for item in data["sections"][VERDICT_APPLY]]
        held = [item["url"] for item in data["holds"]]

        assert published == ["https://example.com/m-1"]
        assert "https://example.com/m-0" in held
        assert data["merged_ids"] == []

    # ─── #6 노이즈는 제목에만 ───────────────────────────────────────────
    def test_summary_boilerplate_does_not_exclude(self):
        result = classify_item(
            "사회적기업 사업개발비 지원사업 모집",
            "신청은 사회적기업 통합정보시스템에서 접수",
            "seis",
        )
        assert result.verdict == VERDICT_APPLY

    def test_noise_with_institution_signal_is_held_not_excluded(self):
        result = classify_item("산림 안전점검 의무 강화 시행", "", "forest_service")
        assert result.verdict == VERDICT_HOLD
        assert result.reason == "노이즈 의심: 점검"

    # ─── #7 미리보기 = 발송본 ───────────────────────────────────────────
    def test_preview_shows_every_send_section(self, tmp_path):
        from alert.digest import preview as preview_mod
        from scripts.apply_commentary import apply_headline

        csv_path = tmp_path / "forms.csv"
        csv_path.write_text(
            "접수일,회원사,유형,내용,관련정책\n"
            "2026-09-10,회원사A,동정,신규 사업 개시,\n",
            encoding="utf-8",
        )
        db_path = tmp_path / "c4_7.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="kofpi",
            source_id="p1",
            title="산림 지원사업 참여기업 모집 공고",
            url="https://example.com/p1",
            period_end="2026-12-31",
        )

        data = compose_digest_data(
            str(db_path),
            week_str=W13,
            forms_csv_path=csv_path,
            today=W13_TODAY,
        )
        data["council_notes"] = ["산림청 면담 완료"]
        markdown = render_markdown(data)
        annotated, replaced = apply_headline(
            markdown, "첫 줄 해설 · 두 번째 줄 해설"
        )
        assert replaced is True

        text = preview_mod.render_preview(W13, annotated, {"pass": True})
        assert "산림청 면담 완료" in text
        assert "회원사A" in text
        assert "신규 사업 개시" in text
        assert "첫 줄 해설" in text
        assert "두 번째 줄 해설" in text

    # ─── #8 날짜 파서 ───────────────────────────────────────────────────
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("2026. 9. 12.", date(2026, 9, 12)),
            ("2026-9-12 18:00", date(2026, 9, 12)),
            ("2026년 9월 7일", date(2026, 9, 7)),
            ("26.09.30.(수)", date(2026, 9, 30)),
            ("2026/09/30", date(2026, 9, 30)),
            ("2026-09-12T09:46:29.074620", date(2026, 9, 12)),
            ("", None),
            ("접수기간 확인", None),
        ],
    )
    def test_parse_deadline_formats(self, value, expected):
        assert parse_deadline(value) == expected

    def test_korean_posted_date_is_fresh_not_standing(self, tmp_path):
        db_path = tmp_path / "c4_8.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="kofpi",
            source_id="kdate",
            title="산림 지원사업 참여기업 모집 공고",
            url="https://example.com/kdate",
            period_start="2026년 9월 7일",
            period_end=None,
            created_at=W37_CREATED_AT,
        )
        markdown = compose_digest(
            db_path=str(db_path), week_str=W37, today=W37_TODAY
        )
        assert "[새 소식]" in markdown
        assert "[상시]" not in markdown

    def test_date_parse_failures_counted(self, tmp_path):
        db_path = tmp_path / "c4_8b.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="kofpi",
            source_id="bad",
            title="산림 지원사업 참여기업 모집 공고",
            url="https://example.com/bad",
            period_start="접수기간 별도 공지",
            period_end="추후 안내",
            created_at=W37_CREATED_AT,
        )
        stats: dict = {}
        compose_digest(
            db_path=str(db_path),
            week_str=W37,
            today=W37_TODAY,
            stats_out=stats,
        )
        assert stats["date_parse_failures"] == 2

    # ─── #9 KST 주간 경계 ───────────────────────────────────────────────
    def test_week_bounds_is_exclusive_range(self):
        assert week_bounds(W37) == ("2026-09-07", "2026-09-14")

    def test_kst_week_boundary(self, tmp_path):
        db_path = tmp_path / "c4_9.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="kofpi",
            source_id="inside",
            title="산림 지원사업 참여기업 모집 공고",
            url="https://example.com/inside",
            created_at="2026-09-07T00:30:00+09:00",
        )
        _insert_one(
            db_path,
            source="fowi",
            source_id="outside",
            title="임업 경영 컨설팅 참여기업 모집",
            url="https://example.com/outside",
            created_at="2026-09-14T00:30:00+09:00",
        )
        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        urls = [item["url"] for item in data["sections"][VERDICT_APPLY]]
        assert urls == ["https://example.com/inside"]

    # ─── #10 제도 우선 ──────────────────────────────────────────────────
    def test_institution_priority_beats_opportunity(self):
        result = classify_item(
            "산지관리법 시행령 개정 입법예고 공고", "", "lawmaking"
        )
        assert result.verdict == VERDICT_NOTICE

    def test_bare_plan_keyword_needs_relevance_context(self):
        """개정 v2.6 (6): 단독 `계획`은 산림 업종 **또는** 사회적경제 정체성 동반 시 인정."""
        assert classify_item("산림 탄소중립 추진계획", "", "forest_service").verdict == (
            VERDICT_NOTICE
        )
        assert classify_item("사회적기업 성장 추진계획", "", "seis").verdict == (
            VERDICT_NOTICE
        )

    # ─── #11 지역 접두사 ────────────────────────────────────────────────
    def test_region_prefix_prevents_merge(self, tmp_path):
        db_path = tmp_path / "c4_11.db"
        _create_announcements_table(db_path)
        for index, region in enumerate(["경기", "강원"]):
            _insert_one(
                db_path,
                source="seis",
                source_id=f"r_{index}",
                title=f"[{region}] 사회적기업 사업개발비 지원사업 모집",
                url=f"https://example.com/r-{index}",
                period_start="2026-09-08",
                period_end="2026-09-30",
                created_at=W37_CREATED_AT,
            )

        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        published = data["sections"][VERDICT_APPLY]
        assert len(published) == 2
        assert {item["region"] for item in published} == {"경기", "강원"}
        assert data["merged_ids"] == []

    def test_region_prefix_after_a_tag_prevents_merge(self, tmp_path):
        """`[모집][경기]` — 선두 괄호 그룹을 **전부** 훑어 지역을 찾는다.

        Codex 3차 HIGH #1 재현 입력: 첫 괄호만 보던 예전 코드는 지역을 둘 다
        None 으로 두고, 같은 소스·같은 마감의 경기/강원 공고를 하나로 병합했다
        (게시 1 · 보류 0 · merged_ids=[2]).
        """
        db_path = tmp_path / "c7_region.db"
        _create_announcements_table(db_path)
        for index, region in enumerate(["경기", "강원"]):
            _insert_one(
                db_path,
                source="seis",
                source_id=f"rt_{index}",
                title=f"[모집][{region}] 사회적기업 사업개발비 지원사업 모집",
                url=f"https://example.com/rt-{index}",
                period_start="2026-09-08",
                period_end="2026-09-30",
                created_at=W37_CREATED_AT,
            )

        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        published = data["sections"][VERDICT_APPLY]
        assert len(published) == 2
        assert {item["region"] for item in published} == {"경기", "강원"}
        assert data["merged_ids"] == []

    @pytest.mark.parametrize("prefix", ["[모집공고]", "[2026년]", "[공지사항]"])
    def test_unresolved_region_does_not_merge(self, tmp_path, prefix):
        """지역 미확정(None)끼리도 접두 문자열이 다르면 별개 공고다 (사이클 8 #3).

        Codex 4차 HIGH #1 재현 입력: `[모집공고][경기]`/`[모집공고][강원]` 은
        접두 태그를 몰라 지역 탐색이 멈추고 region 이 양쪽 None 이 됐다 —
        병합 키가 같아져 유효 공고가 sections·holds 양쪽에서 사라졌다
        (게시 1 · merged_ids=[2]).
        """
        db_path = tmp_path / f"c8_merge_{abs(hash(prefix))}.db"
        _create_announcements_table(db_path)
        for index, region in enumerate(["경기", "강원"]):
            _insert_one(
                db_path, source="seis", source_id=f"m_{index}",
                title=f"{prefix}[{region}] 사회적기업 사업개발비 지원사업 모집",
                url=f"https://example.com/m-{index}",
                period_start="2026-09-08", period_end="2026-09-30",
                created_at=W37_CREATED_AT,
            )
        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        published = data["sections"][VERDICT_APPLY]
        assert len(published) == 2, [item["title"] for item in published]
        assert data["merged_ids"] == []
        assert {item["prefix_signature"] for item in published} == {
            f"{prefix}[경기]", f"{prefix}[강원]"
        }

    @pytest.mark.parametrize("shape", [
        "사회적기업 사업개발비 지원사업 모집 [{}]",
        "[모집공고][{}] 사회적기업 사업개발비 지원사업 모집",
        "사회적기업 사업개발비 지원사업 모집({} 권역)",
    ])
    def test_region_in_any_bracket_prevents_merge(self, tmp_path, shape):
        """선두·후미·중간 어느 괄호에 있어도 지역을 찾는다 (사이클 9 #2).

        Codex 5차 HIGH #3 재현 입력: 지역 괄호를 **제목 뒤**로 옮기면 region 이
        양쪽 None 이 되고 접두 서명도 빈 문자열이라 병합 키가 같아졌다
        (게시 1 · 보류 0 — 살아 있는 공고가 양쪽에서 사라짐).
        """
        db_path = tmp_path / f"c9_region_{abs(hash(shape))}.db"
        _create_announcements_table(db_path)
        for index, region in enumerate(["경기", "강원"]):
            _insert_one(
                db_path, source="seis", source_id=f"t_{index}",
                title=shape.format(region),
                url=f"https://example.com/t-{index}",
                period_start="2026-09-08", period_end="2026-09-30",
                created_at=W37_CREATED_AT,
            )
        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        published = data["sections"][VERDICT_APPLY]
        assert len(published) == 2, [item["title"] for item in published]
        assert {item["region"] for item in published} == {"경기", "강원"}
        assert data["merged_ids"] == []

    def test_region_found_in_trailing_and_middle_brackets(self):
        assert infer_region("지원사업 모집 [경기]") == "경기"
        assert infer_region("지원사업 모집 [강원]") == "강원"
        assert infer_region("지원사업 모집(충청 권역)") == "충청"
        assert bracket_regions("[모집공고][경기] 지원") == ("경기",)
        assert bracket_regions("지원사업 모집 [강원]") == ("강원",)
        # 사이클 14 #2: 제목 **중간** 괄호도 같은 규칙으로 본다
        assert bracket_regions("사회적기업 지원사업 [경기 소재 기업] 모집") == ("경기",)
        assert bracket_regions("지원사업 모집(~9.30)") == ()
        # 행사 장소는 여전히 자격 지역이 아니다
        assert infer_region("전국 지원사업 모집(설명회 장소: 서울)") is None

    def test_dedup_key_keeps_bracket_contents(self):
        """병합 정규화가 괄호 내용을 지우지 않는다 (사이클 9 #2)."""
        assert "경기" in dedup_key("지원사업 모집 [경기]")
        assert "강원" in dedup_key("지원사업 모집 [강원]")
        assert dedup_key("지원사업 모집 [경기]") != dedup_key("지원사업 모집 [강원]")
        # 날짜·기호만 든 괄호는 여전히 사라진다 (표기 차이는 같은 공고다)
        assert dedup_key("산림 공고(~9.30)") == dedup_key("산림 공고")

    def test_prefix_signature_is_part_of_the_merge_key(self):
        assert prefix_signature("[모집공고][경기] 지원사업") == "[모집공고][경기]"
        assert prefix_signature("[2026년] 지원사업") == "[2026년]"
        assert prefix_signature("지원사업 모집") == ""

    def test_prefix_brackets_and_region_scan_rules(self):
        """선두 괄호 순회 규칙 (사이클 7 #1)."""
        assert prefix_brackets("[모집][경기] 지원사업 모집") == ["모집", "경기"]
        assert prefix_brackets("지원사업 모집") == []
        # 인식된 비지역 태그는 건너뛴다
        assert infer_region("[모집][경기] 지원사업 모집") == "경기"
        assert infer_region("[공고][강원] 지원사업 모집") == "강원"
        assert infer_region("[경기][모집] 지원사업 모집") == "경기"
        # 사이클 9 #2: 접두 그룹을 전부 훑으므로 비지역 태그 목록이 필요 없다 —
        # 지역이 없는 접두사(기관명)는 그냥 지역을 못 주고 지나간다.
        assert infer_region("[고용노동부 공고] 사회적기업 인증 공고") is None
        assert infer_region("[국립김해숲체원] 숲체험교육사업 모집공고") is None
        # 행사 장소는 여전히 자격 지역이 아니다
        assert infer_region(
            "[세종대전충청센터] 설명회 안내(설명회 장소: 서울)"
        ) == "충청"

    # ─── #12 카톡 분할·동기화 ───────────────────────────────────────────
    def test_kakao_chunks_respect_limit(self, tmp_path):
        db_path = tmp_path / "c4_12.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="kofpi",
            source_id="long",
            title="산림 지원사업 참여기업 모집 공고 " + "가" * 4100,
            url="https://example.com/long",
            period_end="2026-12-31",
        )
        data = compose_digest_data(
            str(db_path), week_str=W13, today=W13_TODAY
        )
        chunks = render_kakao_chunks(data)
        assert len(chunks) > 1
        assert all(len(chunk) <= 4096 for chunk in chunks)
        assert KAKAO_CHUNK_SEPARATOR in kakao_file_text(data)

    def test_chunk_plaintext_is_lossless(self):
        text = "\n".join(f"{i}. " + "가" * 200 for i in range(1, 120))
        chunks = chunk_plaintext(text)
        assert len(chunks) > 1
        assert "\n".join(chunks) == text

    def test_apply_commentary_syncs_kakao(self, tmp_path):
        import scripts.apply_commentary as apply_mod

        db_path = tmp_path / "c4_12b.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="kofpi",
            source_id="sync",
            title="산림 지원사업 참여기업 모집 공고",
            url="https://example.com/sync",
            period_end="2026-12-31",
        )
        out_dir = tmp_path / "digests"
        compose_digest(
            db_path=str(db_path),
            week_str=W13,
            output_path=out_dir / f"{W13}.md",
            today=W13_TODAY,
        )
        kakao_path = out_dir / f"{W13}.kakao.txt"
        assert KAKAO_HEADLINE_PLACEHOLDER in kakao_path.read_text(
            encoding="utf-8"
        )

        rc = apply_mod.main(
            [W13, "--headline", "이번 주는 산림형 예비사회적기업 지정 공고",
             "--out-dir", str(out_dir)]
        )
        assert rc in (0, 1)
        kakao_text = kakao_path.read_text(encoding="utf-8")
        assert KAKAO_HEADLINE_PLACEHOLDER not in kakao_text
        assert "이번 주는 산림형 예비사회적기업 지정 공고" in kakao_text

    def test_kakao_item_blocks_match_the_block_parser(self):
        """사이클 6 #1·#5: 카톡 항목 덩어리와 블록 파서의 항목이 1:1로 맞는다."""
        md = (
            "# 📋 협의회 주간 정책브리핑 2026-W37 (9/7~9/13)\n\n"
            "이번 주 한 줄: 확정 문구\n\n"
            "## ✅ 신청하세요 (마감순)\n\n"
            "<!-- item id=1 -->\n"
            "[D-9] 산림 공고 — 산림청 · 대상: 산림사업자 · 마감 9/22\n"
            "  [원문](https://example.com/a)\n\n"
            "## 👀 알아두세요\n\n"
            "<!-- item id=2 -->\n"
            "입법예고 — 국민참여입법센터 · 대상: 산림사업자 · 의견 10/19까지\n"
            "  [원문](https://example.com/d)\n"
        )
        items = blocks_mod.item_blocks(md)
        assert len(items) == 2

        pairs = [
            block
            for block in kakao_blocks_from_markdown(md)
            if "https://" in block
        ]
        assert len(pairs) == len(items)
        for item, block in zip(items, pairs):
            assert block == f"{item['title']}\n  {item['url']}"
            assert block.count("\n") == 1

    def test_kakao_is_regenerated_from_markdown(self):
        """사이클 6 #5: 카톡은 자기 파일을 손보지 않고 md 에서 재생성된다."""
        md = (
            "<!-- lane: test -->\n\n"
            "# 📋 협의회 주간 정책브리핑 2026-W37 (9/7~9/13)\n\n"
            f"이번 주 한 줄: {MARKER}\n\n"
            "## ✅ 신청하세요 (마감순)\n\n"
            "<!-- item id=7 -->\n"
            "[D-9] 산림 공고 — 산림청 · 대상: 산림사업자 · 마감 9/22\n"
            "  [원문](https://example.com/a)\n\n"
            "<!-- 보류: 1. 보류 항목 | 이유 | id=1 -->\n"
        )
        placeholder = kakao_file_text_from_markdown(md)
        assert KAKAO_HEADLINE_PLACEHOLDER in placeholder
        # 레인 표기·보류 주석은 카톡 발송본에 실리지 않는다
        assert "lane:" not in placeholder
        assert "보류 항목" not in placeholder

        confirmed = kakao_file_text_from_markdown(
            md.replace(MARKER, "확정 문구")
        )
        assert f"{KAKAO_HEADLINE_PREFIX}확정 문구" in confirmed
        assert KAKAO_HEADLINE_PLACEHOLDER not in confirmed
        # 항목은 제목 줄 + URL 줄 한 덩어리로 남는다
        assert (
            "[D-9] 산림 공고 — 산림청 · 대상: 산림사업자 · 마감 9/22\n"
            "  https://example.com/a"
        ) in confirmed

    # ─── #13 보류 주석 구분자·id ────────────────────────────────────────
    def test_hold_comment_escapes_pipe_and_carries_id(self):
        item = {
            "number": 3,
            "title": "산림 이야기 | 강원",
            "reason": "섹션 판정 불명",
            "id": 77,
        }
        line = hold_comment(item)
        assert line == "<!-- 보류: 3. 산림 이야기 ｜ 강원 | 섹션 판정 불명 | id=77 -->"

    def test_hold_comment_roundtrips_through_preview(self):
        from alert.digest import preview as preview_mod

        item = {
            "number": 1,
            "title": "산림 이야기 | 강원",
            "reason": "섹션 판정 불명",
            "id": 77,
        }
        parsed = preview_mod.parse_digest(hold_comment(item))
        assert parsed["holds"] == [
            {
                "number": 1,
                "title": "산림 이야기 ｜ 강원",
                "reason": "섹션 판정 불명",
                "id": 77,
            }
        ]

    # ─── #14 인덱스 사용 ────────────────────────────────────────────────
    def test_window_query_uses_created_at_index(self, tmp_path):
        db_path = tmp_path / "c4_14.db"
        _create_announcements_table(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE INDEX idx_ann_created ON announcements(created_at)"
        )
        start, end = week_bounds(W37)
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM announcements "
            "WHERE created_at >= ? AND created_at < ?",
            (start, end),
        ).fetchall()
        conn.close()
        detail = " ".join(str(row[-1]) for row in plan)
        assert "SEARCH" in detail
        assert "SCAN announcements" not in detail


class TestFixCycle5:
    """개정 v2.6 — Codex 재검토 신규 발견(1·2·7·8·9·10)과 검증 공백."""

    # ─── (1) 연장 공고 병합 만료 ────────────────────────────────────────
    def test_extension_round_survives_expired_original(self, tmp_path):
        db_path = tmp_path / "c5_1.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="kofpi",
            source_id="orig",
            title="산림 지원사업 참여기업 모집 공고",
            url="https://example.com/orig",
            period_start="2026-09-08",
            period_end="2026-09-12",
            created_at=W37_CREATED_AT,
        )
        _insert_one(
            db_path,
            source="kofpi",
            source_id="ext",
            title="산림 지원사업 참여기업 모집 공고(연장 ~9.30)",
            url="https://example.com/ext",
            period_start="2026-09-08",
            period_end=None,
            raw_data=(
                '{"title": "산림 지원사업 참여기업 모집 공고(연장 ~9.30)"}'
            ),
            created_at=W37_CREATED_AT,
        )

        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        published = [item["url"] for item in data["sections"][VERDICT_APPLY]]
        excluded = [
            (item["url"], item["reason"]) for item in data["excluded"]
        ]

        assert published == ["https://example.com/ext"]
        assert ("https://example.com/orig", "마감 경과") in excluded
        assert data["merged_ids"] == []

    def test_renewal_without_deadline_is_not_merged(self):
        result = classify_item(
            "산림 지원사업 참여기업 추가모집 공고", "", "kofpi"
        )
        assert result.verdict == VERDICT_APPLY

    # ─── (2) 지역 소실 병합 ─────────────────────────────────────────────
    def test_prefix_region_wins_over_venue(self):
        result = classify_item(
            "[경기] 사회적기업 지원사업 모집 (설명회 장소: 서울)", "", "seis"
        )
        assert result.region == "경기"

    def test_prefix_region_difference_blocks_merge(self, tmp_path):
        db_path = tmp_path / "c5_2.db"
        _create_announcements_table(db_path)
        for index, title in enumerate(
            [
                "[경기] 사회적기업 지원사업 모집 (설명회 장소: 서울)",
                "[강원] 사회적기업 지원사업 모집",
            ]
        ):
            _insert_one(
                db_path,
                source="seis",
                source_id=f"reg_{index}",
                title=title,
                url=f"https://example.com/reg-{index}",
                period_start="2026-09-08",
                period_end="2026-09-30",
                created_at=W37_CREATED_AT,
            )

        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        published = data["sections"][VERDICT_APPLY]
        assert len(published) == 2
        assert {item["region"] for item in published} == {"경기", "강원"}
        assert data["merged_ids"] == []

    def test_venue_context_is_ignored_without_prefix(self):
        result = classify_item(
            "사회적기업 지원사업 모집 (설명회 장소: 서울)", "", "seis"
        )
        assert result.region is None

    # ─── (3) sanitize_title: URL만 링크 ────────────────────────────────
    @pytest.mark.parametrize(
        "title",
        [
            ("[모집](~9.30) 산림 지원사업", "[모집](~9.30) 산림 지원사업"),
            ("[모집](산림사업자) 지원사업", "[모집](산림사업자) 지원사업"),
            ("「2026년 산림 공고」(~9.30)", "「2026년 산림 공고」(~9.30)"),
        ],
    )
    def test_sanitize_title_preserves_non_url_parens(self, title):
        """비URL 괄호 표기는 보존한다 — 선두 대괄호만 전각으로 중화 (사이클 8 #2)."""
        raw, expected = title
        assert sanitize_title(raw) == expected
        # 꼬리 표기는 어떤 경우에도 잃지 않는다
        if "(~9.30)" in raw:
            assert "(~9.30)" in sanitize_title(raw)

    def test_sanitize_title_still_strips_real_links(self):
        assert sanitize_title("산림 [신청](https://example.com/dead) 모집") == (
            "산림 신청 모집"
        )
        assert sanitize_title("산림 [자료](www.example.com/x) 모집") == (
            "산림 자료 모집"
        )

    def test_non_url_paren_survives_to_markdown(self, tmp_path):
        db_path = tmp_path / "c5_3.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="kofpi",
            source_id="paren",
            title="[모집](~9.30) 산림 지원사업 참여기업",
            url="https://example.com/paren",
            period_end="2026-12-31",
        )
        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )
        assert "[모집](~9.30) 산림 지원사업 참여기업" in markdown

    @pytest.mark.parametrize(
        "title",
        ["## 산림 제도 개정 시행", "*산림 제도 개정 시행", "1. 산림 제도 개정 시행"],
    )
    def test_structural_title_does_not_move_sections(self, tmp_path, title):
        """구조 구문으로 시작하는 DB 제목도 정상 항목으로 실린다 (사이클 8 #2).

        Codex 4차 HIGH #3 재현 입력: `## …` 제목은 뒤 항목까지 산문 섹션으로
        밀어냈고(preview 1·HTML 링크 4·pass), `*…` 제목은 정상 항목을 산문으로
        판정해 0건·fail 이었다.
        """
        from alert.digest import blocks as blocks_mod

        db_path = tmp_path / f"c8_struct_{abs(hash(title))}.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path, source="lawmaking", source_id="s1", title=title,
            url="https://example.com/s1", period_end="2026-12-31",
        )
        _insert_one(
            db_path, source="kofpi", source_id="s2",
            title="산림 지원사업 참여기업 모집 공고",
            url="https://example.com/s2", period_end="2026-12-31",
        )
        out = tmp_path / f"{W13}.md"
        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, output_path=out, today=W13_TODAY
        )
        # 섹션 구조가 그대로다 (`## ` 헤딩은 composer 가 만든 것뿐)
        headings = [line for line in markdown.split("\n") if line.startswith("## ")]
        assert all(
            line[3:] in SECTION_HEADINGS.values() for line in headings
        ), headings
        # 항목 수 == 마커 수 == 정본 수
        manifest = json.loads(
            (tmp_path / f"{W13}.items.json").read_text(encoding="utf-8")
        )
        assert blocks_mod.item_block_count(markdown) == len(manifest["items"])
        assert markdown.count("<!-- item id=") == len(manifest["items"])
        assert blocks_mod.prose_lines_in_item_sections(markdown) == []
        # 원문 표기는 HTML·카톡에 남는다 (전각으로 중화됐을 뿐)
        from scripts.send_digest import markdown_to_html
        assert "산림 제도 개정 시행" in markdown_to_html(markdown)
        assert "산림 제도 개정 시행" in kakao_file_text_from_markdown(markdown)

    def test_items_manifest_binds_md_to_db(self, tmp_path):
        """compose 가 items.json 을 함께 쓰고, 그 내용이 md·DB 와 맞는다 (사이클 8 #1)."""
        db_path = tmp_path / "c8_manifest.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path, source="kofpi", source_id="m1",
            title="산림 지원사업 참여기업 모집 공고",
            url="https://example.com/m1", period_end="2026-12-31",
        )
        out = tmp_path / f"{W13}.md"
        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, output_path=out, today=W13_TODAY
        )
        manifest = json.loads(
            (tmp_path / f"{W13}.items.json").read_text(encoding="utf-8")
        )
        assert manifest["week"] == W13
        assert manifest["markdown_sha256"] == markdown_sha256(out.read_bytes())
        assert [entry["url"] for entry in manifest["items"]] == [
            "https://example.com/m1"
        ]
        entry = manifest["items"][0]
        assert entry["section"] == VERDICT_APPLY
        assert entry["deadline_label"]
        assert entry["title"] == sanitize_title(
            "산림 지원사업 참여기업 모집 공고"
        )
        # md 의 마커 id 가 정본·DB 와 같은 id 다
        assert f"<!-- item id={entry['id']} -->" in markdown

    # ─── (4) 지역 오탐 ──────────────────────────────────────────────────
    @pytest.mark.parametrize(
        "title",
        [
            "경기침체 대응 사회적기업 지원사업 모집",
            "대구목재 사회적기업 판로 지원사업 모집",
            "사회적기업 모집 (대전 [교육장] 안내)",
        ],
    )
    def test_region_substring_false_positives(self, title):
        assert classify_item(title, "", "seis").region is None

    def test_fullwidth_and_ascii_prefix_agree(self):
        ascii_region = classify_item(
            "[경기센터] 강원 사회적기업 지원사업 모집", "", "seis"
        ).region
        fullwidth_region = classify_item(
            "［경기센터］ 강원 사회적기업 지원사업 모집", "", "seis"
        ).region
        assert ascii_region == fullwidth_region == "경기"

    def test_prefix_bracket_helper(self):
        assert prefix_bracket("[경기] 모집") == "경기"
        assert prefix_bracket("［세종대전충청센터］ 모집") == "세종대전충청센터"
        assert prefix_bracket("2026년 경기도 모집") == ""

    # ─── (5) 카톡 항목·URL 보존 ─────────────────────────────────────────
    def test_kakao_never_splits_url_or_starts_with_it(self, tmp_path):
        db_path = tmp_path / "c5_5.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="kofpi",
            source_id="long",
            title="산림 지원사업 참여기업 모집 공고 " + "가" * 3947,
            url="https://example.com/long-item",
            period_end="2026-12-31",
        )
        data = compose_digest_data(
            str(db_path), week_str=W13, today=W13_TODAY
        )
        chunks = render_kakao_chunks(data)

        assert all(len(chunk) <= 4096 for chunk in chunks)
        for chunk in chunks:
            assert not chunk.lstrip().startswith("https://")
        # URL은 한 조각 안에 온전히 남는다
        assert sum(
            chunk.count("https://example.com/long-item") for chunk in chunks
        ) == 1
        # 항목 블록은 통째로 한 조각에 들어간다 (URL이 다음 메시지로 밀리지 않는다)
        assert any(
            "https://example.com/long-item" in chunk and "가" in chunk
            for chunk in chunks
        )

    def test_kakao_shrinks_title_to_fit_and_keeps_url(self, tmp_path):
        db_path = tmp_path / "c5_5d.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="kofpi",
            source_id="huge",
            title="산림 지원사업 참여기업 모집 공고 " + "가" * 4200,
            url="https://example.com/huge-item",
            period_end="2026-12-31",
        )
        data = compose_digest_data(
            str(db_path), week_str=W13, today=W13_TODAY
        )
        chunks = render_kakao_chunks(data)

        assert all(len(chunk) <= 4096 for chunk in chunks)
        assert any("…" in chunk for chunk in chunks)
        holder = [c for c in chunks if "https://example.com/huge-item" in c]
        assert len(holder) == 1
        assert holder[0].rstrip().endswith("https://example.com/huge-item")

    def test_oversized_url_item_is_held_at_compose(self, tmp_path):
        """사이클 7 (#9): 카톡 한 조각에 못 들어가는 URL 은 보류로 내린다.

        예전에는 그 조각만 한도를 넘긴 채(4,207자) 보냈다 — 오버사이즈 조각은
        이제 어느 경로에서도 만들지 않는다.
        """
        db_path = tmp_path / "c5_5b.db"
        _create_announcements_table(db_path)
        long_url = "https://example.com/" + "a" * 4120
        _insert_one(
            db_path,
            source="kofpi",
            source_id="longurl",
            title="산림 지원사업 참여기업 모집 공고",
            url=long_url,
            period_end="2026-12-31",
        )
        data = compose_digest_data(
            str(db_path), week_str=W13, today=W13_TODAY
        )

        published = data["sections"][VERDICT_APPLY] + data["sections"][VERDICT_NOTICE]
        assert [item["url"] for item in published] == []
        held = [item for item in data["holds"] if item["url"] == long_url]
        assert len(held) == 1
        assert held[0]["reason"] == HOLD_REASON_URL_TOO_LONG

        chunks = render_kakao_chunks(data)
        assert all(len(chunk) <= KAKAO_CHUNK_LIMIT for chunk in chunks)
        assert all(long_url not in chunk for chunk in chunks)
        # 발송본 마크다운에도 실리지 않고 보류 주석으로만 남는다
        markdown = render_markdown(data)
        assert long_url not in markdown
        assert HOLD_REASON_URL_TOO_LONG in markdown

    @pytest.mark.parametrize("url_len", [4090, 4092, 4094, 4096, 4140])
    def test_kakao_fit_decision_matches_the_render(self, tmp_path, url_len):
        """적합성 판정과 렌더가 같은 함수를 본다 (사이클 8 #4).

        Codex 4차 MEDIUM #4: URL 4,092자는 `kakao_item_fits=True` 로 게시됐는데
        카톡에서는 안내 문구로 대체됐다(원문 유실) — 경계가 갈라져 있었다.
        """
        db_path = tmp_path / f"c8_fit_{url_len}.db"
        _create_announcements_table(db_path)
        url = "https://example.com/" + "a" * (url_len - 20)
        _insert_one(
            db_path, source="kofpi", source_id="fit",
            title="산림 지원사업 참여기업 모집 공고",
            url=url, period_end="2026-12-31",
        )
        data = compose_digest_data(
            str(db_path), week_str=W13, today=W13_TODAY
        )
        published = data["sections"][VERDICT_APPLY]
        chunks = render_kakao_chunks(data)
        assert all(len(chunk) <= KAKAO_CHUNK_LIMIT for chunk in chunks)
        if published:
            # 게시했다면 카톡에 **원문 URL이 그대로** 실려야 한다
            assert any(url in chunk for chunk in chunks), url_len
            assert URL_TOO_LONG_NOTICE not in "".join(chunks)
        else:
            # 게시하지 않았다면 보류 사유가 남아야 한다
            assert [item["reason"] for item in data["holds"]] == [
                HOLD_REASON_URL_TOO_LONG
            ]

    def test_long_prose_url_is_replaced_and_warned(self, tmp_path):
        """해설의 한도 초과 URL 은 카톡·미리보기에서 치환되고 경고로 남는다 (#4)."""
        from alert.digest import preview as preview_mod
        from alert.digest.checker import check_digest

        long_url = "https://example.com/" + "b" * 4120
        db_path = tmp_path / "c8_prose.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path, source="kofpi", source_id="p1",
            title="산림 지원사업 참여기업 모집 공고",
            url="https://example.com/p1", period_end="2026-12-31",
        )
        out = tmp_path / f"{W13}.md"
        markdown = compose_digest(
            db_path=str(db_path), week_str=W13, output_path=out, today=W13_TODAY
        )
        annotated = markdown.replace(
            MARKER, f"참고 [자료]({long_url}) 입니다"
        )
        out.write_text(annotated, encoding="utf-8")

        kakao = kakao_file_text_from_markdown(annotated)
        chunks = kakao.split(f"\n{KAKAO_CHUNK_SEPARATOR}\n")
        assert all(len(chunk) <= KAKAO_CHUNK_LIMIT for chunk in chunks)
        assert long_url not in kakao
        assert URL_TOO_LONG_NOTICE in kakao

        body = preview_mod.render_preview(W13, annotated, {"pass": True})
        assert all(
            len(chunk) <= preview_mod.TELEGRAM_LIMIT
            for chunk in preview_mod.chunk_text(body)
        )
        assert long_url not in body

        with mock.patch(
            "alert.digest.checker.check_url_alive", return_value=True
        ):
            result = check_digest(
                db_path=str(db_path), markdown_path=out, output_path=None
            )
        assert result["long_prose_urls"] == [long_url]

    def test_oversized_url_never_makes_an_oversize_chunk(self):
        """손으로 고친 본문에서 와도 오버사이즈 조각은 만들지 않는다 (#9)."""
        long_url = "https://example.com/" + "a" * 4120
        md = (
            "# 📋 협의회 주간 정책브리핑 2026-W37 (9/7~9/13)\n\n"
            "이번 주 한 줄: 확정 문구\n\n"
            "## ✅ 신청하세요 (마감순)\n\n"
            "<!-- item id=1 -->\n"
            "[D-9] 산림 공고 — 산림청 · 대상: 산림사업자 · 마감 9/22\n"
            f"  [원문]({long_url})\n"
        )
        text = kakao_file_text_from_markdown(md)
        chunks = text.split(f"\n{KAKAO_CHUNK_SEPARATOR}\n")
        assert all(len(chunk) <= KAKAO_CHUNK_LIMIT for chunk in chunks)
        assert long_url not in text
        assert URL_TOO_LONG_NOTICE in text

    def test_kakao_blocks_keep_item_with_its_url(self, tmp_path):
        db_path = tmp_path / "c5_5c.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="kofpi",
            source_id="pair",
            title="산림 지원사업 참여기업 모집 공고",
            url="https://example.com/pair",
            period_end="2026-12-31",
        )
        data = compose_digest_data(
            str(db_path), week_str=W13, today=W13_TODAY
        )
        item_blocks = [
            block
            for block in kakao_blocks(data)
            if "https://example.com/pair" in block
        ]
        assert len(item_blocks) == 1
        assert item_blocks[0].count("\n") == 1

    # ─── (8) 시간대 ─────────────────────────────────────────────────────
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("2026-09-06T15:30:00+00:00", date(2026, 9, 7)),
            ("2026-09-13T15:30:00Z", date(2026, 9, 14)),
            ("2026-09-12T09:46:29.074620", date(2026, 9, 12)),
            ("2026-09-07T00:30:00+09:00", date(2026, 9, 7)),
        ],
    )
    def test_kst_date(self, value, expected):
        assert kst_date(value) == expected

    def test_window_prefilter_is_wider_than_window(self):
        assert week_bounds(W37) == ("2026-09-07", "2026-09-14")
        assert window_prefilter_bounds(W37) == ("2026-09-06", "2026-09-15")

    def test_utc_offsets_are_judged_in_kst(self, tmp_path):
        db_path = tmp_path / "c5_8.db"
        _create_announcements_table(db_path)
        _insert_one(
            db_path,
            source="kofpi",
            source_id="utc_in",
            title="산림 지원사업 참여기업 모집 공고",
            url="https://example.com/utc-in",
            period_end="2026-12-31",
            created_at="2026-09-06T15:30:00+00:00",
        )
        _insert_one(
            db_path,
            source="fowi",
            source_id="utc_out",
            title="임업 경영 컨설팅 참여기업 모집",
            url="https://example.com/utc-out",
            period_end="2026-12-31",
            created_at="2026-09-13T15:30:00Z",
        )
        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        urls = [item["url"] for item in data["sections"][VERDICT_APPLY]]
        assert urls == ["https://example.com/utc-in"]

    # ─── (7) 분할 불변식 강화 ───────────────────────────────────────────
    RICH_ROWS = (
        # (source, title, period_start, period_end, raw_data)
        ("kofpi", "산림분야 오픈이노베이션 참여기업 모집 공고", "2026-09-08", "2026-09-30", ""),
        ("kofpi", "임산물 가공유통 지원사업 참여업체 모집", "2026-09-08", "2026-10-01", ""),
        ("kofpi", "목재산업 시설 개선 지원 참여기업 모집", "2026-09-08", "2026-10-02", ""),
        # 만료 + 연장 회차 (병합 금지, 살아 있는 쪽이 남는다)
        ("fowi", "산림복지 지원사업 참여기업 모집 공고", "2026-09-08", "2026-09-11", ""),
        (
            "fowi",
            "산림복지 지원사업 참여기업 모집 공고(연장 ~9.30)",
            "2026-09-08",
            None,
            '{"title": "산림복지 지원사업 참여기업 모집 공고(연장 ~9.30)"}',
        ),
        # 완전 동일 제목·마감 (사이클 10 #1: 이때만 병합한다)
        (
            "forest_service",
            "산림 탄소 흡수량 산정 지원 참여기업 모집 안내",
            "2026-09-08",
            "2026-09-25",
            "",
        ),
        (
            "forest_service",
            "산림 탄소 흡수량 산정 지원 참여기업 모집 안내",
            "2026-09-08",
            "2026-09-25",
            "",
        ),
        # 지역 접두 2건 (병합 금지)
        ("seis", "[경기] 사회적기업 사업개발비 지원사업 모집", "2026-09-08", "2026-09-28", ""),
        ("seis", "[강원] 사회적기업 사업개발비 지원사업 모집", "2026-09-08", "2026-09-28", ""),
        # 배제 (소스 풀 외) / 보류 (B2C) / 알아두세요
        ("smartfarm", "스마트팜 실증단지 입주 모집 공고", "2026-09-08", None, ""),
        ("fowi", "나눔의 숲 캠프 모집 공고", "2026-09-08", None, ""),
        ("lawmaking", "산지관리법 시행령 일부개정령안 입법예고", "2026-09-08", "2026-10-19", ""),
        ("forest_press", "산림청, 국제산림협력 해법 모색", "2026-09-08", None, ""),
    )

    def test_partition_invariant_with_independent_oracle(self, tmp_path):
        """창 내 모든 후보가 섹션∪보류∪배제∪병합됨에 **정확히 한 번** 나타난다."""
        db_path = tmp_path / "c5_7.db"
        _create_announcements_table(db_path)
        for index, (source, title, start, end, raw) in enumerate(
            self.RICH_ROWS
        ):
            _insert_one(
                db_path,
                source=source,
                source_id=f"rich_{index}",
                title=title,
                url=f"https://example.com/rich-{index}",
                period_start=start,
                period_end=end,
                raw_data=raw,
                created_at=W37_CREATED_AT,
            )

        # 오라클: 컴포저와 무관한 별도 SQL + 테스트가 직접 계산한 KST 창
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT id, created_at FROM announcements"
        ).fetchall()
        conn.close()
        first, last = date(2026, 9, 7), date(2026, 9, 13)
        oracle = {
            row_id
            for row_id, created in rows
            if first <= kst_date(created) <= last
        }
        assert len(oracle) == len(self.RICH_ROWS)

        data = compose_digest_data(
            str(db_path), week_str=W37, today=W37_TODAY
        )
        buckets = [
            [item["id"] for item in data["sections"][VERDICT_APPLY]],
            [item["id"] for item in data["sections"][VERDICT_NOTICE]],
            [item["id"] for item in data["holds"]],
            [item["id"] for item in data["excluded"]],
            list(data["merged_ids"]),
        ]
        placed = [item_id for bucket in buckets for item_id in bucket]

        assert len(placed) == len(set(placed)), "두 곳에 배치된 항목이 있다"
        assert set(placed) == oracle, "섹션∪보류∪배제∪병합됨 밖의 항목이 있다"

        # 픽스처가 의도한 사례들이 실제로 등장했는지 (테스트가 비어 돌지 않게)
        reasons = {item["reason"] for item in data["excluded"]}
        assert "마감 경과" in reasons
        assert "협의회 소스 풀 외" in reasons
        hold_reasons = {item["reason"] for item in data["holds"]}
        assert any(r.startswith("참가자 모집(B2C)") for r in hold_reasons)
        assert HOLD_REASON_SECTION_CAP in hold_reasons
        assert len(data["merged_ids"]) == 1
        assert len(data["sections"][VERDICT_NOTICE]) == 1
