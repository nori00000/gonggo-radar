"""상세 페이지 인용 추출 테스트 (계약 v2.1 판정 4 - 원문 인용만, 창작 금지).

kofpi / coop / seis 실제 상세 페이지를 잘라 만든 fixture로 검증하므로
네트워크를 타지 않는다.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.base import BaseCrawler
from alert.crawlers.detail_quotes import (
    QUOTE_AMOUNT,
    QUOTE_DEADLINE,
    QUOTE_ELIGIBILITY,
    extract_quotes,
    normalize_text,
    period_from_quote,
)
from alert.models import RawAnnouncement

FIXTURES = Path(__file__).parent / "fixtures"


def load_text(name: str) -> str:
    """fixture HTML을 정규화된 본문 텍스트로 읽는다."""
    return normalize_text((FIXTURES / name).read_text(encoding="utf-8"))


class TestKofpiDetailQuotes:
    """한국임업진흥원 상세 페이지 - "ㅁ 라벨 / 값" 글머리표 형식."""

    @pytest.fixture
    def text(self):
        return load_text("kofpi_detail.html")

    def test_quotes_are_verbatim_substrings(self, text):
        """인용은 본문의 연속 부분열이어야 한다 (지어낸 문구 금지)."""
        quotes = extract_quotes(text)
        assert quotes
        for quote in quotes.values():
            assert quote in text

    def test_deadline_quote_and_period(self, text):
        """모집기간 문구에서 접수 시작/종료일을 뽑는다."""
        quotes = extract_quotes(text)
        assert quotes[QUOTE_DEADLINE] == "모집기간 2026. 9. 7.(월) ~ 9. 30.(수) 15:00까지"
        # 종료일은 연도가 생략되어 있으므로 시작 연도를 물려받는다
        assert period_from_quote(quotes[QUOTE_DEADLINE]) == ("2026-09-07", "2026-09-30")

    def test_eligibility_quote(self, text):
        quotes = extract_quotes(text)
        assert quotes[QUOTE_ELIGIBILITY].startswith("모집대상 수요과제 모집분야에 해당하는 기술을 보유한")

    def test_next_bullet_ends_the_quote(self, text):
        """다음 글머리표(ㅁ)를 넘어가지 않는다."""
        quotes = extract_quotes(text)
        assert "ㅁ" not in quotes[QUOTE_ELIGIBILITY]
        assert "모집분야" not in quotes[QUOTE_DEADLINE]


class TestCoopDetailQuotes:
    """협동조합 포털 상세 페이지 - "□ ( 라벨 ) 값" 한글문서 붙여넣기 형식."""

    @pytest.fixture
    def text(self):
        return load_text("coop_detail.html")

    def test_quotes_are_verbatim_substrings(self, text):
        quotes = extract_quotes(text)
        assert set(quotes) == {QUOTE_DEADLINE, QUOTE_ELIGIBILITY, QUOTE_AMOUNT}
        for quote in quotes.values():
            assert quote in text

    def test_eligibility_quote(self, text):
        """□ (대상) 괄호 라벨을 인식한다."""
        quotes = extract_quotes(text)
        assert quotes[QUOTE_ELIGIBILITY] == "대상) 사회적협동조합, (예비)사회적기업"

    def test_amount_quote(self, text):
        quotes = extract_quotes(text)
        assert quotes[QUOTE_AMOUNT].startswith("모집규모) 온·오프라인 합계 회차별 기업 25개소 내외")

    def test_multi_round_schedule_yields_no_period(self, text):
        """회차별 일정표는 날짜가 여러 개라 기간을 단정하지 않는다."""
        quotes = extract_quotes(text)
        assert "모집기간" in quotes[QUOTE_DEADLINE]
        assert period_from_quote(quotes[QUOTE_DEADLINE]) == (None, None)

    def test_title_word_does_not_become_eligibility(self, text):
        """제목의 "사회적기업 대상 공공조달"은 자격 인용으로 잡히지 않는다."""
        quotes = extract_quotes(text)
        assert "공공조달 및 민간위탁 1:1" not in quotes[QUOTE_ELIGIBILITY]


class TestSeisDetailQuotes:
    """SEIS 상세 페이지 - th/td 라벨이 두 번 렌더되는 표 형식."""

    @pytest.fixture
    def text(self):
        return load_text("seis_detail.html")

    def test_table_label_repeat_is_handled(self, text):
        """"신청기간 신청기간 값" 중복 라벨에서 값만 남는다."""
        quotes = extract_quotes(text)
        assert quotes[QUOTE_DEADLINE] == "신청기간 2026.09.01 ~ 2026.12.31"
        assert quotes[QUOTE_DEADLINE] in text
        assert period_from_quote(quotes[QUOTE_DEADLINE]) == ("2026-09-01", "2026-12-31")


class TestPeriodFromQuote:
    """기간 파싱은 확신할 수 있을 때만 값을 낸다."""

    @pytest.mark.parametrize("quote,expected", [
        ("접수기간 2026.09.01 ~ 2026.12.31", ("2026-09-01", "2026-12-31")),
        ("접수기간 2026-09-01 ~ 2026-10-19", ("2026-09-01", "2026-10-19")),
        ("모집기간 2026. 9. 7.(월) ~ 9. 30.(수)", ("2026-09-07", "2026-09-30")),
        ("신청기간 '26. 8. 1. ~ '26. 8. 31.", ("2026-08-01", "2026-08-31")),
        ("접수기간 2026년 9월 1일 ~ 2026년 9월 30일", ("2026-09-01", "2026-09-30")),
        ("접수기간 2026년 9월 1일 ~ 9월 30일", ("2026-09-01", "2026-09-30")),
    ])
    def test_range_forms(self, quote, expected):
        assert period_from_quote(quote) == expected

    @pytest.mark.parametrize("quote,expected", [
        ("접수기한 2026.09.30.까지", "2026-09-30"),
        # coop 실측(brd_no=14870): 한글 날짜 표기 + "까지"
        ("신청기간> · 2026년 9월 11일(금)까지 <참여신청> · 네이버 폼을 통해 신청", "2026-09-11"),
    ])
    def test_single_date_with_kkaji_is_deadline_only(self, quote, expected):
        assert period_from_quote(quote) == (None, expected)

    @pytest.mark.parametrize("quote", [
        "",
        "접수기간 별도 공지",
        "접수기간 2026.09.01",                    # 까지/마감 없는 단일 날짜
        "모집기간 제1회 2026.08.25 제2회 2026.09.08",  # 범위 기호 없는 복수 날짜
        "상담시간 1개 참여기업당 60분 상담",
        "모집규모: 50명 (선찬순 마감 예정)",          # 마감 문구만 있고 날짜 없음
    ])
    def test_uncertain_input_invents_nothing(self, quote):
        assert period_from_quote(quote) == (None, None)


class TestExtractQuotesGuards:
    """문구가 없으면 키를 만들지 않는다."""

    def test_empty_text(self):
        assert extract_quotes("") == {}

    def test_no_labels_no_keys(self):
        assert extract_quotes("본문에 아무 라벨도 없습니다.") == {}

    def test_generic_label_needs_bullet_context(self):
        """글머리표 없는 맨 "대상"/"마감"은 인용으로 인정하지 않는다."""
        text = "이 사업은 청년 대상 프로그램이며 마감 이후에도 문의 가능합니다."
        assert QUOTE_ELIGIBILITY not in extract_quotes(text)
        assert QUOTE_DEADLINE not in extract_quotes(text)

    def test_label_only_fragment_rejected(self):
        assert extract_quotes("□ 접수기간 □ 문의") == {}


class _StubCrawler(BaseCrawler):
    """enrich_with_quotes 검증용 최소 크롤러."""

    def fetch(self):  # pragma: no cover - 테스트에서 직접 호출하지 않는다
        return []


def make_stub(fetch_detail: bool) -> _StubCrawler:
    """fetch_detail 플래그만 다른 스텁 크롤러를 만든다."""
    from alert.config import SourceConfig

    config = MagicMock()
    config.crawler.timeout = 30
    config.crawler.retry_count = 1
    config.crawler.retry_delay = 0
    config.crawler.user_agent = "AgriAlert/1.0"
    config.crawler.sources = {
        "stub": SourceConfig(
            enabled=True, base_url="https://example.com", fetch_detail=fetch_detail
        )
    }
    with patch("alert.crawlers.base.get_config", return_value=config):
        return _StubCrawler(source_name="stub")


def make_announcement() -> RawAnnouncement:
    return RawAnnouncement(
        source="stub",
        source_id="1",
        title="테스트 공고",
        url="https://example.com/view/1",
        raw_data=json.dumps({"title": "테스트 공고"}, ensure_ascii=False),
    )


class TestEnrichWithQuotes:
    """상세 수집은 소스별 opt-in이며, 없는 값을 채우지 않는다."""

    def test_disabled_by_default(self):
        """fetch_detail이 꺼져 있으면 요청조차 하지 않는다."""
        crawler = make_stub(fetch_detail=False)
        assert crawler.wants_detail() is False

        announcement = make_announcement()
        with patch.object(crawler, "get") as mock_get:
            crawler.enrich_with_quotes([announcement])
        mock_get.assert_not_called()
        assert json.loads(announcement.raw_data) == {"title": "테스트 공고"}

    def test_mock_config_does_not_enable_detail(self):
        """MagicMock 설정이 우연히 truthy가 되어도 상세 수집은 꺼진 상태다."""
        config = MagicMock()
        config.crawler.timeout = 30
        config.crawler.retry_count = 1
        config.crawler.retry_delay = 0
        config.crawler.user_agent = "test-agent"
        config.crawler.sources = {"stub": MagicMock()}
        with patch("alert.crawlers.base.get_config", return_value=config):
            crawler = _StubCrawler(source_name="stub")
        assert crawler.wants_detail() is False

    def test_quotes_land_in_raw_data_and_period(self):
        """인용은 raw_data에, 확신 가능한 날짜는 기간 필드에 들어간다."""
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()

        response = MagicMock()
        response.text = (FIXTURES / "kofpi_detail.html").read_text(encoding="utf-8")
        response.apparent_encoding = "utf-8"

        with patch.object(crawler, "get", return_value=response) as mock_get:
            with patch("alert.crawlers.base.time.sleep") as mock_sleep:
                crawler.enrich_with_quotes([announcement])

        mock_sleep.assert_not_called()  # 첫 건은 대기 없음
        _, kwargs = mock_get.call_args
        assert kwargs["timeout"] == 10
        assert kwargs["headers"]["User-Agent"].startswith("Mozilla/5.0")

        payload = json.loads(announcement.raw_data)
        assert payload["title"] == "테스트 공고"           # 목록 정보 보존
        assert payload[QUOTE_DEADLINE].startswith("모집기간 2026. 9. 7.")
        assert payload[QUOTE_ELIGIBILITY].startswith("모집대상")
        assert payload["quote_period_start"] == "2026-09-07"
        assert payload["quote_period_end"] == "2026-09-30"
        assert announcement.period_start == "2026-09-07"
        assert announcement.period_end == "2026-09-30"

    def test_rate_limited_to_one_request_per_second(self):
        """두 번째 건부터 1초 대기한다."""
        crawler = make_stub(fetch_detail=True)
        items = [make_announcement(), make_announcement(), make_announcement()]

        response = MagicMock()
        response.text = "<html><body>내용 없음</body></html>"
        response.apparent_encoding = "utf-8"

        with patch.object(crawler, "get", return_value=response):
            with patch("alert.crawlers.base.time.sleep") as mock_sleep:
                crawler.enrich_with_quotes(items)

        assert mock_sleep.call_count == 2
        assert mock_sleep.call_args_list[0][0][0] == 1.0

    def test_missing_quotes_leave_keys_absent(self):
        """페이지에 문구가 없으면 키를 만들지 않고 기간도 건드리지 않는다."""
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()
        announcement.period_end = None

        response = MagicMock()
        response.text = "<html><body><div class='board_view'>본문 없음</div></body></html>"
        response.apparent_encoding = "utf-8"

        with patch.object(crawler, "get", return_value=response):
            crawler.enrich_with_quotes([announcement])

        payload = json.loads(announcement.raw_data)
        assert QUOTE_DEADLINE not in payload
        assert QUOTE_ELIGIBILITY not in payload
        assert QUOTE_AMOUNT not in payload
        assert announcement.period_end is None

    def test_failed_request_is_not_fatal(self):
        """상세 요청이 실패해도 목록 데이터는 그대로 남는다."""
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()

        with patch.object(crawler, "get", return_value=None):
            crawler.enrich_with_quotes([announcement])

        assert json.loads(announcement.raw_data) == {"title": "테스트 공고"}

    def test_unresolved_void_url_is_skipped(self):
        """#void 처럼 해소되지 않은 링크는 요청하지 않는다."""
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()
        announcement.url = "https://example.com/#void"

        with patch.object(crawler, "get") as mock_get:
            crawler.enrich_with_quotes([announcement])

        mock_get.assert_not_called()
