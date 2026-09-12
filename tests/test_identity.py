"""행 식별 회귀 - **한 행이 무엇인가**는 한 함수가 정한다 (13차 게이트).

식별을 크롤러마다 다르게 만들면 서로 다른 공고가 한 행을 덮어쓴다. 실제
재현(13차 Codex 게이트):

- SEIS ``boardId=A&nttId=42`` / ``boardId=B&nttId=42`` → 같은 ``ntt:42``
  → DB·알림 1건, A 의 점수·사유가 B 의 값으로 덮어써짐
- G2B 같은 공고번호의 차수 ``00``/``01`` → 00차 제목에 01차 마감 저장
- Bizinfo ``detailUrl: null`` 두 건 → URL 이 문자열 ``"None"`` 으로 같아져
  B2 가 사라지고 B1 제목에 B2 기간 저장
- G2B ``bidClseDt="202609309999"`` → 없는 시각을 정상 마감으로 저장
"""

import ast
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.bizinfo import BizinfoCrawler
from alert.crawlers.g2b import G2bCrawler
from alert.crawlers.identity import (
    IDENTITY_FIELDS,
    clean_text,
    identity_key,
    normalize_url,
)
from alert.crawlers.period_extractors import g2b_period
from alert.db import Database
from alert.main import _finalize_periods
from alert.models import AnalyzedAnnouncement, RawAnnouncement

REPO = Path(__file__).resolve().parents[1]


def make(cls, source_name):
    config = MagicMock()
    config.crawler.timeout = 10
    config.crawler.retry_count = 1
    config.crawler.retry_delay = 0
    config.crawler.user_agent = "test-agent"
    config.crawler.sources = {}
    with patch("alert.crawlers.base.get_config", return_value=config):
        return cls()


def announcement(source, url, raw=None, source_id="x", title="공고"):
    return RawAnnouncement(
        source=source, source_id=source_id, title=title, url=url,
        raw_data=json.dumps(raw or {}, ensure_ascii=False),
    )


class TestUrlNormalisation:
    """같은 페이지를 가리키면 같은 문자열이어야 한다."""

    def test_query_order_and_case_do_not_matter(self):
        first = normalize_url("https://Example.KR/view.do?b=2&a=1")
        second = normalize_url("https://example.kr/view.do?a=1&b=2")
        assert first == second == "https://example.kr/view.do?a=1&b=2"

    def test_session_parameters_are_dropped(self):
        assert normalize_url("https://x.kr/a?id=1&jsessionid=ZZ&_=99") == (
            normalize_url("https://x.kr/a?id=1")
        )

    def test_path_session_and_fragment_are_dropped(self):
        assert normalize_url("https://x.kr/a;jsessionid=ZZ?id=1#top") == (
            normalize_url("https://x.kr/a?id=1")
        )

    def test_trailing_slash(self):
        assert normalize_url("https://x.kr/a/") == normalize_url("https://x.kr/a")

    @pytest.mark.parametrize("value", ["", "   ", None, "None", "null"])
    def test_absent_urls_are_really_absent(self, value):
        """문자열 ``"None"`` 은 URL 이 아니다 (13차 게이트 HIGH)."""
        assert normalize_url(value) is None
        assert clean_text(value) == ""


class TestIdentityKey:
    """식별 규칙: 선언 필드 > 정규화 URL > 크롤러 source_id."""

    def test_different_boards_with_the_same_number_are_different_rows(self):
        """SEIS 재현: ``boardId`` 가 다르면 다른 공고다."""
        first = identity_key("seis", announcement(
            "seis", "https://www.seis.or.kr/boardView.do?boardId=A&nttId=42"))
        second = identity_key("seis", announcement(
            "seis", "https://www.seis.or.kr/boardView.do?boardId=B&nttId=42"))
        assert first != second
        assert first.startswith("url:") and second.startswith("url:")

    def test_same_page_different_spelling_is_one_row(self):
        first = identity_key("seis", announcement(
            "seis", "https://www.seis.or.kr/v.do?a=1&b=2"))
        second = identity_key("seis", announcement(
            "seis", "https://www.seis.or.kr/v.do?b=2&a=1&jsessionid=Q"))
        assert first == second

    def test_declared_fields_win_over_the_url(self):
        """API ID 가 URL 보다 권위 있다 - 템플릿 링크가 식별자가 되면 안 된다."""
        assert IDENTITY_FIELDS["bizinfo"] == ("pblancId",)
        first = identity_key("bizinfo", announcement(
            "bizinfo", "https://www.bizinfo.go.kr/x", {"pblancId": "B1"}))
        second = identity_key("bizinfo", announcement(
            "bizinfo", "https://www.bizinfo.go.kr/y", {"pblancId": "B1"}))
        assert first == second == "fld:B1"

    def test_g2b_order_is_part_of_the_identity(self):
        """차수가 다르면 마감이 다른 별개 공고다."""
        base = {"bidNtceNo": "20260900123"}
        first = identity_key("g2b", announcement(
            "g2b", None, {**base, "bidNtceOrd": "00"}))
        second = identity_key("g2b", announcement(
            "g2b", None, {**base, "bidNtceOrd": "01"}))
        assert (first, second) == ("fld:20260900123|00", "fld:20260900123|01")

    def test_missing_url_and_fields_falls_back_to_source_id(self):
        assert identity_key("manual", announcement(
            "manual", "", source_id="abc")) == "abc"

    def test_accepts_rows_and_objects_alike(self):
        url = "https://x.kr/a?id=1"
        from_object = identity_key("seis", announcement("seis", url))
        from_mapping = identity_key("seis", {"url": url, "raw_data": "{}"})
        assert from_object == from_mapping


class TestEveryDbPathUsesTheSameKey:
    """조회·저장·갱신이 **같은 키**를 쓴다 (우회 0)."""

    @staticmethod
    def methods_matching(needle):
        source = (REPO / "alert" / "db.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                segment = ast.get_source_segment(source, node) or ""
                if needle in segment:
                    found.add(node.name)
        return found

    def test_only_find_row_looks_up_by_source_id(self):
        """``source_id`` 로 행을 찾는 곳은 관문 하나뿐이다.

        ``is_duplicate`` 는 (source, source_id) 를 직접 받는 레거시 헬퍼로
        남아 있고 생산 경로에서는 쓰지 않는다 - 아래 테스트가 고정한다.
        """
        assert self.methods_matching("AND source_id = ?") == {
            "_find_row", "is_duplicate",
        }

    def test_production_code_never_calls_is_duplicate(self):
        offenders = [
            path.name
            for path in (REPO / "alert").rglob("*.py")
            if "is_duplicate(" in path.read_text(encoding="utf-8")
            and path.name != "db.py"
        ]
        assert offenders == []

    def test_insert_writes_the_identity_key(self):
        source = (REPO / "alert" / "db.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        insert = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "insert_announcement"
        )
        segment = ast.get_source_segment(source, insert) or ""
        assert "ann.source_id = self.identity_of(ann)" in segment

    def test_identity_helper_is_the_only_producer(self):
        source = (REPO / "alert" / "db.py").read_text(encoding="utf-8")
        assert source.count("identity_key(") == 2      # identity_of + 이관
        assert source.count("def identity_of(") == 1


class TestRowsNeverOverwriteEachOther:
    """다른 공고가 남의 행을 덮어쓰지 않는다."""

    @pytest.fixture
    def db(self, tmp_path):
        yield Database(db_path=tmp_path / "announcements.db")

    def insert(self, db, source, url, raw=None, title="공고", score=0.5):
        ann = AnalyzedAnnouncement(
            source=source, source_id="ignored", title=title, url=url or "",
            raw_data=json.dumps(raw or {}, ensure_ascii=False),
            relevance_score=score, relevance_reason=title,
            fetched_at=datetime.now().isoformat(),
        )
        return db.insert_announcement(_finalize_periods(source, ann)), ann

    def test_seis_two_boards_stay_two_rows(self, db):
        """재현 ①: 같은 번호·다른 게시판 → 2행, 점수·사유 보존."""
        first_id, _ = self.insert(
            db, "seis", "https://www.seis.or.kr/boardView.do?boardId=A&nttId=42",
            title="A 공고", score=0.9,
        )
        second_id, _ = self.insert(
            db, "seis", "https://www.seis.or.kr/boardView.do?boardId=B&nttId=42",
            title="B 공고", score=0.3,
        )
        assert first_id and second_id and first_id != second_id

        rows = db._conn.execute(
            "SELECT title, relevance_score, relevance_reason FROM announcements"
            " ORDER BY title"
        ).fetchall()
        assert [(r["title"], r["relevance_score"]) for r in rows] == [
            ("A 공고", 0.9), ("B 공고", 0.3),
        ]

    def test_g2b_orders_keep_their_own_deadline(self, db):
        """재현 ②: 차수별로 행이 갈리고 마감이 섞이지 않는다."""
        crawler = make(G2bCrawler, "g2b")
        built = []
        for order, close in (("00", "202610311700"), ("01", "202611301700")):
            item = {
                "bidNtceNo": "20260900123", "bidNtceOrd": order,
                "bidNtceNm": f"용역 입찰 {order}차", "bidClseDt": close,
            }
            announcement = crawler._parse_item(item, "용역")
            assert announcement is not None
            built.append(_finalize_periods("g2b", announcement))

        for announcement in built:
            db.insert_announcement(
                AnalyzedAnnouncement(**announcement.__dict__, relevance_score=0.9)
            )

        rows = db._conn.execute(
            "SELECT source_id, title, period_end FROM announcements ORDER BY title"
        ).fetchall()
        assert [(r["title"], r["period_end"]) for r in rows] == [
            ("용역 입찰 00차", "2026-10-31"), ("용역 입찰 01차", "2026-11-30"),
        ]
        assert {r["source_id"] for r in rows} == {
            "fld:20260900123|00", "fld:20260900123|01",
        }

    def test_g2b_template_url_carries_the_order(self, db):
        crawler = make(G2bCrawler, "g2b")
        announcement = crawler._parse_item(
            {"bidNtceNo": "1", "bidNtceOrd": "01", "bidNtceNm": "입찰"}, "용역"
        )
        assert "bidno=1" in announcement.url and "bidseq=01" in announcement.url
        assert json.loads(announcement.raw_data)["url_is_template"] is True

    def test_bizinfo_null_urls_stay_two_rows(self, db):
        """재현 ③: ``detailUrl: null`` 두 건이 합쳐지지 않는다."""
        crawler = make(BizinfoCrawler, "bizinfo")
        built = []
        for pblanc, period, name in (
            ("B1", "20261001~20261031", "10월 공고"),
            ("B2", "20261101~20261130", "11월 공고"),
        ):
            item = {
                "pblancId": pblanc, "pblancNm": name, "detailUrl": None,
                "reqstBeginEndDe": period,
            }
            announcement = crawler._parse_item(item)
            assert announcement is not None
            assert announcement.url and "None" not in announcement.url
            built.append(_finalize_periods("bizinfo", announcement))

        for announcement in built:
            db.insert_announcement(
                AnalyzedAnnouncement(**announcement.__dict__, relevance_score=0.9)
            )

        rows = db._conn.execute(
            "SELECT source_id, title, period_end FROM announcements ORDER BY title"
        ).fetchall()
        assert [(r["title"], r["period_end"]) for r in rows] == [
            ("10월 공고", "2026-10-31"), ("11월 공고", "2026-11-30"),
        ]
        assert {r["source_id"] for r in rows} == {"fld:B1", "fld:B2"}


class TestG2bDeadlineValidation:
    """MEDIUM: ``YYYYMMDDHHMM`` 의 시·분도 검증한다."""

    @pytest.mark.parametrize("value,expected", [
        ("202609301700", "2026-09-30"),
        ("20260930", "2026-09-30"),
        ("202609309999", None),      # 없는 시각 (99시 99분)
        ("202609302400", None),      # 24시는 없다
        ("202609301760", None),      # 60분은 없다
        ("20260931", None),          # 없는 날짜
        ("2026093017", None),        # 길이가 계약 밖
    ])
    def test_close_datetime_is_validated(self, value, expected):
        assert g2b_period({"bidClseDt": value})[1] == expected


class TestLegacyIdentityMigration:
    """예전 source_id 는 1회 이관한다 - 삭제는 하지 않는다."""

    @pytest.fixture
    def db(self, tmp_path):
        yield Database(db_path=tmp_path / "announcements.db")

    def seed_legacy(self, db, source, source_id, url, raw=None, title="공고"):
        db._conn.execute(
            "INSERT INTO announcements (source, source_id, title, url,"
            " raw_data, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source, source_id, title, url,
             json.dumps(raw or {}, ensure_ascii=False),
             datetime.now().isoformat(), datetime.now().isoformat()),
        )
        db._conn.commit()

    def test_legacy_ids_are_migrated_once(self, db):
        url = "https://www.seis.or.kr/subPage.do?fncPbofrSn=42"
        self.seed_legacy(db, "seis", "42", url)
        self.seed_legacy(db, "bizinfo", "B1", "", {"pblancId": "B1"})

        migrated, conflicts = db.migrate_identity_keys()
        assert (migrated, conflicts) == (2, 0)

        stored = {
            row["source"]: row["source_id"]
            for row in db._conn.execute(
                "SELECT source, source_id FROM announcements"
            ).fetchall()
        }
        assert stored["seis"] == identity_key("seis", {"url": url, "raw_data": "{}"})
        assert stored["bizinfo"] == "fld:B1"

        # 멱등
        assert db.migrate_identity_keys() == (0, 0)

    def test_migrated_row_is_found_by_the_new_key(self, db):
        url = "https://www.seis.or.kr/subPage.do?fncPbofrSn=42"
        self.seed_legacy(db, "seis", "42", url)
        db.migrate_identity_keys()

        fresh = announcement("seis", url, {"date": "2026.09.01 ~ 2026.09.30"})
        assert db.exists(fresh) is True
        rows = db._conn.execute("SELECT COUNT(*) AS n FROM announcements").fetchone()
        assert rows["n"] == 1

    def test_conflicts_keep_both_rows(self, db):
        """같은 식별자로 몰리면 **중복을 보존**한다 (삭제 금지)."""
        url = "https://www.seis.or.kr/v.do?id=1"
        self.seed_legacy(db, "seis", "legacy-a", url, title="A")
        self.seed_legacy(db, "seis", "legacy-b", url + "&jsessionid=ZZ", title="B")

        migrated, conflicts = db.migrate_identity_keys()
        assert (migrated, conflicts) == (1, 1)
        rows = db._conn.execute(
            "SELECT source_id, title FROM announcements ORDER BY title"
        ).fetchall()
        assert len(rows) == 2                      # 어느 행도 사라지지 않았다
        assert rows[1]["source_id"] == "legacy-b"  # 충돌한 쪽은 그대로 남는다
