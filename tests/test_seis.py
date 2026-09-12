"""Tests for SeisCrawler (사회적기업포털 SEIS)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from alert.crawlers.period_extractors import SEIS_CARD_DATE_FIELD
from alert.crawlers.seis import SeisCrawler
from alert.main import _finalize_periods
from alert.models import RawAnnouncement

FIXTURES = Path(__file__).parent / "fixtures"


class TestSeisCrawler:
    """Test SeisCrawler implementation."""

    @pytest.fixture
    def mock_seis_config(self):
        """Mock config for SeisCrawler."""
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 3
        config.crawler.retry_delay = 1.0
        config.crawler.user_agent = "test-agent"

        seis_source = MagicMock()
        seis_source.enabled = True
        seis_source.base_url = "https://www.seis.or.kr"

        config.crawler.sources = {"seis": seis_source}
        return config

    def test_initialization(self, mock_seis_config):
        """SeisCrawler should initialize without API key (HTML scraper)."""
        with patch("alert.crawlers.base.get_config", return_value=mock_seis_config):
            crawler = SeisCrawler()
            assert crawler.source_name == "seis"

    def test_extract_post_id(self, mock_seis_config):
        """_extract_post_id() should extract ID from various URL patterns."""
        with patch("alert.crawlers.base.get_config", return_value=mock_seis_config):
            crawler = SeisCrawler()

            # Parameter-based ID - **종류 접두**를 포함한다 (11차 게이트)
            assert crawler._extract_post_id("https://example.com?nttId=12345") == "ntt:12345"
            assert crawler._extract_post_id("https://example.com?seq=67890") == "seq:67890"
            assert crawler._extract_post_id("https://example.com?idx=54321") == "idx:54321"

            # Path-based ID
            assert crawler._extract_post_id("https://example.com/view/123456") == "path:123456"

            # Empty link
            assert crawler._extract_post_id("") == ""

            # Hash fallback for unrecognized pattern - 접두를 붙인다
            # (접두 없는 ID 는 옛 규칙의 행으로 판정된다)
            result = crawler._extract_post_id("https://example.com/some-page")
            assert result.startswith("md5:")
            assert len(result) == len("md5:") + 16

    def test_normalize_url(self, mock_seis_config):
        """_normalize_url() should handle relative and absolute URLs."""
        with patch("alert.crawlers.base.get_config", return_value=mock_seis_config):
            crawler = SeisCrawler()
            base_url = "https://www.seis.or.kr"

            # Absolute URL
            assert crawler._normalize_url("https://example.com/test", base_url) == "https://example.com/test"
            assert crawler._normalize_url("http://example.com/test", base_url) == "http://example.com/test"

            # Protocol-relative URL
            assert crawler._normalize_url("//example.com/test", base_url) == "https://example.com/test"

            # Absolute path
            assert crawler._normalize_url("/test/path", base_url) == "https://www.seis.or.kr/test/path"

            # Relative path
            assert crawler._normalize_url("test/path", base_url) == "https://www.seis.or.kr/test/path"

            # Empty link
            assert crawler._normalize_url("", base_url) == ""

    def test_normalize_date(self, mock_seis_config):
        """_normalize_date() should handle various date formats."""
        with patch("alert.crawlers.base.get_config", return_value=mock_seis_config):
            crawler = SeisCrawler()

            # YYYY-MM-DD format
            assert crawler._normalize_date("2026-04-01") == "2026-04-01"

            # YYYY.MM.DD format
            assert crawler._normalize_date("2026.04.01") == "2026-04-01"

            # YYYY/MM/DD format
            assert crawler._normalize_date("2026/04/01") == "2026-04-01"

            # YYYYMMDD format
            assert crawler._normalize_date("20260401") == "2026-04-01"

            # With single-digit month/day
            assert crawler._normalize_date("2026-4-1") == "2026-04-01"

            # Empty string
            assert crawler._normalize_date("") is None

            # Invalid format
            assert crawler._normalize_date("invalid") is None

    def test_to_announcement_valid(self, mock_seis_config):
        """_to_announcement() should convert valid item to RawAnnouncement."""
        with patch("alert.crawlers.base.get_config", return_value=mock_seis_config):
            crawler = SeisCrawler()

            item = {
                "title": "사회적기업 지원사업 공고",
                "link": "/front/board/boardView.do?nttId=12345",
                "author": "한국사회적기업진흥원",
                "category": "지원사업",
                "date": "2026-04-01 ~ 2026-04-30",
            }

            result = crawler._to_announcement(item, "https://www.seis.or.kr")

            assert result is not None
            assert isinstance(result, RawAnnouncement)
            assert result.source == "seis"
            assert result.title == "사회적기업 지원사업 공고"
            assert result.url == "https://www.seis.or.kr/front/board/boardView.do?nttId=12345"
            assert result.author == "한국사회적기업진흥원"
            assert result.category == "지원사업"
            # 11차(허용목록): 목록의 무라벨 범위는 기간이 아니다 - 게시일로만 남는다
            assert result.period_start is None
            assert result.period_end is None
            assert json.loads(result.raw_data)["posted"] == "2026-04-01"

    def test_to_announcement_missing_title(self, mock_seis_config):
        """_to_announcement() should return None if title is missing."""
        with patch("alert.crawlers.base.get_config", return_value=mock_seis_config):
            crawler = SeisCrawler()

            item = {
                "title": "",
                "link": "/front/board/boardView.do?nttId=12345",
                "author": "한국사회적기업진흥원",
                "category": "",
                "date": "2026-04-01",
            }

            result = crawler._to_announcement(item, "https://www.seis.or.kr")
            assert result is None

    def test_fetch_without_beautifulsoup(self, mock_seis_config):
        """fetch() should return empty list if BeautifulSoup is not installed."""
        with patch("alert.crawlers.base.get_config", return_value=mock_seis_config):
            with patch("alert.crawlers.seis.BeautifulSoup", None):
                crawler = SeisCrawler()
                result = crawler.fetch()
                assert result == []

    def test_fetch_board_listing_table_strategy(self, mock_seis_config):
        """_fetch_board_listing() should parse table-based board structure."""
        with patch("alert.crawlers.base.get_config", return_value=mock_seis_config):
            crawler = SeisCrawler()

            html_content = """
            <html>
            <body>
                <table class="board_list">
                    <tbody>
                        <tr>
                            <td class="title"><a href="/front/board/boardView.do?nttId=123">사회적기업 지원사업</a></td>
                            <td class="author">한국사회적기업진흥원</td>
                            <td class="date">2026-04-01</td>
                        </tr>
                        <tr>
                            <td class="title"><a href="/front/board/boardView.do?nttId=456">소셜벤처 육성사업</a></td>
                            <td class="author">한국사회적기업진흥원</td>
                            <td class="date">2026-04-02</td>
                        </tr>
                    </tbody>
                </table>
            </body>
            </html>
            """

            mock_response = MagicMock()
            mock_response.text = html_content
            mock_response.apparent_encoding = "utf-8"

            with patch.object(crawler, "get", return_value=mock_response):
                items = crawler._fetch_board_listing("https://www.seis.or.kr/front/board/boardList.do?boardId=BBS_0000001")

            assert len(items) == 2
            assert items[0]["title"] == "사회적기업 지원사업"
            assert items[0]["link"] == "/front/board/boardView.do?nttId=123"
            assert items[0]["author"] == "한국사회적기업진흥원"
            assert items[1]["title"] == "소셜벤처 육성사업"

    def test_parse_period(self, mock_seis_config):
        """_parse_period() should parse period strings into start and end dates."""
        with patch("alert.crawlers.base.get_config", return_value=mock_seis_config):
            crawler = SeisCrawler()

            # Range with tilde
            start, end = crawler._parse_period("2026-04-01 ~ 2026-04-30")
            assert start == "2026-04-01"
            assert end == "2026-04-30"

            # Single date
            start, end = crawler._parse_period("2026-04-01")
            assert start == "2026-04-01"
            assert end == "2026-04-01"

            # Empty string
            start, end = crawler._parse_period("")
            assert start is None
            assert end is None


class TestSeisMainCards:
    """메인 페이지 공고 카드 파싱 + 복수 링크 병합 (계약 v2.1 판정 6-①).

    2026-09-12 실측: 같은 경기도 사회보험료 지원사업이 회차별 링크
    ``fncPbofrSn=8339,8364..8371`` 로 9행 적재되어 브리핑을 잠식했다.
    """

    @pytest.fixture
    def crawler(self):
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 1
        config.crawler.retry_delay = 0
        config.crawler.user_agent = "test-agent"

        source = MagicMock()
        source.enabled = True
        source.base_url = "https://www.seis.or.kr"
        config.crawler.sources = {"seis": source}

        with patch("alert.crawlers.base.get_config", return_value=config):
            yield SeisCrawler()

    @pytest.fixture
    def soup(self):
        return BeautifulSoup(
            (FIXTURES / "seis_main_cards.html").read_text(encoding="utf-8"),
            "html.parser",
        )

    def test_parse_main_cards_reads_card_metadata(self, crawler, soup):
        """카드에서 제목/링크/분류/주체/회차/접수기간을 함께 읽는다."""
        items = crawler._parse_main_cards(soup)
        assert len(items) == 22

        card = next(i for i in items if "fncPbofrSn=8371" in i["link"])
        assert card["title"] == "2026년 경기도 사회적기업 사회보험료 지원사업 참여기업 모집 공고"
        assert card["category"] == "재정지원"
        assert card["round"] == "2026년도 (9차)"
        assert card["date"] == "2026.09.01 ~ 2026.12.31"
        # 주체는 span.sub 에서만 온다 (ul.info 는 분류 자리다)
        assert card["sub"] == "사회보험료 지원 사업"
        assert card["info"] == ["경기도"]
        # D-day 배지는 어느 필드에도 섞이지 않는다
        assert not any(value.startswith("D-") for value in card["info"])

    def test_separate_rounds_are_never_merged(self, crawler, soup):
        """11차 게이트: 링크가 다르면 회차별 공고를 합치지 않는다.

        예전에는 "목록에 적힌 날짜가 같으면 같은 공고" 로 봐서 1차·2차가
        합쳐졌고, 합쳐진 쪽은 알림에서 사라졌다. 중복이 남는 비용이
        공고가 사라지는 비용보다 싸다.
        """
        items = crawler._parse_main_cards(soup)
        rounds = [
            i for i in items
            if i["title"].startswith("2026년 경기도 사회적기업 사회보험료")
        ]
        assert len(rounds) == 9
        assert len({i["link"] for i in rounds}) == 9      # 링크가 전부 다르다

        deduped = crawler._dedupe_items(items)
        assert len(deduped) == 22                          # 하나도 잃지 않는다
        assert not any(i.get("merged_count") for i in deduped)

    def test_true_replicas_still_collapse(self, crawler):
        """같은 링크가 두 번 나오면(진짜 복제) 1건으로 합친다."""
        card = (
            '<li class="swiper-slide" data-type="사업공고">'
            '<span class="sub">서울센터</span>'
            '<p class="tit"><a href="subPage.do?tabId=view&itgrdAplyPbancSn=7">'
            "공고</a></p>"
            '<ul class="info"><li>교육</li></ul>'
            '<p class="date">2026.09.01 ~ 2026.09.30</p></li>'
        )
        soup = BeautifulSoup("<ul>" + card + card + "</ul>", "html.parser")
        items = crawler._parse_main_cards(soup)
        assert len(items) == 2
        deduped = crawler._dedupe_items(items)
        assert len(deduped) == 1
        assert deduped[0]["merged_count"] == 2

    def test_each_round_keeps_its_own_identity(self, crawler, soup):
        """회차마다 자기 링크·자기 ID 로 남는다 (9차 회차 확인)."""
        deduped = crawler._dedupe_items(crawler._parse_main_cards(soup))
        merged = next(
            i for i in deduped if "fncPbofrSn=8371" in i["link"]
        )

        announcement = crawler._to_announcement(merged, "https://www.seis.or.kr")
        assert announcement is not None
        assert announcement.source_id == "fnc:8371"
        assert announcement.url == (
            "https://www.seis.or.kr/subPage.do"
            "?menuId=30200&tabId=pbancMainView&fncPbofrSn=8371"
        )
        # 13차: 기간은 크롤러가 아니라 저장 직전 관문이 정한다. 카드
        # ``p.date`` 는 **구조 근거**가 있는 자리이므로 관문이 다시 채운다.
        assert (announcement.period_start, announcement.period_end) == (None, None)
        gated = _finalize_periods("seis", announcement)
        assert (gated.period_start, gated.period_end) == (
            "2026-09-01", "2026-12-31"
        )
        assert announcement.author == "한국사회적기업진흥원"

    def test_no_round_is_swallowed_by_a_merge(self, crawler, soup):
        """어떤 회차도 다른 회차의 ``merged_source_ids`` 로 흡수되지 않는다."""
        deduped = crawler._dedupe_items(crawler._parse_main_cards(soup))
        built = [
            crawler._to_announcement(i, "https://www.seis.or.kr") for i in deduped
        ]
        swallowed = [
            sid
            for a in built
            for sid in json.loads(a.raw_data).get("merged_source_ids", [])
        ]
        assert swallowed == []
        # 9개 회차가 각각 독립된 행으로 남는다
        rounds = [a for a in built if "사회보험료" in a.title]
        assert len(rounds) == 9
        assert len({a.source_id for a in rounds}) == 9

    def test_distinct_subjects_stay_separate(self, crawler, soup):
        """지정공모는 주체(span.sub)가 달라 각각 남는다."""
        deduped = crawler._dedupe_items(crawler._parse_main_cards(soup))
        subjects = {
            i["sub"] for i in deduped if i["title"].endswith("지정공모")
        }
        assert {"서울특별시", "광주광역시", "전라남도", "경상북도", "산림청"} <= subjects

    def test_every_card_item_gets_a_deadline_at_the_gate(self, crawler, soup):
        """카드 기간은 **관문에서** 채워진다 - 크롤러는 만들지 않는다.

        크롤러 단계에서는 전부 None 이고(어떤 파서 출력도 저장되지 않는다),
        저장 직전 관문이 구조 근거가 있는 ``p.date`` 값만 다시 읽는다.
        """
        deduped = crawler._dedupe_items(crawler._parse_main_cards(soup))
        announcements = [
            crawler._to_announcement(i, "https://www.seis.or.kr") for i in deduped
        ]
        assert announcements
        assert not any(a.period_start or a.period_end for a in announcements)

        gated = [_finalize_periods("seis", a) for a in announcements]
        assert all(a.period_start and a.period_end for a in gated)

    def test_source_ids_are_unique_and_stable(self, crawler, soup):
        """source_id는 URL의 공고 고유번호이며 배치 내에서 충돌하지 않는다."""
        deduped = crawler._dedupe_items(crawler._parse_main_cards(soup))
        ids = [
            crawler._to_announcement(i, "https://www.seis.or.kr").source_id
            for i in deduped
        ]
        assert len(ids) == len(set(ids))
        assert "fnc:8371" in ids
        # 인·지정 공모는 dsgnPbofrSn 을 쓴다 (예전에는 MD5 해시로 떨어졌다)
        assert "dsgn:8322" in ids
        # 종류 접두가 있으므로 같은 번호라도 종류가 다르면 다른 ID 다
        assert len({i.split(":", 1)[0] for i in ids}) > 1

    def test_epsd_no_is_not_matched_as_generic_no_param(self, crawler):
        """"epsdNo=4" 가 범용 "no=" 패턴에 걸려 다른 공고와 충돌하지 않는다."""
        link = (
            "/subPage.do?menuId=30100&tabId=certPageView&statsYr=2026&epsdNo=4"
        )
        # 12차 게이트: 연도(statsYr)까지 ID 에 들어간다
        assert crawler._extract_post_id(link) == "epsd:2026:4"
        # 같은 자리에 다른 파라미터가 와도 고유번호를 먼저 본다
        assert crawler._extract_post_id(
            "/subPage.do?menuId=30200&tabId=pbancMainView&fncPbofrSn=8371"
        ) == "fnc:8371"
        assert crawler._extract_post_id(
            "/subPage.do?menuId=30100&tabId=certPageView&dsgnPbofrSn=8322"
        ) == "dsgn:8322"

    def test_same_number_in_different_kinds_never_collides(self, crawler):
        """11차 게이트: ``fncPbofrSn=42`` 와 ``dsgnPbofrSn=42`` 는 다른 공고다."""
        financial = crawler._extract_post_id(
            "/subPage.do?menuId=30200&tabId=pbancMainView&fncPbofrSn=42"
        )
        designation = crawler._extract_post_id(
            "/subPage.do?menuId=30100&tabId=certPageView&dsgnPbofrSn=42"
        )
        assert (financial, designation) == ("fnc:42", "dsgn:42")
        assert financial != designation

    def test_normalize_title_ignores_spacing_noise(self, crawler):
        """제목 정규화는 공백·구분기호 차이를 무시한다."""
        assert crawler._normalize_title("2026년  경기도 사회적기업 (모집)") == \
            crawler._normalize_title("2026년 경기도 사회적기업 모집")


class TestSeisCardRegionCritique:
    """Codex 크리틱 #4: ul.info 분류를 지역으로 오인해 별개 공고를 합치던 결함.

    재현 입력: 제목이 같고 ``span.sub`` 가 서울센터/부산센터로 다르며
    ``ul.info li`` 는 둘 다 "교육" 인 카드 두 장. 예전 파서는 둘 다
    ``region="교육"`` 으로 읽어 1건으로 병합했다.
    """

    CARD = """
    <li class="swiper-slide" data-type="사업공고">
      <span class="badge cate">사업공고</span>
      <span class="sub">{sub}</span>
      <p class="tit"><a href="subPage.do?menuId=30400&tabId=view&itgrdAplyPbancSn={sid}">
         2026년 사회적기업 성장지원센터 상주기업 모집 공고</a></p>
      <ul class="info"><li>{info}</li><li>D-10</li></ul>
      <p class="date">{period}</p>
    </li>
    """

    @pytest.fixture
    def crawler(self):
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 1
        config.crawler.retry_delay = 0
        config.crawler.user_agent = "test-agent"
        source = MagicMock()
        source.enabled = True
        source.base_url = "https://www.seis.or.kr"
        config.crawler.sources = {"seis": source}
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield SeisCrawler()

    def build(self, crawler, cards):
        soup = BeautifulSoup("<ul>" + "".join(cards) + "</ul>", "html.parser")
        items = crawler._parse_main_cards(soup)
        return items, crawler._dedupe_items(items)

    def test_different_centers_are_two_items(self, crawler):
        """서울센터/부산센터는 제목이 같아도 2건으로 남는다."""
        items, deduped = self.build(crawler, [
            self.CARD.format(sub="서울센터", sid="100", info="교육",
                             period="2026.09.01 ~ 2026.09.30"),
            self.CARD.format(sub="부산센터", sid="101", info="교육",
                             period="2026.09.01 ~ 2026.09.30"),
        ])
        assert len(items) == 2
        assert len(deduped) == 2
        assert {i["sub"] for i in deduped} == {"서울센터", "부산센터"}

    def test_info_is_category_not_region(self, crawler):
        """ul.info 값은 분류로 남고 주체 자리에 들어가지 않는다."""
        _items, deduped = self.build(crawler, [
            self.CARD.format(sub="서울센터", sid="100", info="교육",
                             period="2026.09.01 ~ 2026.09.30"),
        ])
        assert deduped[0]["info"] == ["교육"]
        assert deduped[0]["sub"] == "서울센터"

    def test_different_period_end_is_not_merged(self, crawler):
        """주체·제목이 같아도 접수 종료일이 다르면 1차·2차 별개 공고다."""
        _items, deduped = self.build(crawler, [
            self.CARD.format(sub="서울센터", sid="200", info="교육",
                             period="2026.08.01 ~ 2026.08.31"),
            self.CARD.format(sub="서울센터", sid="201", info="교육",
                             period="2026.09.01 ~ 2026.09.30"),
        ])
        assert len(deduped) == 2
        built = [
            _finalize_periods(
                "seis", crawler._to_announcement(i, "https://www.seis.or.kr")
            )
            for i in deduped
        ]
        assert {a.period_end for a in built} == {"2026-08-31", "2026-09-30"}

    def test_different_links_are_never_merged(self, crawler):
        """종료일이 같아도 **링크가 다르면** 별개 공고다 (11차 게이트)."""
        _items, deduped = self.build(crawler, [
            self.CARD.format(sub="사회보험료 지원 사업", sid="8371", info="경기도",
                             period="2026.09.01 ~ 2026.12.31"),
            self.CARD.format(sub="사회보험료 지원 사업", sid="8370", info="경기도",
                             period="2026.08.01 ~ 2026.12.31"),
        ])
        assert len(deduped) == 2
        built = [
            _finalize_periods(
                "seis", crawler._to_announcement(i, "https://www.seis.or.kr")
            )
            for i in deduped
        ]
        assert {a.source_id for a in built} == {"itgrd:8371", "itgrd:8370"}
        assert {a.period_end for a in built} == {"2026-12-31"}

    def test_group_key_components(self, crawler):
        """수집 단계 키는 제목·주체·**링크**·회차 네 조각이다 (11차 게이트)."""
        base = {
            "title": "같은 제목",
            "sub": "서울센터",
            "link": "subPage.do?tabId=view&itgrdAplyPbancSn=1",
            "date": "2026.09.01 ~ 2026.09.30",
        }
        assert crawler._group_key(base) == crawler._group_key(dict(base))
        assert crawler._group_key(base) != crawler._group_key(
            {**base, "sub": "부산센터"}
        )
        # 링크가 다르면 다른 공고다 - 날짜는 키에 들어가지 않는다
        assert crawler._group_key(base) != crawler._group_key(
            {**base, "link": "subPage.do?tabId=view&itgrdAplyPbancSn=2"}
        )
        assert crawler._group_key(base) == crawler._group_key(
            {**base, "date": "2026.09.01 ~ 2026.10.31"}
        )
        # 회차 토큰이 다르면 같은 링크여도 합치지 않는다
        assert crawler._group_key({**base, "round": "1차"}) != crawler._group_key(
            {**base, "round": "2차"}
        )


class TestGate6PostedDateLabels:
    """6차 게이트 #1: 목록의 게시일이 마감으로 저장되던 결함.

    상세 수집이 꺼진 상태에서도 재현됐다 - 목록 파서 자체의 결함이다.
    """

    TABLE = (
        "<table class=board_list><thead><tr>"
        "<th>번호</th><th>제목</th><th>{label}</th></tr></thead><tbody><tr>"
        "<td>1</td><td class=title>"
        '<a href="subPage.do?menuId=30200&tabId=pbancMainView&fncPbofrSn=99">'
        "사회적기업 모집 공고</a></td>"
        "<td>{value}</td></tr></tbody></table>"
    )

    @pytest.fixture
    def crawler(self):
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 1
        config.crawler.retry_delay = 0
        config.crawler.user_agent = "test-agent"
        source = MagicMock()
        source.enabled = True
        source.base_url = "https://www.seis.or.kr"
        source.fetch_detail = False
        config.crawler.sources = {"seis": source}
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield SeisCrawler()

    def announce(self, crawler, label, value):
        html = self.TABLE.format(label=label, value=value)
        items = crawler._parse_table_board(BeautifulSoup(html, "html.parser"))
        assert items, "행을 파싱하지 못했다"
        return items[0], crawler._to_announcement(items[0], "https://www.seis.or.kr")

    @pytest.mark.parametrize("label", ["게시일", "작성일", "등록일", "공고일"])
    def test_posting_labels_never_become_a_period(self, crawler, label):
        """게시일/작성일/등록일은 기간 필드에 들어가지 않는다."""
        item, announcement = self.announce(crawler, label, "2026.09.11")
        assert item["date_label"] == label
        assert announcement.period_start is None
        assert announcement.period_end is None
        assert json.loads(announcement.raw_data)["posted"] == "2026-09-11"

    def test_unlabelled_single_date_is_treated_as_posted(self, crawler):
        """라벨을 모르는 단일 날짜도 게시일로 본다 - 마감을 지어내지 않는다."""
        _item, announcement = self.announce(crawler, "구분", "2026.09.11")
        assert (announcement.period_start, announcement.period_end) == (None, None)
        assert json.loads(announcement.raw_data)["posted"] == "2026-09-11"

    def test_reception_header_alone_is_not_a_period(self, crawler):
        """13차: 표 헤더가 ``접수기간`` 이어도 기간이 되지 않는다.

        헤더는 파서가 읽은 컬럼 라벨이지 값의 근거가 아니다. 같은 자리에
        교육기간·행사일정이 들어오는 것을 여덟 차례 게이트에서 봤다.
        """
        _item, announcement = self.announce(
            crawler, "접수기간", "2026.09.01 ~ 2026.09.30"
        )
        assert (announcement.period_start, announcement.period_end) == (None, None)
        assert json.loads(announcement.raw_data)["posted"] == "2026-09-01"

    def test_deadline_header_makes_no_period_either(self, crawler):
        """``접수마감`` 헤더 + 단일 날짜도 기간이 아니다 (13차)."""
        _item, announcement = self.announce(crawler, "접수마감", "2026.09.30")
        assert (announcement.period_start, announcement.period_end) == (None, None)
        assert json.loads(announcement.raw_data)["posted"] == "2026-09-30"

    def test_range_without_a_label_is_not_a_period(self, crawler):
        """11차(허용목록): 범위여도 접수 라벨이 없으면 게시일로만 남는다."""
        _item, announcement = self.announce(
            crawler, "구분", "2026.09.01 ~ 2026.09.30"
        )
        assert (announcement.period_start, announcement.period_end) == (None, None)
        assert json.loads(announcement.raw_data)["posted"] == "2026-09-01"

    def test_value_label_is_the_only_evidence(self, crawler):
        """값 본문이 ``접수기간 …`` 이라고 말할 때만 기간이 된다 (13차)."""
        from alert.crawlers.period_extractors import seis_period

        assert seis_period({"date": "2026.09.11", "date_label": "게시일"}) == (
            None, None
        )
        assert seis_period(
            {"date": "2026.09.01 ~ 2026.09.30", "date_label": "접수기간"}
        ) == (None, None)
        assert seis_period({"date": "접수기간 2026.09.01 ~ 2026.09.30"}) == (
            "2026-09-01", "2026-09-30"
        )
        assert seis_period({"date": ""}) == (None, None)


class TestGate6RoundInKey:
    """6차 게이트 #2: 마감 없는 별도 회차가 병합되던 결함."""

    CARD = """
    <li class="swiper-slide" data-type="사업공고">
      <span class="badge cate">사업공고</span>
      <span class="sub">서울센터</span>
      <p class="tit"><a href="subPage.do?menuId=30400&tabId=view&itgrdAplyPbancSn={sid}">
         성장지원센터 상주기업 모집 공고</a></p>
      <ul class="info"><li>교육</li><li>{rnd}</li><li>D-10</li></ul>
      <p class="date">{period}</p>
    </li>
    """

    @pytest.fixture
    def crawler(self):
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 1
        config.crawler.retry_delay = 0
        config.crawler.user_agent = "test-agent"
        source = MagicMock()
        source.enabled = True
        source.base_url = "https://www.seis.or.kr"
        source.fetch_detail = False
        config.crawler.sources = {"seis": source}
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield SeisCrawler()

    def build(self, crawler, cards):
        soup = BeautifulSoup("<ul>" + "".join(cards) + "</ul>", "html.parser")
        items = crawler._parse_main_cards(soup)
        return items, crawler._dedupe_items(items)

    def test_different_rounds_without_dates_stay_separate(self, crawler):
        """마감이 없으면 회차가 유일한 구분자다 - 1차와 2차는 별개 공고다."""
        items, deduped = self.build(crawler, [
            self.CARD.format(sid="100", rnd="1차", period=""),
            self.CARD.format(sid="101", rnd="2차", period=""),
        ])
        assert len(items) == 2
        assert len(deduped) == 2

    @pytest.mark.parametrize("first,second", [
        ("1차", "2차"), ("상시", "1차"), ("1차", "추가"), ("2차", "연장"),
    ])
    def test_round_tokens_separate(self, crawler, first, second):
        _items, deduped = self.build(crawler, [
            self.CARD.format(sid="200", rnd=first, period=""),
            self.CARD.format(sid="201", rnd=second, period=""),
        ])
        assert len(deduped) == 2

    def test_same_round_but_different_links_stays_two(self, crawler):
        """회차가 같아도 링크가 다르면 합치지 않는다 (11차 게이트)."""
        _items, deduped = self.build(crawler, [
            self.CARD.format(sid="300", rnd="2차", period=""),
            self.CARD.format(sid="301", rnd="2차", period=""),
        ])
        assert len(deduped) == 2

    def test_same_link_twice_merges(self, crawler):
        """같은 링크가 두 번 = 진짜 복제 -> 1건."""
        _items, deduped = self.build(crawler, [
            self.CARD.format(sid="400", rnd="교육", period=""),
            self.CARD.format(sid="400", rnd="교육", period=""),
        ])
        assert len(deduped) == 1
        assert deduped[0]["merged_count"] == 2

    def test_equal_deadline_no_longer_merges_rounds(self, crawler):
        """11차 게이트: 마감이 같아도 **회차가 다르면** 합치지 않는다.

        예전에는 같은 마감이면 회차를 무시하고 합쳤다. 그 규칙이 게시일
        슬롯과 만나면서 별개 회차가 사라졌다 - 이제 어느 쪽도 잃지 않는다.
        """
        _items, deduped = self.build(crawler, [
            self.CARD.format(sid="8371", rnd="9차", period="2026.09.01 ~ 2026.12.31"),
            self.CARD.format(sid="8370", rnd="8차", period="2026.08.01 ~ 2026.12.31"),
        ])
        assert len(deduped) == 2

    def test_live_fixture_keeps_every_card(self, crawler):
        """실측 fixture 회귀: 22 카드 -> 22 건 (진짜 복제 없음)."""
        soup = BeautifulSoup(
            (FIXTURES / "seis_main_cards.html").read_text(encoding="utf-8"),
            "html.parser",
        )
        items = crawler._parse_main_cards(soup)
        deduped = crawler._dedupe_items(items)
        assert (len(items), len(deduped)) == (22, 22)
        assert not any(i.get("merged_count") for i in deduped)

    def test_extract_round_tokens(self):
        from alert.crawlers.dedupe_keys import extract_round

        assert extract_round(["2026년도 (9차)"]) == "9차"
        assert extract_round(["상시 모집"]) == "상시"
        assert extract_round(["추가 모집", "1차"]) == "1차|추가"
        assert extract_round(["교육", "시설/공간"]) == ""

    def test_round_only_enters_the_cleanup_key_when_the_deadline_is_unknown(self):
        """정리 스크립트가 쓰는 ``group_key`` 의 규칙은 그대로다."""
        from alert.crawlers.dedupe_keys import group_key

        known = group_key("공고", ["서울"], "2026-12-31", round_candidates=["9차"])
        unknown = group_key("공고", ["서울"], None, "2026-09", round_candidates=["9차"])
        assert known[3] == ""          # 마감을 알면 회차는 키에서 빠진다
        assert unknown[3] == "9차"

    def test_collection_key_always_keeps_the_round(self):
        """수집 단계 키(``replica_key``)는 회차를 **항상** 담는다."""
        from alert.crawlers.dedupe_keys import replica_key

        first = replica_key("공고", ["서울"], "/view?id=1", round_candidates=["9차"])
        second = replica_key("공고", ["서울"], "/view?id=1", round_candidates=["8차"])
        assert first[3] == "9차" and second[3] == "8차"
        assert first != second


class TestGate7AlwaysOpenToken:
    """7차 게이트 #4: `상시` 토큰이 키에 닿기 전에 사라지던 결함.

    ``_DDAY_RE`` 가 D-day 배지를 걸러낼 때 ``^상시$`` 까지 버렸다. 그래서
    같은 카드의 ``[교육,1차,상시]`` 와 ``[교육,1차]`` 가 둘 다
    ``round=1차, info=[교육]`` 이 되어 2건이 1건으로 병합됐다.
    """

    CARD = """
    <li class="swiper-slide" data-type="사업공고">
      <span class="badge cate">사업공고</span>
      <span class="sub">서울센터</span>
      <p class="tit"><a href="subPage.do?menuId=30400&tabId=view&itgrdAplyPbancSn={sid}">
         상주기업 모집 공고</a></p>
      <ul class="info">{info}</ul>
      <p class="date">{period}</p>
    </li>
    """

    @pytest.fixture
    def crawler(self):
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 1
        config.crawler.retry_delay = 0
        config.crawler.user_agent = "test-agent"
        source = MagicMock()
        source.enabled = True
        source.base_url = "https://www.seis.or.kr"
        source.fetch_detail = False
        config.crawler.sources = {"seis": source}
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield SeisCrawler()

    def build(self, crawler, cards):
        soup = BeautifulSoup("<ul>" + "".join(cards) + "</ul>", "html.parser")
        items = crawler._parse_main_cards(soup)
        return items, crawler._dedupe_items(items)

    def test_always_open_card_stays_separate(self, crawler):
        """``[교육,1차,상시]`` 와 ``[교육,1차]`` 는 별개 공고다."""
        items, deduped = self.build(crawler, [
            self.CARD.format(
                sid="100", info="<li>교육</li><li>1차</li><li>상시</li>", period=""
            ),
            self.CARD.format(sid="101", info="<li>교육</li><li>1차</li>", period=""),
        ])
        assert len(items) == 2
        assert len(deduped) == 2

    def test_always_open_survives_parsing(self, crawler):
        """상시가 info 에서 버려지지 않는다."""
        items, _deduped = self.build(crawler, [
            self.CARD.format(
                sid="100", info="<li>교육</li><li>1차</li><li>상시</li>", period=""
            ),
        ])
        assert "상시" in items[0]["info"]

    def test_always_open_lands_in_raw_data(self, crawler):
        """상시 표기는 raw_data.always_open 으로도 남는다."""
        items, _deduped = self.build(crawler, [
            self.CARD.format(sid="100", info="<li>교육</li><li>상시</li>", period=""),
        ])
        announcement = crawler._to_announcement(items[0], "https://www.seis.or.kr")
        assert json.loads(announcement.raw_data)["always_open"] is True

    def test_no_always_open_flag_when_absent(self, crawler):
        items, _deduped = self.build(crawler, [
            self.CARD.format(sid="101", info="<li>교육</li><li>1차</li>", period=""),
        ])
        announcement = crawler._to_announcement(items[0], "https://www.seis.or.kr")
        assert "always_open" not in json.loads(announcement.raw_data)

    def test_dday_badges_are_still_filtered(self, crawler):
        """D-day·마감 배지는 여전히 걸러낸다 - 지역·회차가 아니다."""
        items, _deduped = self.build(crawler, [
            self.CARD.format(
                sid="102", info="<li>경기도</li><li>D-109</li><li>마감</li>", period=""
            ),
        ])
        assert items[0]["info"] == ["경기도"]


class TestGate7RoundTokenBoundaries:
    """7차 게이트 #5: 기관명 부분 문자열이 회차로 잡히던 결함."""

    def test_org_name_substring_is_not_a_round(self):
        """"여수시청" 안의 "수시" 를 회차로 보지 않는다."""
        from alert.crawlers.dedupe_keys import extract_round, group_key

        assert extract_round(["여수시청 지원사업"]) == ""
        assert extract_round(["여수 시청 지원사업"]) == ""
        assert group_key("여수시청 지원사업", ["교육"], None, "2026-09") == \
            group_key("여수 시청 지원사업", ["교육"], None, "2026-09")

    @pytest.mark.parametrize("text,expected", [
        ("2026년도 (9차)", "9차"),
        ("1차 모집", "1차"),
        ("상시", "상시"),
        ("상시모집", "상시"),
        ("추가모집", "추가"),
        ("연장 공고", "연장"),
        ("제1차수 배정", ""),       # 차수는 회차 표기가 아니다
        ("여수시청", ""),
        ("교육", ""),
    ])
    def test_round_token_extraction(self, text, expected):
        from alert.crawlers.dedupe_keys import extract_round

        assert extract_round([text]) == expected


class TestCycle11SeisWhitelist:
    """11차 허용목록 (a): SEIS 메인 카드의 ``p.date`` 만 접수기간이다.

    카드의 날짜 자리는 사이트 구조상 접수기간 필드이므로 파서가
    ``date_label="접수기간"`` 을 붙인다. 그래서 이 소스만 목록 단계에서
    기간을 만들 수 있다.
    """

    @pytest.fixture
    def crawler(self):
        config = MagicMock()
        config.crawler.timeout = 10
        config.crawler.retry_count = 1
        config.crawler.retry_delay = 0
        config.crawler.user_agent = "test-agent"
        source = MagicMock()
        source.enabled = True
        source.base_url = "https://www.seis.or.kr"
        source.fetch_detail = False
        config.crawler.sources = {"seis": source}
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield SeisCrawler()

    @pytest.fixture
    def soup(self):
        return BeautifulSoup(
            (FIXTURES / "seis_main_cards.html").read_text(encoding="utf-8"),
            "html.parser",
        )

    def test_card_parser_records_provenance_not_a_label(self, crawler, soup):
        """파서는 **라벨을 지어내지 않고 출처를 기록**한다.

        12차까지 카드 파서는 ``date_label="접수기간"`` 이라는 자유 라벨을
        붙였고, 그 라벨이 무라벨 범위를 전부 마감으로 만들었다 (9차 게이트
        HIGH ①). 지금은 라벨 대신 **어느 셀렉터에서 읽었는지**를 남기고,
        추출기가 그 자리만 인정한다.
        """
        items = crawler._parse_main_cards(soup)
        assert items and all(i["date_label"] == "" for i in items)
        assert all(i["date_field"] == SEIS_CARD_DATE_FIELD for i in items)

        deduped = crawler._dedupe_items(items)
        announcements = [
            _finalize_periods(
                "seis", crawler._to_announcement(i, "https://www.seis.or.kr")
            )
            for i in deduped
        ]
        assert len(announcements) == 22           # 어떤 카드도 잃지 않는다
        assert sum(1 for a in announcements if a.period_end) == 22
        # 날짜 자체도 잃지 않는다
        assert all(json.loads(a.raw_data).get("posted") for a in announcements)

    def test_author_difference_keeps_two_items(self, crawler):
        """재현 4: 제목·기간이 같아도 주체가 다르면 병합하지 않는다."""
        base = {
            "title": "2026년 사회적기업 지원사업 참여기업 모집 공고",
            "date": "2026.09.01 ~ 2026.12.31",
            "info": ["경기도"],
        }
        deduped = crawler._dedupe_items([
            {**base, "sub": "사회보험료 지원 사업", "link": "subPage.do?fncPbofrSn=1"},
            {**base, "sub": "일자리창출 지원 사업", "link": "subPage.do?fncPbofrSn=2"},
        ])
        assert len(deduped) == 2

        # 11차 게이트: 주체가 같아도 **링크가 다르면** 합치지 않는다
        other_link = crawler._dedupe_items([
            {**base, "sub": "사회보험료 지원 사업", "link": "subPage.do?fncPbofrSn=1"},
            {**base, "sub": "사회보험료 지원 사업", "link": "subPage.do?fncPbofrSn=2"},
        ])
        assert len(other_link) == 2

        same_link = crawler._dedupe_items([
            {**base, "sub": "사회보험료 지원 사업", "link": "subPage.do?fncPbofrSn=1"},
            {**base, "sub": "사회보험료 지원 사업", "link": "subPage.do?fncPbofrSn=1"},
        ])
        assert len(same_link) == 1
