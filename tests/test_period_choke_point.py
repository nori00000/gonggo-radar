"""기간 choke point 회귀 - 선언하지 않은 소스는 기간을 저장할 수 없다 (12차).

열한 차례의 게이트에서 환각 마감은 늘 같은 모양으로 들어왔다: 목록의
어떤 날짜를 **접수기간으로 잘못 읽는다**. 읽기 규칙을 열한 번 좁히는
대신, 12차는 **쓸 수 있는 소스를 선언으로 제한**했다.

``BaseCrawler.PERIOD_EXTRACTOR`` 를 선언한 크롤러만 기간을 만들 수 있고,
``safe_fetch`` 가 나머지 전부의 기간을 지운다. 파서를 어떻게 고치든,
새 크롤러를 어떻게 쓰든 이 관문을 지난다.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

import alert.crawlers as crawlers_pkg
from alert.crawlers.base import BaseCrawler
from alert.crawlers.coop import CoopCrawler
from alert.crawlers.forest_press import ForestPressCrawler
from alert.crawlers.forest_service import ForestServiceCrawler
from alert.crawlers.fowi import FowiCrawler
from alert.crawlers.ipet import IpetCrawler
from alert.crawlers.kofpi import KofpiCrawler
from alert.crawlers.lawmaking import LawmakingCrawler
from alert.crawlers.nongup_gg import NongupGgCrawler
from alert.crawlers.rda import RdaCrawler
from alert.crawlers.seis import SeisCrawler
from alert.crawlers.semas import SemasCrawler
from alert.crawlers.socialenterprise import SocialenterpriseCrawler
from alert.models import RawAnnouncement

# 기간을 만들 수 있는 소스 = 허용목록 3곳
#   (a) SEIS 메인 카드/표의 접수기간 필드
#   (b) KOFPI 제목 끝의 ``(~M.D)``
#   (c) 국민참여입법센터 목록의 의견제출 기간
WHITELISTED = {"SeisCrawler", "KofpiCrawler", "LawmakingCrawler"}


def all_crawler_classes():
    """``alert.crawlers`` 가 내보내는 모든 크롤러 클래스."""
    found = []
    for name in crawlers_pkg.__all__:
        obj = getattr(crawlers_pkg, name)
        if isinstance(obj, type) and issubclass(obj, BaseCrawler) and obj is not BaseCrawler:
            found.append(obj)
    return found


def make(cls, source_name):
    config = MagicMock()
    config.crawler.timeout = 10
    config.crawler.retry_count = 1
    config.crawler.retry_delay = 0
    config.crawler.user_agent = "test-agent"
    source = MagicMock()
    source.enabled = True
    source.base_url = "https://example.test"
    source.fetch_detail = False
    config.crawler.sources = {source_name: source}
    with patch("alert.crawlers.base.get_config", return_value=config):
        return cls()


class TestDeclarationIsTheWhitelist:
    """선언이 허용목록이다 - 코드가 곧 명세다."""

    def test_exactly_three_crawlers_declare_an_extractor(self):
        declared = {
            cls.__name__ for cls in all_crawler_classes() if cls.PERIOD_EXTRACTOR
        }
        assert declared == WHITELISTED

    def test_base_declares_nothing(self):
        assert BaseCrawler.PERIOD_EXTRACTOR is None

    def test_every_declared_name_resolves_to_a_method(self):
        for cls in all_crawler_classes():
            if not cls.PERIOD_EXTRACTOR:
                continue
            assert callable(getattr(cls, cls.PERIOD_EXTRACTOR, None)), cls.__name__

    def test_forest_press_inherits_no_declaration(self):
        """상속으로 선언이 새지 않는다 (forest_press → forest_service)."""
        assert ForestPressCrawler.PERIOD_EXTRACTOR is None


EXCLUDED = [
    ("fowi", FowiCrawler),
    ("forest_press", ForestPressCrawler),
    ("forest_service", ForestServiceCrawler),
    ("socialenterprise", SocialenterpriseCrawler),
    ("coop", CoopCrawler),
    ("nongup_gg", NongupGgCrawler),
    ("ipet", IpetCrawler),
    ("rda", RdaCrawler),
    ("semas", SemasCrawler),
]


class TestChokePointStripsUndeclaredSources:
    """선언 없는 소스는 **어떤 파서 출력이 와도** 기간이 지워진다."""

    @pytest.mark.parametrize("source_name,cls", EXCLUDED)
    def test_safe_fetch_nulls_any_period(self, source_name, cls):
        crawler = make(cls, source_name)
        assert crawler.declares_period_extractor() is False

        # 파서가 기간을 채워 돌려주더라도
        forged = RawAnnouncement(
            source=source_name,
            source_id="1",
            title="공고",
            url="https://example.test/1",
            period_start="2026-04-01",
            period_end="2026-04-30",
        )
        with patch.object(cls, "fetch", return_value=[forged]):
            results = crawler.safe_fetch()

        assert len(results) == 1
        assert (results[0].period_start, results[0].period_end) == (None, None)

    @pytest.mark.parametrize("source_name,cls", EXCLUDED)
    def test_resolve_period_is_always_empty(self, source_name, cls):
        crawler = make(cls, source_name)
        assert crawler.resolve_period({"date": "2026.09.01 ~ 2026.09.30"}) == (
            None, None
        )

    @pytest.mark.parametrize("source_name,cls", [
        ("seis", SeisCrawler), ("kofpi", KofpiCrawler),
        ("lawmaking", LawmakingCrawler),
    ])
    def test_declared_sources_pass_through(self, source_name, cls):
        """허용목록 소스의 기간은 관문이 건드리지 않는다."""
        crawler = make(cls, source_name)
        assert crawler.declares_period_extractor() is True
        kept = RawAnnouncement(
            source=source_name,
            source_id="1",
            title="공고",
            url="https://example.test/1",
            period_start="2026-09-01",
            period_end="2026-09-30",
        )
        with patch.object(cls, "fetch", return_value=[kept]):
            results = crawler.safe_fetch()
        assert (results[0].period_start, results[0].period_end) == (
            "2026-09-01", "2026-09-30"
        )


class TestSeisExtractor:
    """허용목록 (a) - 접수기간 필드만."""

    @pytest.fixture
    def crawler(self):
        return make(SeisCrawler, "seis")

    @pytest.mark.parametrize("label,value,expected", [
        # 정상: 카드의 무라벨 범위 (파서가 구조 라벨을 붙인다)
        ("접수기간", "2026.09.01 ~ 2026.09.30", ("2026-09-01", "2026-09-30")),
        # 정상: 값 자체가 접수기간 라벨을 품은 경우
        ("접수기간", "접수기간 2026.09.01 ~ 2026.09.30", ("2026-09-01", "2026-09-30")),
        # 정상: 표 헤더가 접수마감 = 마감 하나
        ("접수마감", "2026.09.30", (None, "2026-09-30")),
        # 결함 재현: 셀 본문이 교육기간이면 구조 라벨을 믿지 않는다
        ("접수기간", "교육기간 2026.10.01 ~ 2026.10.31", (None, None)),
        ("접수기간", "행사일정 2026.10.15", (None, None)),
        # 접수기간 라벨인데 범위가 아니면 기간이 아니다
        ("접수기간", "2026.09.11", (None, None)),
        # 허용목록 밖 라벨
        ("구분", "2026.09.01 ~ 2026.09.30", (None, None)),
        ("게시일", "2026.09.11", (None, None)),
        ("", "2026.09.01 ~ 2026.09.30", (None, None)),
        # 빈 값
        ("접수기간", "", (None, None)),
    ])
    def test_period_from_reception_field(self, crawler, label, value, expected):
        assert crawler._period_from_reception_field(
            {"date": value, "date_label": label}
        ) == expected

    def test_rejected_date_survives_as_posted(self, crawler):
        """기간으로 인정되지 않은 날짜는 잃지 않고 게시일로 남는다."""
        announcement = crawler._to_announcement(
            {
                "title": "교육 공고",
                "link": "subPage.do?fncPbofrSn=1",
                "date": "교육기간 2026.10.01 ~ 2026.10.31",
                "date_label": "접수기간",
            },
            "https://www.seis.or.kr",
        )
        assert (announcement.period_start, announcement.period_end) == (None, None)
        assert json.loads(announcement.raw_data)["posted"] == "2026-10-01"
