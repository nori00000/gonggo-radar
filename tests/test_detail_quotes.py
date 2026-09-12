"""상세 페이지 인용 추출 테스트 (계약 v2.1 판정 4 - 원문 인용만, 창작 금지).

kofpi / coop / seis 실제 상세 페이지를 잘라 만든 fixture로 검증하므로
네트워크를 타지 않는다. ``TestCodexCritique*`` 클래스는 2026-09-13 Codex
크리틱 #3·#6·#7·#8 의 재현 입력을 그대로 쓴다.
"""

import json
import threading
import time
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.base import BaseCrawler
from alert.crawlers.detail_quotes import (
    ALWAYS_OPEN,
    DETAIL_TRUNCATED,
    EARLY_CLOSE,
    MAX_DETAIL_BYTES,
    MAX_DETAIL_REQUESTS,
    QUOTE_AMOUNT,
    QUOTE_DEADLINE,
    QUOTE_ELIGIBILITY,
    extract_quotes,
    flatten,
    has_quote_keys,
    complete_html_prefix,
    is_always_open,
    is_early_close,
    normalize_text,
    period_from_quote,
    resolve_period,
)
from alert.models import RawAnnouncement

FIXTURES = Path(__file__).parent / "fixtures"

# 기준일을 고정한다 - "오늘 이후 가장 이른 마감" 규칙이 달력에 따라 흔들리면 안 된다
TODAY = date(2026, 9, 13)


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
            assert quote in flatten(text)

    def test_deadline_quote_and_period(self, text):
        """모집기간 문구에서 접수 시작/종료일을 뽑는다."""
        quotes = extract_quotes(text)
        assert quotes[QUOTE_DEADLINE] == "모집기간 2026. 9. 7.(월) ~ 9. 30.(수) 15:00까지"
        # 종료일은 연도가 생략되어 있으므로 시작 연도를 물려받는다
        assert period_from_quote(quotes[QUOTE_DEADLINE], today=TODAY) == (
            "2026-09-07", "2026-09-30"
        )

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
            assert quote in flatten(text)

    def test_eligibility_quote(self, text):
        """□ (대상) 괄호 라벨을 인식한다."""
        quotes = extract_quotes(text)
        assert quotes[QUOTE_ELIGIBILITY] == "대상) 사회적협동조합, (예비)사회적기업"

    def test_amount_quote(self, text):
        quotes = extract_quotes(text)
        assert quotes[QUOTE_AMOUNT].startswith("모집규모) 온·오프라인 합계 회차별 기업 25개소 내외")

    def test_multi_round_schedule_picks_the_live_round(self, text):
        """회차별 일정표에서는 **오늘 이후 가장 이른 회차 마감**을 쓴다.

        실측(2026-09-13 기준): 1·2회차 접수는 끝났고 3회차가
        ’26. 9. 15.(화) 18:00까지다. 중첩 표를 한 값으로 읽게 되면서
        살아있는 회차를 고를 수 있게 됐다 (최종 게이트 #4).
        """
        quotes = extract_quotes(text)
        assert "모집기간" in quotes[QUOTE_DEADLINE]
        assert period_from_quote(quotes[QUOTE_DEADLINE], today=TODAY) == (
            None, "2026-09-15"
        )

    def test_earlier_rounds_are_not_chosen(self, text):
        """지난 1·2회차 마감(8/14, 8/27)은 고르지 않는다."""
        quotes = extract_quotes(text)
        _start, end = period_from_quote(quotes[QUOTE_DEADLINE], today=TODAY)
        assert end not in {"2026-08-14", "2026-08-27"}

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
        assert quotes[QUOTE_DEADLINE] in flatten(text)
        assert period_from_quote(quotes[QUOTE_DEADLINE], today=TODAY) == (
            "2026-09-01", "2026-12-31"
        )


class TestCodexCritiqueTableBoundary:
    """크리틱 #3: 표 행 경계를 잃어 게시일이 마감으로 저장되던 결함."""

    REPRO = (
        '<div class="board_view"><table>'
        "<tr><th>접수기간</th><td>상시 (예산 소진 시 마감)</td></tr>"
        "<tr><th>작성일</th><td>2026.09.11</td></tr>"
        "</table></div>"
    )

    @pytest.fixture
    def text(self):
        return normalize_text(self.REPRO)

    def test_boundaries_separate_the_cells(self, text):
        """셀·행 경계가 값들을 갈라 놓는다 (평탄화하면 원문 순서 그대로)."""
        assert flatten(text) == "접수기간 상시 (예산 소진 시 마감) 작성일 2026.09.11"
        # 경계 표시는 원문 공백과 겹치지 않는 사용자 영역 문자여야 한다
        assert "\ue000" in text or "\ue001" in text

    def test_quote_stops_before_the_next_row(self, text):
        """인용이 다음 행(작성일/게시일)까지 넘어가지 않는다."""
        quotes = extract_quotes(text)
        assert quotes[QUOTE_DEADLINE] == "접수기간 상시 (예산 소진 시 마감)"
        assert "2026.09.11" not in quotes[QUOTE_DEADLINE]

    def test_posting_date_never_becomes_the_deadline(self, text):
        """게시일이 period_end 로 저장되지 않는다."""
        quotes = extract_quotes(text)
        assert period_from_quote(quotes[QUOTE_DEADLINE], today=TODAY) == (None, None)

    def test_always_open_is_flagged(self, text):
        """"상시 / 예산 소진 시" 는 마감 없음으로 표시된다."""
        quotes = extract_quotes(text)
        assert is_always_open(quotes[QUOTE_DEADLINE]) is True


class TestCodexCritiqueStartVsEnd:
    """크리틱 #6: 시작일을 마감으로 뒤집던 결함."""

    def test_buteo_date_is_a_start_not_a_deadline(self):
        quote = "접수기간 2026.09.01부터 상시 (예산 소진 시 마감)"
        assert period_from_quote(quote, today=TODAY) == ("2026-09-01", None)

    def test_deadline_word_anywhere_does_not_finalize_an_end(self):
        """"마감" 이 문장 아무 곳에나 있다는 이유로 종료일을 만들지 않는다."""
        assert period_from_quote("모집규모 50명 (선착순 마감 예정) 2026.09.01 접수 시작",
                                 today=TODAY)[1] is None
        assert period_from_quote("문의 후 마감 여부 확인, 게시 2026.09.11",
                                 today=TODAY) == (None, None)

    def test_deadline_label_adjacent_to_date_does_finalize(self):
        """"마감: 날짜" 는 종료일로 인정한다."""
        assert period_from_quote("접수 안내 마감: 2026.09.30", today=TODAY) == (
            None, "2026-09-30"
        )

    def test_until_suffix_finalizes_an_end(self):
        assert period_from_quote("접수기한 2026.09.30.까지", today=TODAY) == (
            None, "2026-09-30"
        )


class TestCodexCritiqueYearAndRounds:
    """크리틱 #7: 연도 넘김과 복수 회차를 잘못 확정하던 결함."""

    def test_year_rolls_over_when_end_precedes_start(self):
        """2026.12.20 ~ 1.10 의 종료는 2027-01-10 이다."""
        assert period_from_quote("접수기간 2026.12.20 ~ 1.10", today=TODAY) == (
            "2026-12-20", "2027-01-10"
        )

    def test_same_year_when_end_follows_start(self):
        assert period_from_quote("접수기간 2026.09.01 ~ 9.30", today=TODAY) == (
            "2026-09-01", "2026-09-30"
        )

    def test_multiple_rounds_pick_earliest_future_deadline(self):
        """1차가 지났으면 진행 중인 2차를 쓴다."""
        quote = "모집기간 1차 2026.08.01 ~ 2026.08.31 2차 2026.09.01 ~ 2026.09.30"
        assert period_from_quote(quote, today=TODAY) == ("2026-09-01", "2026-09-30")

    def test_multiple_rounds_all_past_pick_last(self):
        """모두 지났으면 마지막 회차를 쓴다."""
        quote = "모집기간 1차 2026.01.01 ~ 2026.01.31 2차 2026.02.01 ~ 2026.02.28"
        assert period_from_quote(quote, today=TODAY) == ("2026-02-01", "2026-02-28")

    def test_three_rounds_skip_past_ones(self):
        quote = (
            "접수기간 1차 2026.07.01 ~ 2026.07.31 2차 2026.08.01 ~ 2026.08.31 "
            "3차 2026.10.01 ~ 2026.10.31"
        )
        assert period_from_quote(quote, today=TODAY) == ("2026-10-01", "2026-10-31")


class TestPeriodFromQuote:
    """기간 파싱은 확신할 수 있을 때만 값을 낸다."""

    @pytest.mark.parametrize("quote,expected", [
        ("접수기간 2026.09.01 ~ 2026.12.31", ("2026-09-01", "2026-12-31")),
        ("접수기간 2026-09-01 ~ 2026-10-19", ("2026-09-01", "2026-10-19")),
        ("모집기간 2026. 9. 7.(월) ~ 9. 30.(수)", ("2026-09-07", "2026-09-30")),
        ("신청기간 '26. 9. 1. ~ '26. 9. 30.", ("2026-09-01", "2026-09-30")),
        ("접수기간 2026년 9월 1일 ~ 2026년 9월 30일", ("2026-09-01", "2026-09-30")),
        ("접수기간 2026년 9월 1일 ~ 9월 30일", ("2026-09-01", "2026-09-30")),
    ])
    def test_range_forms(self, quote, expected):
        assert period_from_quote(quote, today=TODAY) == expected

    @pytest.mark.parametrize("quote,expected", [
        ("접수기한 2026.09.30.까지", "2026-09-30"),
        # coop 실측(brd_no=14870): 한글 날짜 표기 + "까지"
        ("신청기간> · 2026년 9월 11일(금)까지 <참여신청>", "2026-09-11"),
    ])
    def test_single_date_with_kkaji_is_deadline_only(self, quote, expected):
        assert period_from_quote(quote, today=TODAY) == (None, expected)

    @pytest.mark.parametrize("quote", [
        "",
        "접수기간 별도 공지",
        "접수기간 2026.09.01",                      # 붙어 있는 말이 없는 단일 날짜
        "모집기간 제1회 2026.08.25 제2회 2026.09.08",  # 범위 기호 없는 복수 날짜
        "상담시간 1개 참여기업당 60분 상담",
        "모집규모: 50명 (선찬순 마감 예정)",              # 마감 문구만 있고 날짜 없음
    ])
    def test_uncertain_input_invents_nothing(self, quote):
        assert period_from_quote(quote, today=TODAY) == (None, None)


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
        """라벨 바로 뒤에 글머리표가 오면 값이 없다는 뜻이다."""
        assert extract_quotes("□ 접수기간 □ 문의") == {}

    def test_has_quote_keys(self):
        assert has_quote_keys({}) is False
        assert has_quote_keys({"title": "x"}) is False
        assert has_quote_keys({QUOTE_DEADLINE: "접수기간 ..."}) is True


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


def make_announcement(source_id: str = "1") -> RawAnnouncement:
    return RawAnnouncement(
        source="stub",
        source_id=source_id,
        title="테스트 공고",
        url=f"https://example.com/view/{source_id}",
        raw_data=json.dumps({"title": "테스트 공고"}, ensure_ascii=False),
    )


def fake_response(text: str, chunks: int = 1, content_length: bool = True):
    """스트리밍 응답 목(mock)."""
    body = text.encode("utf-8")
    size = max(1, len(body) // chunks + 1)
    response = MagicMock()
    response.encoding = "utf-8"
    response.headers = {"Content-Length": str(len(body))} if content_length else {}
    response.iter_content.return_value = [
        body[i:i + size] for i in range(0, len(body), size)
    ]
    response.raise_for_status.return_value = None
    return response


class TestEnrichWithQuotes:
    """상세 수집은 소스별 opt-in이며, 없는 값을 채우지 않는다."""

    def test_disabled_by_default(self):
        """fetch_detail이 꺼져 있으면 요청조차 하지 않는다."""
        crawler = make_stub(fetch_detail=False)
        assert crawler.wants_detail() is False

        announcement = make_announcement()
        with patch.object(crawler.session, "get") as mock_get:
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
        html = (FIXTURES / "kofpi_detail.html").read_text(encoding="utf-8")

        with patch.object(
            crawler.session, "get", return_value=fake_response(html)
        ) as mock_get:
            with patch("alert.crawlers.base.time.sleep") as mock_sleep:
                crawler.enrich_with_quotes([announcement])

        mock_sleep.assert_not_called()  # 첫 건은 대기 없음
        _, kwargs = mock_get.call_args
        assert kwargs["timeout"] == 10
        assert kwargs["headers"]["User-Agent"].startswith("Mozilla/5.0")
        assert kwargs["stream"] is True

        payload = json.loads(announcement.raw_data)
        assert payload["title"] == "테스트 공고"           # 목록 정보 보존
        assert payload[QUOTE_DEADLINE].startswith("모집기간 2026. 9. 7.")
        assert payload[QUOTE_ELIGIBILITY].startswith("모집대상")
        assert payload["quote_period_start"] == "2026-09-07"
        assert payload["quote_period_end"] == "2026-09-30"
        assert announcement.period_start == "2026-09-07"
        assert announcement.period_end == "2026-09-30"

    def test_always_open_is_flagged_without_inventing_a_deadline(self):
        """"상시" 인용은 마감을 만들지 않고 always_open 으로 표시한다."""
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()
        html = (
            '<div class="board_view"><table>'
            "<tr><th>접수기간</th><td>상시 (예산 소진 시 마감)</td></tr>"
            "<tr><th>작성일</th><td>2026.09.11</td></tr></table></div>"
        )

        with patch.object(crawler.session, "get", return_value=fake_response(html)):
            crawler.enrich_with_quotes([announcement])

        payload = json.loads(announcement.raw_data)
        assert payload[ALWAYS_OPEN] is True
        assert "quote_period_end" not in payload
        assert announcement.period_end is None      # 게시일을 끌어오지 않는다

    def test_explicit_deadline_survives_an_always_open_detail(self):
        """제목에서 확정된 마감은 상세의 "상시" 문구에 지워지지 않는다.

        Codex 재검토 #9: KOFPI 제목 ``마감 변경 모집(~2026.10.31)`` 이
        상세 ``모집기간 상시`` 때문에 NULL이 되어 살아있는 공고를 잃었다.
        """
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()
        announcement.period_end = "2026-10-31"      # 제목에서 뽑은 마감
        html = (
            '<div class="board_view"><table>'
            "<tr><th>모집기간</th><td>상시</td></tr></table></div>"
        )

        with patch.object(crawler.session, "get", return_value=fake_response(html)):
            crawler.enrich_with_quotes([announcement])

        payload = json.loads(announcement.raw_data)
        assert announcement.period_end == "2026-10-31"   # 명시된 날짜가 이긴다
        assert payload[ALWAYS_OPEN] is True              # 상시 표시는 남긴다

    def test_early_close_keeps_the_deadline(self):
        """"예산 소진 시 조기마감" 은 마감이 있는 공고다 (재검토 #9)."""
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()
        html = (
            '<div class="board_view"><table><tr><th>접수기간</th>'
            "<td>2026.09.01 ~ 2026.09.30 (예산 소진 시 조기마감)</td>"
            "</tr></table></div>"
        )

        with patch.object(crawler.session, "get", return_value=fake_response(html)):
            crawler.enrich_with_quotes([announcement])

        payload = json.loads(announcement.raw_data)
        assert announcement.period_end == "2026-09-30"
        assert payload[EARLY_CLOSE] is True
        assert ALWAYS_OPEN not in payload

    def test_rate_limited_to_one_request_per_second(self):
        """두 번째 건부터 1초 대기한다."""
        crawler = make_stub(fetch_detail=True)
        items = [make_announcement(str(i)) for i in range(3)]

        with patch.object(
            crawler.session, "get", return_value=fake_response("<html>내용 없음</html>")
        ):
            with patch("alert.crawlers.base.time.sleep") as mock_sleep:
                crawler.enrich_with_quotes(items)

        assert mock_sleep.call_count == 2
        assert mock_sleep.call_args_list[0][0][0] == 1.0

    def test_missing_quotes_leave_keys_absent(self):
        """페이지에 문구가 없으면 키를 만들지 않고 기간도 건드리지 않는다."""
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()
        announcement.period_end = None

        with patch.object(
            crawler.session, "get",
            return_value=fake_response("<div class='board_view'>본문 없음</div>"),
        ):
            crawler.enrich_with_quotes([announcement])

        payload = json.loads(announcement.raw_data)
        assert QUOTE_DEADLINE not in payload
        assert QUOTE_ELIGIBILITY not in payload
        assert QUOTE_AMOUNT not in payload
        assert announcement.period_end is None

    def test_failed_request_is_not_fatal(self):
        """상세 요청이 실패해도 목록 데이터는 그대로 남는다."""
        import requests

        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()

        with patch.object(
            crawler.session, "get", side_effect=requests.Timeout("timed out")
        ):
            crawler.enrich_with_quotes([announcement])

        assert json.loads(announcement.raw_data) == {"title": "테스트 공고"}

    def test_unresolved_void_url_is_skipped(self):
        """#void 처럼 해소되지 않은 링크는 요청하지 않는다."""
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()
        announcement.url = "https://example.com/#void"

        with patch.object(crawler.session, "get") as mock_get:
            crawler.enrich_with_quotes([announcement])

        mock_get.assert_not_called()


class TestCodexCritiqueExecutionLimits:
    """크리틱 #8: 상세 수집에 실행시간·요청수 상한이 없던 결함."""

    def test_request_cap_per_run(self):
        """소스·실행당 새 요청은 MAX_DETAIL_REQUESTS 건을 넘지 않는다."""
        crawler = make_stub(fetch_detail=True)
        items = [make_announcement(str(i)) for i in range(MAX_DETAIL_REQUESTS + 10)]

        with patch.object(
            crawler.session, "get", return_value=fake_response("<html>x</html>")
        ) as mock_get:
            with patch("alert.crawlers.base.time.sleep"):
                crawler.enrich_with_quotes(items)

        assert mock_get.call_count == MAX_DETAIL_REQUESTS

    def test_time_budget_stops_the_loop(self):
        """예산을 넘기면 경고를 남기고 멈춘다 (수집은 실패시키지 않는다)."""
        crawler = make_stub(fetch_detail=True)
        items = [make_announcement(str(i)) for i in range(5)]

        # 호출마다 100초씩 흘려 두 번째 항목 차례에 예산을 넘긴다
        ticks = {"now": 0.0}

        def clock():
            ticks["now"] += 100.0
            return ticks["now"]

        with patch.object(
            crawler.session, "get", return_value=fake_response("<html>x</html>")
        ) as mock_get:
            with patch("alert.crawlers.base.time.sleep"):
                with patch("alert.crawlers.base.time.monotonic", clock):
                    crawler.enrich_with_quotes(items)

        assert 1 <= mock_get.call_count < len(items)

    def test_items_that_already_have_quotes_are_skipped(self):
        """이미 인용이 있는 항목은 다시 요청하지 않는다."""
        crawler = make_stub(fetch_detail=True)
        done = make_announcement("1")
        done.raw_data = json.dumps({QUOTE_DEADLINE: "접수기간 ..."}, ensure_ascii=False)
        todo = make_announcement("2")

        with patch.object(
            crawler.session, "get", return_value=fake_response("<html>x</html>")
        ) as mock_get:
            with patch("alert.crawlers.base.time.sleep"):
                crawler.enrich_with_quotes([done, todo])

        assert mock_get.call_count == 1
        assert mock_get.call_args[0][0].endswith("/view/2")

    def test_skip_source_ids_are_not_requested(self):
        """호출자가 건너뛰라고 준 source_id는 요청하지 않는다."""
        crawler = make_stub(fetch_detail=True)
        items = [make_announcement("1"), make_announcement("2")]

        with patch.object(
            crawler.session, "get", return_value=fake_response("<html>x</html>")
        ) as mock_get:
            with patch("alert.crawlers.base.time.sleep"):
                crawler.enrich_with_quotes(items, skip_source_ids={"1"})

        assert mock_get.call_count == 1

    def test_oversize_body_is_truncated_not_refused(self):
        """상한을 넘는 본문은 거부하지 않고 앞부분까지 읽어 파싱한다.

        2026-09-13 조정자 판정: kofpi 상세 중 698KB 페이지가 있어 거부하면
        마감을 잃는다. 마감 문구는 본문 앞쪽에 있으므로 절단해도 얻을 수 있다.
        """
        crawler = make_stub(fetch_detail=True)
        head = (
            '<div class="board_view"><table>'
            "<tr><th>접수기간</th><td>2026.09.01 ~ 2026.09.30</td></tr>"
            "</table>"
        )
        filler = "<p>" + ("가" * 2000) + "</p>"
        body = (head + filler * 600 + "</div>").encode("utf-8")
        assert len(body) > MAX_DETAIL_BYTES

        response = MagicMock()
        response.encoding = "utf-8"
        response.headers = {"Content-Length": str(len(body))}
        response.raise_for_status.return_value = None
        response.iter_content.return_value = [
            body[i:i + 8192] for i in range(0, len(body), 8192)
        ]

        with patch.object(crawler.session, "get", return_value=response):
            quotes = crawler.fetch_detail_quotes("https://example.com/x")

        assert quotes[QUOTE_DEADLINE] == "접수기간 2026.09.01 ~ 2026.09.30"
        assert period_from_quote(quotes[QUOTE_DEADLINE], today=TODAY) == (
            "2026-09-01", "2026-09-30"
        )

    def test_declared_oversize_body_is_still_read(self):
        """Content-Length가 크다고 해서 건너뛰지 않는다 (절단 의미론)."""
        crawler = make_stub(fetch_detail=True)
        response = MagicMock()
        response.encoding = "utf-8"
        response.headers = {"Content-Length": str(MAX_DETAIL_BYTES * 2)}
        response.raise_for_status.return_value = None
        response.iter_content.return_value = [
            b"<div class='board_view'>\xec\xa0\x91\xec\x88\x98\xea\xb8\xb0\xea\xb0\x84"
        ]

        with patch.object(crawler.session, "get", return_value=response):
            crawler.fetch_detail_quotes("https://example.com/x")
        response.iter_content.assert_called_once()

    def test_infinite_stream_is_cut_off_at_the_cap(self):
        """Content-Length를 숨기고 계속 흘려보내도 상한에서 끊고 돌아온다."""
        crawler = make_stub(fetch_detail=True)
        chunk = b"x" * 8192
        served = {"count": 0}

        def endless(chunk_size=8192):
            while True:
                served["count"] += 1
                if served["count"] > 10_000:      # 안전장치 - 상한이 없으면 여기서 터진다
                    raise AssertionError("본문 상한이 동작하지 않는다")
                yield chunk

        response = MagicMock()
        response.encoding = "utf-8"
        response.headers = {}
        response.raise_for_status.return_value = None
        response.iter_content.side_effect = endless

        with patch.object(crawler.session, "get", return_value=response):
            assert crawler.fetch_detail_quotes("https://example.com/x") == {}

        # 상한(1MB)에 도달하는 데 필요한 청크 수 이상은 읽지 않는다
        assert served["count"] <= MAX_DETAIL_BYTES // len(chunk) + 2

    def test_no_retries_on_detail_fetch(self):
        """상세 요청은 재시도하지 않는다 - 인용 하나에 재시도 예산을 쓰지 않는다."""
        import requests

        crawler = make_stub(fetch_detail=True)
        with patch.object(
            crawler.session, "get", side_effect=requests.Timeout("t")
        ) as mock_get:
            assert crawler.fetch_detail_quotes("https://example.com/x") == {}
        assert mock_get.call_count == 1


class TestCodexReviewReadDeadline:
    """최종 게이트 #2: 청크 **내부** 대기를 막지 못했던 결함.

    ``requests``/``urllib3`` 의 iterator는 ``chunk_size`` 만큼 모일 때까지
    반환하지 않는다. 1바이트를 9초마다 보내는 서버는 첫 청크까지 8192×9초
    = 73,728초를 쓰므로 "청크마다 시간 검사" 로는 진입조차 못 했다. 이제
    읽기를 작업 스레드에 넘기고 ``join(timeout)`` 으로 바깥에서 끊는다.

    실제 시계로 검증하되 테스트가 오래 걸리지 않도록 상한 상수를 줄여
    주입한다 - 검증 대상은 "상한이 강제되는가" 이다.
    """

    @staticmethod
    def _blocking_response(delay: float = 5.0):
        """첫 청크를 ``delay`` 초 동안 내놓지 않는 응답 (1바이트/9초 모사).

        ``time.sleep`` 대신 ``Event.wait`` 로 기다린다 - 테스트가
        ``base.time.sleep`` 를 가로채면 ``time`` 모듈 전체가 바뀌어 이
        블로킹까지 사라지기 때문이다(그래서 예산 테스트가 20요청을 통과해
        버렸다).
        """
        def iter_content(chunk_size=8192):
            threading.Event().wait(delay)   # 청크가 모이기를 기다리는 구간
            yield b"x" * 16
        response = MagicMock()
        response.encoding = "utf-8"
        response.headers = {}
        response.raise_for_status.return_value = None
        response.iter_content.side_effect = iter_content
        return response

    def test_read_gives_up_at_the_request_deadline(self):
        """청크가 오지 않아도 요청 상한에서 포기한다 (스레드 join)."""
        crawler = make_stub(fetch_detail=True)
        response = self._blocking_response(delay=5.0)

        with patch("alert.crawlers.base.DETAIL_REQUEST_DEADLINE", 0.3):
            with patch.object(crawler.session, "get", return_value=response):
                began = time.monotonic()
                result = crawler.fetch_detail_quotes("https://example.com/slow")
                elapsed = time.monotonic() - began

        assert result == {}
        assert elapsed < 2.0, f"상한이 강제되지 않았다 ({elapsed:.1f}s)"

    def test_source_budget_bounds_a_single_read(self):
        """남은 소스 예산이 요청 상한보다 짧으면 그 예산이 먼저 걸린다."""
        crawler = make_stub(fetch_detail=True)
        response = self._blocking_response(delay=5.0)

        with patch("alert.crawlers.base.DETAIL_REQUEST_DEADLINE", 30.0):
            with patch.object(crawler.session, "get", return_value=response):
                began = time.monotonic()
                result = crawler.fetch_detail_quotes(
                    "https://example.com/slow", deadline=time.monotonic() + 0.3
                )
                elapsed = time.monotonic() - began

        assert result == {}
        assert elapsed < 2.0

    def test_no_time_left_skips_the_request(self):
        """예산이 이미 소진됐으면 요청조차 하지 않는다."""
        crawler = make_stub(fetch_detail=True)
        with patch.object(crawler.session, "get") as mock_get:
            assert crawler.fetch_detail_quotes(
                "https://example.com/x", deadline=time.monotonic() - 1
            ) == {}
        mock_get.assert_not_called()

    def test_whole_source_run_stays_inside_the_budget(self):
        """느린 상세가 여러 건이어도 소스 예산 안에서 끝난다."""
        crawler = make_stub(fetch_detail=True)
        items = [make_announcement(str(i)) for i in range(20)]
        response = self._blocking_response(delay=5.0)

        # DETAIL_DELAY_SEC 를 줄인다 - time.sleep 을 가로채면 위 블로킹까지
        # 사라져 검증이 무의미해진다
        with patch("alert.crawlers.base.DETAIL_REQUEST_DEADLINE", 0.2):
            with patch("alert.crawlers.base.DETAIL_BUDGET_SEC", 0.5):
                with patch("alert.crawlers.base.DETAIL_DELAY_SEC", 0.0):
                    with patch.object(
                        crawler.session, "get", return_value=response
                    ) as mock_get:
                        began = time.monotonic()
                        crawler.enrich_with_quotes(items)
                        elapsed = time.monotonic() - began

        assert elapsed < 3.0, f"예산이 강제되지 않았다 ({elapsed:.1f}s)"
        assert mock_get.call_count < len(items)


class TestCodexReviewCellInternals:
    """재검토 #7: 셀 경계와 셀 **안쪽** 블록 경계를 혼동하던 결함."""

    ROUNDS_TD = (
        '<div class="board_view"><table><tr><th>접수기간</th><td>'
        "<div>1차 2026.08.01~2026.08.31</div>"
        "<div>2026.09.01~2026.09.30</div>"
        "</td></tr></table></div>"
    )

    def test_rounds_inside_one_cell_are_all_quoted(self):
        """한 셀 안의 div 로 나뉜 회차는 모두 인용에 들어간다."""
        quotes = extract_quotes(normalize_text(self.ROUNDS_TD))
        assert "2026.08.01" in quotes[QUOTE_DEADLINE]
        assert "2026.09.30" in quotes[QUOTE_DEADLINE]

    def test_div_and_span_give_the_same_period(self):
        """div 로 감싸든 span 으로 감싸든 같은 마감이 나와야 한다."""
        div_quotes = extract_quotes(normalize_text(self.ROUNDS_TD))
        span_html = self.ROUNDS_TD.replace("<div>", "<span>").replace("</div>", "</span>")
        span_quotes = extract_quotes(normalize_text(span_html))

        div_period = period_from_quote(div_quotes[QUOTE_DEADLINE], today=TODAY)
        span_period = period_from_quote(span_quotes[QUOTE_DEADLINE], today=TODAY)
        assert div_period == span_period == ("2026-09-01", "2026-09-30")

    def test_empty_cell_does_not_absorb_the_next_row(self):
        """빈 마감 셀은 다음 행의 날짜를 흡수하지 않는다."""
        html = (
            '<div class="board_view"><table>'
            "<tr><th>접수기간</th><td></td></tr>"
            "<tr><td>2026.09.11</td><td>작성일</td></tr>"
            "</table></div>"
        )
        assert extract_quotes(normalize_text(html)) == {}


class TestCodexReviewTruncation:
    """재검토 #8: 1MB 절단이 날짜를 조작하던 결함."""

    def test_partial_date_is_discarded(self):
        """``2026.09.30`` 이 ``2026.09.3`` 으로 잘려도 09-03을 만들지 않는다."""
        html = (
            '<div class="board_view"><table><tr><th>접수기간</th>'
            "<td>2026.09.01 ~ 2026.09.3"
        )
        trimmed = complete_html_prefix(html)
        quotes = extract_quotes(normalize_text(trimmed))
        start, end = period_from_quote(quotes.get(QUOTE_DEADLINE, ""), today=TODAY)
        assert end != "2026-09-03"

    def test_cut_inside_an_attribute_does_not_leak(self):
        """속성 중간에서 잘린 조각이 본문 마감으로 추출되지 않는다."""
        html = (
            '<div class="board_view"><p>본문</p>'
            '<div title="접수기간 2026.09.30'
        )
        trimmed = complete_html_prefix(html)
        assert "접수기간" not in trimmed
        assert extract_quotes(normalize_text(trimmed)) == {}

    def test_complete_prefix_keeps_finished_markup(self):
        html = '<div class="board_view"><p>접수기간 2026.09.30까지</p></div><span'
        trimmed = complete_html_prefix(html)
        assert trimmed.endswith("</div>")

    def test_no_close_bracket_yields_nothing(self):
        assert complete_html_prefix("2026.09.3") == ""
        assert complete_html_prefix("") == ""

    def test_truncated_flag_is_recorded(self):
        """절단된 응답은 raw_data에 표시를 남긴다."""
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()
        head = (
            '<div class="board_view"><table><tr><th>접수기간</th>'
            "<td>2026.09.01 ~ 2026.09.30</td></tr></table>"
        )
        body = (head + "<p>" + "가" * 2000 + "</p>" * 1).encode("utf-8")
        body = head.encode("utf-8") + ("<p>" + "가" * 400_000 + "</p>").encode("utf-8")
        assert len(body) > MAX_DETAIL_BYTES

        response = MagicMock()
        response.encoding = "utf-8"
        response.headers = {}
        response.raise_for_status.return_value = None
        response.iter_content.return_value = [
            body[i:i + 8192] for i in range(0, len(body), 8192)
        ]

        with patch.object(crawler.session, "get", return_value=response):
            crawler.enrich_with_quotes([announcement])

        payload = json.loads(announcement.raw_data)
        assert payload[DETAIL_TRUNCATED] is True
        assert payload["quote_period_end"] == "2026-09-30"


class TestCodexReviewRoundSelection:
    """재검토 #12: 회차 선택과 까지/부터 판정의 잔여 결함."""

    @pytest.mark.parametrize("quote,expected", [
        # 짧은 날짜에도 연도를 물려줘 2차가 선택된다 (예전엔 지난 1차)
        ("모집기간 1차 2026.08.01~8.31 2차 9.01~9.30", ("2026-09-01", "2026-09-30")),
        # 본문 순서가 2차 -> 1차 여도 결과는 같다 (모두 지났으면 가장 늦은 종료)
        ("모집기간 2차 2026.02.01~2.28 1차 2026.01.01~1.31", ("2026-02-01", "2026-02-28")),
        ("모집기간 1차 2026.01.01~1.31 2차 2026.02.01~2.28", ("2026-02-01", "2026-02-28")),
        # 연도가 뒤에만 있어도 앞 날짜를 되돌려 받는다
        ("접수기간 12.20~2027.1.10", ("2026-12-20", "2027-01-10")),
        ("접수기간 2026.12.20 ~ 1.10", ("2026-12-20", "2027-01-10")),
    ])
    def test_round_and_year_resolution(self, quote, expected):
        assert period_from_quote(quote, today=TODAY) == expected

    def test_kkaji_is_an_end_even_when_ihu_follows(self):
        """"09.30까지, 이후 접수불가" 의 "이후" 를 시작 신호로 오인하지 않는다."""
        assert period_from_quote(
            "접수기간 2026.09.30까지, 이후 접수불가", today=TODAY
        ) == (None, "2026-09-30")

    def test_end_only_allows_a_none_start(self):
        start, end = period_from_quote("마감: 2026.09.30", today=TODAY)
        assert start is None and end == "2026-09-30"


class TestResolvePeriodPriority:
    """재검토 #9: 명시된 날짜가 "상시" 보다 우선한다."""

    @pytest.mark.parametrize("quote,expected", [
        ("모집기간 상시", (None, None, True, False)),
        ("모집기간 수시 모집", (None, None, True, False)),
        ("접수기간 2026.09.01 ~ 2026.09.30 (예산 소진 시 조기마감)",
         ("2026-09-01", "2026-09-30", False, True)),
        ("접수기간 2026.09.01부터 상시 (예산 소진 시 마감)",
         ("2026-09-01", None, True, False)),
    ])
    def test_resolution(self, quote, expected):
        assert resolve_period(quote, today=TODAY) == expected

    def test_early_close_detector(self):
        assert is_early_close("예산 소진 시 조기마감") is True
        assert is_early_close("선착순 접수") is True
        assert is_early_close("접수기간 2026.09.30까지") is False

    def test_always_open_detector_excludes_early_close_wording(self):
        """"예산 소진" 만으로는 상시가 아니다 - 마감이 있는 공고다."""
        assert is_always_open("예산 소진 시 조기마감") is False
        assert is_always_open("상시 모집") is True


class TestCodexReviewBudgetStarvation:
    """재검토 #11: 요청 상한 때문에 목록 뒤쪽이 영구히 미수집되던 결함."""

    def test_quoted_ids_from_db_free_the_budget(self):
        """DB에서 받은 "이미 인용 있음" 목록은 예산을 쓰지 않는다."""
        crawler = make_stub(fetch_detail=True)
        items = [make_announcement(str(i)) for i in range(MAX_DETAIL_REQUESTS + 5)]
        already = {str(i) for i in range(MAX_DETAIL_REQUESTS)}
        crawler.set_quoted_source_ids(already)

        with patch.object(
            crawler.session, "get", return_value=fake_response("<html>x</html>")
        ) as mock_get:
            with patch("alert.crawlers.base.time.sleep"):
                crawler.enrich_with_quotes(items)

        requested = [call[0][0] for call in mock_get.call_args_list]
        assert len(requested) == 5
        # 정확히 뒤쪽 5건만 요청한다
        assert [url.rsplit("/", 1)[-1] for url in requested] == [
            str(i) for i in range(MAX_DETAIL_REQUESTS, MAX_DETAIL_REQUESTS + 5)
        ]

    def test_two_runs_cover_a_list_longer_than_the_cap(self):
        """상한보다 긴 목록도 두 번 실행하면 전부 수집된다."""
        total = MAX_DETAIL_REQUESTS + 5
        seen: set = set()

        for _ in range(2):
            crawler = make_stub(fetch_detail=True)
            crawler.set_quoted_source_ids(seen)
            items = [make_announcement(str(i)) for i in range(total)]
            with patch.object(
                crawler.session, "get", return_value=fake_response("<html>x</html>")
            ) as mock_get:
                with patch("alert.crawlers.base.time.sleep"):
                    crawler.enrich_with_quotes(items)
            for call in mock_get.call_args_list:
                seen.add(call[0][0].rsplit("/", 1)[-1])

        assert seen == {str(i) for i in range(total)}


class TestFinalGateBlockScope:
    """최종 게이트 #4: 라벨의 값은 그 라벨 블록 안에서만 찾는다."""

    def test_sibling_paragraph_does_not_leak(self):
        """``<p>접수기간 …부터</p><p>교육기간 09.20~09.30</p>`` → 마감 없음."""
        html = (
            '<div class="board_view">'
            "<p>접수기간 2026.09.01부터</p>"
            "<p>교육기간 2026.09.20~2026.09.30</p></div>"
        )
        quotes = extract_quotes(normalize_text(html))
        assert quotes[QUOTE_DEADLINE] == "접수기간 2026.09.01부터"
        assert period_from_quote(quotes[QUOTE_DEADLINE], today=TODAY) == (
            "2026-09-01", None
        )

    def test_br_inside_one_cell_does_not_leak(self):
        """한 셀 안의 ``<br>`` 뒤 다른 라벨도 경계다."""
        html = (
            '<div class="board_view"><table><tr><td>'
            "접수기간 2026.09.01부터<br>교육기간 2026.09.20~2026.09.30"
            "</td></tr></table></div>"
        )
        quotes = extract_quotes(normalize_text(html))
        assert "교육기간" not in quotes[QUOTE_DEADLINE]
        assert period_from_quote(quotes[QUOTE_DEADLINE], today=TODAY)[1] is None

    def test_nested_table_rounds_stay_together(self):
        """중첩 표의 1·2차는 라벨 블록의 값이므로 모두 인용에 들어간다."""
        html = (
            '<div class="board_view"><table><tr><th>접수기간</th><td>'
            "<table><tr><td>1차 2026.08.01~2026.08.31</td></tr>"
            "<tr><td>2차 2026.09.01~2026.09.30</td></tr></table>"
            "</td></tr></table></div>"
        )
        quotes = extract_quotes(normalize_text(html))
        assert "2026.08.01" in quotes[QUOTE_DEADLINE]
        assert "2026.09.30" in quotes[QUOTE_DEADLINE]
        assert period_from_quote(quotes[QUOTE_DEADLINE], today=TODAY) == (
            "2026-09-01", "2026-09-30"
        )

    def test_same_concept_label_is_not_a_boundary(self):
        """"접수기간 … 예산 소진 시 마감" 의 "마감" 은 값의 일부다."""
        html = (
            '<div class="board_view"><table><tr><th>접수기간</th>'
            "<td>상시 (예산 소진 시 마감)</td></tr></table></div>"
        )
        quotes = extract_quotes(normalize_text(html))
        assert quotes[QUOTE_DEADLINE] == "접수기간 상시 (예산 소진 시 마감)"

    def test_label_word_mid_sentence_is_not_a_boundary(self):
        """"(제출서류 완비 기준)" 처럼 문장 중간의 라벨 낱말은 경계가 아니다."""
        html = (
            '<div class="board_view"><table><tr><th>모집규모</th>'
            "<td>25개소 내외 (제출서류 완비 기준) 선착순 접수</td></tr></table></div>"
        )
        quotes = extract_quotes(normalize_text(html))
        assert "선착순 접수" in quotes[QUOTE_AMOUNT]


class TestFinalGateTruncationSafety:
    """최종 게이트 #3: 완결 태그가 완결 인용을 보장하지 않았다."""

    def test_span_split_partial_date_is_dropped(self):
        """``2026.09.<span>3</span>`` 뒤에서 잘려도 09-03을 만들지 않는다."""
        html = (
            '<div class="board_view"><p>접수기간 2026.09.01 ~ 2026.09.'
            "<span>3</span>"
        )
        trimmed = complete_html_prefix(html)
        quotes = extract_quotes(normalize_text(trimmed))
        _start, end = period_from_quote(quotes.get(QUOTE_DEADLINE, ""), today=TODAY)
        assert end != "2026-09-03"

    def test_cut_inside_an_attribute_value_is_dropped(self):
        """속성 **안쪽** 의 ``>`` 에서 잘려도 숨은 날짜를 인용하지 않는다."""
        html = (
            '<div class="board_view"><p>본문</p>'
            '<div title="접수기간 2026.09.01~2026.09.30 >'
        )
        trimmed = complete_html_prefix(html)
        assert "접수기간" not in trimmed
        assert extract_quotes(normalize_text(trimmed)) == {}

    def test_attribute_dates_are_never_extracted(self):
        """완결된 마크업에서도 속성 값은 본문이 아니다."""
        html = (
            '<div class="board_view"><p>접수기간 2026.09.01 ~ 2026.09.30</p>'
            '<div title="접수기간 2027.01.01~2027.12.31">보기</div></div>'
        )
        quotes = extract_quotes(normalize_text(html))
        assert period_from_quote(quotes[QUOTE_DEADLINE], today=TODAY) == (
            "2026-09-01", "2026-09-30"
        )
        assert "2027" not in quotes[QUOTE_DEADLINE]

    def test_only_block_boundaries_survive(self):
        assert complete_html_prefix("<p>a</p><span>b") == "<p>a</p>"
        assert complete_html_prefix("<span>b</span>") == ""
        assert complete_html_prefix("2026.09.3") == ""


class TestFinalGateYearInference:
    """최종 게이트 #5: 회차 사이 날짜 역전을 해 넘김으로 오인했다."""

    def test_round_reversal_does_not_roll_the_year(self):
        """"2차 2.01~2.28 / 1차 1.01~1.31" 은 모두 2026년이다."""
        quote = "모집기간 2차 2026.02.01~2.28 1차 1.01~1.31"
        assert period_from_quote(quote, today=TODAY) == ("2026-02-01", "2026-02-28")

    def test_single_range_still_rolls_over(self):
        """회차 표기가 없는 한 범위는 해 넘김을 따진다."""
        assert period_from_quote("접수기간 2026.12.20 ~ 1.10", today=TODAY) == (
            "2026-12-20", "2027-01-10"
        )

    def test_explicit_year_is_never_overridden(self):
        assert period_from_quote("접수기간 12.20~2027.1.10", today=TODAY) == (
            "2026-12-20", "2027-01-10"
        )

    def test_forward_rounds_pick_the_live_one(self):
        assert period_from_quote(
            "모집기간 1차 2026.08.01~8.31 2차 9.01~9.30", today=TODAY
        ) == ("2026-09-01", "2026-09-30")


class TestFinalGateEarlyCloseReset:
    """최종 게이트 #9: 조기마감 플래그가 교체 후에도 남았다."""

    def test_flag_is_cleared_when_the_quote_is_replaced(self):
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()

        early = (
            '<div class="board_view"><table><tr><th>접수기간</th>'
            "<td>2026.09.01 ~ 2026.09.30 (예산 소진 시 조기마감)</td>"
            "</tr></table></div>"
        )
        plain = (
            '<div class="board_view"><table><tr><th>접수기간</th>'
            "<td>2026.10.01 ~ 2026.10.31</td></tr></table></div>"
        )

        with patch.object(crawler.session, "get", return_value=fake_response(early)):
            crawler.enrich_with_quotes([announcement])
        assert json.loads(announcement.raw_data)[EARLY_CLOSE] is True

        # 같은 공고에 조건 없는 인용이 새로 도착한다
        announcement.raw_data = json.dumps(
            {k: v for k, v in json.loads(announcement.raw_data).items()
             if not k.startswith("quote_")},
            ensure_ascii=False,
        )
        with patch.object(crawler.session, "get", return_value=fake_response(plain)):
            crawler._apply_quotes(
                announcement,
                extract_quotes(normalize_text(plain)),
            )

        payload = json.loads(announcement.raw_data)
        assert EARLY_CLOSE not in payload
        assert payload["quote_period_end"] == "2026-10-31"
        assert announcement.period_end == "2026-10-31"

    def test_stale_quote_periods_are_replaced(self):
        """교체된 인용의 예전 파생 날짜가 남지 않는다."""
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()
        announcement.raw_data = json.dumps(
            {"quote_period_start": "2026-01-01", "quote_period_end": "2026-01-31"},
            ensure_ascii=False,
        )
        crawler._apply_quotes(
            announcement, {QUOTE_DEADLINE: "접수기간 2026.10.01 ~ 2026.10.31"}
        )
        payload = json.loads(announcement.raw_data)
        assert payload["quote_period_start"] == "2026-10-01"
        assert payload["quote_period_end"] == "2026-10-31"

