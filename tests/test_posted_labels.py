"""목록 날짜 회귀 - 목록의 날짜는 **게시일 증거**로만 남는다.

6차 게이트 #1: 목록 HTML의 ``게시일=2026.09.11`` 이
``period_start=period_end=2026-09-11`` 로 저장됐다.

13차에서 라벨 분류(``classify_date``)를 **삭제**했다. 여섯 차례에 걸쳐
허용목록을 좁혀도 라벨 추론은 계속 새어 들어왔다(구조 라벨·제목 라벨·
인접 셀). 지금 기간을 만들 수 있는 것은 소스 전용 순수 함수
(``period_extractors``) 둘뿐이고, 이 모듈은 **날짜를 읽어 게시일로 남기는
텍스트 유틸**만 담당한다. 그래서 이 파일은 두 가지를 고정한다:

1. 실 fixture 파서가 어떤 라벨을 만나도 기간을 만들지 않는다
2. 그러면서 날짜 자체는 ``raw_data.posted`` 로 살아 남는다

fixture 는 라이브 목록 페이지에서 받아 잘랐다(2026-09-13).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from alert.crawlers.date_labels import (
    extract_date_and_label,
    posted_date,
    strip_notes,
)
from alert.crawlers.forest_press import ForestPressCrawler
from alert.crawlers.forest_service import ForestServiceCrawler
from alert.crawlers.fowi import FowiCrawler
from alert.crawlers.ipet import IpetCrawler
from alert.crawlers.nongup_gg import NongupGgCrawler
from alert.crawlers.period_extractors import lawmaking_period, seis_period
from alert.crawlers.seis import SeisCrawler

FIXTURES = Path(__file__).parent / "fixtures"

# (소스 이름, 크롤러, fixture, base_url, 기대 라벨, 실제 진입 파서)
# 파서 이름을 명시한다 - "전략을 순서대로 시도" 하면 그 크롤러가 실제로 쓰지
# 않는 폴백 전략을 검사하게 되어 회귀를 놓친다(이 테스트를 처음 짤 때 겪었다).
CASES = [
    ("nongup_gg", NongupGgCrawler, "nongup_gg_list.html",
     "https://nongup.gg.go.kr", "작성일", lambda c, s: c._parse_table_board(s)),
    ("forest_service", ForestServiceCrawler, "forest_service_list.html",
     "https://www.forest.go.kr", "작성일", lambda c, s: c._parse_table_board(s)),
    ("ipet", IpetCrawler, "ipet_list.html",
     "https://www.ipet.re.kr", "등록일", lambda c, s: c._parse_table_board(s)),
    ("fowi", FowiCrawler, "fowi_board_list.html",
     "https://fowi.or.kr", "등록일", lambda c, s: c._parse_goview_board(s, "MNG")),
    ("forest_press", ForestPressCrawler, "forest_press_list.html",
     "https://www.forest.go.kr", "게시일", lambda c, s: c._parse_press_list(s)),
]


def make_crawler(name, cls, base_url):
    config = MagicMock()
    config.crawler.timeout = 10
    config.crawler.retry_count = 1
    config.crawler.retry_delay = 0
    config.crawler.user_agent = "test-agent"
    source = MagicMock()
    source.enabled = True
    source.base_url = base_url
    source.fetch_detail = False
    config.crawler.sources = {name: source}
    with patch("alert.crawlers.base.get_config", return_value=config):
        return cls()


def extract(html):
    item = BeautifulSoup(html, "html.parser").find("li")
    return extract_date_and_label(item)


@pytest.mark.parametrize("name,cls,fixture,base_url,label,parser", CASES)
class TestPostedLabelsAcrossCrawlers:
    """다섯 소스 모두 목록 날짜를 게시일로 기록하고 기간은 만들지 않는다."""

    def announcements(self, name, cls, fixture, base_url, parser):
        crawler = make_crawler(name, cls, base_url)
        soup = BeautifulSoup(
            (FIXTURES / fixture).read_text(encoding="utf-8"), "html.parser"
        )
        items = parser(crawler, soup)
        assert items, f"{name}: fixture 에서 항목을 파싱하지 못했다"
        built = [crawler._to_announcement(item, base_url) for item in items]
        return items, [a for a in built if a is not None]

    def test_no_row_gets_a_period(self, name, cls, fixture, base_url, label, parser):
        """전용 추출기가 없는 소스는 어떤 라벨에서도 기간을 만들지 않는다."""
        _items, announcements = self.announcements(
            name, cls, fixture, base_url, parser
        )
        assert announcements
        offenders = [
            (a.source_id, a.period_start, a.period_end)
            for a in announcements
            if a.period_start or a.period_end
        ]
        assert offenders == [], f"{name}: 기간이 생긴 행 {offenders}"

    def test_posting_date_is_preserved(
        self, name, cls, fixture, base_url, label, parser
    ):
        """게시일 자체는 raw_data.posted 로 남는다 - 버리지 않는다."""
        _items, announcements = self.announcements(
            name, cls, fixture, base_url, parser
        )
        posted = [json.loads(a.raw_data).get("posted") for a in announcements]
        assert any(posted), f"{name}: posted 가 하나도 없다"
        for value in posted:
            if value:
                assert len(value) == 10 and value[4] == "-", value

    def test_label_is_recognised(
        self, name, cls, fixture, base_url, label, parser
    ):
        """파서가 실제 응답에서 라벨을 읽어 낸다 (raw_data 증거용)."""
        items, _announcements = self.announcements(
            name, cls, fixture, base_url, parser
        )
        labels = {str(item.get("date_label", "")) for item in items}
        assert any(label in value for value in labels), (
            f"{name}: 기대 라벨 {label!r} 을 찾지 못했다 (읽은 라벨 {labels})"
        )


class TestForestPressDelegatesToTheParent:
    """forest_press 는 forest_service 의 ``_to_announcement`` 를 위임 호출한다."""

    def test_delegates_to_the_parent_reader(self):
        crawler = make_crawler(
            "forest_press", ForestPressCrawler, "https://www.forest.go.kr"
        )
        item = {
            "title": "산림청 보도자료",
            "link": "/kfsweb/cop/bbs/selectBoardArticle.do?nttId=1",
            "author": "산림청",
            "category": "정책/보도",
            "date": "2026.09.12",
            "date_label": "게시일",
        }
        with patch(
            "alert.crawlers.forest_service.posted_date",
            return_value="2026-09-12",
        ) as reader:
            announcement = crawler._to_announcement(
                item, "https://www.forest.go.kr"
            )
        reader.assert_called_once_with("2026.09.12")
        assert (announcement.period_start, announcement.period_end) == (None, None)
        assert json.loads(announcement.raw_data)["posted"] == "2026-09-12"


class TestPostedDateContract:
    """``posted_date`` - 라벨과 무관하게 값의 첫 날짜를 게시일로 남긴다."""

    @pytest.mark.parametrize("value,expected", [
        ("2026.09.11", "2026-09-11"),
        ("2026-09-11", "2026-09-11"),
        ("2026. 9. 11.", "2026-09-11"),
        ("20260911", "2026-09-11"),
        ("2026.09.01 ~ 2026.09.30", "2026-09-01"),
        ("2026.09.01(화)", "2026-09-01"),
        ("", None),
        ("   ", None),
        ("별도 공지", None),
        ("2026.13.01", None),
    ])
    def test_posted_date(self, value, expected):
        assert posted_date(value) == expected

    def test_strip_notes_helper(self):
        assert strip_notes("2026.09.01(화)").strip() == "2026.09.01"
        # 숫자가 든 괄호는 남긴다 (회차 등)
        assert "(9차)" in strip_notes("2026년도 (9차)")


class TestLabelsNeverBecomeAPeriod:
    """13차: **어떤 라벨도** 기간을 만들지 않는다.

    아래 값들은 12차까지 허용목록을 통과해 기간이 됐다(또는 게이트마다
    재현으로 올라왔다). 이제 기간은 소스 전용 추출기만 만들고, 그 추출기는
    **값 자체가 접수기간이라고 말할 때만** 읽는다.
    """

    @pytest.mark.parametrize("label,value", [
        ("접수기간", "2026.09.01 ~ 2026.09.30"),
        ("신청기간", "2026.09.01 ~ 2026.09.30"),
        ("모집기간", "2026.09.01 ~ 2026.09.30"),
        ("공모기간", "2026.09.01 ~ 2026.09.30"),
        ("의견제출기간", "2026.09.01 ~ 2026.09.30"),
        ("접수마감", "2026.09.30"),
        ("신청기한", "2026.09.30"),
        ("제출기한", "2026.09.30"),
        ("마감일", "2026.09.30"),
        ("교육기간", "2026.10.01 ~ 2026.10.31"),
        ("행사일정", "2026.10.15"),
        ("심사기간", "2026.10.01 ~ 2026.10.31"),
        ("접수시작", "2026.09.15"),
        ("게시일", "2026.09.11"),
        ("", "2026.09.01 ~ 2026.09.30"),
    ])
    def test_column_label_alone_makes_no_period(self, label, value):
        """컬럼 라벨은 근거가 아니다 - ``date`` 값 본문만 본다."""
        assert seis_period({"date": value, "date_label": label}) == (None, None)

    @pytest.mark.parametrize("value", [
        "2026.09.01 ~ 2026.09.30",
        "교육기간 2026.10.01 ~ 2026.10.31",
        "2026.09.30",
    ])
    def test_unlabelled_values_make_no_period(self, value):
        assert seis_period({"date": value}) == (None, None)
        assert posted_date(value) is not None

    def test_only_the_value_label_counts(self):
        assert seis_period({"date": "접수기간 2026.09.01 ~ 2026.09.30"}) == (
            "2026-09-01", "2026-09-30"
        )


class TestListFallbackLabelExtraction:
    """7차·8차 게이트: 라벨은 **같은 요소의 날짜 앞부분**에서만 읽는다.

    라벨은 이제 기간을 만들지 않지만, ``raw_data`` 증거와 정리 스크립트의
    판단 재료이므로 읽는 규칙 자체는 회귀로 남겨 둔다.
    """

    def test_title_text_is_not_a_label(self):
        value, label = extract(
            "<li><a>참여기업 모집 공고</a><span>2026.09.11</span></li>"
        )
        assert value == "2026.09.11"
        assert "모집" not in label
        assert posted_date(value) == "2026-09-11"

    def test_range_is_captured_whole(self):
        """첫 날짜만 집으면 종료일이 09-01이 된다 - 범위를 통째로 읽는다."""
        value, label = extract(
            "<li><span>접수 기간 2026.09.01 ~ 2026.09.30</span></li>"
        )
        assert value == "2026.09.01 ~ 2026.09.30"
        assert label == "접수 기간"

    def test_adjacent_label_is_not_used(self):
        value, label = extract(
            "<li><span>신청기한</span><span>2026.09.30</span></li>"
        )
        assert label == ""

    def test_range_split_across_child_elements(self):
        value, label = extract(
            "<li><span>접수기간 <em>2026.09.01</em> ~ <em>2026.09.30</em></span></li>"
        )
        assert label == "접수기간"
        assert value == "2026.09.01 ~ 2026.09.30"

    def test_weekday_parentheses_do_not_break_the_range(self):
        value, _label = extract(
            "<li><span>접수기간 2026.09.01(화) ~ 2026.09.30(수)</span></li>"
        )
        assert value == "2026.09.01 ~ 2026.09.30"

    def test_direct_text_date_has_no_label(self):
        """``<a>참여기업 모집 공고</a>2026.09.11`` → 라벨 없음."""
        value, label = extract(
            '<li><a href="/view.do?nttId=1">참여기업 모집 공고</a>2026.09.11</li>'
        )
        assert label == ""
        assert posted_date(value) == "2026-09-11"

    def test_same_element_label_is_used(self):
        value, label = extract(
            '<li><span class="date">신청기한 2026.09.30</span></li>'
        )
        assert label == "신청기한"
        # 라벨을 읽었어도 기간은 만들지 않는다 (13차)
        assert seis_period({"date": value, "date_label": label}) == (None, None)

    def test_class_names_are_not_labels(self):
        value, label = extract('<li><span class="date">2026.09.11</span></li>')
        assert label == ""

    def test_no_date_returns_empty(self):
        assert extract("<li><a>날짜 없는 공고</a></li>") == ("", "")


class TestForestPressHasNoPeriodRemnant:
    """7차 게이트 #3: forest_press 에 period_start 게시일이 남던 결함."""

    def test_press_item_has_no_period_at_all(self):
        crawler = make_crawler(
            "forest_press", ForestPressCrawler, "https://www.forest.go.kr"
        )
        item = {
            "title": "산림청 보도자료",
            "link": "/kfsweb/cop/bbs/selectBoardArticle.do?nttId=1",
            "author": "산림청",
            "category": "정책/보도",
            "date": "2026.09.11",
            "date_label": "게시일",
            "summary": "요약",
        }
        announcement = crawler._to_announcement(item, "https://www.forest.go.kr")
        assert announcement.period_start is None
        assert announcement.period_end is None
        assert json.loads(announcement.raw_data)["posted"] == "2026-09-11"
        assert announcement.summary == "요약"


class TestSeisListPathHasNoPeriod:
    """seis 목록(표·리스트) 경로도 값 라벨이 없으면 기간이 없다."""

    def test_table_row_with_a_reception_header(self):
        crawler = make_crawler("seis", SeisCrawler, "https://www.seis.or.kr")
        html = (
            '<table class="board_list"><thead><tr><th>제목</th>'
            "<th>접수기간</th></tr></thead><tbody><tr>"
            '<td><a href="/view.do?nttId=1">공고</a></td>'
            "<td>2026.09.01 ~ 2026.09.30</td></tr></tbody></table>"
        )
        items = crawler._parse_table_board(BeautifulSoup(html, "html.parser"))
        assert items
        built = [crawler._to_announcement(item, "https://www.seis.or.kr")
                 for item in items]
        for announcement in built:
            assert (announcement.period_start, announcement.period_end) == (
                None, None
            )


class TestLawmakingCellIsTheOnlyTableSource:
    """lawmaking 셀만 표 기반 기간을 만든다 (셀 자체가 기간 필드)."""

    def test_single_range_gives_the_deadline(self):
        assert lawmaking_period({"period": "2026. 9. 7. ~2026. 10. 19."}) == (
            None, "2026-10-19"
        )

    def test_two_ranges_give_nothing(self):
        assert lawmaking_period(
            {"period": "2026.09.01 ~ 2026.09.30 / 심사기간 2026.10.01 ~ 2026.10.31"}
        ) == (None, None)
