"""목록 날짜 라벨 회귀 - 게시일을 접수기간으로 저장하지 않는다 (6차 게이트 #1).

여섯 개 크롤러가 같은 템플릿을 공유해 같은 결함을 갖고 있었다:
``단일 날짜 -> (date, date)``. 공용 규칙(``alert/crawlers/date_labels``)으로
모았고, 여기서 **실제 목록 응답을 잘라 만든 fixture**로 파서별 회귀를 고정한다.

fixture 는 라이브 목록 페이지에서 받아 잘랐다(2026-09-13):
``nongup_gg_list.html`` · ``forest_service_list.html`` · ``ipet_list.html`` ·
``fowi_board_list.html``.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from alert.crawlers.date_labels import classify_date
from alert.crawlers.forest_press import ForestPressCrawler
from alert.crawlers.forest_service import ForestServiceCrawler
from alert.crawlers.fowi import FowiCrawler
from alert.crawlers.ipet import IpetCrawler
from alert.crawlers.nongup_gg import NongupGgCrawler
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
        """게시일 라벨이 붙은 목록은 기간 필드를 만들지 않는다."""
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
        """파서가 실제 응답에서 게시일 라벨을 읽어 낸다."""
        items, _announcements = self.announcements(
            name, cls, fixture, base_url, parser
        )
        labels = {str(item.get("date_label", "")) for item in items}
        assert any(label in value for value in labels), (
            f"{name}: 기대 라벨 {label!r} 을 찾지 못했다 (읽은 라벨 {labels})"
        )


class TestForestPressInheritsTheFix:
    """forest_press 는 forest_service 의 ``_to_announcement`` 를 위임 호출한다.

    자기 override 안에서 ``super()._to_announcement`` 를 부르므로 부모의
    날짜 분류 수리가 함께 적용된다 - 클래스 속성 동일성으로는 확인할 수
    없어 **동작**으로 확인한다.
    """

    def test_delegates_to_the_parent_classifier(self):
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
            "alert.crawlers.forest_service.classify_date",
            return_value=(None, None, "2026-09-12"),
        ) as classifier:
            announcement = crawler._to_announcement(
                item, "https://www.forest.go.kr"
            )
        classifier.assert_called_once_with("2026.09.12", "게시일")
        assert (announcement.period_start, announcement.period_end) == (None, None)
        assert json.loads(announcement.raw_data)["posted"] == "2026-09-12"


class TestSeisUsesTheSharedRule:
    """seis 도 같은 공용 규칙을 쓴다 (사본이 갈라지지 않게)."""

    def test_seis_classifier_matches_the_shared_one(self):
        crawler = make_crawler("seis", SeisCrawler, "https://www.seis.or.kr")
        for value, label in [
            ("2026.09.11", "게시일"),
            ("2026.09.01 ~ 2026.09.30", "접수기간"),
            ("2026.09.30", "접수마감"),
            ("2026.09.11", "구분"),
        ]:
            assert crawler._classify_date(value, label) == classify_date(value, label)


class TestSharedRuleContract:
    """공용 규칙 자체의 계약."""

    @pytest.mark.parametrize("label", ["게시일", "작성일", "등록일", "공고일", "게시날짜"])
    def test_posting_labels_never_yield_a_period(self, label):
        start, end, posted = classify_date("2026.09.11", label)
        assert (start, end) == (None, None)
        assert posted == "2026-09-11"

    @pytest.mark.parametrize("label", ["접수기간", "신청기간", "모집기간", "공모기간"])
    def test_period_labels_with_a_range(self, label):
        assert classify_date("2026.09.01 ~ 2026.09.30", label) == (
            "2026-09-01", "2026-09-30", None
        )

    def test_deadline_label_gives_only_an_end(self):
        assert classify_date("2026.09.30", "접수마감") == (None, "2026-09-30", None)

    def test_unknown_label_defaults_to_posted(self):
        assert classify_date("2026.09.11", "구분") == (None, None, "2026-09-11")
        assert classify_date("2026.09.11", "") == (None, None, "2026-09-11")

    def test_range_without_a_label_is_not_a_period(self):
        """11차(허용목록): 라벨 없는 범위는 접수기간이 되지 않는다.

        7차 게이트 #1 재현 - 무라벨/무관 범위가 마감으로 저장된 결함.
        """
        assert classify_date("2026.09.01~2026.09.30", "") == (
            None, None, "2026-09-01"
        )

    def test_empty_input(self):
        assert classify_date("", "게시일") == (None, None, None)
        assert classify_date("   ", "접수기간") == (None, None, None)

    def test_unparseable_date_yields_nothing(self):
        assert classify_date("별도 공지", "접수기간") == (None, None, None)


class TestGate7ListFallbackLabels:
    """7차 게이트 #3: 목록 폴백에서 게시일 재유입·접수기간 손실."""

    @staticmethod
    def extract(html):
        from alert.crawlers.date_labels import extract_date_and_label

        item = BeautifulSoup(html, "html.parser").find("li")
        return extract_date_and_label(item)

    def test_title_text_is_not_a_label(self):
        """제목의 "모집" 을 라벨로 오인하지 않는다 - 게시일로 남는다."""
        value, label = self.extract(
            "<li><a>참여기업 모집 공고</a><span>2026.09.11</span></li>"
        )
        assert value == "2026.09.11"
        assert "모집" not in label
        assert classify_date(value, label) == (None, None, "2026-09-11")

    def test_deadline_label_inside_the_date_element(self):
        """``신청기한 2026.09.30`` 은 종료일이다."""
        value, label = self.extract(
            '<li><a>공고</a><span class="date">신청기한 2026.09.30</span></li>'
        )
        assert classify_date(value, label) == (None, "2026-09-30", None)

    def test_range_is_captured_whole(self):
        """첫 날짜만 집으면 종료일이 09-01이 된다 - 범위를 통째로 읽는다."""
        value, label = self.extract(
            "<li><a>공고</a><span>접수 기간 2026.09.01 ~ 2026.09.30</span></li>"
        )
        assert value == "2026.09.01 ~ 2026.09.30"
        assert classify_date(value, label) == ("2026-09-01", "2026-09-30", None)

    @pytest.mark.parametrize("label_text,expected_end", [
        ("신청기한", "2026-09-30"),
        ("접수기한", "2026-09-30"),
        ("마감일", "2026-09-30"),
        ("제출기한", "2026-09-30"),
    ])
    def test_new_deadline_labels(self, label_text, expected_end):
        start, end, posted = classify_date("2026.09.30", label_text)
        assert (start, end, posted) == (None, expected_end, None)

    def test_no_date_returns_empty(self):
        assert self.extract("<li><a>날짜 없는 공고</a></li>") == ("", "")


class TestGate7ForestPressHasNoPeriodRemnant:
    """7차 게이트 #3: forest_press 에 period_start 게시일이 남던 결함."""

    def test_press_item_has_no_period_at_all(self):
        from alert.crawlers.forest_press import ForestPressCrawler

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


class TestGate8ConservativeLabels:
    """8차 게이트 #2·#3·#4: 라벨 추론을 **같은 요소 안**으로 좁혔다.

    환각 0이 우선이므로 라벨을 확신할 수 없으면 게시일로 돌린다 -
    커버리지 손실은 받아들인다(조정자 판정).
    """

    @staticmethod
    def extract(html):
        from alert.crawlers.date_labels import extract_date_and_label

        item = BeautifulSoup(html, "html.parser").find("li")
        return extract_date_and_label(item)

    def test_adjacent_label_is_not_used(self):
        """``<span>신청기한</span><span>날짜</span>`` → 기간 NULL, posted.

        인접 형제의 라벨은 쓰지 않는다 - 커버리지 손실을 감수한다.
        """
        value, label = self.extract(
            "<li><span>신청기한</span><span>2026.09.30</span></li>"
        )
        assert label == ""
        assert classify_date(value, label) == (None, None, "2026-09-30")

    def test_range_split_across_child_elements(self):
        """``<span>접수기간 <em>A</em> ~ <em>B</em></span>`` → A/B.

        가장 짧은 요소만 고르면 라벨과 범위를 모두 잃는다 - 범위를 품은
        요소를 우선한다.
        """
        value, label = self.extract(
            "<li><span>접수기간 <em>2026.09.01</em> ~ <em>2026.09.30</em></span></li>"
        )
        assert label == "접수기간"
        assert classify_date(value, label) == ("2026-09-01", "2026-09-30", None)

    def test_weekday_parentheses_do_not_break_the_range(self):
        """``접수기간 2026.09.01(화) ~ 2026.09.30(수)`` → 09-01/09-30."""
        value, label = self.extract(
            "<li><span>접수기간 2026.09.01(화) ~ 2026.09.30(수)</span></li>"
        )
        assert classify_date(value, label) == ("2026-09-01", "2026-09-30", None)

    def test_direct_text_date_has_no_label(self):
        """``<a>참여기업 모집 공고</a>2026.09.11`` → posted only.

        컨테이너 직접 텍스트의 날짜는 라벨이 없다 - 제목이 라벨로
        새어 들어오던 자리다.
        """
        value, label = self.extract(
            '<li><a href="/view.do?nttId=1">참여기업 모집 공고</a>2026.09.11</li>'
        )
        assert label == ""
        assert classify_date(value, label) == (None, None, "2026-09-11")

    def test_same_element_label_is_used(self):
        """같은 요소 안의 라벨은 쓴다."""
        value, label = self.extract(
            '<li><span class="date">신청기한 2026.09.30</span></li>'
        )
        assert label == "신청기한"
        assert classify_date(value, label) == (None, "2026-09-30", None)

    def test_class_names_are_not_labels(self):
        """클래스 이름은 텍스트가 아니므로 라벨이 아니다."""
        value, label = self.extract('<li><span class="date">2026.09.11</span></li>')
        assert label == ""
        assert classify_date(value, label) == (None, None, "2026-09-11")

    @pytest.mark.parametrize("text,expected", [
        ("접수기간 2026.09.01 ~ 2026.09.30", ("2026-09-01", "2026-09-30", None)),
        ("접수기간 2026.09.01부터 2026.09.30까지", ("2026-09-01", "2026-09-30", None)),
        ("접수기간 2026.09.01 - 2026.09.30", ("2026-09-01", "2026-09-30", None)),
    ])
    def test_range_separators(self, text, expected):
        value, label = self.extract(f"<li><span>{text}</span></li>")
        assert classify_date(value, label) == expected

    def test_strip_notes_helper(self):
        from alert.crawlers.date_labels import strip_notes

        assert strip_notes("2026.09.01(화)").strip() == "2026.09.01"
        # 숫자가 든 괄호는 남긴다 (회차 등)
        assert "(9차)" in strip_notes("2026년도 (9차)")


class TestCycle11WhitelistOnly:
    """11차: 기간은 **허용목록 세 자리**에서만 나온다.

    (a) SEIS 메인 카드 ``p.date`` = 구조적 접수기간 필드
    (b) KOFPI 제목 ``(~M.D)`` = 마감 하나
    (c) 국민참여입법센터 목록의 의견제출 기간 필드

    그 밖의 소스·필드는 기간을 만들지 않는다. 아래는 게이트 크리틱
    재현 3건(교육기간 / 링크 제목 / 접수시작·행사일정)이다.
    """

    @staticmethod
    def extract(html):
        from alert.crawlers.date_labels import extract_date_and_label

        item = BeautifulSoup(html, "html.parser").find("li")
        return extract_date_and_label(item)

    def test_training_period_never_becomes_a_period(self):
        """재현 1: ``교육기간 2026.10.01 ~ 2026.10.31`` → 기간 NULL.

        교육 일정은 접수 일정이 아니다. 범위 표기가 있어도 승격하지 않는다.
        """
        assert classify_date("2026.10.01 ~ 2026.10.31", "교육기간") == (
            None, None, "2026-10-01"
        )

    def test_link_title_never_becomes_a_label(self):
        """재현 2: 링크 제목이 라벨로 새어 기간이 되던 자리 → 게시일."""
        value, label = self.extract(
            '<li><a href="/view.do?nttId=1">모집기간 연장 공고</a>'
            "<span>2026.09.01 ~ 2026.09.30</span></li>"
        )
        assert label == ""
        assert classify_date(value, label) == (None, None, "2026-09-01")

    @pytest.mark.parametrize("label,value", [
        ("접수시작", "2026.09.15"),
        ("신청시작", "2026.09.15"),
        ("모집시작", "2026.09.15"),
        ("행사일정", "2026.10.15"),
        ("행사기간", "2026.10.01 ~ 2026.10.31"),
        ("운영기간", "2026.10.01 ~ 2026.10.31"),
        ("사업기간", "2026.10.01 ~ 2026.10.31"),
        ("심사기간", "2026.10.01 ~ 2026.10.31"),
    ])
    def test_never_period_labels(self, label, value):
        """재현 3: 시작·행사·운영 라벨은 접수기간이 아니다 → 기간 NULL."""
        start, end, posted = classify_date(value, label)
        assert (start, end) == (None, None)
        assert posted is not None

    @pytest.mark.parametrize("label", [
        "접수기간", "신청기간", "모집기간", "공모기간", "의견제출기간",
    ])
    def test_whitelisted_range_labels_still_work(self, label):
        """허용목록 라벨 + 범위 = 진짜 접수기간 (허용목록이 죽지 않았다)."""
        assert classify_date("2026.09.01 ~ 2026.09.30", label) == (
            "2026-09-01", "2026-09-30", None
        )

    def test_label_must_lead(self):
        """라벨은 **선두**여야 한다 - 문장 중간의 "접수기간" 은 안 된다."""
        assert classify_date(
            "2026.10.01 ~ 2026.10.31", "교육 접수기간 안내"
        ) == (None, None, "2026-10-01")
