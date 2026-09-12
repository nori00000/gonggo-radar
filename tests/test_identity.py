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
from alert.crawlers.period_extractors import EVIDENCE_KEYS, g2b_period
from alert.db import Database
from alert.main import _finalize_periods, _periods_from_raw
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

    @pytest.mark.parametrize("noise", [
        "jsessionid=ZZ", "JSESSIONID=ZZ", "PHPSESSID=ZZ", "_=99",
        "fbclid=abc", "utm_source=mail", "utm_campaign=x",
    ])
    def test_known_session_and_tracking_parameters_are_dropped(self, noise):
        assert normalize_url(f"https://x.kr/a?id=1&{noise}") == (
            normalize_url("https://x.kr/a?id=1")
        )

    @pytest.mark.parametrize("param", [
        "sid=A", "ts=1", "rnd=7", "sessionid=A", "aspsessionid=A",
        "timestamp=5", "random=3", "cachebust=9",
    ])
    def test_unknown_parameters_are_kept(self, param):
        """14차 게이트: 의미를 모르는 파라미터는 **남긴다**.

        ``sid`` 를 지웠더니 ``/boardView.do?sid=A&nttId=42`` 와 ``sid=B`` 가
        한 행으로 합쳐져, A 제목에 B 의 마감이 저장·전달됐다.
        """
        assert normalize_url(f"https://x.kr/a?id=1&{param}") != (
            normalize_url("https://x.kr/a?id=1")
        )

    def test_sid_variants_are_separate_rows(self):
        first = identity_key("seis", announcement(
            "seis", "https://www.seis.or.kr/boardView.do?sid=A&nttId=42"))
        second = identity_key("seis", announcement(
            "seis", "https://www.seis.or.kr/boardView.do?sid=B&nttId=42"))
        assert first != second

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

    def test_the_url_wins_even_for_api_sources(self):
        """15차 게이트: 키를 두 갈래로 두면 같은 공고가 갈린다.

        같은 URL 을 ①ID 없이 ②ID 와 함께 수집하면 예전에는 URL 키 행과
        필드 키 행으로 갈려, 한쪽에 철회된 기간이 남았다.
        """
        assert IDENTITY_FIELDS["bizinfo"] == ("pblancId",)
        url = "https://www.bizinfo.go.kr/view.do?pblancId=B1"
        without_id = identity_key("bizinfo", announcement("bizinfo", url))
        with_id = identity_key("bizinfo", announcement(
            "bizinfo", url, {"pblancId": "B1"}))
        assert without_id == with_id
        assert with_id.startswith("url:")

    def test_declared_fields_are_the_fallback_when_no_url(self):
        """URL 이 **전혀 없을 때만** 선언 필드를 쓴다."""
        assert identity_key("bizinfo", announcement(
            "bizinfo", None, {"pblancId": "B1"})) == "fld:B1"

    def test_g2b_order_is_part_of_the_identity(self):
        """차수가 다르면 마감이 다른 별개 공고다."""
        base = {"bidNtceNo": "20260900123"}
        first = identity_key("g2b", announcement(
            "g2b", None, {**base, "bidNtceOrd": "00"}))
        second = identity_key("g2b", announcement(
            "g2b", None, {**base, "bidNtceOrd": "01"}))
        assert (first, second) == ("fld:20260900123|00", "fld:20260900123|01")

    def test_partial_declared_fields_fall_back_to_the_url(self):
        """14차 게이트: 반쪽 필드 키(``fld:번호|``)는 다른 공고와 겹친다."""
        first = identity_key("g2b", announcement(
            "g2b", "https://g2b.kr/d.do?bidno=1&bidseq=00", {"bidNtceNo": "1"}))
        second = identity_key("g2b", announcement(
            "g2b", "https://g2b.kr/d.do?bidno=1&bidseq=01", {"bidNtceNo": "1"}))
        assert first != second
        assert first.startswith("url:") and second.startswith("url:")

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
        # URL(템플릿)이 차수를 담으므로 키는 URL 키다 (15차: URL 우선)
        keys = {r["source_id"] for r in rows}
        assert len(keys) == 2 and all(k.startswith("url:") for k in keys)

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
        keys = {r["source_id"] for r in rows}
        assert len(keys) == 2 and all(k.startswith("url:") for k in keys)


class TestPartialIdentifiersKeepTheAnnouncement:
    """식별 필드가 반쪽이거나 없어도 **공고를 버리지 않는다** (14차 게이트)."""

    @pytest.fixture
    def db(self, tmp_path):
        yield Database(db_path=tmp_path / "announcements.db")

    def store(self, db, announcement):
        db.insert_announcement(
            AnalyzedAnnouncement(
                **_finalize_periods(announcement.source, announcement).__dict__,
                relevance_score=0.9,
            )
        )

    def test_g2b_without_order_splits_by_url(self, db):
        """차수 필드가 없어도 URL 의 ``bidseq`` 가 공고를 가른다."""
        crawler = make(G2bCrawler, "g2b")
        for seq, close in (("00", "202610311700"), ("01", "202611301700")):
            announcement = crawler._parse_item({
                "bidNtceNo": "20260900123",
                "bidNtceNm": f"용역 입찰 {seq}",
                "bidNtceUrl": f"https://g2b.kr/detail.do?bidno=1&bidseq={seq}",
                "bidClseDt": close,
            }, "용역")
            assert announcement is not None
            self.store(db, announcement)

        rows = db._conn.execute(
            "SELECT source_id, title, period_end FROM announcements ORDER BY title"
        ).fetchall()
        assert [(r["title"], r["period_end"]) for r in rows] == [
            ("용역 입찰 00", "2026-10-31"), ("용역 입찰 01", "2026-11-30"),
        ]
        assert len({r["source_id"] for r in rows}) == 2
        assert all(r["source_id"].startswith("url:") for r in rows)

    def test_g2b_without_a_notice_number_is_still_kept(self, db):
        crawler = make(G2bCrawler, "g2b")
        announcement = crawler._parse_item({
            "bidNtceNm": "번호 없는 입찰",
            "bidNtceUrl": "https://g2b.kr/detail.do?x=9",
            "bidClseDt": "202610311700",
        }, "용역")
        assert announcement is not None
        self.store(db, announcement)
        row = db._conn.execute(
            "SELECT source_id, period_end FROM announcements"
        ).fetchone()
        assert row["source_id"].startswith("url:")
        assert row["period_end"] == "2026-10-31"

    def test_bizinfo_without_pblanc_id_is_still_kept(self, db):
        crawler = make(BizinfoCrawler, "bizinfo")
        announcement = crawler._parse_item({
            "pblancId": None,
            "pblancNm": "ID 없는 공고",
            "detailUrl": "https://www.bizinfo.go.kr/view.do?x=7",
            "reqstBeginEndDe": "20261001~20261031",
        })
        assert announcement is not None
        self.store(db, announcement)
        row = db._conn.execute(
            "SELECT source_id, url, period_end FROM announcements"
        ).fetchone()
        assert row["source_id"].startswith("url:")
        assert row["url"] == "https://www.bizinfo.go.kr/view.do?x=7"
        assert row["period_end"] == "2026-10-31"

    def test_items_without_any_identifier_are_still_dropped(self, db):
        """제목뿐인 항목은 여전히 버린다 - 식별할 수단이 없다."""
        crawler = make(BizinfoCrawler, "bizinfo")
        assert crawler._parse_item({"pblancNm": "제목만"}) is None


class TestDuplicateGroupsMoveTogether:
    """``duplicate_of`` 로 묶인 행은 **그룹 전체**가 함께 갱신된다."""

    URL = "https://www.seis.or.kr/subPage.do?fncPbofrSn=1"
    EVIDENCE = ("date", "date_field", "dday")

    @pytest.fixture
    def db(self, tmp_path):
        yield Database(db_path=tmp_path / "announcements.db")

    def grouped(self, db):
        """대표 1행 + 묶인 중복 1행 (옛 10월 기간을 들고 있다)."""
        raw = json.dumps(
            {"date": "2026.10.01 ~ 2026.10.31",
             "date_field": "li.swiper-slide p.date"},
            ensure_ascii=False,
        )
        representative = AnalyzedAnnouncement(
            source="seis", source_id="ignored", title="지원사업 공고",
            url=self.URL, raw_data=raw, period_start="2026-10-01",
            period_end="2026-10-31", relevance_score=0.9,
            fetched_at=datetime.now().isoformat(),
        )
        rep_id = db.insert_announcement(representative)
        db._conn.execute(
            "INSERT INTO announcements (source, source_id, title, url,"
            " raw_data, period_start, period_end, relevance_score,"
            " duplicate_of, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("seis", "legacy-b", "지원사업 공고", self.URL + "&jsessionid=ZZ",
             raw, "2026-10-01", "2026-10-31", 0.9, rep_id,
             datetime.now().isoformat(), datetime.now().isoformat()),
        )
        db._conn.commit()
        return rep_id

    def test_retraction_clears_the_whole_group(self, db):
        """철회된 기간이 묶인 행에 남아 기간 조회에 나오지 않는다."""
        self.grouped(db)
        retracted = RawAnnouncement(
            source="seis", source_id="ignored", title="지원사업 공고",
            url=self.URL,
            raw_data=json.dumps(
                {"date": "접수기간 미정 / 교육기간 2026.10.01 ~ 2026.10.31",
                 "date_field": "li.swiper-slide p.date"},
                ensure_ascii=False,
            ),
        )
        gated = _finalize_periods("seis", retracted)
        assert db.overwrite_periods(gated, self.EVIDENCE) is True

        rows = db._conn.execute(
            "SELECT period_start, period_end FROM announcements"
        ).fetchall()
        assert [(r["period_start"], r["period_end"]) for r in rows] == [
            (None, None), (None, None),
        ]

        assert db.revalidate_periods(
            "seis", lambda raw: _periods_from_raw("seis", raw)
        ) == 0
        notified = db.get_unnotified()
        assert len(notified) == 1                   # 알림은 대표만
        assert notified[0].period_end is None

    def test_group_is_rewritten_even_when_the_leader_is_current(self, db):
        """15차 MEDIUM: 대표가 이미 최신이어도 묶인 행을 갱신한다."""
        rep_id = self.grouped(db)
        # 대표만 먼저 비운다 (묶인 행은 10월을 그대로 들고 있다)
        db._conn.execute(
            "UPDATE announcements SET period_start = NULL, period_end = NULL,"
            " raw_data = '{}' WHERE id = ?",
            (rep_id,),
        )
        db._conn.commit()

        empty = RawAnnouncement(
            source="seis", source_id="ignored", title="지원사업 공고",
            url=self.URL, raw_data="{}",
        )
        gated = _finalize_periods("seis", empty)
        assert db.overwrite_periods(gated, self.EVIDENCE) is True

        ends = [
            row["period_end"] for row in db._conn.execute(
                "SELECT period_end FROM announcements"
            ).fetchall()
        ]
        assert ends == [None, None]

    def test_duplicates_are_never_notified(self, db):
        self.grouped(db)
        assert len(db.get_unnotified()) == 1


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


class TestLegacyRowsGoDark:
    """옛 규칙으로 저장된 행은 **표시만** 한다 - 이관도 삭제도 없다.

    15차 게이트: 예전 ``source_id`` 를 새 식별자로 **추측해 이관**했더니
    서로 다른 공고가 같은 키로 수렴해 중복으로 묶이고, 남의 기간이
    덮어써졌다. 추측하지 않고 어둡게 둔다(fail-safe).
    """

    @pytest.fixture
    def db(self, tmp_path):
        yield Database(db_path=tmp_path / "announcements.db")

    def seed_legacy(self, db, source, source_id, url, raw=None,
                    title="공고", period_end="2026-10-31"):
        db._conn.execute(
            "INSERT INTO announcements (source, source_id, title, url,"
            " raw_data, period_start, period_end, relevance_score,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source, source_id, title, url,
             json.dumps(raw or {}, ensure_ascii=False),
             "2026-10-01", period_end, 0.9,
             datetime.now().isoformat(), datetime.now().isoformat()),
        )
        db._conn.commit()

    def test_legacy_rows_are_marked_and_silenced(self, db):
        """기간·근거를 지우고 알림·다이제스트에서 뺀다 (삭제는 없다)."""
        self.seed_legacy(
            db, "seis", "ntt:42",
            "https://www.seis.or.kr/boardView.do?nttId=42",
            {"date": "2026.10.01 ~ 2026.10.31",
             "date_field": "li.swiper-slide p.date"},
            title="레거시 B",
        )
        assert len(db.get_unnotified()) == 1

        assert db.mark_legacy_rows(EVIDENCE_KEYS) == 1

        row = db._conn.execute(
            "SELECT legacy, period_start, period_end, raw_data, title"
            " FROM announcements"
        ).fetchone()
        assert row["legacy"] == 1
        assert (row["period_start"], row["period_end"]) == (None, None)
        assert json.loads(row["raw_data"]) == {}          # 근거 삭제
        assert row["title"] == "레거시 B"                  # 행은 남아 있다
        assert db.get_unnotified() == []                  # 알림 제외
        assert db.get_announcements_by_period("2000", "2100") == []

        assert db.mark_legacy_rows(EVIDENCE_KEYS) == 0    # 멱등

    def test_new_rows_are_untouched(self, db):
        db.insert_announcement(AnalyzedAnnouncement(
            source="seis", source_id="ignored", title="새 A",
            url="https://www.seis.or.kr/boardView.do?sid=A&nttId=42",
            raw_data="{}", period_end="2026-11-30", relevance_score=0.9,
            fetched_at=datetime.now().isoformat(),
        ))
        assert db.mark_legacy_rows(EVIDENCE_KEYS) == 0
        row = db._conn.execute(
            "SELECT legacy, period_end FROM announcements"
        ).fetchone()
        assert (row["legacy"], row["period_end"]) == (0, "2026-11-30")

    def test_legacy_and_fresh_rows_coexist(self, db):
        """재현: 레거시 B 와 새 A 는 **별개 행**이고 A 만 살아 있다."""
        self.seed_legacy(
            db, "seis", "ntt:42",
            "https://www.seis.or.kr/boardView.do?nttId=42",
            title="레거시 B",
        )
        db.insert_announcement(AnalyzedAnnouncement(
            source="seis", source_id="ignored", title="새 A",
            url="https://www.seis.or.kr/boardView.do?sid=A&nttId=42",
            raw_data="{}", period_end="2026-11-30", relevance_score=0.9,
            fetched_at=datetime.now().isoformat(),
        ))

        assert db.mark_legacy_rows(EVIDENCE_KEYS) == 1
        rows = db._conn.execute(
            "SELECT title, legacy, period_end FROM announcements ORDER BY title"
        ).fetchall()
        assert [(r["title"], r["legacy"], r["period_end"]) for r in rows] == [
            ("레거시 B", 1, None), ("새 A", 0, "2026-11-30"),
        ]
        assert [a.title for a in db.get_unnotified()] == ["새 A"]

    def test_recollected_row_becomes_the_truth(self, db):
        """레거시 행이 있어도 재수집은 **새 행**을 만들고 그것이 정본이다."""
        url = "https://www.seis.or.kr/subPage.do?fncPbofrSn=42"
        self.seed_legacy(db, "seis", "42", url, title="옛 행")
        db.mark_legacy_rows(EVIDENCE_KEYS)

        fresh = announcement(
            "seis", url, {"date": "접수기간 2026.09.01 ~ 2026.09.30"}
        )
        assert db.exists(fresh) is False               # 레거시 키와 만나지 않는다
        db.insert_announcement(AnalyzedAnnouncement(
            **_finalize_periods("seis", fresh).__dict__, relevance_score=0.9,
        ))
        rows = db._conn.execute(
            "SELECT legacy, period_end FROM announcements ORDER BY legacy"
        ).fetchall()
        assert [(r["legacy"], r["period_end"]) for r in rows] == [
            (0, "2026-09-30"), (1, None),
        ]
        assert len(db.get_unnotified()) == 1


class TestBizinfoUrlConvergence:
    """같은 URL 의 ①ID 없음 ②ID 있음 수집은 **한 행**으로 수렴한다."""

    URL = "https://www.bizinfo.go.kr/view.do?pblancId=B1"

    @pytest.fixture
    def db(self, tmp_path):
        yield Database(db_path=tmp_path / "announcements.db")

    def collect(self, db, crawler, item):
        built = crawler._parse_item(item)
        assert built is not None
        gated = _finalize_periods("bizinfo", built)
        if db.exists(gated):
            db.overwrite_periods(gated, EVIDENCE_KEYS["bizinfo"])
        else:
            db.insert_announcement(
                AnalyzedAnnouncement(**gated.__dict__, relevance_score=0.9)
            )
        return gated

    def test_same_url_converges_and_retraction_applies(self, db):
        crawler = make(BizinfoCrawler, "bizinfo")
        self.collect(db, crawler, {
            "pblancNm": "지원 공고", "detailUrl": self.URL,
            "reqstBeginEndDe": "20261001~20261031",
        })
        assert db._conn.execute(
            "SELECT period_end FROM announcements"
        ).fetchone()["period_end"] == "2026-10-31"

        self.collect(db, crawler, {
            "pblancId": "B1", "pblancNm": "지원 공고", "detailUrl": self.URL,
            "reqstBeginEndDe": "",
        })
        rows = db._conn.execute(
            "SELECT period_end FROM announcements"
        ).fetchall()
        assert len(rows) == 1                       # 한 행으로 수렴
        assert rows[0]["period_end"] is None        # 철회가 반영된다
        assert [a.period_end for a in db.get_unnotified()] == [None]
