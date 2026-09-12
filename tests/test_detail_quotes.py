"""상세 페이지 인용 추출 테스트 (계약 v2.1 판정 4 - 원문 인용만, 창작 금지).

kofpi / coop / seis 실제 상세 페이지를 잘라 만든 fixture로 검증하므로
네트워크를 타지 않는다. ``TestCodexCritique*`` 클래스는 2026-09-13 Codex
크리틱 #3·#6·#7·#8 의 재현 입력을 그대로 쓴다.
"""

import json
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import alert.crawlers.detail_quotes as detail_quotes
from alert.crawlers.base import BaseCrawler
from alert.crawlers.detail_quotes import (
    ALWAYS_OPEN,
    DETAIL_BUDGET_SEC,
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
    QUOTES_ATTEMPTED_AT,
    WORKER_MODULE,
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


def quotes_for(html: str) -> dict:
    """HTML에서 인용을 뽑는다 (자식 프로세스가 하는 일과 같다)."""
    return extract_quotes(normalize_text(html))


def fake_worker(mapping: dict, timed_out: bool = False):
    """``run_detail_worker`` 대역 - source_id -> 결과 딕셔너리."""
    def run(items):
        results = []
        for item in items:
            source_id = str(item["source_id"])
            if source_id not in mapping:
                continue
            payload = dict(mapping[source_id])
            payload["source_id"] = source_id
            results.append(payload)
        return results, timed_out
    return run


def worker_html(html: str):
    """모든 항목에 같은 HTML의 인용을 주는 워커 대역."""
    def run(items):
        return (
            [
                {"source_id": str(item["source_id"]), "quotes": quotes_for(html)}
                for item in items
            ],
            False,
        )
    return run


def collecting_worker(sent: list, results=None, timed_out: bool = False):
    """워커에 실제로 무엇이 전달됐는지 기록하는 대역."""
    def run(items):
        sent.extend(items)
        return list(results or []), timed_out
    return run


class TestEnrichWithQuotes:
    """상세 수집은 소스별 opt-in이며, 없는 값을 채우지 않는다."""

    def test_disabled_by_default(self):
        """fetch_detail이 꺼져 있으면 워커를 띄우지 않는다."""
        crawler = make_stub(fetch_detail=False)
        assert crawler.wants_detail() is False

        announcement = make_announcement()
        with patch.object(crawler, "run_detail_worker") as mock_worker:
            crawler.enrich_with_quotes([announcement])
        mock_worker.assert_not_called()
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

        with patch.object(crawler, "run_detail_worker", worker_html(html)):
            crawler.enrich_with_quotes([announcement])

        payload = json.loads(announcement.raw_data)
        assert payload["title"] == "테스트 공고"           # 목록 정보 보존
        assert payload[QUOTE_DEADLINE].startswith("모집기간 2026. 9. 7.")
        assert payload[QUOTE_ELIGIBILITY].startswith("모집대상")
        assert payload["quote_period_start"] == "2026-09-07"
        assert payload["quote_period_end"] == "2026-09-30"
        assert payload[QUOTES_ATTEMPTED_AT]
        assert announcement.period_start == "2026-09-07"
        assert announcement.period_end == "2026-09-30"

    def test_always_open_is_flagged_without_inventing_a_deadline(self):
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()
        html = (
            '<div class="board_view"><table>'
            "<tr><th>접수기간</th><td>상시 (예산 소진 시 마감)</td></tr>"
            "<tr><th>작성일</th><td>2026.09.11</td></tr></table></div>"
        )

        with patch.object(crawler, "run_detail_worker", worker_html(html)):
            crawler.enrich_with_quotes([announcement])

        payload = json.loads(announcement.raw_data)
        assert payload[ALWAYS_OPEN] is True
        assert "quote_period_end" not in payload
        assert announcement.period_end is None      # 게시일을 끌어오지 않는다

    def test_explicit_deadline_survives_an_always_open_detail(self):
        """제목에서 확정된 마감은 상세의 "상시" 문구에 지워지지 않는다."""
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()
        announcement.period_end = "2026-10-31"
        html = (
            '<div class="board_view"><table>'
            "<tr><th>모집기간</th><td>상시</td></tr></table></div>"
        )

        with patch.object(crawler, "run_detail_worker", worker_html(html)):
            crawler.enrich_with_quotes([announcement])

        payload = json.loads(announcement.raw_data)
        assert announcement.period_end == "2026-10-31"   # 명시된 날짜가 이긴다
        assert payload[ALWAYS_OPEN] is True

    def test_early_close_keeps_the_deadline(self):
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()
        html = (
            '<div class="board_view"><table><tr><th>접수기간</th>'
            "<td>2026.09.01 ~ 2026.09.30 (예산 소진 시 조기마감)</td>"
            "</tr></table></div>"
        )

        with patch.object(crawler, "run_detail_worker", worker_html(html)):
            crawler.enrich_with_quotes([announcement])

        payload = json.loads(announcement.raw_data)
        assert announcement.period_end == "2026-09-30"
        assert payload[EARLY_CLOSE] is True
        assert ALWAYS_OPEN not in payload

    def test_early_close_flag_is_cleared_on_replacement(self):
        """조건 없는 인용으로 교체되면 조기마감 표시가 사라진다."""
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

        crawler._apply_quotes(announcement, quotes_for(early))
        assert json.loads(announcement.raw_data)[EARLY_CLOSE] is True

        crawler._apply_quotes(announcement, quotes_for(plain))
        payload = json.loads(announcement.raw_data)
        assert EARLY_CLOSE not in payload
        assert payload["quote_period_end"] == "2026-10-31"
        assert announcement.period_end == "2026-10-31"

    def test_missing_quotes_leave_keys_absent_but_record_the_attempt(self):
        """문구가 없으면 키를 만들지 않지만 **시도는 기록한다** (게이트 #6)."""
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()
        announcement.period_end = None

        with patch.object(
            crawler, "run_detail_worker",
            worker_html("<div class='board_view'>본문 없음</div>"),
        ):
            crawler.enrich_with_quotes([announcement])

        payload = json.loads(announcement.raw_data)
        assert QUOTE_DEADLINE not in payload
        assert QUOTE_ELIGIBILITY not in payload
        assert QUOTE_AMOUNT not in payload
        assert payload[QUOTES_ATTEMPTED_AT]
        assert announcement.period_end is None

    def test_worker_failure_is_not_fatal(self):
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()

        with patch.object(crawler, "run_detail_worker", lambda items: ([], True)):
            crawler.enrich_with_quotes([announcement])

        assert json.loads(announcement.raw_data) == {"title": "테스트 공고"}

    def test_unresolved_void_url_is_skipped(self):
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()
        announcement.url = "https://example.com/#void"

        sent: list = []
        with patch.object(crawler, "run_detail_worker", collecting_worker(sent)):
            crawler.enrich_with_quotes([announcement])
        assert sent == []

    def test_truncated_result_yields_no_quotes(self):
        """절단된 응답은 인용을 만들지 않고 표시만 남긴다 (게이트 #2)."""
        crawler = make_stub(fetch_detail=True)
        announcement = make_announcement()

        with patch.object(
            crawler, "run_detail_worker",
            fake_worker({"1": {"truncated": True, "reason": "body over"}}),
        ):
            crawler.enrich_with_quotes([announcement])

        payload = json.loads(announcement.raw_data)
        assert payload[DETAIL_TRUNCATED] is True
        assert QUOTE_DEADLINE not in payload
        assert payload[QUOTES_ATTEMPTED_AT]


class TestExecutionLimits:
    """요청 상한·건너뛰기·순서 (부모 쪽 로직)."""

    def test_request_cap_per_run(self):
        crawler = make_stub(fetch_detail=True)
        items = [make_announcement(str(i)) for i in range(MAX_DETAIL_REQUESTS + 10)]

        sent: list = []
        with patch.object(crawler, "run_detail_worker", collecting_worker(sent)):
            crawler.enrich_with_quotes(items)
        assert len(sent) == MAX_DETAIL_REQUESTS

    def test_items_that_already_have_quotes_are_skipped(self):
        crawler = make_stub(fetch_detail=True)
        done = make_announcement("1")
        done.raw_data = json.dumps({QUOTE_DEADLINE: "접수기간 ..."}, ensure_ascii=False)
        todo = make_announcement("2")

        sent: list = []
        with patch.object(crawler, "run_detail_worker", collecting_worker(sent)):
            crawler.enrich_with_quotes([done, todo])
        assert [item["source_id"] for item in sent] == ["2"]

    def test_skip_source_ids_are_not_requested(self):
        crawler = make_stub(fetch_detail=True)
        items = [make_announcement("1"), make_announcement("2")]

        sent: list = []
        with patch.object(crawler, "run_detail_worker", collecting_worker(sent)):
            crawler.enrich_with_quotes(items, skip_source_ids={"1"})
        assert [item["source_id"] for item in sent] == ["2"]

    def test_never_attempted_items_go_first(self):
        """시도 기록이 오래된 것부터, 시도 없는 것이 가장 먼저다 (게이트 #6)."""
        crawler = make_stub(fetch_detail=True)
        items = [make_announcement(str(i)) for i in range(4)]
        crawler.set_quote_attempts({
            "0": "2026-09-12T00:00:00",
            "1": "2026-09-10T00:00:00",
            "2": "2026-09-11T00:00:00",
        })

        sent: list = []
        with patch.object(crawler, "run_detail_worker", collecting_worker(sent)):
            crawler.enrich_with_quotes(items)
        assert [item["source_id"] for item in sent] == ["3", "1", "2", "0"]

    def test_attempt_stamp_in_raw_data_also_orders(self):
        """raw_data에 남은 시도 시각도 순서에 쓰인다."""
        crawler = make_stub(fetch_detail=True)
        old = make_announcement("old")
        old.raw_data = json.dumps(
            {QUOTES_ATTEMPTED_AT: "2026-09-01T00:00:00"}, ensure_ascii=False
        )
        fresh = make_announcement("fresh")
        fresh.raw_data = json.dumps(
            {QUOTES_ATTEMPTED_AT: "2026-09-12T00:00:00"}, ensure_ascii=False
        )

        sent: list = []
        with patch.object(crawler, "run_detail_worker", collecting_worker(sent)):
            crawler.enrich_with_quotes([fresh, old])
        assert [item["source_id"] for item in sent] == ["old", "fresh"]


class TestChildFetch:
    """자식 프로세스의 한 항목 수집 (``detail_quotes.fetch_detail_quotes``)."""

    @staticmethod
    def _response(body: bytes, headers=None, encoding="utf-8"):
        response = MagicMock()
        response.encoding = encoding
        response.headers = headers if headers is not None else {}
        response.raise_for_status.return_value = None
        response.iter_content.return_value = [
            body[i:i + 8192] for i in range(0, len(body), 8192)
        ]
        return response

    def test_quotes_are_extracted_with_split_timeouts(self):
        html = '<div class="board_view"><p>접수기간 2026.09.01 ~ 2026.09.30</p></div>'
        session = MagicMock()
        session.get.return_value = self._response(html.encode("utf-8"))

        result = detail_quotes.fetch_detail_quotes(session, "https://example.com/x")
        assert result["quotes"][QUOTE_DEADLINE] == "접수기간 2026.09.01 ~ 2026.09.30"
        _args, kwargs = session.get.call_args
        assert kwargs["timeout"] == (10, 10)
        assert kwargs["stream"] is True

    def test_declared_oversize_is_skipped_without_reading(self):
        session = MagicMock()
        response = self._response(
            b"", headers={"Content-Length": str(MAX_DETAIL_BYTES + 1)}
        )
        session.get.return_value = response

        result = detail_quotes.fetch_detail_quotes(session, "https://example.com/x")
        assert result["truncated"] is True
        assert "quotes" not in result
        response.iter_content.assert_not_called()

    def test_oversize_body_yields_no_quotes(self):
        """상한을 넘는 본문은 **부분 파싱하지 않는다** (게이트 #2).

        예전에는 "안전한 절단 지점" 을 찾으려 했고, 속성 안쪽·중첩 블록에서
        거짓 날짜(09-03, 숨은 09-30)를 세 번 만들었다.
        """
        html = (
            '<div class="board_view"><p>접수기간 2026.09.01 ~ 2026.09.30</p>'
            + "<p>" + "가" * 600_000 + "</p>"
        )
        session = MagicMock()
        session.get.return_value = self._response(html.encode("utf-8"))

        result = detail_quotes.fetch_detail_quotes(session, "https://example.com/x")
        assert result["truncated"] is True
        assert "quotes" not in result

    def test_request_error_is_captured(self):
        import requests

        session = MagicMock()
        session.get.side_effect = requests.Timeout("timed out")
        result = detail_quotes.fetch_detail_quotes(session, "https://example.com/x")
        assert "error" in result
        assert "quotes" not in result

    def test_parse_failure_is_isolated(self):
        """한 페이지의 파싱 실패가 결과 전체를 죽이지 않는다 (게이트 #5)."""
        session = MagicMock()
        session.get.return_value = self._response(b"<div class='board_view'>x</div>")
        with patch.object(
            detail_quotes, "normalize_text", side_effect=RecursionError("boom")
        ):
            result = detail_quotes.fetch_detail_quotes(session, "https://example.com/x")
        assert result["error"].startswith("parse RecursionError")

    def test_deadline_in_the_past_skips(self):
        session = MagicMock()
        result = detail_quotes.fetch_detail_quotes(
            session, "https://example.com/x", deadline=time.monotonic() - 1
        )
        assert result == {"skipped": "deadline"}
        session.get.assert_not_called()

    def test_empty_url_is_an_error(self):
        assert "error" in detail_quotes.fetch_detail_quotes(MagicMock(), "")


class TestWorkerIsolation:
    """4차 게이트 #1: 프로세스 경계만이 협조 없이 시간을 강제한다.

    프로세스 **안에서** 상한을 걸려는 시도는 세 번 실패했다: 청크별 시간
    검사(iterator가 chunk_size 만큼 모일 때까지 돌아오지 않음), 작업 스레드
    + join(이어지는 동기 ``close()`` 가 스트림 종료를 기다림), 그리고
    GET·close·파싱이 상한 밖에 있던 문제. 이제 부모가
    ``subprocess.run(timeout=…)`` 으로 프로세스를 죽인다.
    """

    REPO = Path(__file__).resolve().parent.parent

    def test_worker_module_is_runnable(self):
        completed = subprocess.run(
            [sys.executable, "-m", WORKER_MODULE, "--source", "t"],
            input="[]", capture_output=True, text=True, timeout=60,
            cwd=str(self.REPO),
        )
        assert completed.returncode == 0
        assert completed.stdout.strip() == ""

    def test_worker_emits_one_flushed_line_per_item(self):
        """항목마다 한 줄씩 즉시 flush 한다 - 부분 결과 보존의 근거."""
        items = [{"source_id": "a", "url": ""}, {"source_id": "b", "url": ""}]
        completed = subprocess.run(
            [sys.executable, "-m", WORKER_MODULE, "--source", "t", "--delay", "0"],
            input=json.dumps(items), capture_output=True, text=True, timeout=60,
            cwd=str(self.REPO),
        )
        lines = [json.loads(ln) for ln in completed.stdout.splitlines() if ln.strip()]
        assert [line["source_id"] for line in lines] == ["a", "b"]
        assert all("error" in line for line in lines)     # 빈 URL

    def test_invalid_items_payload_exits_nonzero(self):
        completed = subprocess.run(
            [sys.executable, "-m", WORKER_MODULE],
            input="not json", capture_output=True, text=True, timeout=60,
            cwd=str(self.REPO),
        )
        assert completed.returncode == 2

    def test_hanging_process_is_killed_and_partial_output_kept(self, tmp_path):
        """멈춘 자식은 예산에서 죽고 그때까지의 줄은 살아남는다."""
        hanging = tmp_path / "hang.py"
        hanging.write_text(
            "import sys, time\n"
            'print(\'{"source_id": "0", "quotes": {}}\', flush=True)\n'
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        began = time.monotonic()
        killed = False
        partial = ""
        try:
            subprocess.run(
                [sys.executable, str(hanging)],
                input="[]", capture_output=True, text=True, timeout=1.0,
            )
        except subprocess.TimeoutExpired as exc:
            killed = True
            partial = exc.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode()
        elapsed = time.monotonic() - began

        assert killed is True
        assert elapsed < 5.0
        assert '"source_id": "0"' in partial

    def test_run_detail_worker_parses_json_lines(self):
        crawler = make_stub(fetch_detail=True)
        completed = MagicMock()
        completed.stdout = (
            '{"source_id": "1", "quotes": {"quote_deadline": "접수기간 2026.09.30까지"}}\n'
            "쓰레기 줄\n"
            '{"source_id": "2", "truncated": true}\n'
        )
        completed.stderr = ""
        completed.returncode = 0

        with patch.object(subprocess, "run", return_value=completed):
            results, timed_out = crawler.run_detail_worker(
                [{"source_id": "1", "url": "u"}, {"source_id": "2", "url": "u"}]
            )
        assert timed_out is False
        assert [r["source_id"] for r in results] == ["1", "2"]

    def test_run_detail_worker_keeps_partial_output_on_timeout(self):
        crawler = make_stub(fetch_detail=True)
        exc = subprocess.TimeoutExpired(cmd="x", timeout=1.0)
        exc.stdout = '{"source_id": "1", "quotes": {}}\n'

        with patch.object(subprocess, "run", side_effect=exc):
            results, timed_out = crawler.run_detail_worker(
                [{"source_id": "1", "url": "u"}]
            )
        assert timed_out is True
        assert [r["source_id"] for r in results] == ["1"]

    def test_run_detail_worker_passes_the_budget_as_the_timeout(self):
        """부모는 소스 예산을 프로세스 timeout 으로 넘긴다."""
        crawler = make_stub(fetch_detail=True)
        completed = MagicMock()
        completed.stdout = ""
        completed.stderr = ""
        completed.returncode = 0

        with patch.object(subprocess, "run", return_value=completed) as mock_run:
            crawler.run_detail_worker([{"source_id": "1", "url": "u"}])
        _args, kwargs = mock_run.call_args
        assert kwargs["timeout"] == DETAIL_BUDGET_SEC

    def test_worker_launch_failure_is_not_fatal(self):
        crawler = make_stub(fetch_detail=True)
        with patch.object(subprocess, "run", side_effect=OSError("no exec")):
            results, timed_out = crawler.run_detail_worker(
                [{"source_id": "1", "url": "u"}]
            )
        assert results == []
        assert timed_out is False

    def test_no_items_means_no_process(self):
        crawler = make_stub(fetch_detail=True)
        with patch.object(subprocess, "run") as mock_run:
            assert crawler.run_detail_worker([]) == ([], False)
        mock_run.assert_not_called()


class TestHtmlCommentsAndDepth:
    """4차 게이트 #3·#5: 주석 누출과 재귀 한계."""

    def test_comment_is_not_body_text(self):
        """주석에 든 옛 접수기간이 실제 마감을 덮지 않는다."""
        html = (
            '<div class="board_view">'
            "<!-- 접수기간 2026.09.01~2026.09.30 -->"
            "<p>모집기간 2026.10.01~2026.10.31</p></div>"
        )
        quotes = extract_quotes(normalize_text(html))
        assert quotes[QUOTE_DEADLINE] == "모집기간 2026.10.01~2026.10.31"
        assert period_from_quote(quotes[QUOTE_DEADLINE], today=TODAY) == (
            "2026-10-01", "2026-10-31"
        )

    def test_comment_only_yields_nothing(self):
        html = '<div class="board_view"><!-- 접수기간 2026.09.01~2026.09.30 --></div>'
        assert extract_quotes(normalize_text(html)) == {}

    def test_script_and_style_are_dropped(self):
        html = (
            '<div class="board_view">'
            "<script>var s='접수기간 2026.01.01~2026.01.31';</script>"
            "<style>.x{content:'접수기간 2026.02.01~2026.02.28'}</style>"
            "<p>접수기간 2026.09.01 ~ 2026.09.30</p></div>"
        )
        quotes = extract_quotes(normalize_text(html))
        assert period_from_quote(quotes[QUOTE_DEADLINE], today=TODAY) == (
            "2026-09-01", "2026-09-30"
        )

    def test_deeply_nested_html_does_not_break_the_walk(self):
        """1,100단 중첩도 견딘다 - 재귀 순회는 소스 전체 수집을 잃었다."""
        html = (
            '<div class="board_view">' + "<div>" * 1100
            + "접수기간 2026.09.01~2026.09.30"
            + "</div>" * 1100 + "</div>"
        )
        quotes = extract_quotes(normalize_text(html))
        assert period_from_quote(quotes[QUOTE_DEADLINE], today=TODAY) == (
            "2026-09-01", "2026-09-30"
        )

    def test_attribute_dates_are_never_extracted(self):
        """트리 순회이므로 속성 값은 구조적으로 본문이 될 수 없다."""
        html = (
            '<div class="board_view"><p>접수기간 2026.09.01 ~ 2026.09.30</p>'
            '<div title="접수기간 2027.01.01~2027.12.31">보기</div></div>'
        )
        quotes = extract_quotes(normalize_text(html))
        assert "2027" not in quotes[QUOTE_DEADLINE]


class TestParenLabelBoundary:
    """4차 게이트 #6: 괄호로 감싼 다른 라벨도 경계다."""

    def test_paren_label_blocks_the_range(self):
        html = (
            '<div class="board_view"><table><tr><td>'
            "접수기간 2026.09.01부터<br>(교육기간) 2026.09.20~2026.09.30"
            "</td></tr></table></div>"
        )
        quotes = extract_quotes(normalize_text(html))
        assert "2026.09.20" not in quotes[QUOTE_DEADLINE]
        assert period_from_quote(quotes[QUOTE_DEADLINE], today=TODAY) == (
            "2026-09-01", None
        )

    @pytest.mark.parametrize("decorated", [
        "(교육기간)", "[교육기간]", "【교육기간】", "교육기간:", "  (교육기간) ",
    ])
    def test_decorated_labels_are_recognised(self, decorated):
        html = (
            '<div class="board_view"><table><tr><td>'
            f"접수기간 2026.09.01부터<br>{decorated} 2026.09.20~2026.09.30"
            "</td></tr></table></div>"
        )
        quotes = extract_quotes(normalize_text(html))
        assert period_from_quote(quotes[QUOTE_DEADLINE], today=TODAY)[1] is None


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
