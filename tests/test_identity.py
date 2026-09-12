"""행 식별 회귀 - 키는 **크롤러 ``source_id``** 하나다 (16차 게이트).

13~15차에 걸쳐 URL 정규화 해시·소스별 선언 필드로 행을 다시 식별하려 했다.
사이클마다 새 경합이 나왔다: URL 정규화를 넓게 잡으면 다른 페이지가 합쳐지고,
좁게 잡으면 같은 공고가 갈렸다. 필드 키와 URL 키를 함께 두면 같은 공고가 두
키로 갈려 한쪽에 철회된 기간이 남았고, URL 우선으로 통일하니 **URL 이 번호만
담은 API 공고**(G2B 차수, Bizinfo ``detailUrl="#"``)가 합쳐졌다.

그래서 식별자 재설계를 폐기했다. 남은 것은 **범위가 좁은 두 가지**다:

1. seis 의 ``source_id`` 가 공고 **종류·연도·게시판**을 담는다 - 같은 번호를
   다시 쓰는 다른 공고가 한 행을 덮어쓰던 실재 결함(12·13차)의 수리
2. 옛 규칙(번호만)으로 저장된 seis 행은 **표시만** 하고 어둡게 둔다
"""

import ast
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.bizinfo import BizinfoCrawler
from alert.crawlers.g2b import G2bCrawler
from alert.crawlers.identity import clean_text, is_legacy_source_id
from alert.crawlers.period_extractors import EVIDENCE_KEYS
from alert.crawlers.seis import SeisCrawler
from alert.db import Database
from alert.main import _finalize_periods
from alert.models import AnalyzedAnnouncement, RawAnnouncement

REPO = Path(__file__).resolve().parents[1]


def make(cls, source_name="test"):
    config = MagicMock()
    config.crawler.timeout = 10
    config.crawler.retry_count = 1
    config.crawler.retry_delay = 0
    config.crawler.user_agent = "test-agent"
    config.crawler.sources = {}
    with patch("alert.crawlers.base.get_config", return_value=config):
        return cls()


class TestCleanText:
    """``str(None)`` 이 만드는 문자열 ``"None"`` 을 막는다 (13차 게이트)."""

    @pytest.mark.parametrize("value", [None, "None", "null", "  none  ", ""])
    def test_absent_values_become_empty(self, value):
        assert clean_text(value) == ""

    def test_real_values_survive(self):
        assert clean_text("  https://x.kr/a  ") == "https://x.kr/a"

    def test_bizinfo_null_url_never_becomes_a_string(self):
        crawler = make(BizinfoCrawler, "bizinfo")
        announcement = crawler._parse_item({
            "pblancId": "B1", "pblancNm": "공고", "detailUrl": None,
        })
        assert announcement is not None
        assert "None" not in announcement.url


class TestSeisSourceIdSeparatesAnnouncements:
    """seis ``source_id`` 는 종류·연도·게시판을 담는다."""

    @pytest.fixture
    def crawler(self):
        return make(SeisCrawler, "seis")

    @pytest.mark.parametrize("link,expected", [
        ("/subPage.do?fncPbofrSn=8371", "fnc:8371"),
        ("/subPage.do?dsgnPbofrSn=8322", "dsgn:8322"),
        ("/subPage.do?itgrdAplyPbancSn=1272", "itgrd:1272"),
        ("/subPage.do?statsYr=2026&epsdNo=4", "epsd:2026:4"),
        ("/subPage.do?statsYr=2027&epsdNo=4", "epsd:2027:4"),
        ("/boardView.do?boardId=A&nttId=42", "ntt:A:42"),
        ("/boardView.do?boardId=B&nttId=42", "ntt:B:42"),
        # 17차 게이트: ``sid`` 도 게시판 구분자다
        ("/boardView.do?sid=A&nttId=42", "ntt:A:42"),
        ("/boardView.do?sid=B&nttId=42", "ntt:B:42"),
        ("/subPage.do?bsIdx=10002&bIdx=252629", "bidx:10002:252629"),
    ])
    def test_extract_post_id(self, crawler, link, expected):
        assert crawler._extract_post_id(link) == expected

    @pytest.mark.parametrize("first,second", [
        # 같은 번호, 다른 종류 (12차 게이트)
        ("/subPage.do?fncPbofrSn=42", "/subPage.do?dsgnPbofrSn=42"),
        # 같은 회차 번호, 다른 연도 (12차 게이트)
        ("/subPage.do?statsYr=2026&epsdNo=4", "/subPage.do?statsYr=2027&epsdNo=4"),
        # 같은 글번호, 다른 게시판 (13·17차 게이트)
        ("/boardView.do?boardId=A&nttId=42", "/boardView.do?boardId=B&nttId=42"),
        ("/boardView.do?sid=A&nttId=42", "/boardView.do?sid=B&nttId=42"),
        ("/subPage.do?bsIdx=1&bIdx=99", "/subPage.do?bsIdx=2&bIdx=99"),
    ])
    def test_same_number_in_a_different_slot_never_collides(
        self, crawler, first, second
    ):
        assert crawler._extract_post_id(first) != crawler._extract_post_id(second)

    def test_every_id_carries_a_prefix(self, crawler):
        """접두 없는 ID 는 **옛 규칙의 행**으로 판정되므로 남기면 안 된다."""
        for link in ("/subPage.do?fncPbofrSn=1", "/view/123456",
                     "https://example.com/some-page"):
            assert ":" in crawler._extract_post_id(link), link

    # 파서별 최소 입력 - **모든 id 생성 경로**를 지난다
    PARSER_CASES = [
        ("main_card", "_parse_main_cards",
         '<ul><li class="swiper-slide" data-type="사업공고">'
         '<span class="sub">서울센터</span>'
         '<p class="tit"><a href="subPage.do?fncPbofrSn=1">공고</a></p>'
         '<ul class="info"><li>교육</li></ul>'
         '<p class="date">2026.09.01 ~ 2026.09.30</p></li></ul>'),
        ("table_with_href", "_parse_table_board",
         '<table class="board_list"><thead><tr><th>제목</th><th>등록일</th>'
         "</tr></thead><tbody><tr>"
         '<td><a href="/view.do?sid=A&nttId=42">공고</a></td>'
         "<td>2026.09.11</td></tr></tbody></table>"),
        # 17차 MEDIUM: href 가 없는 표 제목 - 제목 MD5 경로
        ("table_without_href", "_parse_table_board",
         '<table class="board_list"><thead><tr><th>제목</th><th>등록일</th>'
         "</tr></thead><tbody><tr>"
         "<td><a>href 없는 공고</a></td>"
         "<td>2026.09.11</td></tr></tbody></table>"),
        ("list_board", "_parse_list_board",
         '<ul class="board_list"><li><div>'
         '<a href="/view.do?nttId=7">공고</a>2026.09.11</div></li></ul>'),
        ("generic_links", "_parse_generic_links",
         '<div><a href="/subPage.do?tabId=view&itgrdAplyPbancSn=3">'
         "일반 링크 공고</a></div>"),
    ]

    @pytest.mark.parametrize(
        "name,parser,html", PARSER_CASES, ids=[c[0] for c in PARSER_CASES]
    )
    def test_no_parser_path_produces_a_prefixless_id(
        self, crawler, name, parser, html
    ):
        """어떤 파서 경로도 ``:`` 없는 ID 를 만들지 않는다 (17차 MEDIUM).

        접두 없는 seis ID 는 옛 규칙의 행으로 판정되어, **새 수집이 매번
        레거시로 표시되고 기간이 지워졌다가 다시 채워진다**.
        """
        from bs4 import BeautifulSoup

        items = getattr(crawler, parser)(BeautifulSoup(html, "html.parser"))
        assert items, name
        built = [
            crawler._to_announcement(item, "https://www.seis.or.kr")
            for item in items
        ]
        built = [a for a in built if a is not None]
        assert built, name
        for announcement in built:
            assert ":" in announcement.source_id, (name, announcement.source_id)
            assert is_legacy_source_id("seis", announcement.source_id) is False


class TestRowsNeverOverwriteEachOther:
    """저장·조회는 ``(source, source_id)`` 하나로 한다."""

    @pytest.fixture
    def db(self, tmp_path):
        yield Database(db_path=tmp_path / "announcements.db")

    def store(self, db, announcement, score=0.9):
        return db.insert_announcement(
            AnalyzedAnnouncement(
                **_finalize_periods(announcement.source, announcement).__dict__,
                relevance_score=score,
            )
        )

    def test_seis_two_boards_stay_two_rows(self, db):
        crawler = make(SeisCrawler, "seis")
        for board, title in (("A", "A 공고"), ("B", "B 공고")):
            item = {
                "title": title,
                "link": f"/boardView.do?boardId={board}&nttId=42",
                "date": "",
            }
            self.store(db, crawler._to_announcement(item, "https://www.seis.or.kr"))

        rows = db._conn.execute(
            "SELECT source_id, title FROM announcements ORDER BY title"
        ).fetchall()
        assert [(r["source_id"], r["title"]) for r in rows] == [
            ("ntt:A:42", "A 공고"), ("ntt:B:42", "B 공고"),
        ]

    def test_seis_sid_variants_stay_two_rows(self, db):
        """17차 HIGH 재현: ``sid=A/B`` + 같은 글번호 → 2행, 덮어쓰기 없음."""
        crawler = make(SeisCrawler, "seis")
        for sid, title in (("A", "A 공고"), ("B", "B 공고")):
            item = {
                "title": title,
                "link": f"/boardView.do?sid={sid}&nttId=42",
                "date": f"접수기간 2026.{'10' if sid == 'A' else '11'}.01"
                        f" ~ 2026.{'10' if sid == 'A' else '11'}.30",
                "date_field": "li.swiper-slide p.date",
            }
            self.store(db, crawler._to_announcement(item, "https://www.seis.or.kr"))

        rows = db._conn.execute(
            "SELECT source_id, title, period_end FROM announcements ORDER BY title"
        ).fetchall()
        assert [(r["source_id"], r["title"], r["period_end"]) for r in rows] == [
            ("ntt:A:42", "A 공고", "2026-10-30"),
            ("ntt:B:42", "B 공고", "2026-11-30"),
        ]

    def test_g2b_orders_keep_their_own_deadline(self, db):
        """차수는 크롤러가 이미 ``source_id`` 에 넣는다 (기간은 화이트리스트 밖)."""
        crawler = make(G2bCrawler, "g2b")
        for order, close in (("00", "202610311700"), ("01", "202611301700")):
            announcement = crawler._parse_item({
                "bidNtceNo": "20260900123", "bidNtceOrd": order,
                "bidNtceNm": f"용역 입찰 {order}차", "bidClseDt": close,
            }, "용역")
            assert announcement is not None
            self.store(db, announcement)

        rows = db._conn.execute(
            "SELECT source_id, title, period_end FROM announcements ORDER BY title"
        ).fetchall()
        assert [r["source_id"] for r in rows] == [
            "20260900123-00", "20260900123-01",
        ]
        # 16차: g2b 는 허용목록 밖이므로 기간을 만들지 않는다
        assert all(r["period_end"] is None for r in rows)

    def test_bizinfo_ids_stay_two_rows(self, db):
        crawler = make(BizinfoCrawler, "bizinfo")
        for pblanc, name in (("B1", "10월 공고"), ("B2", "11월 공고")):
            announcement = crawler._parse_item({
                "pblancId": pblanc, "pblancNm": name, "detailUrl": None,
                "reqstBeginEndDe": "20261001~20261031",
            })
            assert announcement is not None
            self.store(db, announcement)

        rows = db._conn.execute(
            "SELECT source_id, period_end FROM announcements ORDER BY source_id"
        ).fetchall()
        assert [r["source_id"] for r in rows] == ["B1", "B2"]
        assert all(r["period_end"] is None for r in rows)   # 허용목록 밖

    def test_the_same_id_is_the_same_row(self, db):
        """같은 ID = 같은 공고 - 점수·사유만 갱신하고 행은 하나다."""
        crawler = make(SeisCrawler, "seis")
        item = {"title": "공고", "link": "/subPage.do?fncPbofrSn=7", "date": ""}
        first = self.store(db, crawler._to_announcement(item, "https://x"), 0.9)
        second = self.store(db, crawler._to_announcement(item, "https://x"), 0.3)
        assert first and second is None                  # 중복은 새 행이 아니다
        rows = db._conn.execute("SELECT COUNT(*) AS n FROM announcements").fetchone()
        assert rows["n"] == 1


class TestEveryDbPathUsesTheSameKey:
    """조회·저장·갱신이 같은 키를 쓴다 (우회 0)."""

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

    def test_only_find_row_and_the_legacy_helper_look_up_by_source_id(self):
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

    def test_insert_does_not_rewrite_the_id(self):
        """16차: 저장 직전 식별자를 다시 만들지 않는다."""
        source = (REPO / "alert" / "db.py").read_text(encoding="utf-8")
        assert "identity_of" not in source
        assert "identity_key(" not in source

    def test_url_based_identification_is_gone(self):
        """URL 해시·필드 키 기계가 남아 있지 않다."""
        identity = (REPO / "alert" / "crawlers" / "identity.py").read_text(
            encoding="utf-8"
        )
        for dead in ("normalize_url", "IDENTITY_FIELDS", "sha1", "urlsplit"):
            assert dead not in identity, dead


class TestLegacyRowsGoDark:
    """옛 규칙(번호만)으로 저장된 **seis** 행은 표시만 한다 - 삭제는 없다."""

    @pytest.fixture
    def db(self, tmp_path):
        yield Database(db_path=tmp_path / "announcements.db")

    def seed(self, db, source, source_id, title="공고", period_end="2026-10-31"):
        db._conn.execute(
            "INSERT INTO announcements (source, source_id, title, url,"
            " raw_data, period_start, period_end, relevance_score,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source, source_id, title, f"https://x/{source_id}",
             json.dumps({"date": "2026.10.01 ~ 2026.10.31",
                         "date_field": "li.swiper-slide p.date"},
                        ensure_ascii=False),
             "2026-10-01", period_end, 0.9,
             datetime.now().isoformat(), datetime.now().isoformat()),
        )
        db._conn.commit()

    @pytest.mark.parametrize("source,source_id,expected", [
        ("seis", "8371", True),           # 옛 규칙: 번호만
        ("seis", "fnc:8371", False),
        ("seis", "epsd:2026:4", False),
        ("seis", "md5:abcd", False),
        # 다른 소스의 ID 형식은 바뀌지 않았다 - 건드리지 않는다
        ("bizinfo", "PBLN_000", False),
        ("g2b", "20260900123-00", False),
        ("kofpi", "12658", False),
        ("manual", "abc123", False),
    ])
    def test_legacy_rule(self, source, source_id, expected):
        assert is_legacy_source_id(source, source_id) is expected

    def test_legacy_seis_rows_are_marked_and_silenced(self, db):
        self.seed(db, "seis", "8371", title="옛 seis 행")
        assert len(db.get_unnotified()) == 1

        assert db.mark_legacy_rows(EVIDENCE_KEYS) == 1

        row = db._conn.execute(
            "SELECT legacy, period_start, period_end, raw_data, title"
            " FROM announcements"
        ).fetchone()
        assert row["legacy"] == 1
        assert (row["period_start"], row["period_end"]) == (None, None)
        assert json.loads(row["raw_data"]) == {}          # 근거 삭제
        assert row["title"] == "옛 seis 행"                # 행은 남아 있다
        assert db.get_unnotified() == []
        assert db.get_announcements_by_period("2000", "2100") == []

        assert db.mark_legacy_rows(EVIDENCE_KEYS) == 0    # 멱등

    def test_other_sources_are_never_marked(self, db):
        for source, source_id in (("kofpi", "12658"), ("bizinfo", "PBLN_1"),
                                  ("g2b", "2026-00"), ("fowi", "9191")):
            self.seed(db, source, source_id)
        assert db.mark_legacy_rows(EVIDENCE_KEYS) == 0
        assert len(db.get_unnotified()) == 4

    def test_new_seis_rows_are_untouched(self, db):
        self.seed(db, "seis", "fnc:8371", period_end="2026-11-30")
        assert db.mark_legacy_rows(EVIDENCE_KEYS) == 0
        row = db._conn.execute(
            "SELECT legacy, period_end FROM announcements"
        ).fetchone()
        assert (row["legacy"], row["period_end"]) == (0, "2026-11-30")

    def test_recollected_row_becomes_the_truth(self, db):
        """옛 행이 있어도 재수집은 **새 행**을 만들고 그것이 정본이다."""
        self.seed(db, "seis", "8371", title="옛 행")
        db.mark_legacy_rows(EVIDENCE_KEYS)

        fresh = RawAnnouncement(
            source="seis", source_id="fnc:8371", title="새 행",
            url="https://www.seis.or.kr/subPage.do?fncPbofrSn=8371",
            raw_data=json.dumps(
                {"date": "접수기간 2026.09.01 ~ 2026.09.30"}, ensure_ascii=False
            ),
        )
        assert db.exists(fresh) is False
        db.insert_announcement(AnalyzedAnnouncement(
            **_finalize_periods("seis", fresh).__dict__, relevance_score=0.9,
        ))
        rows = db._conn.execute(
            "SELECT source_id, legacy, period_end FROM announcements"
            " ORDER BY legacy"
        ).fetchall()
        assert [(r["source_id"], r["legacy"], r["period_end"]) for r in rows] == [
            ("fnc:8371", 0, "2026-09-30"), ("8371", 1, None),
        ]
        assert [a.source_id for a in db.get_unnotified()] == ["fnc:8371"]
