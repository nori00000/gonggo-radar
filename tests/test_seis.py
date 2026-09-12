"""Tests for SeisCrawler (사회적기업포털 SEIS)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from alert.crawlers.seis import SeisCrawler
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

            # Parameter-based ID
            assert crawler._extract_post_id("https://example.com?nttId=12345") == "12345"
            assert crawler._extract_post_id("https://example.com?seq=67890") == "67890"
            assert crawler._extract_post_id("https://example.com?idx=54321") == "54321"

            # Path-based ID
            assert crawler._extract_post_id("https://example.com/view/123456") == "123456"

            # Empty link
            assert crawler._extract_post_id("") == ""

            # Hash fallback for unrecognized pattern
            result = crawler._extract_post_id("https://example.com/some-page")
            assert len(result) == 16  # MD5 hash truncated to 16 chars

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
            assert result.period_start == "2026-04-01"
            assert result.period_end == "2026-04-30"

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

    def test_duplicate_rounds_collapse_to_one_item(self, crawler, soup):
        """같은 제목·지역의 회차별 링크 9건이 1건으로 합쳐진다."""
        items = crawler._parse_main_cards(soup)
        duplicates = [
            i for i in items
            if i["title"].startswith("2026년 경기도 사회적기업 사회보험료")
        ]
        assert len(duplicates) == 9

        deduped = crawler._dedupe_items(items)
        assert len(deduped) == 14

        merged = [
            i for i in deduped
            if i["title"].startswith("2026년 경기도 사회적기업 사회보험료")
        ]
        assert len(merged) == 1
        assert merged[0]["merged_count"] == 9

    def test_canonical_item_is_the_current_round(self, crawler, soup):
        """대표는 접수 시작일이 가장 늦은 최신 회차(9차)다."""
        deduped = crawler._dedupe_items(crawler._parse_main_cards(soup))
        merged = next(
            i for i in deduped
            if i["title"].startswith("2026년 경기도 사회적기업 사회보험료")
        )

        announcement = crawler._to_announcement(merged, "https://www.seis.or.kr")
        assert announcement is not None
        assert announcement.source_id == "8371"
        assert announcement.url == (
            "https://www.seis.or.kr/subPage.do"
            "?menuId=30200&tabId=pbancMainView&fncPbofrSn=8371"
        )
        assert announcement.period_start == "2026-09-01"
        assert announcement.period_end == "2026-12-31"
        assert announcement.author == "한국사회적기업진흥원"

    def test_merged_links_are_auditable(self, crawler, soup):
        """병합된 나머지 링크 ID와 회차가 raw_data에 남는다."""
        deduped = crawler._dedupe_items(crawler._parse_main_cards(soup))
        merged = next(
            i for i in deduped
            if i["title"].startswith("2026년 경기도 사회적기업 사회보험료")
        )
        announcement = crawler._to_announcement(merged, "https://www.seis.or.kr")
        payload = json.loads(announcement.raw_data)

        assert payload["merged_count"] == 9
        assert payload["merged_source_ids"] == [
            "8370", "8369", "8368", "8367", "8366", "8365", "8364", "8339"
        ]
        assert len(payload["merged_rounds"]) == 9

    def test_distinct_subjects_stay_separate(self, crawler, soup):
        """지정공모는 주체(span.sub)가 달라 각각 남는다."""
        deduped = crawler._dedupe_items(crawler._parse_main_cards(soup))
        subjects = {
            i["sub"] for i in deduped if i["title"].endswith("지정공모")
        }
        assert {"서울특별시", "광주광역시", "전라남도", "경상북도", "산림청"} <= subjects

    def test_every_card_item_has_a_deadline(self, crawler, soup):
        """카드 파싱은 접수기간을 채운다 - 마감 NULL 문제 해소 (판정 4)."""
        deduped = crawler._dedupe_items(crawler._parse_main_cards(soup))
        announcements = [
            crawler._to_announcement(i, "https://www.seis.or.kr") for i in deduped
        ]
        assert announcements
        assert all(a.period_end for a in announcements)

    def test_source_ids_are_unique_and_stable(self, crawler, soup):
        """source_id는 URL의 공고 고유번호이며 배치 내에서 충돌하지 않는다."""
        deduped = crawler._dedupe_items(crawler._parse_main_cards(soup))
        ids = [
            crawler._to_announcement(i, "https://www.seis.or.kr").source_id
            for i in deduped
        ]
        assert len(ids) == len(set(ids))
        assert "8371" in ids
        # 인·지정 공모는 dsgnPbofrSn 을 쓴다 (예전에는 MD5 해시로 떨어졌다)
        assert "8322" in ids

    def test_epsd_no_is_not_matched_as_generic_no_param(self, crawler):
        """"epsdNo=4" 가 범용 "no=" 패턴에 걸려 다른 공고와 충돌하지 않는다."""
        link = (
            "/subPage.do?menuId=30100&tabId=certPageView&statsYr=2026&epsdNo=4"
        )
        assert crawler._extract_post_id(link) == "4"
        # 같은 자리에 다른 파라미터가 와도 고유번호를 먼저 본다
        assert crawler._extract_post_id(
            "/subPage.do?menuId=30200&tabId=pbancMainView&fncPbofrSn=8371"
        ) == "8371"
        assert crawler._extract_post_id(
            "/subPage.do?menuId=30100&tabId=certPageView&dsgnPbofrSn=8322"
        ) == "8322"

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
        ends = {
            crawler._to_announcement(i, "https://www.seis.or.kr").period_end
            for i in deduped
        }
        assert ends == {"2026-08-31", "2026-09-30"}

    def test_genuine_round_duplication_still_merges(self, crawler):
        """주체·제목·종료일이 모두 같은 회차 중복은 여전히 1건으로 합친다."""
        _items, deduped = self.build(crawler, [
            self.CARD.format(sub="사회보험료 지원 사업", sid="8371", info="경기도",
                             period="2026.09.01 ~ 2026.12.31"),
            self.CARD.format(sub="사회보험료 지원 사업", sid="8370", info="경기도",
                             period="2026.08.01 ~ 2026.12.31"),
        ])
        assert len(deduped) == 1
        assert deduped[0]["merged_count"] == 2
        announcement = crawler._to_announcement(deduped[0], "https://www.seis.or.kr")
        assert announcement.source_id == "8371"          # 최신 회차가 대표
        assert announcement.period_end == "2026-12-31"

    def test_group_key_components(self, crawler):
        """중복 판별 키는 제목·주체·종료일 세 조각이다."""
        base = {"title": "같은 제목", "sub": "서울센터", "date": "2026.09.01 ~ 2026.09.30"}
        assert crawler._group_key(base) == crawler._group_key(dict(base))
        assert crawler._group_key(base) != crawler._group_key(
            {**base, "sub": "부산센터"}
        )
        assert crawler._group_key(base) != crawler._group_key(
            {**base, "date": "2026.09.01 ~ 2026.10.31"}
        )
        # 양쪽 다 기간이 없으면 같은 키다
        assert crawler._group_key({**base, "date": ""}) == crawler._group_key(
            {**base, "date": ""}
        )

