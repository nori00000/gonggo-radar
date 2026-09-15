"""상세 근거 기간 추출기 (P2-D) - 목록·제목은 마감을 만들지 못한다.

세 소스(forest_service·kofpi·socialenterprise)는 목록에 마감이 없다. 마감은
상세 본문의 기간 라벨 **한 자리**에서만 읽는다. 이 파일은 그 한 자리가
무엇이고 무엇이 아닌지를 **실제 페이지 본문**(``tests/fixtures/periods/``,
2026-09-15 소스당 라이브 GET 1회)으로 고정한다.

거절 케이스가 통과 케이스보다 중요하다: 열세 차례의 게이트에서 환각 마감은
언제나 "옆의 날짜를 접수기간으로 읽는" 모양으로 들어왔다.
"""
import json
from pathlib import Path

import pytest

from alert.crawlers.period_detail import (
    DETAIL_TEXT_FETCHED_AT_KEY,
    DETAIL_TEXT_KEY,
    DETAIL_TEXT_TRUNCATED_KEY,
    PERIOD_DETAIL_SOURCES,
    attach_detail_text,
    wants_period_detail,
)
from alert.crawlers.period_extractors import (
    DETAIL_TEXT_FIELD,
    DETAIL_TEXT_TRUNCATED_FIELD,
    EVIDENCE_KEYS,
    PERIOD_EXTRACTORS,
    forest_service_period,
    kofpi_period,
    socialenterprise_period,
)
from alert.main import _finalize_periods, _periods_from_raw
from alert.models import RawAnnouncement
from alert.utils.http_fetch import DetailText

FIXTURES = Path(__file__).parent / "fixtures" / "periods"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}_detail.txt").read_text(encoding="utf-8")


def evidence(text: str, truncated: bool = False) -> dict:
    return {DETAIL_TEXT_FIELD: text, DETAIL_TEXT_TRUNCATED_FIELD: truncated}


def item(source: str, raw: dict, url: str = "https://example.test/1"):
    return RawAnnouncement(
        source=source,
        source_id="1",
        title="공고",
        url=url,
        raw_data=json.dumps(raw, ensure_ascii=False),
    )


# ---------------------------------------------------------------------------
# 등록
# ---------------------------------------------------------------------------

class TestRegistry:
    """기간을 만들 수 있는 소스는 다섯이고, 전부 근거 키를 선언한다."""

    def test_registry_is_the_declared_five(self):
        assert set(PERIOD_EXTRACTORS) == {
            "seis", "lawmaking", "forest_service", "kofpi", "socialenterprise",
        }

    def test_detail_sources_all_have_an_extractor(self):
        assert PERIOD_DETAIL_SOURCES <= set(PERIOD_EXTRACTORS)

    def test_every_detail_source_declares_the_detail_evidence(self):
        for source in PERIOD_DETAIL_SOURCES:
            assert DETAIL_TEXT_FIELD in EVIDENCE_KEYS[source]
            assert DETAIL_TEXT_TRUNCATED_FIELD in EVIDENCE_KEYS[source]

    @pytest.mark.parametrize("source", ["coop", "fowi", "mafra"])
    def test_sources_without_a_deterministic_deadline_stay_out(self, source):
        """실측에서 마감이 결정론적으로 드러나지 않은 소스는 등록하지 않는다.

        coop  상세 ``모집기간`` 이 "회차별 상이" + 3행 표(마감 셋)
        fowi  호스트가 빈 응답을 돌려줘 근거를 받을 수 없다
        mafra 입법예고 본문에 마감이 없다(첨부 hwpx 안)
        """
        assert source not in PERIOD_EXTRACTORS
        assert source not in PERIOD_DETAIL_SOURCES


# ---------------------------------------------------------------------------
# 통과 - 실제 페이지 본문
# ---------------------------------------------------------------------------

class TestRealPagesThatDoCarryADeadline:

    def test_forest_service_reads_the_reception_period(self):
        """``ㅇ 접수기간 : 2026. 9. 7. ~ 9. 28. 18:00까지``"""
        text = fixture("forest_service")
        assert "접수기간 : 2026. 9. 7. ~ 9. 28. 18:00까지" in text
        assert forest_service_period(evidence(text)) == (
            "2026-09-07", "2026-09-28"
        )

    def test_kofpi_reads_the_recruitment_period(self):
        """``ㅁ 모집기간 2026. 9. 7.(월) ~ 9. 30.(수) 15:00까지``"""
        text = fixture("kofpi")
        assert "모집기간 2026. 9. 7.(월) ~ 9. 30.(수) 15:00까지" in text
        assert kofpi_period(evidence(text)) == ("2026-09-07", "2026-09-30")

    def test_socialenterprise_reads_the_end_only(self):
        """``□ (공모기간) 공고일 ~ 2026. 9. 28.(월) 13:00까지``

        "공고일" 은 날짜가 아니다 - 시작일은 만들지 않는다.
        """
        text = fixture("socialenterprise")
        assert "(공모기간) 공고일 ~ 2026. 9. 28.(월) 13:00까지" in text
        assert socialenterprise_period(evidence(text)) == (None, "2026-09-28")

    def test_kofpi_comma_before_the_clock(self):
        """``신청 기간 : 2026. 8. 27.(목) ~ 2026. 9. 11.(금), 18:00까지``

        쉼표는 같은 필드 안의 문장부호다 - 필드 경계가 아니다. 뽑힌 마감이
        사이트가 제목에 쓴 ``(~9.11)`` 과 같다.
        """
        text = fixture("kofpi_comma_clock")
        assert "신청 기간 : 2026. 8. 27.(목) ~ 2026. 9. 11.(금), 18:00까지" in text
        assert kofpi_period(evidence(text)) == ("2026-08-27", "2026-09-11")

    def test_socialenterprise_single_deadline_with_kkaji(self):
        """``□ 모집기간 : 2026. 9. 14.(월)까지 첨부파일 …``

        날짜가 하나일 때는 ``까지`` 가 **필수**다 - 그 말이 없으면 그 날짜가
        시작인지 끝인지 행사일인지 페이지가 말하지 않은 것이다.
        """
        text = fixture("socialenterprise_deadline_only")
        assert "모집기간 : 2026. 9. 14.(월)까지" in text
        assert socialenterprise_period(evidence(text)) == (None, "2026-09-14")

    def test_explicit_end_date_beats_the_always_open_note(self):
        """``신청기간: 2026. 8. 24.(월) ~ 12. 31.(금) * 연중 상시 접수``

        각주(``*``)는 값의 끝이다. **적힌 종료일이 "상시" 보다 우선한다** -
        ``detail_quotes.resolve_quote_period`` 가 Codex 재검토 #9 에서 정한
        규칙과 같다(조기마감은 마감이 있는 공고다).
        """
        text = fixture("kofpi_always_open")
        assert "신청기간: 2026. 8. 24.(월) ~ 12. 31.(금)" in text
        assert "연중 상시 접수" in text
        assert kofpi_period(evidence(text)) == ("2026-08-24", "2026-12-31")

    @pytest.mark.parametrize("name,suffix", [
        ("kofpi", "9.30"),
        ("kofpi_comma_clock", "9.11"),
        ("socialenterprise", "9.28"),
        ("socialenterprise_deadline_only", "9/14"),
    ])
    def test_detail_deadline_agrees_with_the_title_the_site_wrote(
        self, name, suffix
    ):
        """상세에서 읽은 마감이 사이트가 **제목에** 써 둔 날짜와 같다.

        제목은 근거로 쓰지 않지만(연도가 없다), 교차 확인에는 쓸 수 있다.
        """
        import json as _json
        manifest = _json.loads(
            (FIXTURES / "MANIFEST.json").read_text(encoding="utf-8")
        )
        title = manifest[name]["title"]
        assert suffix in title, title
        source = manifest[name].get("source") or name.split("_")[0]
        if source == "forest":
            source = "forest_service"
        _start, end = PERIOD_EXTRACTORS[source](evidence(fixture(name)))
        month, day = (suffix.replace("/", ".")).split(".")
        assert end == f"2026-{int(month):02d}-{int(day):02d}"

    def test_the_choke_point_produces_the_same_answer(self):
        """관문과 재검증이 같은 함수를 쓴다 - 답이 갈리면 안 된다."""
        raw = evidence(fixture("kofpi"))
        built = _finalize_periods("kofpi", item("kofpi", raw))
        assert (built.period_start, built.period_end) == (
            "2026-09-07", "2026-09-30"
        )
        assert _periods_from_raw("kofpi", built.raw_data) == (
            "2026-09-07", "2026-09-30"
        )

    def test_a_planted_period_is_still_reset_before_the_extractor_runs(self):
        """근거가 없으면 심은 기간은 사라진다 - 추출기가 생겨도 그대로다."""
        for source in sorted(PERIOD_DETAIL_SOURCES):
            planted = item(source, {})
            planted.period_start, planted.period_end = "2099-01-01", "2099-12-31"
            _finalize_periods(source, planted)
            assert (planted.period_start, planted.period_end) == (None, None)


# ---------------------------------------------------------------------------
# 거절 - 실제 페이지 본문
# ---------------------------------------------------------------------------

class TestRealPagesThatDoNotCarryADeadline:

    def test_coop_multi_row_table_is_never_a_deadline(self):
        """coop 상세는 마감이 **셋**이다 - 어느 것도 이 공고의 마감이 아니다.

        기존 인용 경로(``detail_quotes.resolve_quote_period``)는 이 본문에서
        마지막 행의 2026-09-15 를 마감으로 만들었다. 라벨이 둘이면 거절한다.
        """
        text = fixture("coop_full")
        assert "모집기간) 1~3회 컨설팅 회차별 상이" in text
        assert "’26. 8. 14.(금) 18:00까지" in text
        assert "’26. 9. 15.(화) 18:00까지" in text
        for extractor in (forest_service_period, kofpi_period,
                          socialenterprise_period):
            assert extractor(evidence(text)) == (None, None)

    def test_coop_window_has_no_period_label_at_all(self):
        text = fixture("coop")
        assert kofpi_period(evidence(text, truncated=True)) == (None, None)

    def test_mafra_notice_body_has_no_deadline(self):
        """입법예고 본문은 "붙임과 같이 입법예고합니다" 한 줄이다."""
        text = fixture("mafra")
        assert "입법예고" in text
        for extractor in (forest_service_period, kofpi_period,
                          socialenterprise_period):
            assert extractor(evidence(text)) == (None, None)


# ---------------------------------------------------------------------------
# 거절 규칙 (합성 케이스 - 실제 본문 모양을 최소로 재현)
# ---------------------------------------------------------------------------

REJECTED = [
    ("라벨 없음", "ㅁ 일정 2026. 9. 1. ~ 9. 30.까지 ㅁ 문의처"),
    ("다른 라벨의 날짜", "ㅁ 교육기간 2026. 9. 1. ~ 9. 30.까지 ㅁ 문의처"),
    ("행사 일시", "ㅁ 일시 ’26. 9. 30.(수) 14:00 ㅁ 장소 대전"),
    ("라벨이 둘", "ㅁ 접수기간 2026. 9. 1. ~ 9. 30. ㅁ 모집기간 2026. 10. 1. ~ 10. 5. ㅁ 끝"),
    ("범위가 아님", "ㅁ 접수기간 2026. 9. 30. ㅁ 문의처"),
    ("옆 항목이 붙음", "ㅁ 접수기간 2026. 9. 1. ~ 9. 30. 심사 2026. 10. 5. ㅁ 문의처"),
    ("달력에 없는 날", "ㅁ 접수기간 2027. 2. 1. ~ 2. 30. ㅁ 문의처"),
    ("뒤집힌 범위", "ㅁ 접수기간 2026. 9. 30. ~ 9. 1. ㅁ 문의처"),
    ("연도 없는 시작", "ㅁ 접수기간 9. 1. ~ 9. 30. ㅁ 문의처"),
    ("공고일 시작 + 연도 없는 끝", "ㅁ 접수기간 공고일 ~ 9. 30.까지 ㅁ 문의처"),
    ("아포스트로피 없는 두 자리", "ㅁ 접수기간 26. 9. 1. ~ 26. 9. 30. ㅁ 문의처"),
    ("토큰 경계 위반", "ㅁ 접수기간 2026. 9. 1. ~ 9. 300 ㅁ 문의처"),
    ("빈 근거", ""),
]


class TestRejectionRules:

    @pytest.mark.parametrize(
        "label,text", REJECTED, ids=[name for name, _ in REJECTED]
    )
    @pytest.mark.parametrize("extractor", [
        forest_service_period, kofpi_period, socialenterprise_period,
    ], ids=["forest_service", "kofpi", "socialenterprise"])
    def test_rejected(self, extractor, label, text):
        assert extractor(evidence(text)) == (None, None), label

    def test_two_digit_year_with_apostrophe_is_read(self):
        """계약: 두 자리 연도 ``’26.`` 은 읽는다 (아포스트로피가 있을 때만)."""
        text = "ㅁ 접수기간 ’26. 9. 1. ~ ’26. 9. 30. 18:00까지 ㅁ 문의처"
        assert kofpi_period(evidence(text)) == ("2026-09-01", "2026-09-30")

    def test_truncated_window_ending_in_the_match_is_rejected(self):
        """창이 잘렸고 매치가 창 끝에 닿으면 범위가 끝났는지 알 수 없다."""
        text = "ㅁ 접수기간 2026. 9. 1. ~ 9. 30."
        assert kofpi_period(evidence(text, truncated=False)) == (
            "2026-09-01", "2026-09-30"
        )
        assert kofpi_period(evidence(text, truncated=True)) == (None, None)

    def test_no_evidence_key_means_no_period(self):
        for extractor in (forest_service_period, kofpi_period,
                          socialenterprise_period):
            assert extractor({}) == (None, None)
            assert extractor({DETAIL_TEXT_FIELD: None}) == (None, None)


class TestNothingIsFabricated:
    """근거 문자열에 없는 날짜는 period 에 들어갈 수 없다."""

    @pytest.mark.parametrize("source", sorted(PERIOD_DETAIL_SOURCES))
    def test_every_produced_date_appears_in_the_evidence(self, source):
        """만든 날짜의 월·일은 근거에 **적혀 있어야** 하고, 연도는 근거에
        있는 연도(시작일의 연도 포함)여야 한다.

        종료일에 연도가 없을 때 시작일의 연도를 쓰는 것만 허용된다 - 그래서
        연·월·일이 나란히 붙어 있기를 요구하지는 않는다.
        """
        import re as _re

        text = fixture(source)
        start, end = PERIOD_EXTRACTORS[source](evidence(text))
        assert start or end, source
        years = set(_re.findall(r"\b(20\d{2})\b", text))
        years |= {f"20{yy}" for yy in _re.findall(r"['\u2019\u2018`]\s*(\d{2})\s*\.", text)}
        for iso in (start, end):
            if not iso:
                continue
            year, month, day = iso.split("-")
            assert year in years, (source, iso, sorted(years))
            written = rf"{int(month)}\s*\.\s*{int(day)}\b"
            assert _re.search(written, text), (source, iso)

    def test_a_date_only_in_the_title_never_becomes_a_period(self):
        """제목의 ``(~9.30)`` 은 근거가 아니다 (kofpi 폐기 패턴 재현)."""
        built = item("kofpi", {"title": "2026년 사업 공모(~9.30)",
                               "date": "2026-09-07"})
        built.title = "2026년 사업 공모(~9.30)"
        _finalize_periods("kofpi", built)
        assert (built.period_start, built.period_end) == (None, None)


# ---------------------------------------------------------------------------
# 수집기 - 기간을 만들지 않고 근거만 싣는다
# ---------------------------------------------------------------------------

class TestAttachDetailText:

    def test_only_declared_sources_are_fetched(self):
        calls = []

        def fetch(url, **kwargs):
            calls.append(url)
            return DetailText("ㅁ 접수기간 2026. 9. 1. ~ 9. 30. ㅁ 끝")

        assert attach_detail_text(item("coop", {}), fetch=fetch) is False
        assert attach_detail_text(item("kofpi", {}), fetch=fetch) is True
        assert calls == ["https://example.test/1"]

    def test_one_request_per_item(self):
        calls = []

        def fetch(url, **kwargs):
            calls.append(url)
            return DetailText("ㅁ 접수기간 2026. 9. 1. ~ 9. 30. ㅁ 끝")

        attach_detail_text(item("kofpi", {}), fetch=fetch)
        assert len(calls) == 1

    def test_failure_leaves_no_key(self):
        def fetch(url, **kwargs):
            raise RuntimeError("boom")

        target = item("kofpi", {"title": "x"})
        assert attach_detail_text(target, fetch=fetch) is False
        assert DETAIL_TEXT_KEY not in json.loads(target.raw_data)

    def test_empty_response_leaves_no_key(self):
        target = item("kofpi", {"title": "x"})
        assert attach_detail_text(
            target, fetch=lambda url, **kw: DetailText("")
        ) is False
        assert DETAIL_TEXT_KEY not in json.loads(target.raw_data)

    def test_void_url_is_not_fetched(self):
        calls = []
        target = item("kofpi", {}, url="https://example.test/1#void")
        assert attach_detail_text(
            target, fetch=lambda url, **kw: calls.append(url)
        ) is False
        assert calls == []

    def test_it_never_touches_the_period_fields(self):
        target = item("kofpi", {})
        target.period_start, target.period_end = "2099-01-01", "2099-12-31"
        attach_detail_text(
            target,
            fetch=lambda url, **kw: DetailText("ㅁ 접수기간 2026. 9. 1. ~ 9. 30. ㅁ 끝"),
        )
        assert (target.period_start, target.period_end) == (
            "2099-01-01", "2099-12-31"
        )
        # 기간은 관문이 정한다
        _finalize_periods("kofpi", target)
        assert (target.period_start, target.period_end) == (
            "2026-09-01", "2026-09-30"
        )

    def test_truncation_flag_is_stored(self):
        target = item("kofpi", {})
        attach_detail_text(
            target, fetch=lambda url, **kw: DetailText("본문", truncated=True)
        )
        payload = json.loads(target.raw_data)
        assert payload[DETAIL_TEXT_TRUNCATED_KEY] is True
        assert payload[DETAIL_TEXT_FETCHED_AT_KEY]

    def test_existing_list_fields_survive(self):
        target = item("kofpi", {"seq": "1", "title": "t"})
        attach_detail_text(target, fetch=lambda url, **kw: DetailText("본문"))
        payload = json.loads(target.raw_data)
        assert payload["seq"] == "1" and payload["title"] == "t"

    def test_exhausted_budget_skips_the_request(self):
        class Spent:
            deadline = 0.0

            def exhausted(self):
                return True

        calls = []
        assert attach_detail_text(
            item("kofpi", {}),
            budget=Spent(),
            fetch=lambda url, **kw: calls.append(url),
        ) is False
        assert calls == []

    def test_wants_period_detail(self):
        assert wants_period_detail("kofpi") is True
        assert wants_period_detail(" kofpi ") is True
        assert wants_period_detail("coop") is False
        assert wants_period_detail(None) is False
