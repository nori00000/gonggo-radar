"""상세 페이지 인용 추출 테스트 (계약 v2.1 판정 4 - 원문 인용만, 창작 금지).

kofpi / coop / seis 실제 상세 페이지를 잘라 만든 fixture로 검증하므로
네트워크를 타지 않는다. ``TestCodexCritique*`` 클래스는 2026-09-13 Codex
크리틱 #3·#6·#7·#8 의 재현 입력을 그대로 쓴다.
"""

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from alert.crawlers.base import BaseCrawler
from alert.crawlers.detail_quotes import (
    ALWAYS_OPEN,
    MAX_DETAIL_BYTES,
    MAX_DETAIL_REQUESTS,
    QUOTE_AMOUNT,
    QUOTE_DEADLINE,
    QUOTE_ELIGIBILITY,
    extract_quotes,
    flatten,
    has_quote_keys,
    is_always_open,
    normalize_text,
    period_from_quote,
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

    def test_multi_round_schedule_yields_no_period(self, text):
        """회차별 일정표는 셀 경계에서 끊기므로 기간을 단정하지 않는다."""
        quotes = extract_quotes(text)
        assert "모집기간" in quotes[QUOTE_DEADLINE]
        assert period_from_quote(quotes[QUOTE_DEADLINE], today=TODAY) == (None, None)

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

    def test_row_boundary_becomes_a_newline(self, text):
        """셀·행이 끝나는 자리에 줄바꿈이 남는다."""
        assert text.splitlines() == [
            "접수기간", "상시 (예산 소진 시 마감)", "작성일", "2026.09.11",
        ]

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

    def test_always_open_clears_the_deadline(self):
        """"상시" 공고는 마감을 비우고 always_open 으로 표시한다 (크리틱 #3)."""
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()
        announcement.period_end = "2026-09-11"   # 예전에 잘못 들어간 값
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
        assert announcement.period_end is None

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

        # 두 번째 확인 시점에 예산을 초과한 것처럼 시간을 흘린다
        clock = iter([0.0, 0.0, 10_000.0, 10_000.0, 10_000.0])
        with patch.object(
            crawler.session, "get", return_value=fake_response("<html>x</html>")
        ) as mock_get:
            with patch("alert.crawlers.base.time.sleep"):
                with patch("alert.crawlers.base.time.monotonic", lambda: next(clock)):
                    crawler.enrich_with_quotes(items)

        assert mock_get.call_count == 1

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
