"""Regression tests for crawler source_id / detail-URL defects found in live crawls.

Two defects were measured on 2026-09-12:

1. ``socialenterprise``: ``_extract_post_id`` matched the *board* discriminator
   ``bsIdx=10002`` (identical for every row) instead of the post id ``bIdx``,
   so UNIQUE(source, source_id) collapsed a batch of 4 rows into 1 stored row.
2. ``smartfarm``: list rows link with ``href="#void"`` and submit a POST form,
   so every announcement got the URL ``https://www.smartfarmkorea.net/#void``
   and 9 of 10 rows were lost to the same collision.

Both are exercised here against saved fixtures - no network access.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from alert.crawlers.smartfarm import SmartfarmCrawler
from alert.crawlers.socialenterprise import SocialenterpriseCrawler

FIXTURES = Path(__file__).parent / "fixtures"


def make_config(source_name: str, base_url: str) -> MagicMock:
    """Build a mock AppConfig exposing a single enabled crawler source."""
    config = MagicMock()
    config.crawler.timeout = 10
    config.crawler.retry_count = 1
    config.crawler.retry_delay = 0
    config.crawler.user_agent = "test-agent"

    source = MagicMock()
    source.enabled = True
    source.base_url = base_url
    config.crawler.sources = {source_name: source}
    return config


class TestSocialenterpriseSourceId:
    """socialenterprise: 게시판 구분자(bsIdx)가 아니라 글 번호(bIdx)를 ID로 쓴다."""

    @pytest.fixture
    def crawler(self):
        config = make_config("socialenterprise", "https://www.socialenterprise.or.kr")
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield SocialenterpriseCrawler()

    @pytest.fixture
    def ajax_payload(self):
        return json.loads(
            (FIXTURES / "socialenterprise_board_ajax.json").read_text(encoding="utf-8")
        )

    def test_extract_post_id_prefers_bidx_over_bsidx(self, crawler):
        """bsIdx(게시판)와 bIdx(글)가 함께 있으면 bIdx를 써야 한다."""
        link = (
            "/homepage/bbs/boardView.do"
            "?bsIdx=10002&bIdx=252629&page=1&menuId=822&bcIdx=10001"
        )
        assert crawler._extract_post_id(link) == "252629"

    def test_extract_post_id_keeps_existing_patterns(self, crawler):
        """기존에 지원하던 파라미터/경로 패턴은 그대로 동작한다."""
        assert crawler._extract_post_id("https://example.com?nttId=12345") == "12345"
        assert crawler._extract_post_id("https://example.com?seq=67890") == "67890"
        assert crawler._extract_post_id("https://example.com?idx=54321") == "54321"
        assert crawler._extract_post_id("https://example.com/view/123456") == "123456"
        assert crawler._extract_post_id("") == ""

    def test_ajax_batch_yields_unique_source_ids(self, crawler, ajax_payload):
        """AJAX 한 배치의 모든 글이 서로 다른 source_id를 가져야 한다.

        이 어서션이 실패하면 UNIQUE(source, source_id) 충돌로 한 건만 저장된다.
        """
        response = MagicMock()
        response.json.return_value = ajax_payload

        with patch.object(crawler, "post", return_value=response):
            items = crawler._fetch_ajax_board(
                "https://www.socialenterprise.or.kr/homepage/bbs/ajax/boardList.do",
                {"bsIdx": "10002", "menuId": "822"},
                "https://www.socialenterprise.or.kr",
            )

        assert len(items) == len(ajax_payload["resultList"])

        announcements = [
            crawler._to_announcement(item, "https://www.socialenterprise.or.kr")
            for item in items
        ]
        source_ids = [a.source_id for a in announcements if a]

        assert len(source_ids) == len(items)
        assert len(set(source_ids)) == len(source_ids)
        # 게시판 구분자가 ID로 새어 들어오지 않았는지 확인
        assert "10002" not in source_ids
        assert set(source_ids) >= {
            row["B_IDX"] for row in ajax_payload["resultList"][:3]
        }


class TestSmartfarmDetailUrl:
    """smartfarm: href="#void" 대신 실제 상세 URL을 만들어야 한다."""

    @pytest.fixture
    def crawler(self):
        config = make_config("smartfarm", "https://www.smartfarmkorea.net")
        with patch("alert.crawlers.base.get_config", return_value=config):
            yield SmartfarmCrawler()

    @pytest.fixture
    def soup(self):
        html = (FIXTURES / "smartfarm_notice.html").read_text(encoding="utf-8")
        return BeautifulSoup(html, "html.parser")

    def test_board_view_params_read_from_form(self, crawler, soup):
        """상세 URL 파라미터를 목록 페이지의 searchFrm 히든 필드에서 읽는다."""
        params = crawler._board_view_params(soup)
        assert params["menuId"] == "M110502"
        assert params["searchBbsId"] == "BBSMSTR_000000000021"

    def test_void_links_are_resolved_to_detail_urls(self, crawler, soup):
        """href="#void" 링크가 /board/view.do 상세 URL로 치환된다."""
        items = crawler._resolve_view_links(soup, crawler._parse_table_board(soup))

        assert len(items) >= 5
        assert all("#void" not in item["link"] for item in items)
        assert items[0]["link"] == (
            "/board/view.do?menuId=M110502&searchNttId=4529"
            "&searchBbsId=BBSMSTR_000000000021"
        )

    def test_batch_yields_unique_source_ids_and_urls(self, crawler, soup):
        """한 목록 페이지의 모든 글이 서로 다른 source_id/URL을 가져야 한다."""
        items = crawler._resolve_view_links(soup, crawler._parse_table_board(soup))
        announcements = [
            crawler._to_announcement(item, "https://www.smartfarmkorea.net")
            for item in items
        ]
        announcements = [a for a in announcements if a]

        assert len(announcements) == len(items)

        source_ids = [a.source_id for a in announcements]
        urls = [a.url for a in announcements]

        assert len(set(source_ids)) == len(source_ids)
        assert len(set(urls)) == len(urls)
        assert all(url.startswith("https://www.smartfarmkorea.net/board/view.do")
                   for url in urls)

        first = announcements[0]
        assert first.source_id == "4529"
        assert first.period_start == "2026-09-07"

    def test_source_id_falls_back_to_title_and_date_hash(self, crawler):
        """글 번호를 못 구하면 제목+게시일 해시로 글마다 다른 ID를 만든다."""
        base = "https://www.smartfarmkorea.net"
        a = crawler._to_announcement(
            {"title": "같은 제목 공고", "link": "", "date": "2026-09-01"}, base
        )
        b = crawler._to_announcement(
            {"title": "같은 제목 공고", "link": "", "date": "2026-09-02"}, base
        )

        assert a is not None and b is not None
        assert len(a.source_id) == 16
        assert a.source_id != b.source_id

    def test_rows_without_onclick_get_empty_link(self, crawler):
        """boardView 호출이 없는 행은 링크를 비워 두고 해시 ID로 넘어간다."""
        soup = BeautifulSoup(
            '<table class="board_list"><tbody><tr>'
            '<td>1</td><td class="taL"><a href="#void">제목만 있는 행</a></td>'
            '<td>2026-09-01</td></tr></tbody></table>',
            "html.parser",
        )
        items = crawler._resolve_view_links(soup, crawler._parse_table_board(soup))

        assert items[0]["link"] == ""
