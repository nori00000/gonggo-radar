"""상세 수집이 **전 소스에서 꺼져 있음**을 고정한다 (2026-09-13 판정).

5차 게이트까지 HIGH가 남았고, 사전에 정해 둔 규칙("HIGH 잔존이면 머지 시
fetch_detail 전면 OFF")에 따라 전 소스를 껐다. 코드는 남기되 비활성이다.
이 테스트는 그 상태가 실수로 뒤집히지 않게 지킨다 - 다시 켜려면 이
테스트를 의식적으로 고쳐야 한다.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from alert.config import SourceConfig, get_config
from alert.crawlers.base import BaseCrawler
from alert.crawlers.coop import CoopCrawler
from alert.crawlers.kofpi import KofpiCrawler
from alert.crawlers.lawmaking import LawmakingCrawler
from alert.crawlers.seis import SeisCrawler
from alert.crawlers.socialenterprise import SocialenterpriseCrawler
from alert.models import RawAnnouncement

CONFIG_PATH = Path(__file__).resolve().parent.parent / "alert" / "config.yaml"
DETAIL_CRAWLERS = {
    "seis": SeisCrawler,
    "kofpi": KofpiCrawler,
    "lawmaking": LawmakingCrawler,
    "coop": CoopCrawler,
    "socialenterprise": SocialenterpriseCrawler,
}


@pytest.fixture
def raw_sources():
    """배포되는 config.yaml 의 소스 블록."""
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return data["crawler"]["sources"]


class TestShippedConfigHasDetailOff:
    """배포되는 config.yaml 자체를 검사한다."""

    def test_no_source_enables_detail(self, raw_sources):
        enabled = [
            name for name, cfg in raw_sources.items()
            if isinstance(cfg, dict) and cfg.get("fetch_detail") is True
        ]
        assert enabled == [], f"fetch_detail 이 켜진 소스: {enabled}"

    def test_previously_enabled_sources_are_explicitly_false(self, raw_sources):
        """예전에 켜져 있던 다섯 소스는 **명시적으로** false 로 적혀 있다."""
        for name in DETAIL_CRAWLERS:
            assert raw_sources[name].get("fetch_detail") is False, name

    def test_loaded_config_reports_detail_off(self):
        config = get_config(reload=True)
        for name in DETAIL_CRAWLERS:
            assert config.crawler.sources[name].fetch_detail is False, name

    def test_dataclass_default_is_off(self):
        """설정에 키가 없으면 꺼진 상태가 기본이다."""
        assert SourceConfig().fetch_detail is False


class TestCrawlersRequestNothing:
    """실제 config로 만든 크롤러는 상세를 한 건도 요청하지 않는다."""

    @pytest.mark.parametrize("name", sorted(DETAIL_CRAWLERS))
    def test_wants_detail_is_false(self, name):
        config = get_config(reload=True)
        with patch("alert.crawlers.base.get_config", return_value=config):
            crawler = DETAIL_CRAWLERS[name]()
        assert crawler.wants_detail() is False

    @pytest.mark.parametrize("name", sorted(DETAIL_CRAWLERS))
    def test_enrich_requests_zero_items(self, name):
        """``enrich_with_quotes`` 가 워커를 띄우지 않는다."""
        config = get_config(reload=True)
        with patch("alert.crawlers.base.get_config", return_value=config):
            crawler = DETAIL_CRAWLERS[name]()

        announcements = [
            RawAnnouncement(
                source=name, source_id=str(index), title="공고",
                url=f"https://example.com/view/{index}",
            )
            for index in range(5)
        ]
        with patch.object(crawler, "run_detail_worker") as worker:
            returned = crawler.enrich_with_quotes(announcements)

        worker.assert_not_called()
        assert returned is announcements
        assert all(not a.raw_data for a in announcements)

    @pytest.mark.parametrize("name", sorted(DETAIL_CRAWLERS))
    def test_no_subprocess_is_spawned(self, name):
        """프로세스도 뜨지 않는다 - launchd 실행 시 자식이 생기지 않는다."""
        import subprocess

        config = get_config(reload=True)
        with patch("alert.crawlers.base.get_config", return_value=config):
            crawler = DETAIL_CRAWLERS[name]()

        announcements = [
            RawAnnouncement(source=name, source_id="1", title="공고",
                            url="https://example.com/view/1")
        ]
        with patch.object(subprocess, "run") as run:
            crawler.enrich_with_quotes(announcements)
        run.assert_not_called()


class TestDetailCodeStaysCallableButInactive:
    """코드는 남아 있다 - 다시 켜는 판정이 내려지면 쓸 수 있어야 한다."""

    class _Stub(BaseCrawler):
        def fetch(self):  # pragma: no cover
            return []

    def _stub(self, fetch_detail: bool):
        config = MagicMock()
        config.crawler.timeout = 30
        config.crawler.retry_count = 1
        config.crawler.retry_delay = 0
        config.crawler.user_agent = "AgriAlert/1.0"
        config.crawler.sources = {
            "stub": SourceConfig(
                enabled=True, base_url="https://example.com",
                fetch_detail=fetch_detail,
            )
        }
        with patch("alert.crawlers.base.get_config", return_value=config):
            return self._Stub(source_name="stub")

    def test_opt_in_still_works_when_asked(self):
        """소스별로 켜면 동작한다 (배포 설정은 꺼져 있다)."""
        assert self._stub(fetch_detail=True).wants_detail() is True
        assert self._stub(fetch_detail=False).wants_detail() is False

    def test_worker_module_still_exists(self):
        from alert.crawlers.detail_quotes import WORKER_MODULE, fetch_detail_quotes

        assert WORKER_MODULE == "alert.crawlers.detail_quotes"
        assert callable(fetch_detail_quotes)
