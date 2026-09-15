"""상세 근거 기간 추출기 (P2-D) - 목록·제목은 마감을 만들지 못한다.

세 소스(forest_service·kofpi·socialenterprise)는 목록에 마감이 없다. 마감은
상세 본문의 기간 라벨 **한 자리**에서만 읽는다. 이 파일은 그 한 자리가
무엇이고 무엇이 아닌지를 **실제 페이지 본문**(``tests/fixtures/periods/``,
2026-09-15 수집, ``MANIFEST.json`` 에 제목·URL·라벨 수)으로 고정한다.

거절 케이스가 통과 케이스보다 중요하다: 열세 차례의 게이트에서 환각 마감은
언제나 "옆의 날짜를 접수기간으로 읽는" 모양으로 들어왔다.

라운드 2(Codex 게이트)가 막은 것:
  ① 각주·연장·회차 (``※ 2026. 9. 30.까지 연장`` 이 9-16 을 만들던 자리)
  ② 잘린 창 / 창 밖의 두 번째 라벨
  ③ 요청 상한에 걸린 항목의 마감이 지워지던 자리
"""
import json
from pathlib import Path

import pytest

from alert.crawlers.period_detail import (
    DETAIL_LABEL_COUNT_KEY,
    DETAIL_TEXT_FETCHED_AT_KEY,
    DETAIL_TEXT_KEY,
    DETAIL_TEXT_TRUNCATED_KEY,
    EVIDENCE_FIELDS,
    MAX_PERIOD_DETAIL_REQUESTS,
    PERIOD_DETAIL_SOURCES,
    DetailQuota,
    attach_detail_text,
    carry_forward,
    fetch_order,
    wants_period_detail,
)
from alert.crawlers.period_extractors import (
    DETAIL_LABEL_COUNT_FIELD,
    DETAIL_TEXT_FIELD,
    DETAIL_TEXT_TRUNCATED_FIELD,
    EVIDENCE_KEYS,
    PERIOD_EXTRACTORS,
    REASON_AMBIGUOUS_LABEL,
    REASON_CONFLICT,
    REASON_NO_EVIDENCE,
    REASON_SHAPE,
    REASON_TRUNCATED,
    count_period_labels,
    detail_period_reason,
    forest_service_period,
    kofpi_period,
    socialenterprise_period,
)
from alert.db import Database
from alert.main import _finalize_periods, _periods_from_raw
from alert.models import RawAnnouncement
from alert.utils.http_fetch import DetailText

FIXTURES = Path(__file__).parent / "fixtures" / "periods"
MANIFEST = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))

EXTRACTORS = (forest_service_period, kofpi_period, socialenterprise_period)


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}_detail.txt").read_text(encoding="utf-8")


def real(name: str) -> dict:
    """픽스처 그대로의 근거 - 라벨 수·잘림 여부는 **수집 당시 실측값**이다."""
    meta = MANIFEST[name]
    return {
        DETAIL_TEXT_FIELD: fixture(name),
        DETAIL_TEXT_TRUNCATED_FIELD: meta["truncated"],
        DETAIL_LABEL_COUNT_FIELD: meta["detail_label_count"],
    }


def evidence(text: str, truncated: bool = False, label_count=None) -> dict:
    """합성 근거. 라벨 수를 안 주면 본문 전체 = 창으로 본다."""
    return {
        DETAIL_TEXT_FIELD: text,
        DETAIL_TEXT_TRUNCATED_FIELD: truncated,
        DETAIL_LABEL_COUNT_FIELD: (
            count_period_labels(text) if label_count is None else label_count
        ),
    }


def item(source: str, raw: dict, url: str = "https://example.test/1",
         source_id: str = "1", title: str = "공고"):
    return RawAnnouncement(
        source=source, source_id=source_id, title=title, url=url,
        raw_data=json.dumps(raw, ensure_ascii=False),
    )


def ok_fetch(text: str = "ㅁ 접수기간 2026. 9. 1. ~ 9. 30. ㅁ 끝"):
    return lambda url, **kwargs: DetailText(text)


# ---------------------------------------------------------------------------
# 등록
# ---------------------------------------------------------------------------

class TestRegistry:

    def test_registry_is_the_declared_five(self):
        assert set(PERIOD_EXTRACTORS) == {
            "seis", "lawmaking", "forest_service", "kofpi", "socialenterprise",
        }

    def test_detail_sources_all_have_an_extractor(self):
        assert PERIOD_DETAIL_SOURCES <= set(PERIOD_EXTRACTORS)

    def test_evidence_is_declared_as_one_atomic_group(self):
        """근거 넷이 한 묶음이어야 재수집이 옛 근거를 남기지 않는다 (LOW ④)."""
        for source in PERIOD_DETAIL_SOURCES:
            assert tuple(EVIDENCE_KEYS[source]) == EVIDENCE_FIELDS
        assert DETAIL_TEXT_FETCHED_AT_KEY in EVIDENCE_FIELDS
        assert DETAIL_LABEL_COUNT_KEY in EVIDENCE_FIELDS

    @pytest.mark.parametrize("source", ["coop", "fowi", "mafra"])
    def test_sources_without_a_deterministic_deadline_stay_out(self, source):
        assert source not in PERIOD_EXTRACTORS
        assert source not in PERIOD_DETAIL_SOURCES


# ---------------------------------------------------------------------------
# 통과 - 실제 페이지 본문
# ---------------------------------------------------------------------------

class TestRealPagesThatDoCarryADeadline:

    def test_forest_service_reads_the_reception_period(self):
        """``ㅇ 접수기간 : 2026. 9. 7. ~ 9. 28. 18:00까지``"""
        assert "접수기간 : 2026. 9. 7. ~ 9. 28. 18:00까지" in fixture("forest_service")
        assert forest_service_period(real("forest_service")) == (
            "2026-09-07", "2026-09-28"
        )

    def test_kofpi_reads_the_recruitment_period(self):
        """``ㅁ 모집기간 2026. 9. 7.(월) ~ 9. 30.(수) 15:00까지``"""
        assert "모집기간 2026. 9. 7.(월) ~ 9. 30.(수) 15:00까지" in fixture("kofpi")
        assert kofpi_period(real("kofpi")) == ("2026-09-07", "2026-09-30")

    def test_socialenterprise_reads_the_end_only(self):
        """``□ (공모기간) 공고일 ~ 2026. 9. 28.(월) 13:00까지``

        "공고일" 은 날짜가 아니다 - 시작일은 만들지 않는다.
        """
        assert "(공모기간) 공고일 ~ 2026. 9. 28.(월) 13:00까지" in fixture(
            "socialenterprise")
        assert socialenterprise_period(real("socialenterprise")) == (
            None, "2026-09-28"
        )

    def test_socialenterprise_single_deadline_with_kkaji(self):
        """``□ 모집기간 : 2026. 9. 14.(월)까지 첨부파일 …``

        날짜가 하나일 때는 ``까지`` 가 **필수**이고, 값은 다음 절
        (``첨부파일``)에서 끝난다.
        """
        assert "모집기간 : 2026. 9. 14.(월)까지" in fixture(
            "socialenterprise_deadline_only")
        assert socialenterprise_period(real("socialenterprise_deadline_only")) == (
            None, "2026-09-14"
        )

    def test_kofpi_value_ends_at_the_next_section(self):
        """``신청기간: ’26. 8. 10.(월) ~ 9. 11.(금) 사. 담당기관 …``"""
        text = fixture("kofpi_section_boundary")
        assert "신청기간: '26. 8. 10.(월) ~ 9. 11.(금) 사. 담당기관" in text
        assert kofpi_period(real("kofpi_section_boundary")) == (
            "2026-08-10", "2026-09-11"
        )

    @pytest.mark.parametrize("name,suffix", [
        ("kofpi", "9.30"),
        ("kofpi_section_boundary", "9.11"),
        ("socialenterprise", "9.28"),
        ("socialenterprise_deadline_only", "9/14"),
    ])
    def test_detail_deadline_agrees_with_the_title_the_site_wrote(
        self, name, suffix
    ):
        """상세에서 읽은 마감이 사이트가 **제목에** 써 둔 날짜와 같다.

        제목은 근거로 쓰지 않지만(연도가 없다), 교차 확인에는 쓸 수 있다.
        """
        meta = MANIFEST[name]
        assert suffix in meta["title"], meta["title"]
        _start, end = PERIOD_EXTRACTORS[meta["source"]](real(name))
        month, day = suffix.replace("/", ".").split(".")
        assert end == f"2026-{int(month):02d}-{int(day):02d}"

    def test_the_choke_point_produces_the_same_answer(self):
        """관문과 재검증이 같은 함수를 쓴다 - 답이 갈리면 안 된다."""
        built = _finalize_periods("kofpi", item("kofpi", real("kofpi")))
        assert (built.period_start, built.period_end) == (
            "2026-09-07", "2026-09-30"
        )
        assert _periods_from_raw("kofpi", built.raw_data) == (
            "2026-09-07", "2026-09-30"
        )

    def test_a_planted_period_is_still_reset_before_the_extractor_runs(self):
        for source in sorted(PERIOD_DETAIL_SOURCES):
            planted = item(source, {})
            planted.period_start, planted.period_end = "2099-01-01", "2099-12-31"
            _finalize_periods(source, planted)
            assert (planted.period_start, planted.period_end) == (None, None)


# ---------------------------------------------------------------------------
# 라운드 2 HIGH ① - 각주·연장·회차는 경계가 아니라 충돌이다
# ---------------------------------------------------------------------------

CODEX_CONFLICTS = [
    ("연장 각주", "ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. ※ 2026. 9. 30.까지 연장 ㅁ 끝"),
    ("종료일 폐지", "ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. ※ 종료일 폐지, 이후 상시 접수 ㅁ 끝"),
    ("별표 연장", "ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. * 9. 30. 까지 연장 ㅁ 끝"),
    ("변경 공지", "ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. 변경 ㅁ 끝"),
    ("정정 공지", "ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. 정정 ㅁ 끝"),
    ("철회", "ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. 철회 ㅁ 끝"),
    ("상시 전환", "ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. 이후 상시 ㅁ 끝"),
    ("회차 표", "ㅁ 접수기간 제1회 2026. 9. 1. ~ 9. 16. 제2회 2026. 10. 1. ~ 10. 16. ㅁ 끝"),
    ("회차 낱말", "ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. 회차별 상이 ㅁ 끝"),
    ("차수 낱말", "ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. 차수별 상이 ㅁ 끝"),
    ("두 번째 범위", "ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. 2026. 10. 1. ~ 10. 16. ㅁ 끝"),
    ("표 같은 날짜 나열", "ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. 2026. 9. 20. 2026. 9. 25. ㅁ 끝"),
]


class TestFootnotesAndExtensionsAreConflicts:
    """값 뒤에 남는 것이 있으면 **근거가 스스로 충돌한다** → 거절."""

    @pytest.mark.parametrize(
        "label,text", CODEX_CONFLICTS, ids=[n for n, _ in CODEX_CONFLICTS]
    )
    @pytest.mark.parametrize(
        "extractor", EXTRACTORS,
        ids=["forest_service", "kofpi", "socialenterprise"])
    def test_conflict_is_rejected(self, extractor, label, text):
        assert extractor(evidence(text)) == (None, None), label

    @pytest.mark.parametrize(
        "label,text", CODEX_CONFLICTS, ids=[n for n, _ in CODEX_CONFLICTS]
    )
    def test_conflict_reason_is_reported(self, label, text):
        assert detail_period_reason(evidence(text)) in (
            REASON_CONFLICT, REASON_SHAPE
        ), label

    def test_the_extension_string_would_have_given_the_stale_date(self):
        """Codex 가 지목한 자리: 이 문자열이 예전에는 09-16 을 만들었다."""
        text = "ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. ※ 2026. 9. 30.까지 연장 ㅁ 끝"
        assert "9. 16." in text and "9. 30." in text
        assert kofpi_period(evidence(text)) == (None, None)
        assert detail_period_reason(evidence(text)) == REASON_CONFLICT

    def test_real_page_footnote_is_a_conflict(self):
        """실제 kofpi 본문: ``… 18:00까지 * 제출 마감기한 엄수``"""
        text = fixture("kofpi_footnote_extension")
        assert "18:00까지" in text and "*" in text
        assert kofpi_period(real("kofpi_footnote_extension")) == (None, None)
        assert detail_period_reason(real("kofpi_footnote_extension")) == (
            REASON_CONFLICT
        )

    def test_real_page_always_open_note_is_a_conflict(self):
        """실제 kofpi 본문: ``~ 12. 31.(금) * 연중 상시 접수 * 예산 소진 시 …``

        라운드 1 은 12-31 을 썼다(기존 인용 경로의 "명시 날짜 우선" 규칙).
        라운드 2 는 **거절**한다 - "연중 상시" 와 12-31 은 같은 필드 안에서
        서로 다른 말을 하고 있고, 어느 쪽이 참인지 페이지가 말하지 않는다.
        """
        text = fixture("kofpi_always_open")
        assert "12. 31.(금)" in text and "연중 상시 접수" in text
        assert kofpi_period(real("kofpi_always_open")) == (None, None)
        assert detail_period_reason(real("kofpi_always_open")) == REASON_CONFLICT

    def test_a_clean_single_range_still_passes(self):
        assert kofpi_period(
            evidence("ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. ㅁ 끝")
        ) == ("2026-09-01", "2026-09-16")

    def test_a_harmless_trailing_word_still_passes(self):
        """숫자도 표시도 없는 말은 충돌이 아니다."""
        assert kofpi_period(
            evidence("ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. 마감 ㅁ 끝")
        ) == ("2026-09-01", "2026-09-16")


# ---------------------------------------------------------------------------
# 라운드 2 HIGH ② - 잘린 근거와 창 밖의 라벨
# ---------------------------------------------------------------------------

class TestTruncatedEvidenceIsNeverRead:

    def test_truncated_evidence_is_rejected_even_when_the_shape_is_clean(self):
        """정정이 창 밖에 있을 수 있다 - 잘렸으면 아예 읽지 않는다."""
        text = "ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. ㅁ 끝"
        assert kofpi_period(evidence(text)) == ("2026-09-01", "2026-09-16")
        assert kofpi_period(evidence(text, truncated=True)) == (None, None)
        assert detail_period_reason(evidence(text, truncated=True)) == (
            REASON_TRUNCATED
        )

    def test_real_truncated_page_is_rejected(self):
        name = "kofpi_truncated"
        assert MANIFEST[name]["truncated"] is True
        assert kofpi_period(real(name)) == (None, None)
        assert detail_period_reason(real(name)) == REASON_TRUNCATED

    def test_a_second_label_outside_the_window_is_rejected(self):
        """창 안에는 라벨이 하나여도 본문 전체에 둘이면 읽지 않는다."""
        text = "ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. ㅁ 끝"
        assert kofpi_period(evidence(text, label_count=1)) == (
            "2026-09-01", "2026-09-16"
        )
        assert kofpi_period(evidence(text, label_count=2)) == (None, None)
        assert detail_period_reason(evidence(text, label_count=2)) == (
            REASON_AMBIGUOUS_LABEL
        )

    def test_real_duplicate_label_page_is_rejected(self):
        name = "kofpi_duplicate_label"
        assert MANIFEST[name]["detail_label_count"] == 2
        assert kofpi_period(real(name)) == (None, None)
        assert detail_period_reason(real(name)) == REASON_AMBIGUOUS_LABEL

    def test_a_missing_label_count_is_not_readable(self):
        """라벨 수를 모르면(옛 근거) 읽지 않는다 - fail-closed."""
        raw = {DETAIL_TEXT_FIELD: "ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. ㅁ 끝"}
        assert kofpi_period(raw) == (None, None)
        assert detail_period_reason(raw) == REASON_NO_EVIDENCE

    def test_a_boolean_is_not_a_label_count(self):
        raw = {DETAIL_TEXT_FIELD: "ㅁ 접수기간: 2026. 9. 1. ~ 9. 16. ㅁ 끝",
               DETAIL_LABEL_COUNT_FIELD: True}
        assert kofpi_period(raw) == (None, None)

    def test_count_period_labels_counts_the_full_text(self):
        assert count_period_labels("접수기간 … 모집기간 …") == 2
        assert count_period_labels("사업기간 … 운영기간 …") == 0
        assert count_period_labels("") == 0
        assert count_period_labels(None) == 0


# ---------------------------------------------------------------------------
# 거절 - 실제 페이지 본문
# ---------------------------------------------------------------------------

class TestRealPagesThatDoNotCarryADeadline:

    def test_coop_multi_row_table_is_never_a_deadline(self):
        """coop 상세는 마감이 **셋**이다 - 어느 것도 이 공고의 마감이 아니다.

        기존 인용 경로(``detail_quotes.resolve_quote_period``)는 이 본문에서
        마지막 행의 2026-09-15 를 마감으로 만들었다.
        """
        text = fixture("coop_full")
        assert "모집기간) 1~3회 컨설팅 회차별 상이" in text
        assert "’26. 8. 14.(금) 18:00까지" in text
        assert "’26. 9. 15.(화) 18:00까지" in text
        raw = {DETAIL_TEXT_FIELD: text, DETAIL_TEXT_TRUNCATED_FIELD: False,
               DETAIL_LABEL_COUNT_FIELD: MANIFEST["coop_full"]["detail_label_count"]}
        for extractor in EXTRACTORS:
            assert extractor(raw) == (None, None)

    def test_coop_window_is_truncated_and_has_two_labels(self):
        assert MANIFEST["coop"]["truncated"] is True
        assert MANIFEST["coop"]["detail_label_count"] == 2
        for extractor in EXTRACTORS:
            assert extractor(real("coop")) == (None, None)

    def test_mafra_notice_body_has_no_deadline(self):
        """입법예고 본문은 "붙임과 같이 입법예고합니다" 한 줄이다."""
        assert MANIFEST["mafra"]["detail_label_count"] == 0
        assert "입법예고" in fixture("mafra")
        for extractor in EXTRACTORS:
            assert extractor(real("mafra")) == (None, None)


# ---------------------------------------------------------------------------
# 거절 규칙 (합성 케이스)
# ---------------------------------------------------------------------------

REJECTED = [
    ("라벨 없음", "ㅁ 일정 2026. 9. 1. ~ 9. 30.까지 ㅁ 문의처"),
    ("다른 라벨의 날짜", "ㅁ 교육기간 2026. 9. 1. ~ 9. 30.까지 ㅁ 문의처"),
    ("앞에 한글이 붙은 라벨", "ㅁ 사업기간 2026. 9. 1. ~ 9. 30.까지 ㅁ 문의처"),
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
        "label,text", REJECTED, ids=[name for name, _ in REJECTED])
    @pytest.mark.parametrize(
        "extractor", EXTRACTORS,
        ids=["forest_service", "kofpi", "socialenterprise"])
    def test_rejected(self, extractor, label, text):
        assert extractor(evidence(text)) == (None, None), label

    def test_two_digit_year_with_apostrophe_is_read(self):
        """계약: 두 자리 연도 ``’26.`` 은 읽는다 (아포스트로피가 있을 때만)."""
        text = "ㅁ 접수기간 ’26. 9. 1. ~ ’26. 9. 30. 18:00까지 ㅁ 문의처"
        assert kofpi_period(evidence(text)) == ("2026-09-01", "2026-09-30")

    def test_no_evidence_key_means_no_period(self):
        for extractor in EXTRACTORS:
            assert extractor({}) == (None, None)
            assert extractor({DETAIL_TEXT_FIELD: None}) == (None, None)


class TestNothingIsFabricated:
    """근거 문자열에 없는 날짜는 period 에 들어갈 수 없다."""

    @pytest.mark.parametrize("name", [
        "forest_service", "kofpi", "kofpi_section_boundary",
        "socialenterprise", "socialenterprise_deadline_only",
    ])
    def test_every_produced_date_appears_in_the_evidence(self, name):
        """만든 날짜의 월·일은 근거에 **적혀 있어야** 하고, 연도는 근거에
        있는 연도(시작일의 연도 포함)여야 한다.
        """
        import re as _re

        text = fixture(name)
        start, end = PERIOD_EXTRACTORS[MANIFEST[name]["source"]](real(name))
        assert start or end, name
        years = set(_re.findall(r"\b(20\d{2})\b", text))
        years |= {f"20{yy}" for yy in
                  _re.findall(r"['’‘`]\s*(\d{2})\s*\.", text)}
        for iso in (start, end):
            if not iso:
                continue
            year, month, day = iso.split("-")
            assert year in years, (name, iso, sorted(years))
            assert _re.search(rf"{int(month)}\s*\.\s*{int(day)}\b", text), (name, iso)

    def test_a_date_only_in_the_title_never_becomes_a_period(self):
        """제목의 ``(~9.30)`` 은 근거가 아니다 (kofpi 폐기 패턴 재현)."""
        built = item("kofpi", {"title": "2026년 사업 공모(~9.30)",
                               "date": "2026-09-07"},
                     title="2026년 사업 공모(~9.30)")
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

    def test_it_stores_the_full_text_label_count(self):
        target = item("kofpi", {}, title="공고")
        attach_detail_text(
            target,
            fetch=ok_fetch("공고 ㅁ 접수기간 2026. 9. 1. ~ 9. 30. ㅁ 모집기간 2026. 10. 1. ~ 10. 5."),
        )
        payload = json.loads(target.raw_data)
        assert payload[DETAIL_LABEL_COUNT_KEY] == 2
        _finalize_periods("kofpi", target)
        assert (target.period_start, target.period_end) == (None, None)

    def test_failure_leaves_no_key(self):
        def fetch(url, **kwargs):
            raise RuntimeError("boom")

        target = item("kofpi", {"title": "x"})
        assert attach_detail_text(target, fetch=fetch) is False
        assert DETAIL_TEXT_KEY not in json.loads(target.raw_data)

    def test_empty_response_leaves_no_key(self):
        target = item("kofpi", {"title": "x"})
        assert attach_detail_text(
            target, fetch=lambda url, **kw: DetailText("")) is False
        assert DETAIL_TEXT_KEY not in json.loads(target.raw_data)

    def test_void_url_is_not_fetched(self):
        calls = []
        target = item("kofpi", {}, url="https://example.test/1#void")
        assert attach_detail_text(
            target, fetch=lambda url, **kw: calls.append(url)) is False
        assert calls == []

    def test_it_never_touches_the_period_fields(self):
        target = item("kofpi", {})
        target.period_start, target.period_end = "2099-01-01", "2099-12-31"
        attach_detail_text(target, fetch=ok_fetch())
        assert (target.period_start, target.period_end) == (
            "2099-01-01", "2099-12-31")
        _finalize_periods("kofpi", target)      # 기간은 관문이 정한다
        assert (target.period_start, target.period_end) == (
            "2026-09-01", "2026-09-30")

    def test_truncation_flag_and_timestamp_are_stored(self):
        target = item("kofpi", {})
        attach_detail_text(
            target, fetch=lambda url, **kw: DetailText("본문", truncated=True))
        payload = json.loads(target.raw_data)
        assert payload[DETAIL_TEXT_TRUNCATED_KEY] is True
        assert payload[DETAIL_TEXT_FETCHED_AT_KEY]

    def test_a_window_shorter_than_the_body_is_marked_truncated(self):
        target = item("kofpi", {}, title="머리말")
        attach_detail_text(
            target, fetch=ok_fetch("머리말 " + "가" * 500), limit=100)
        payload = json.loads(target.raw_data)
        assert payload[DETAIL_TEXT_TRUNCATED_KEY] is True

    def test_existing_list_fields_survive(self):
        target = item("kofpi", {"seq": "1", "title": "t"})
        attach_detail_text(target, fetch=ok_fetch())
        payload = json.loads(target.raw_data)
        assert payload["seq"] == "1" and payload["title"] == "t"

    def test_exhausted_budget_skips_the_request(self):
        class Spent:
            deadline = 0.0

            def exhausted(self):
                return True

        calls = []
        assert attach_detail_text(
            item("kofpi", {}), budget=Spent(),
            fetch=lambda url, **kw: calls.append(url)) is False
        assert calls == []

    def test_wants_period_detail(self):
        assert wants_period_detail("kofpi") is True
        assert wants_period_detail(" kofpi ") is True
        assert wants_period_detail("coop") is False
        assert wants_period_detail(None) is False


# ---------------------------------------------------------------------------
# 라운드 2 MEDIUM ③ - 상한·회전·보존
# ---------------------------------------------------------------------------

def _seeded(tmp_path, name: str = "t.db"):
    """근거와 마감이 이미 저장된 DB 한 행."""
    db = Database(db_path=tmp_path / name)
    seed = item("kofpi", {"seq": "7"}, source_id="7", url="https://x/7")
    attach_detail_text(seed, fetch=ok_fetch())
    _finalize_periods("kofpi", seed)
    db._conn.execute(
        "INSERT INTO announcements (source, source_id, title, url, raw_data,"
        " period_start, period_end, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ("kofpi", "7", seed.title, seed.url, seed.raw_data,
         seed.period_start, seed.period_end, "2026-09-14", "2026-09-14"))
    db._conn.commit()
    assert _stored_period(db, "7") == ("2026-09-01", "2026-09-30")
    return db


def _stored_period(db, source_id: str):
    row = db._conn.execute(
        "SELECT period_start, period_end FROM announcements"
        " WHERE source = 'kofpi' AND source_id = ?", (source_id,)).fetchone()
    return (row["period_start"] or None, row["period_end"] or None)


class TestQuotaCountsOnlyRealRequests:

    def test_quota_is_job_wide_and_counts_requests(self):
        quota = DetailQuota(limit=2)
        calls = []
        for index in range(4):
            attach_detail_text(
                item("kofpi", {}, source_id=str(index)),
                quota=quota, fetch=lambda url, **kw: calls.append(url) or DetailText("본문"))
        assert quota.used == 2 and len(calls) == 2

    def test_skipped_items_do_not_spend_the_quota(self):
        """중복·잘못된 URL·다른 소스는 상한을 쓰지 않는다."""
        quota = DetailQuota(limit=3)
        attach_detail_text(item("coop", {}), quota=quota, fetch=ok_fetch())
        attach_detail_text(item("kofpi", {}, url="https://x/1#void"),
                           quota=quota, fetch=ok_fetch())
        attach_detail_text(item("kofpi", {}, url=""), quota=quota, fetch=ok_fetch())
        assert quota.used == 0
        assert quota.remaining == 3

    def test_default_limit(self):
        assert DetailQuota().limit == MAX_PERIOD_DETAIL_REQUESTS


class TestRotation:

    def test_never_fetched_items_come_first(self):
        items = [item("kofpi", {}, source_id=str(n)) for n in range(3)]
        prior = {
            "0": {DETAIL_TEXT_FETCHED_AT_KEY: "2026-09-15T10:00:00"},
            "1": {DETAIL_TEXT_FETCHED_AT_KEY: "2026-09-14T10:00:00"},
        }
        order = [i.source_id for i in fetch_order(items, prior)]
        assert order == ["2", "1", "0"]

    def test_unfetchable_items_are_not_planned(self):
        items = [item("kofpi", {}, source_id="a", url="https://x/1#void"),
                 item("coop", {}, source_id="b"),
                 item("kofpi", {}, source_id="c")]
        assert [i.source_id for i in fetch_order(items, {})] == ["c"]

    def test_twenty_six_items_are_all_fetched_within_two_runs(self):
        """상한 25 · 항목 26 - 두 번째 실행에서 전부 한 번씩은 받는다."""
        seen = set()
        fetched_at = {}
        clock = {"n": 0}

        def run():
            items = [item("kofpi", {}, source_id=str(n)) for n in range(26)]
            prior = {sid: {DETAIL_TEXT_FETCHED_AT_KEY: at}
                     for sid, at in fetched_at.items()}
            quota = DetailQuota(limit=25)
            planned = {i.source_id for i in fetch_order(items, prior)[:quota.remaining]}
            for target in items:
                if target.source_id not in planned:
                    continue
                clock["n"] += 1
                stamp = f"2026-09-15T00:00:{clock['n']:02d}"
                if attach_detail_text(
                    target, quota=quota, fetch=ok_fetch(),
                    now=lambda s=stamp: __import__("datetime").datetime.fromisoformat(s),
                ):
                    seen.add(target.source_id)
                    fetched_at[target.source_id] = stamp

        run()
        assert len(seen) == 25
        run()
        assert len(seen) == 26, sorted(set(str(n) for n in range(26)) - seen)


class TestCarryForwardKeepsAStoredPeriod:

    def test_a_skipped_item_keeps_its_evidence_and_period(self):
        prior = {"7": {
            DETAIL_TEXT_KEY: "ㅁ 접수기간 2026. 9. 1. ~ 9. 30. ㅁ 끝",
            DETAIL_TEXT_TRUNCATED_KEY: False,
            DETAIL_LABEL_COUNT_KEY: 1,
            DETAIL_TEXT_FETCHED_AT_KEY: "2026-09-14T10:00:00",
        }}
        skipped = item("kofpi", {"seq": "7"}, source_id="7")
        assert carry_forward(skipped, prior) is True
        _finalize_periods("kofpi", skipped)
        assert (skipped.period_start, skipped.period_end) == (
            "2026-09-01", "2026-09-30")
        assert json.loads(skipped.raw_data)["seq"] == "7"

    def test_without_carry_forward_the_period_would_be_lost(self):
        """이것이 라운드 2 Codex MEDIUM ③ 이 지목한 자리다."""
        skipped = item("kofpi", {"seq": "7"}, source_id="7")
        _finalize_periods("kofpi", skipped)
        assert (skipped.period_start, skipped.period_end) == (None, None)

    def test_carry_forward_is_a_no_op_without_prior_evidence(self):
        target = item("kofpi", {"seq": "9"}, source_id="9")
        assert carry_forward(target, {}) is False
        assert json.loads(target.raw_data) == {"seq": "9"}

    def test_overwrite_periods_keeps_the_stored_period_when_carried(self, tmp_path):
        """DB 왕복 - 이것이 라운드 2 Codex MEDIUM ③ 의 회귀 테스트다.

        1회차: 근거를 받아 마감이 저장된다.
        2회차: 상한에 걸려 **받지 않는다**. ``carry_forward`` 가 옛 근거를 다시
        실어 주면 ``overwrite_periods`` 가 마감을 그대로 둔다. 안 실어 주면
        같은 호출이 마감을 **지운다**.
        """
        db = _seeded(tmp_path)

        # 받지 않은 채로 저장 경로를 지나면 마감이 지워진다
        naked = item("kofpi", {"seq": "7"}, source_id="7",
                     url="https://x/7")
        _finalize_periods("kofpi", naked)
        db.overwrite_periods(naked, EVIDENCE_KEYS["kofpi"])
        assert _stored_period(db, "7") == (None, None)

        # 옛 근거를 다시 실으면 그대로 남는다
        db = _seeded(tmp_path, name="t2.db")
        carried = item("kofpi", {"seq": "7"}, source_id="7", url="https://x/7")
        assert carry_forward(carried, db.get_detail_evidence("kofpi")) is True
        _finalize_periods("kofpi", carried)
        db.overwrite_periods(carried, EVIDENCE_KEYS["kofpi"])
        assert _stored_period(db, "7") == ("2026-09-01", "2026-09-30")


class TestGetDetailEvidence:

    def test_it_returns_only_detail_keys(self, tmp_path):
        db = Database(db_path=tmp_path / "t.db")
        db._conn.execute(
            "INSERT INTO announcements (source, source_id, title, url, raw_data,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("kofpi", "7", "t", "https://x/7", json.dumps({
                "seq": "7",
                DETAIL_TEXT_KEY: "본문",
                DETAIL_TEXT_TRUNCATED_KEY: False,
                DETAIL_LABEL_COUNT_KEY: 1,
                DETAIL_TEXT_FETCHED_AT_KEY: "2026-09-14T10:00:00",
            }), "2026-09-14", "2026-09-14"))
        db._conn.commit()
        found = db.get_detail_evidence("kofpi")
        assert set(found) == {"7"}
        assert found["7"][DETAIL_TEXT_KEY] == "본문"
        assert "seq" not in found["7"]

    def test_rows_without_evidence_are_absent(self, tmp_path):
        db = Database(db_path=tmp_path / "t.db")
        db._conn.execute(
            "INSERT INTO announcements (source, source_id, title, url, raw_data,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("kofpi", "8", "t", "https://x/8", json.dumps({"seq": "8"}),
             "2026-09-14", "2026-09-14"))
        db._conn.commit()
        assert db.get_detail_evidence("kofpi") == {}
