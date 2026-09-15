"""기간 관문 회귀 - 기간은 **저장 직전 한 곳**에서만 정해진다 (13차 게이트).

12차는 관문을 크롤러 쪽(``BaseCrawler.safe_fetch``)에 뒀다. 9차 Codex
게이트가 그 관문을 세 가지로 뚫었다:

1. 하위 클래스가 ``fetch()``/``safe_fetch()`` 에서 직접 만든 객체
2. 선언(``PERIOD_EXTRACTOR``)을 상속·override 하는 클래스
3. 라벨 추론(``classify_date``)으로 새어 들어오는 기간
   (구조 라벨 ``date_label="접수기간"``, 제목 라벨 ``신청기한 변경 안내``,
   인접 셀 ``/ 심사기간 …``)

13차는 관문을 **DB 도달 직전**(``alert.main._finalize_periods``)으로 옮기고,
라벨 추론을 **삭제**했다. 기간을 만들 수 있는 소스는 전용 순수 함수를 가진
``seis``·``lawmaking`` 둘뿐이다.
"""

import ast
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

import alert.crawlers as crawlers_pkg
from alert.analyzer import KeywordAnalyzer
from alert.crawlers.base import BaseCrawler
from alert.crawlers.forest_press import ForestPressCrawler
from alert.crawlers.kofpi import KofpiCrawler
from alert.crawlers.lawmaking import LawmakingCrawler
from alert.crawlers.period_extractors import (
    EVIDENCE_KEYS,
    PERIOD_EXTRACTORS,
    SEIS_CARD_DATE_FIELD,
    lawmaking_period,
    seis_period,
)
from alert.crawlers.seis import SeisCrawler
from alert.db import Database
from alert.main import (
    _finalize_periods,
    _import_crawlers,
    _periods_from_raw,
)
from alert.models import AnalyzedAnnouncement, RawAnnouncement

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
PLANTED = "2099-12-31"


def make(cls, source_name="test"):
    """설정을 목으로 채워 크롤러를 만든다 (소스 설정 없음 = 활성)."""
    config = MagicMock()
    config.crawler.timeout = 10
    config.crawler.retry_count = 1
    config.crawler.retry_delay = 0
    config.crawler.user_agent = "test-agent"
    config.crawler.sources = {}
    with patch("alert.crawlers.base.get_config", return_value=config):
        return cls()


def all_crawler_classes():
    """``alert.crawlers`` 가 내보내는 모든 크롤러 클래스."""
    found = []
    for name in crawlers_pkg.__all__:
        obj = getattr(crawlers_pkg, name)
        if isinstance(obj, type) and issubclass(obj, BaseCrawler) and obj is not BaseCrawler:
            found.append(obj)
    return found


def planted(source, raw=None):
    """근거 없이 기간이 심긴 수집 결과."""
    return RawAnnouncement(
        source=source,
        source_id="1",
        title="공고",
        url="https://example.test/1",
        period_start="2099-01-01",
        period_end=PLANTED,
        raw_data=json.dumps(raw or {}, ensure_ascii=False),
    )


PRODUCTION_SOURCES = sorted(_import_crawlers().keys())
CRAWLER_CLASSES = all_crawler_classes()


class TestOnlyTwoSourcesCanMakeAPeriod:
    """허용목록 = 전용 추출기를 가진 소스뿐이다.

    16차 게이트: bizinfo·g2b 추출기는 등록을 해제했다 - 회사용 경로여서
    협의회 브리핑과 무관하고, 그 기간을 유지하려다 식별자 설계가 사이클마다
    새 경합을 만들었다. 두 소스의 기간은 정규화가 비운다.

    P2-D: 목록 근거로 읽는 소스는 여전히 ``seis``·``lawmaking`` 둘뿐이고,
    ``forest_service``·``kofpi``·``socialenterprise`` 는 **상세 본문 근거**
    (``detail_text``)로만 읽는다 - 목록·제목은 어느 소스에서도 마감이 되지
    못한다(아래 ``TestKofpiNeverMakesADeadline`` 이 그 자리를 고정한다).
    """

    def test_registry_is_the_declared_five(self):
        assert set(PERIOD_EXTRACTORS) == {
            "seis", "lawmaking", "forest_service", "kofpi", "socialenterprise",
        }

    def test_list_only_extractors_are_still_two(self):
        """목록 ``raw_data`` 만으로 기간을 만드는 소스는 둘뿐이다."""
        list_only = {
            name for name, keys in EVIDENCE_KEYS.items()
            if "detail_text" not in keys
        }
        assert list_only == {"seis", "lawmaking"}

    def test_every_extractor_declares_its_evidence(self):
        """근거 키가 선언돼 있어야 재수집 때 근거를 교체할 수 있다."""
        assert set(EVIDENCE_KEYS) == set(PERIOD_EXTRACTORS)
        assert all(keys for keys in EVIDENCE_KEYS.values())

    def test_kofpi_title_pattern_is_still_retired(self):
        """제목 괄호 ``(~9.30)`` 패턴은 폐기 상태 그대로다 (9차 게이트 HIGH).

        kofpi 는 추출기가 생겼지만 **상세 본문 근거만** 읽는다. 목록 raw_data
        만 있는 행은 여전히 기간이 없다.
        """
        from alert.crawlers.period_extractors import kofpi_period

        assert PERIOD_EXTRACTORS["kofpi"] is kofpi_period
        assert kofpi_period({
            "title": "2026년 사업 공모(~9.30)", "date": "2026-09-07",
        }) == (None, None)

    def test_production_registry_is_not_empty(self):
        assert len(PRODUCTION_SOURCES) > 10

    @pytest.mark.parametrize("source", PRODUCTION_SOURCES)
    def test_every_production_source_loses_a_planted_period(self, source):
        """**모든 소스**에서 심은 기간은 저장 직전 사라진다."""
        item = _finalize_periods(source, planted(source))
        assert (item.period_start, item.period_end) == (None, None)


class TestChokePointBeatsTheCrawlers:
    """크롤러가 무엇을 반환하든 관문이 이긴다."""

    @pytest.mark.parametrize("cls", CRAWLER_CLASSES, ids=lambda c: c.__name__)
    def test_safe_fetch_output_is_reset(self, cls):
        crawler = make(cls)
        forged = planted(crawler.source_name)
        with patch.object(cls, "fetch", return_value=[forged]):
            results = crawler.safe_fetch()
        assert len(results) == 1
        item = _finalize_periods(results[0].source, results[0])
        assert (item.period_start, item.period_end) == (None, None)

    def test_object_that_never_saw_safe_fetch_is_reset(self):
        """``safe_fetch`` 를 아예 건너뛴 직접 생성 객체도 관문을 지난다."""
        item = _finalize_periods("forest_press", planted("forest_press"))
        assert (item.period_start, item.period_end) == (None, None)

    def test_undeclared_subclass_override_cannot_smuggle(self):
        """미선언 소스의 하위 클래스가 관문을 override 해도 소용없다."""

        class SmugglerPress(ForestPressCrawler):
            def fetch(self):
                return [planted("forest_press")]

            def safe_fetch(self):
                return self.fetch()         # 베이스 관문을 통째로 우회

        crawler = make(SmugglerPress)
        results = crawler.safe_fetch()
        assert results[0].period_end == PLANTED        # 크롤러 단계에서는 남는다
        item = _finalize_periods(results[0].source, results[0])
        assert (item.period_start, item.period_end) == (None, None)

    def test_declared_subclass_override_cannot_smuggle(self):
        """허용목록 소스도 근거(raw_data) 없이는 기간을 못 만든다."""

        class SmugglerSeis(SeisCrawler):
            def safe_fetch(self):
                return [planted("seis", {"date": "2026.09.01 ~ 2026.09.30"})]

        crawler = make(SmugglerSeis)
        results = crawler.safe_fetch()
        item = _finalize_periods(results[0].source, results[0])
        # 무라벨 범위는 접수 의미가 입증되지 않았다 -> 기간 없음
        assert (item.period_start, item.period_end) == (None, None)

    def test_declared_source_is_refilled_from_raw_evidence(self):
        """근거가 있으면 관문이 **다시 채운다** - 심은 값은 버린다."""
        item = _finalize_periods(
            "seis", planted("seis", {"date": "접수기간 2026.09.01 ~ 2026.09.30"})
        )
        assert (item.period_start, item.period_end) == ("2026-09-01", "2026-09-30")

    def test_broken_raw_data_yields_no_period(self):
        item = planted("seis")
        item.raw_data = "{not json"
        assert (_finalize_periods("seis", item).period_end) is None

    def test_unknown_source_name_yields_no_period(self):
        item = _finalize_periods(
            "manual", planted("manual", {"date": "접수기간 2026.09.01 ~ 2026.09.30"})
        )
        assert (item.period_start, item.period_end) == (None, None)

    def test_extractor_exception_is_fail_closed(self):
        boom = {"seis": lambda raw: 1 / 0}
        with patch.dict("alert.main.PERIOD_EXTRACTORS", boom, clear=True):
            item = _finalize_periods(
                "seis", planted("seis", {"date": "접수기간 2026.09.01 ~ 2026.09.30"})
            )
        assert (item.period_start, item.period_end) == (None, None)


class TestTheGateSitsBeforeEveryWrite:
    """관문이 DB 쓰기보다 **앞**에 있는지 구조로 고정한다."""

    @staticmethod
    def pipeline_source():
        tree = ast.parse((REPO / "alert" / "main.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "run_pipeline":
                return ast.get_source_segment(
                    (REPO / "alert" / "main.py").read_text(encoding="utf-8"), node
                )
        raise AssertionError("run_pipeline 을 찾지 못했다")

    def test_gate_is_called_once_before_the_db_touches(self):
        source = self.pipeline_source()
        assert source.count("_finalize_periods(") == 1, "관문 호출은 한 곳이다"
        gate = source.index("_finalize_periods(")
        for call in ("db.exists(", "db.overwrite_periods(",
                     "db.insert_announcement("):
            assert gate < source.index(call), f"{call} 이 관문보다 앞에 있다"

    def test_only_known_statements_write_period_columns(self):
        """기간 컬럼을 쓰는 SQL 은 **네 자리**뿐이다.

        ``mark_legacy_rows``(옛 식별자 행 비우기),
        ``revalidate_periods``(기존 행 근거 재검증),
        ``clear_periods_except``(추출기 없는 소스 정규화),
        ``overwrite_periods``(관문 값 기록). 다른 자리가 생기면 이 테스트가
        먼저 깨진다.
        """
        db_source = (REPO / "alert" / "db.py").read_text(encoding="utf-8")
        writes = [
            line.strip() for line in db_source.splitlines()
            if "period_start = " in line
        ]
        assert writes == [
            '"UPDATE announcements SET legacy = 1, period_start = NULL,"',
            '"UPDATE announcements SET period_start = ?, period_end = ?,"',
            '"UPDATE announcements SET period_start = NULL,"',
            '"UPDATE announcements SET period_start = ?, period_end = ?,"',
        ]

    def test_revalidation_runs_before_notification(self):
        """추출기 소스의 기존 행 재검증도 알림보다 앞에서 돈다."""
        source = self.pipeline_source()
        assert source.count("revalidate_periods(") == 1
        assert source.index("revalidate_periods(") < source.index(
            "db.get_unnotified("
        )

    def test_period_derivation_has_a_single_implementation(self):
        """관문과 재검증이 같은 함수를 쓴다."""
        main_source = (REPO / "alert" / "main.py").read_text(encoding="utf-8")
        assert main_source.count("def _periods_from_raw(") == 1
        # 추출기를 직접 부르는 곳은 그 함수 하나뿐이다
        assert main_source.count("extractor(raw)") == 1

    def test_normalisation_runs_before_notification(self):
        """정규화는 알림 조회보다 **앞**에서 한 번 돈다."""
        source = self.pipeline_source()
        assert source.count("clear_periods_except(") == 1
        assert source.index("clear_periods_except(") < source.index(
            "db.get_unnotified("
        )

    def test_legacy_rows_are_marked_before_anything_else(self):
        """레거시 표시는 정규화·재검증보다 먼저 한 번 돈다."""
        source = self.pipeline_source()
        assert source.count("mark_legacy_rows(") == 1
        assert source.index("mark_legacy_rows(") < source.index(
            "clear_periods_except("
        )

    def test_keyword_analysis_preserves_the_gate(self):
        """신규 저장 경로는 관문이 정한 값을 **복사**해 간다."""
        db = MagicMock()
        db.get_keywords.return_value = []
        analyzer = KeywordAnalyzer(db=db)

        gated = _finalize_periods("forest_press", planted("forest_press"))
        analyzed = analyzer.analyze(gated)
        assert (analyzed.period_start, analyzed.period_end) == (None, None)

        kept = _finalize_periods(
            "seis", planted("seis", {"date": "접수기간 2026.09.01 ~ 2026.09.30"})
        )
        analyzed = analyzer.analyze(kept)
        assert (analyzed.period_start, analyzed.period_end) == (
            "2026-09-01", "2026-09-30"
        )


class TestLabelInferenceIsGone:
    """``classify_date``·``resolve_period`` 기간 경로는 **삭제**했다."""

    @staticmethod
    def identifiers_used(path):
        """이 모듈이 **코드로** 쓰는 이름들 (주석·독스트링은 제외)."""
        names = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.FunctionDef):
                names.add(node.name)
            elif isinstance(node, ast.alias):
                names.add(node.name.split(".")[-1])
                if node.asname:
                    names.add(node.asname)
        return names

    @pytest.mark.parametrize("dead", ["classify_date", "resolve_period",
                                      "PERIOD_EXTRACTOR",
                                      "declares_period_extractor",
                                      "_enforce_period_whitelist"])
    def test_dead_period_paths_are_gone(self, dead):
        offenders = [
            path.name
            for path in (REPO / "alert").rglob("*.py")
            if dead in self.identifiers_used(path)
        ]
        assert offenders == []

    def test_base_crawler_has_no_period_machinery(self):
        for attribute in ("PERIOD_EXTRACTOR", "resolve_period",
                          "declares_period_extractor",
                          "_enforce_period_whitelist"):
            assert not hasattr(BaseCrawler, attribute), attribute


class TestSeisExtractor:
    """seis - 값이 ``접수기간 …`` 으로 시작하는 **단일 범위**만 기간이다."""

    @pytest.mark.parametrize("value,expected", [
        # 정상 1: 라벨 + 단일 범위
        ("접수기간 2026.09.01 ~ 2026.09.30", ("2026-09-01", "2026-09-30")),
        # 정상 2: 콜론 라벨 + 종료일이 월.일 (연도는 시작일에서)
        ("접수 기간: 2026.09.01 ~ 09.30", ("2026-09-01", "2026-09-30")),
        # Codex 재현 ①: 라벨 없는 범위
        ("2026.09.01 ~ 2026.09.30", (None, None)),
        # 다른 라벨
        ("교육기간 2026.10.01 ~ 2026.10.31", (None, None)),
        ("2026년 교육기간 2026.10.01 ~ 2026.10.31", (None, None)),
        ("접수마감 2026.09.30", (None, None)),
        ("행사일정 2026.10.15", (None, None)),
        # 라벨이 선두가 아니면 라벨이 아니다
        ("공고 접수기간 2026.09.01 ~ 2026.09.30", (None, None)),
        # 범위가 아님
        ("접수기간 2026.09.30", (None, None)),
        # Codex 재현: 범위 둘 이상 (심사기간 결합)
        ("접수기간 2026.09.01 ~ 2026.09.30 / 심사기간 2026.10.01 ~ 2026.10.31",
         (None, None)),
        # 달력에 없는 날짜
        ("접수기간 2027.02.30 ~ 2027.03.01", (None, None)),
        ("접수기간 2026.09.01 ~ 2027.02.30", (None, None)),
        # 뒤집힌 범위
        ("접수기간 2026.09.30 ~ 2026.09.01", (None, None)),
        ("", (None, None)),
        ("접수기간", (None, None)),
        ("접수기간 별도 공지", (None, None)),
    ])
    def test_seis_period(self, value, expected):
        assert seis_period({"date": value}) == expected

    def test_structural_label_is_ignored(self):
        """파서가 붙인 구조 라벨(``date_label``)은 근거가 아니다."""
        assert seis_period(
            {"date": "2026.09.01 ~ 2026.09.30", "date_label": "접수기간"}
        ) == (None, None)

    def test_only_the_date_field_is_read(self):
        assert seis_period({"title": "접수기간 2026.09.01 ~ 2026.09.30"}) == (
            None, None
        )

    @pytest.mark.parametrize("value", [
        # 11차 게이트: 전체 일치 **전에 아무 것도 지우지 않는다**.
        # 괄호 안의 말도 필드가 무엇을 말하는지의 일부다.
        "(교육기간) 2026.10.01 ~ 2026.10.31",
        "(접수기간 미정 / 심사기간) 2026.10.01 ~ 2026.10.31",
        # 요일 주석도 예외가 아니다 - 커버리지 손실을 감수한다
        # (실 카드·실 셀에는 요일 표기가 없다, 2026-09-13 실측).
        "접수기간 2026.09.01(화) ~ 2026.09.30(수)",
    ])
    def test_nothing_is_stripped_before_the_whole_match(self, value):
        assert seis_period({"date": value}) == (None, None)
        assert seis_period(
            {"date": value, "date_field": SEIS_CARD_DATE_FIELD}
        ) == (None, None)
        assert lawmaking_period({"period": value}) == (None, None)


class TestLawmakingExtractor:
    """lawmaking - 의견제출 기간 셀에 범위가 **하나**일 때 그 종료일."""

    @pytest.mark.parametrize("value,expected", [
        # 정상: 실제 목록 표기 - 셀 전체가 의견제출 기간 필드이므로
        # 시작일도 같은 근거로 적혀 있다
        ("2026. 9. 7. ~2026. 10. 19.", ("2026-09-07", "2026-10-19")),
        ("2026.09.01 ~ 2026.09.30", ("2026-09-01", "2026-09-30")),
        # Codex 재현: 심사기간이 붙어 범위가 둘
        ("2026.09.01 ~ 2026.09.30 / 심사기간 2026.10.01 ~ 2026.10.31",
         (None, None)),
        # 단일 날짜는 범위가 아니다
        ("2026.09.30", (None, None)),
        ("", (None, None)),
        ("별도 공지", (None, None)),
        # 달력에 없는 날짜
        ("2026.09.01 ~ 2026.02.30", (None, None)),
    ])
    def test_lawmaking_period(self, value, expected):
        assert lawmaking_period({"period": value}) == expected

    def test_only_the_period_field_is_read(self):
        assert lawmaking_period({"date": "2026.09.01 ~ 2026.09.30"}) == (None, None)


class TestKofpiNeverMakesADeadline:
    """kofpi - 제목 괄호 표기는 더 이상 마감이 아니다 (Codex 재현 ③)."""

    @pytest.fixture
    def crawler(self):
        return make(KofpiCrawler)

    @pytest.mark.parametrize("title", [
        "2026년 사업 공모(~9.30)",
        "2025년 사업 결과 안내(~9.30)",
        "공모 안내(~2025.9.30)",
        "신청 안내(~2027.2.30)",
    ])
    def test_title_deadline_is_not_a_period(self, crawler, title):
        announcement = crawler._to_announcement(
            {"title": title, "link": "/view?seq=1", "seq": "1",
             "date": "2026-09-07"},
            "https://www.kofpi.or.kr",
        )
        assert (announcement.period_start, announcement.period_end) == (None, None)
        item = _finalize_periods(announcement.source, announcement)
        assert (item.period_start, item.period_end) == (None, None)
        assert json.loads(item.raw_data)["posted"] == "2026-09-07"


class TestSeisTitleLabelReentry:
    """Codex 재현 ②: 제목 ``신청기한 변경 안내`` 가 마감이 되던 자리."""

    def test_title_label_never_becomes_a_deadline(self):
        crawler = make(SeisCrawler)
        html = (
            '<ul class="board_list"><li><div>'
            '<a href="/view.do?nttId=1">신청기한 변경 안내</a>2026.09.11'
            "</div></li></ul>"
        )
        items = crawler._parse_list_board(BeautifulSoup(html, "html.parser"))
        assert items, "fixture 에서 항목을 파싱하지 못했다"
        built = [
            _finalize_periods("seis", crawler._to_announcement(item, "https://www.seis.or.kr"))
            for item in items
        ]
        for announcement in built:
            assert (announcement.period_start, announcement.period_end) == (
                None, None
            ), announcement.title
        assert json.loads(built[0].raw_data)["posted"] == "2026-09-11"


class TestRealFixtures:
    """실 픽스처 - 커버리지가 어디서 사라지고 어디서 남는지 못박는다."""

    def test_seis_cards_carry_no_text_label_at_all(self):
        """실 카드에는 ``접수기간`` 텍스트가 **한 건도 없다** (2026-09-13 실측).

        그래서 텍스트 근거만으로는 이 22건을 영원히 읽을 수 없다 - 구조
        근거(``p.date`` 자리)가 필요한 이유다.
        """
        html = (FIXTURES / "seis_main_cards.html").read_text(encoding="utf-8")
        assert "접수" not in html and "기간" not in html

        crawler = make(SeisCrawler)
        items = crawler._parse_main_cards(BeautifulSoup(html, "html.parser"))
        assert items and all(item["date_label"] == "" for item in items)

    def test_seis_cards_are_read_through_the_structural_field(self):
        """``p.date`` 자리에서 읽은 22건은 기간이 된다 (커버리지 복구)."""
        crawler = make(SeisCrawler)
        soup = BeautifulSoup(
            (FIXTURES / "seis_main_cards.html").read_text(encoding="utf-8"),
            "html.parser",
        )
        items = crawler._parse_main_cards(soup)
        assert len(items) == 22
        assert all(
            item["date_field"] == SEIS_CARD_DATE_FIELD for item in items
        )

        built = [
            _finalize_periods("seis", crawler._to_announcement(item, "https://www.seis.or.kr"))
            for item in items
        ]
        assert all(a.period_start and a.period_end for a in built)
        assert (built[0].period_start, built[0].period_end) == (
            "2026-07-30", "2026-09-15"
        )

    def test_notice_cards_have_no_date_field(self):
        """공지사항 카드는 빈 ``p.date-temp`` 를 쓴다 - 기간이 없다.

        사이트가 **클래스로 분기**한다는 실측 근거(12건). 이 카드들은
        ``p.date`` 셀렉터에 걸리지 않으므로 출처 표시도 붙지 않는다.
        """
        html = (FIXTURES / "seis_main_cards.html").read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")
        notices = [
            card for card in soup.select("li.swiper-slide")
            if card.get("data-type") == "공지사항"
        ]
        assert len(notices) == 12
        assert all(card.select_one("p.date") is None for card in notices)
        assert all(
            card.select_one("p.date-temp").get_text(strip=True) == ""
            for card in notices
        )

    def test_dday_badge_counts_down_to_the_period_end(self):
        """같은 카드의 D-day 가 ``p.date`` 종료일까지의 남은 날짜다.

        사이트 **자신이** 그 종료일을 마감으로 세고 있다는 증거 - 구조
        근거를 인정한 이유다. 픽스처 수집 기준일 2026-09-13, 22/22 일치
        (라이브 페이지에서도 같은 날 22/22 일치를 실측했다).
        """
        captured = date(2026, 9, 13)
        crawler = make(SeisCrawler)
        soup = BeautifulSoup(
            (FIXTURES / "seis_main_cards.html").read_text(encoding="utf-8"),
            "html.parser",
        )
        checked = 0
        for item in crawler._parse_main_cards(soup):
            _start, end = seis_period(item)
            days = int(re.sub(r"[^0-9]", "", item["dday"] or "") or -1)
            assert end and days >= 0, item
            assert date.fromisoformat(end) == captured + timedelta(days=days), item
            checked += 1
        assert checked == 22

    def test_a_bare_range_without_the_structural_field_is_refused(self):
        """출처 표시가 없으면 같은 문자열도 기간이 아니다."""
        assert seis_period({"date": "2026.07.30 ~ 2026.09.15"}) == (None, None)
        assert seis_period(
            {"date": "2026.07.30 ~ 2026.09.15", "date_field": "p.date"}
        ) == (None, None)

    LIVE_CARD = (
        '<li class="swiper-slide" data-type="인·지정">\n<div class="link">\n'
        '<div class="txt-area">\n<span class="badge cate">인·지정 </span>\n'
        '<span class="sub">서울특별시 </span>\n'
        '<p class="tit"><a href="subPage.do?menuId=30100&amp;tabId=certPageView'
        '&amp;dsgnPbofrSn=8313" title="공고 게시물 자세히보기 링크">'
        '서울특별시 2026년도 2차 지정공모 </a></p>\n'
        '<ul class="info" style="width:310px;">\n<li>서울특별시 </li>\n'
        '<li style="width:100px;">D-2 </li>\n</ul>\n</div>\n'
        '<p class="date">\n\t\t\t\t\t\t\t\t\t\t\t2026.07.30 ~ 2026.09.15</p>\n'
        '</div>\n</li>'
    )

    def test_verbatim_live_card_fragment(self):
        """라이브 원문 조각(2026-09-13 수집) 그대로 -> 07-30 / 09-15."""
        crawler = make(SeisCrawler)
        soup = BeautifulSoup("<ul>" + self.LIVE_CARD + "</ul>", "html.parser")
        items = crawler._parse_main_cards(soup)
        assert len(items) == 1
        assert items[0]["date_field"] == SEIS_CARD_DATE_FIELD
        assert items[0]["dday"] == "D-2"
        announcement = _finalize_periods(
            "seis", crawler._to_announcement(items[0], "https://www.seis.or.kr")
        )
        assert (announcement.period_start, announcement.period_end) == (
            "2026-07-30", "2026-09-15"
        )

    def test_lawmaking_list_keeps_its_periods(self):
        """lawmaking 은 셀이 진짜 기간 필드다 - 시작·종료가 함께 산다."""
        crawler = make(LawmakingCrawler)
        soup = BeautifulSoup(
            (FIXTURES / "lawmaking_list.html").read_text(encoding="utf-8"),
            "html.parser",
        )
        items = crawler.parse_list(soup)
        assert items
        built = [
            _finalize_periods("lawmaking", crawler._to_announcement(item, "https://opinion.lawmaking.go.kr"))
            for item in items
        ]
        assert all(a.period_end is not None for a in built if a)
        assert all(a.period_start is not None for a in built if a)
        assert (built[0].period_start, built[0].period_end) == (
            "2026-09-07", "2026-10-19"
        )


class TestGate10Reproductions:
    """10차 게이트 재현 - 구조 근거의 범위와 전체 일치."""

    # HIGH ①: swiper 카드가 아닌 임의 부모의 p.tit/p.date
    ARBITRARY_PARENT = (
        '<div><p class="tit"><a href="/view.do?nttId=1">교육 안내</a></p>'
        '<p class="date">2026.10.01 ~ 2026.10.31</p></div>'
    )

    def test_arbitrary_parent_is_not_a_card(self):
        """임의 부모는 카드가 아니다 - 항목 자체가 안 나온다.

        예전에는 ``p.tit`` 전부를 훑고 부모를 카드로 삼아, swiper 카드가
        아닌 ``<div>`` 의 ``p.date`` 도 카드 출처 표시를 받았다.
        """
        crawler = make(SeisCrawler)
        soup = BeautifulSoup(self.ARBITRARY_PARENT, "html.parser")
        items = crawler._parse_main_cards(soup)
        assert items == []

    def test_arbitrary_parent_yields_no_period_and_no_posted(self):
        """그 자리에서 억지로 항목을 만들어도 기간도 게시일도 없다."""
        crawler = make(SeisCrawler)
        soup = BeautifulSoup(
            "<ul>" + self.ARBITRARY_PARENT + "</ul>", "html.parser"
        )
        # 카드 경로에서는 하나도 나오지 않는다
        assert crawler._parse_main_cards(soup) == []
        # 출처 표시가 없으므로 같은 텍스트로도 기간이 나오지 않는다
        assert seis_period({"date": "2026.10.01 ~ 2026.10.31"}) == (None, None)

    @pytest.mark.parametrize("value", [
        "교육기간 2026.10.01 ~ 2026.10.31",
        "접수기간 미정 / 교육기간 2026.10.01 ~ 2026.10.31",
        "행사일정 2026.10.15 ~ 2026.10.31",
        "2026.09.01부터 2026.09.30까지",
        "2026.09.01 ~ 2026.09.300",
        "2026.09.01 ~ 2026.09.30 / 심사기간 2026.10.01 ~ 2026.10.31",
    ])
    def test_structural_field_still_requires_a_whole_range(self, value):
        """구조 근거가 있는 카드라도 필드 **전체**가 범위여야 한다."""
        assert seis_period(
            {"date": value, "date_field": SEIS_CARD_DATE_FIELD}
        ) == (None, None)

    def test_real_card_with_a_training_period_is_refused(self):
        """실 카드 모양 + ``교육기간`` -> 기간 NULL (10차 HIGH 재현)."""
        crawler = make(SeisCrawler)
        card = (
            '<ul><li class="swiper-slide" data-type="사업공고">'
            '<div class="link"><div class="txt-area">'
            '<span class="badge cate">사업공고</span>'
            '<span class="sub">서울센터</span>'
            '<p class="tit"><a href="subPage.do?fncPbofrSn=1">교육 안내</a></p>'
            '<ul class="info"><li>교육</li></ul></div>'
            '<p class="date">교육기간 2026.10.01 ~ 2026.10.31</p>'
            "</div></li></ul>"
        )
        items = crawler._parse_main_cards(BeautifulSoup(card, "html.parser"))
        assert len(items) == 1
        assert items[0]["date_field"] == SEIS_CARD_DATE_FIELD
        announcement = _finalize_periods(
            "seis", crawler._to_announcement(items[0], "https://www.seis.or.kr")
        )
        assert (announcement.period_start, announcement.period_end) == (None, None)
        assert json.loads(announcement.raw_data)["posted"] == "2026-10-01"

    def test_notice_card_with_a_date_gets_no_provenance(self):
        """``data-type="공지사항"`` 카드는 출처 표시를 받지 않는다."""
        crawler = make(SeisCrawler)
        card = (
            '<ul><li class="swiper-slide" data-type="공지사항">'
            '<div class="link"><div class="txt-area">'
            '<span class="badge cate">공지사항</span>'
            '<p class="tit"><a href="subPage.do?fncPbofrSn=2">선정결과</a></p>'
            '</div><p class="date">2026.10.01 ~ 2026.10.31</p>'
            "</div></li></ul>"
        )
        items = crawler._parse_main_cards(BeautifulSoup(card, "html.parser"))
        assert len(items) == 1
        assert items[0]["date_field"] == ""
        announcement = _finalize_periods(
            "seis", crawler._to_announcement(items[0], "https://www.seis.or.kr")
        )
        assert (announcement.period_start, announcement.period_end) == (None, None)

    def test_nested_date_is_not_the_card_field(self):
        """카드 안이라도 **직속이 아닌** ``p.date`` 는 출처가 아니다."""
        crawler = make(SeisCrawler)
        card = (
            '<ul><li class="swiper-slide" data-type="사업공고">'
            '<div class="link"><div class="txt-area">'
            '<p class="tit"><a href="subPage.do?fncPbofrSn=3">공고</a></p>'
            '<div class="deep"><p class="date">2026.10.01 ~ 2026.10.31</p></div>'
            "</div></div></li></ul>"
        )
        items = crawler._parse_main_cards(BeautifulSoup(card, "html.parser"))
        assert len(items) == 1
        assert items[0]["date_field"] == ""
        assert items[0]["date"] == ""

    @pytest.mark.parametrize("value", [
        # 의견접수 셀에 심사기간이 붙은 표기 (10차 HIGH 재현)
        "2026.09.01부터 2026.09.30까지 / 심사기간 2026.10.01 ~ 2026.10.31",
        "2026.09.01 ~ 2026.09.30 / 심사기간 2026.10.01 ~ 2026.10.31",
        "의견제출 2026.09.01 ~ 2026.09.30",
        "2026.09.01 ~ 2026.09.300",
    ])
    def test_lawmaking_requires_a_whole_range(self, value):
        assert lawmaking_period({"period": value}) == (None, None)


class TestLegacyPollutionIsNormalised:
    """10차 MEDIUM - 재수집되지 않은 기존 오염은 알림까지 갔다."""

    @pytest.fixture
    def db(self, tmp_path):
        yield Database(db_path=tmp_path / "announcements.db")

    def seed(self, database, source, end, notified=0):
        database.insert_announcement(
            AnalyzedAnnouncement(
                source=source,
                source_id="1",
                title="공고",
                url="https://example.test/1",
                period_start="2099-01-01",
                period_end=end,
                relevance_score=0.9,
                raw_data="{}",
                fetched_at=datetime.now().isoformat(),
            )
        )

    def test_stale_fowi_period_is_cleared_and_never_notified(self, db):
        self.seed(db, "fowi", PLANTED)
        before = db.get_unnotified()
        assert [a.period_end for a in before] == [PLANTED]

        assert db.clear_periods_except(PERIOD_EXTRACTORS) == 1
        after = db.get_unnotified()
        assert len(after) == 1
        assert (after[0].period_start, after[0].period_end) in (
            (None, None), ("", ""),
        )

    def test_normalisation_is_idempotent(self, db):
        self.seed(db, "forest_press", PLANTED)
        assert db.clear_periods_except(PERIOD_EXTRACTORS) == 1
        assert db.clear_periods_except(PERIOD_EXTRACTORS) == 0

    @pytest.mark.parametrize("source", [
        "fowi", "forest_service", "forest_press", "kofpi", "coop",
        "socialenterprise", "bizinfo", "g2b", "rda", "semas", "manual",
    ])
    def test_every_source_outside_the_whitelist_is_cleared(self, db, source):
        """12차 게이트: 정규화 범위는 **허용목록의 여집합 전체**다.

        예전에는 대상이 협의회 소스 몇 개였고, 그 밖(bizinfo 등)의 기존
        오염은 알림까지 그대로 갔다.
        """
        self.seed(db, source, PLANTED)
        expected = 0 if source in PERIOD_EXTRACTORS else 1
        assert db.clear_periods_except(PERIOD_EXTRACTORS) == expected

    def test_extractor_sources_are_never_touched(self, db):
        """추출기가 있는 소스 행은 정규화 대상이 아니다."""
        self.seed(db, "seis", "2026-09-30")
        assert db.clear_periods_except(PERIOD_EXTRACTORS) == 0
        assert db.get_unnotified()[0].period_end == "2026-09-30"

    def test_empty_keep_list_clears_everything(self, db):
        """허용목록이 비면(추출기 로드 실패) 전부 비운다 - fail-closed."""
        self.seed(db, "seis", PLANTED)
        assert db.clear_periods_except([]) == 1
        assert db.clear_periods_except(["", None]) == 0


class TestGate11Reproductions:
    """11차 게이트 재현 - ID 충돌과 기존 행 근거 재검증."""

    @pytest.fixture
    def db(self, tmp_path):
        yield Database(db_path=tmp_path / "announcements.db")

    def card(self, kind_param, number, period):
        return (
            '<li class="swiper-slide" data-type="사업공고">'
            '<span class="sub">서울센터</span>'
            f'<p class="tit"><a href="subPage.do?{kind_param}={number}">'
            "지원사업 공고</a></p>"
            '<ul class="info"><li>교육</li></ul>'
            f'<p class="date">{period}</p></li>'
        )

    def test_same_number_in_two_kinds_stays_two_rows(self, db):
        """``fncPbofrSn=42`` 와 ``dsgnPbofrSn=42`` 는 **두 행**이다.

        예전에는 둘 다 ``source_id="42"`` 여서 하나만 저장되고, 나중에
        수집된 쪽의 기간이 먼저 저장된 다른 공고를 덮어썼다.
        """
        crawler = make(SeisCrawler)
        soup = BeautifulSoup(
            "<ul>"
            + self.card("fncPbofrSn", 42, "2026.09.01 ~ 2026.09.30")
            + self.card("dsgnPbofrSn", 42, "2026.11.01 ~ 2026.11.30")
            + "</ul>",
            "html.parser",
        )
        items = crawler._dedupe_items(crawler._parse_main_cards(soup))
        assert len(items) == 2

        saved = []
        for item in items:
            announcement = _finalize_periods(
                "seis", crawler._to_announcement(item, "https://www.seis.or.kr")
            )
            assert db.exists(announcement) is False
            db.insert_announcement(
                AnalyzedAnnouncement(
                    **announcement.__dict__, relevance_score=0.9
                )
            )
            saved.append(announcement)

        # 저장 키는 **행 식별자**다 - 두 공고가 서로 다른 키를 받는다
        assert len({a.source_id for a in saved}) == 2
        rows = db._conn.execute(
            "SELECT source_id, url, period_end FROM announcements"
        ).fetchall()
        assert len(rows) == 2
        assert len({r["source_id"] for r in rows}) == 2
        by_url = {r["url"]: r["period_end"] for r in rows}
        assert by_url == {
            "https://www.seis.or.kr/subPage.do?fncPbofrSn=42": "2026-09-30",
            "https://www.seis.or.kr/subPage.do?dsgnPbofrSn=42": "2026-11-30",
        }

    def test_legacy_row_goes_dark_instead_of_being_matched(self, db):
        """옛 숫자 ID 행은 **표시**될 뿐 새 수집과 만나지 않는다 (16차).

        URL 로 옛 행을 찾아 갱신하려던 설계는 서로 다른 공고를 합쳐서
        폐기했다. 옛 행은 어둡게 두고, 재수집은 새 행을 만든다.
        """
        url = "https://www.seis.or.kr/subPage.do?fncPbofrSn=42"
        db.insert_announcement(
            AnalyzedAnnouncement(
                source="seis", source_id="42", title="지원사업 공고", url=url,
                period_start="2099-01-01", period_end=PLANTED,
                relevance_score=0.9,
                raw_data="{}", fetched_at=datetime.now().isoformat(),
            )
        )
        assert db.mark_legacy_rows(EVIDENCE_KEYS) == 1

        fresh = RawAnnouncement(
            source="seis", source_id="fnc:42", title="지원사업 공고", url=url,
            raw_data=json.dumps({"date": "접수기간 2026.09.01 ~ 2026.09.30"}),
        )
        assert db.exists(fresh) is False         # 옛 행과 만나지 않는다
        db.insert_announcement(
            AnalyzedAnnouncement(
                **_finalize_periods("seis", fresh).__dict__, relevance_score=0.9
            )
        )
        rows = db._conn.execute(
            "SELECT source_id, legacy, period_end FROM announcements"
            " ORDER BY legacy"
        ).fetchall()
        assert [(r["legacy"], r["period_end"]) for r in rows] == [
            (0, "2026-09-30"), (1, None),
        ]
        assert rows[0]["source_id"] == "fnc:42"       # 새 행 (레거시 아님)
        assert [a.source_id for a in db.get_unnotified()] == ["fnc:42"]

    def test_stale_seis_row_is_revalidated_and_not_notified(self, db):
        """재수집되지 않은 seis 행도 **근거를 다시 본다**."""
        db.insert_announcement(
            AnalyzedAnnouncement(
                source="seis", source_id="fnc:1", title="교육 공고",
                url="https://www.seis.or.kr/subPage.do?fncPbofrSn=1",
                period_start="2099-01-01", period_end="2099-12-31",
                relevance_score=0.9,
                raw_data=json.dumps(
                    {"date": "교육기간 2099.01.01 ~ 2099.12.31",
                     "date_field": SEIS_CARD_DATE_FIELD},
                    ensure_ascii=False,
                ),
                fetched_at=datetime.now().isoformat(),
            )
        )
        assert [a.period_end for a in db.get_unnotified()] == ["2099-12-31"]

        changed = db.revalidate_periods(
            "seis", lambda raw: _periods_from_raw("seis", raw)
        )
        assert changed == 1
        assert [a.period_end for a in db.get_unnotified()] == [None]
        # 멱등
        assert db.revalidate_periods(
            "seis", lambda raw: _periods_from_raw("seis", raw)
        ) == 0

    def test_revalidation_restores_evidence_backed_periods(self, db):
        """근거가 있으면 재검증이 **값을 되살린다**."""
        db.insert_announcement(
            AnalyzedAnnouncement(
                source="lawmaking", source_id="88388", title="입법예고",
                url="https://opinion.lawmaking.go.kr/gcom/ogLmPp/88388",
                period_start=None, period_end=None, relevance_score=0.9,
                raw_data=json.dumps(
                    {"period": "2026. 9. 7. ~2026. 10. 19."}, ensure_ascii=False
                ),
                fetched_at=datetime.now().isoformat(),
            )
        )
        assert db.revalidate_periods(
            "lawmaking", lambda raw: _periods_from_raw("lawmaking", raw)
        ) == 1
        row = db._conn.execute(
            "SELECT period_start, period_end FROM announcements"
        ).fetchone()
        assert (row["period_start"], row["period_end"]) == (
            "2026-09-07", "2026-10-19"
        )

    def test_broken_raw_data_revalidates_to_null(self, db):
        db.insert_announcement(
            AnalyzedAnnouncement(
                source="seis", source_id="fnc:2", title="공고",
                url="https://www.seis.or.kr/subPage.do?fncPbofrSn=2",
                period_end=PLANTED, relevance_score=0.9,
                raw_data="{not json", fetched_at=datetime.now().isoformat(),
            )
        )
        assert db.revalidate_periods(
            "seis", lambda raw: _periods_from_raw("seis", raw)
        ) == 1
        assert db.get_unnotified()[0].period_end is None


class TestGate12Reproductions:
    """12차 게이트 재현 - 연도별 ID, 철회된 기간의 부활, 정규화 범위."""

    @pytest.fixture
    def db(self, tmp_path):
        yield Database(db_path=tmp_path / "announcements.db")

    @staticmethod
    def cert_link(year, number):
        return (
            f"/subPage.do?menuId=30100&tabId=certPageView"
            f"&statsYr={year}&epsdNo={number}"
        )

    def test_certification_years_never_collide(self, db):
        """``statsYr`` 가 다르면 같은 ``epsdNo`` 도 다른 공고다 (HIGH ①).

        예전에는 둘 다 ``epsd:4`` 여서 2027 년 공고의 기간이 2026 년 공고
        제목·URL 에 덮어써져 알림에 나갔다.
        """
        crawler = make(SeisCrawler)
        first = crawler._extract_post_id(self.cert_link(2026, 4))
        second = crawler._extract_post_id(self.cert_link(2027, 4))
        assert first != second

        for year, end in ((2026, "2026-09-30"), (2027, "2027-11-30")):
            db.insert_announcement(
                AnalyzedAnnouncement(
                    source="seis",
                    source_id=crawler._extract_post_id(self.cert_link(year, 4)),
                    title=f"{year}년도 인증 공고",
                    url=f"https://www.seis.or.kr{self.cert_link(year, 4)}",
                    period_end=end, relevance_score=0.9, raw_data="{}",
                    fetched_at=datetime.now().isoformat(),
                )
            )

        rows = db._conn.execute(
            "SELECT source_id, url, period_end FROM announcements"
        ).fetchall()
        assert len(rows) == 2
        assert len({r["source_id"] for r in rows}) == 2      # 행이 갈린다
        assert {(r["url"].split("statsYr=")[1][:4], r["period_end"])
                for r in rows} == {
            ("2026", "2026-09-30"), ("2027", "2027-11-30"),
        }

    def test_the_id_itself_must_separate_the_announcements(self, db):
        """행 키는 ``(source, source_id)`` 하나다 - **ID 가 갈라야 한다**.

        16차: URL 로 한 번 더 가르려던 방어는 다른 공고를 합치는 부작용이
        커서 폐기했다. 대신 seis ID 가 종류·연도를 담는다(위 테스트).
        같은 ID 를 준다는 것은 같은 공고라고 크롤러가 말하는 것이다.
        """
        crawler = make(SeisCrawler)
        assert crawler._extract_post_id(self.cert_link(2026, 4)) != (
            crawler._extract_post_id(self.cert_link(2027, 4))
        )
        assert crawler._extract_post_id(
            "/subPage.do?fncPbofrSn=42"
        ) != crawler._extract_post_id("/subPage.do?dsgnPbofrSn=42")

    def recrawl(self, db, date_text):
        """같은 공고를 새 근거로 재수집한 것처럼 처리한다."""
        item = RawAnnouncement(
            source="seis", source_id="fnc:1", title="지원사업 공고",
            url="https://www.seis.or.kr/subPage.do?fncPbofrSn=1",
            raw_data=json.dumps(
                {"date": date_text, "date_field": SEIS_CARD_DATE_FIELD},
                ensure_ascii=False,
            ),
        )
        gated = _finalize_periods(item.source, item)
        db.overwrite_periods(gated, EVIDENCE_KEYS.get("seis", ()))
        return gated

    def test_a_retracted_period_never_comes_back(self, db):
        """철회된 기간이 다음 실행에서 부활하지 않는다 (HIGH ② REGRESSED).

        ① 10월 범위로 수집 ② ``접수기간 미정 / 교육기간 …`` 으로 재수집하면
        NULL ③ 그 다음 ``fetch()=[]`` 실행의 재검증에서 10월 범위가
        되살아났다 - 기간만 갱신하고 **근거**(raw_data.date)를 남겨서다.
        """
        db.insert_announcement(
            AnalyzedAnnouncement(
                source="seis", source_id="fnc:1", title="지원사업 공고",
                url="https://www.seis.or.kr/subPage.do?fncPbofrSn=1",
                relevance_score=0.9, raw_data="{}",
                fetched_at=datetime.now().isoformat(),
            )
        )

        # ① 근거가 있는 수집 -> 기간이 생긴다
        self.recrawl(db, "2026.10.01 ~ 2026.10.31")
        assert db.get_unnotified()[0].period_end == "2026-10-31"

        # ② 근거가 철회된 재수집 -> NULL
        self.recrawl(db, "접수기간 미정 / 교육기간 2026.10.01 ~ 2026.10.31")
        assert db.get_unnotified()[0].period_end is None
        stored = json.loads(
            db._conn.execute("SELECT raw_data FROM announcements").fetchone()[
                "raw_data"
            ]
        )
        assert stored["date"] == "접수기간 미정 / 교육기간 2026.10.01 ~ 2026.10.31"

        # ③ 다음 실행(fetch()=[])의 재검증에서도 부활하지 않는다
        assert db.revalidate_periods(
            "seis", lambda raw: _periods_from_raw("seis", raw)
        ) == 0
        assert db.get_unnotified()[0].period_end is None

    def test_evidence_removal_is_written_too(self, db):
        """근거 필드가 아예 사라진 재수집도 그대로 반영된다."""
        db.insert_announcement(
            AnalyzedAnnouncement(
                source="seis", source_id="fnc:1", title="공고",
                url="https://www.seis.or.kr/subPage.do?fncPbofrSn=1",
                relevance_score=0.9, raw_data="{}",
                fetched_at=datetime.now().isoformat(),
            )
        )
        self.recrawl(db, "2026.10.01 ~ 2026.10.31")

        empty = RawAnnouncement(
            source="seis", source_id="fnc:1", title="공고",
            url="https://www.seis.or.kr/subPage.do?fncPbofrSn=1",
            raw_data=json.dumps({"title": "공고"}, ensure_ascii=False),
        )
        db.overwrite_periods(
            _finalize_periods("seis", empty), EVIDENCE_KEYS["seis"]
        )
        stored = json.loads(
            db._conn.execute("SELECT raw_data FROM announcements").fetchone()[
                "raw_data"
            ]
        )
        assert "date" not in stored and "date_field" not in stored
        assert db.revalidate_periods(
            "seis", lambda raw: _periods_from_raw("seis", raw)
        ) == 0
        assert db.get_unnotified()[0].period_end is None


class TestStaleRowsAreOverwritten:
    """재수집이 기존 오염을 **지운다** (9차 게이트 MEDIUM)."""

    @pytest.fixture
    def db(self, tmp_path):
        database = Database(db_path=tmp_path / "announcements.db")
        yield database

    def stored(self, database, source, _source_id="1"):
        """행은 **행 식별자**로 저장된다 - URL 로 찾는다 (13차 게이트)."""
        row = database._conn.execute(
            "SELECT period_start, period_end FROM announcements"
            " WHERE source = ? AND url = ?",
            (source, "https://example.test/1"),
        ).fetchone()
        return (row["period_start"] or None, row["period_end"] or None)

    def seed(self, database, source, start, end, raw=None):
        database.insert_announcement(
            AnalyzedAnnouncement(
                source=source,
                source_id="1",
                title="공고",
                url="https://example.test/1",
                period_start=start,
                period_end=end,
                raw_data=json.dumps(raw or {}, ensure_ascii=False),
                fetched_at=datetime.now().isoformat(),
            )
        )

    def test_council_row_is_reset_to_none(self, db):
        """협의회 소스(forest_press)의 09-01/09-30 행 -> 재수집 후 None."""
        self.seed(db, "forest_press", "2026-09-01", "2026-09-30")
        assert self.stored(db, "forest_press", "1") == (
            "2026-09-01", "2026-09-30"
        )

        recrawled = _finalize_periods(
            "forest_press",
            planted("forest_press", {"date": "2026.09.12", "date_label": "게시일"}),
        )
        assert db.overwrite_periods(recrawled) is True
        assert self.stored(db, "forest_press", "1") == (None, None)

    def test_quote_merge_no_longer_resurrects_a_period(self, db):
        """인용 병합이 기간을 되살리지 않는다 (예전엔 ``or row[...]``)."""
        self.seed(db, "forest_press", "2026-09-01", "2026-09-30")
        recrawled = _finalize_periods("forest_press", planted("forest_press"))
        db.overwrite_periods(recrawled)

        recrawled.raw_data = json.dumps(
            {"quote_deadline": "접수기간 2026.09.01 ~ 2026.09.30",
             "quote_period_start": "2026-09-01",
             "quote_period_end": "2026-09-30"},
            ensure_ascii=False,
        )
        assert db.merge_quote_fields(recrawled) is True
        assert self.stored(db, "forest_press", "1") == (None, None)

    def test_seis_row_is_replaced_by_fresh_evidence(self, db):
        self.seed(db, "seis", "2099-01-01", PLANTED)
        recrawled = _finalize_periods(
            "seis", planted("seis", {"date": "접수기간 2026.09.01 ~ 2026.09.30"})
        )
        assert db.overwrite_periods(recrawled) is True
        assert self.stored(db, "seis", "1") == ("2026-09-01", "2026-09-30")

    def test_unchanged_row_is_left_alone(self, db):
        self.seed(db, "forest_press", None, None)
        recrawled = _finalize_periods("forest_press", planted("forest_press"))
        assert db.overwrite_periods(recrawled) is False

    def test_missing_row_is_not_inserted(self, db):
        recrawled = _finalize_periods("forest_press", planted("forest_press"))
        assert db.overwrite_periods(recrawled) is False
