"""Integration tests for API crawlers - requires real API keys."""
import os
import pytest
from alert.crawlers.g2b import G2bCrawler
from alert.crawlers.kstartup import KStartupCrawler

# Skip all tests if no API key
pytestmark = pytest.mark.integration
HAS_API_KEY = bool(os.getenv("DATA_GO_KR_API_KEY"))


@pytest.mark.skipif(not HAS_API_KEY, reason="DATA_GO_KR_API_KEY not set")
class TestG2bIntegration:
    def test_fetch_returns_list(self):
        crawler = G2bCrawler()
        results = crawler.fetch()
        assert isinstance(results, list)

    def test_items_have_required_fields(self):
        crawler = G2bCrawler()
        results = crawler.fetch()
        if results:  # May be empty if no recent bids
            item = results[0]
            assert item.source == "g2b"
            assert item.source_id
            assert item.title

    def test_dates_are_iso_format(self):
        crawler = G2bCrawler()
        results = crawler.fetch()
        import re
        for item in results[:5]:
            if item.period_start:
                assert re.match(r"\d{4}-\d{2}-\d{2}", item.period_start)


@pytest.mark.skipif(not HAS_API_KEY, reason="DATA_GO_KR_API_KEY not set")
class TestKStartupIntegration:
    def test_fetch_returns_list(self):
        crawler = KStartupCrawler()
        results = crawler.fetch()
        assert isinstance(results, list)

    def test_items_have_required_fields(self):
        crawler = KStartupCrawler()
        results = crawler.fetch()
        if results:
            item = results[0]
            assert item.source == "kstartup"
            assert item.source_id
            assert item.title

    def test_dates_are_iso_format(self):
        crawler = KStartupCrawler()
        results = crawler.fetch()
        import re
        for item in results[:5]:
            if item.period_start:
                assert re.match(r"\d{4}-\d{2}-\d{2}", item.period_start)
