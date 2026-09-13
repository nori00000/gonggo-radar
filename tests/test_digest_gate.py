"""텔레그램 승인 게이트: 상태 파일·미리보기 렌더·해설 치환·발송 기록 테스트 (계약 W10)."""

import hashlib
import json
import sqlite3
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

from alert.digest import blocks as blocks_mod
from alert.digest import checker as checker_mod
from alert.digest import composer as composer_mod
from alert.digest import prune as prune_mod
from alert.digest import sections as sections_mod
from alert.digest import state as state_mod
from alert.digest import preview as preview_mod
from alert.digest.checker import check_digest, markdown_sha256
from alert.digest.composer import KAKAO_CHUNK_SEPARATOR
from alert.utils.redact import redact
from scripts.apply_commentary import (
    apply_commentary,
    apply_headline,
    main as apply_commentary_main,
    text_error,
)
from scripts.recheck_digest import main as recheck_main
from scripts.send_digest import check_fail_closed, send_digest


SAMPLE_MD = """<!-- lane: Claude opus executor -->

# 📋 협의회 주간 정책브리핑 2026-W37 (9/7~9/13)

이번 주 한 줄: <!-- 상민 확정 필요 -->

## ✅ 신청하세요 (마감순)

<!-- item id=11 -->
[D-9] 사회적협동조합·사회적기업 공공조달 1:1 컨설팅 참여기업 모집 — 협동조합포털(기재부) · 대상: 사협·사회적기업 · 마감 9/22
  [원문](https://example.com/a)

<!-- item id=12 -->
[새 소식] 2026년도 제2차 산림형 예비사회적기업 지정 계획 공고 — 산림청 · 대상: 사회적기업·산림사업자 · 접수 9/7부터, 마감 원문 확인
  [원문](https://example.com/b)

<!-- item id=13 -->
[상시] 산양삼 등 산촌자원 활용 시제품 개발 지원 참여자 모집 — 한국임업진흥원 · 대상: 산림사업자 · 마감 연중 상시 모집
  [원문](https://example.com/c)

## 👀 알아두세요

<!-- item id=14 -->
산림재난방지법 시행령 일부개정령안 입법예고 — 국민참여입법센터(산림청 소관) · 대상: 산림사업자 · 의견 10/19까지
  [원문](https://example.com/d)

## 🤝 협의회에서

· (면담·건의·수렴 현황 — 이번 주 기록 없음)

<!-- 보류: 1. 국립새만금수목원, 지역민과 함께 만든다 | 섹션 판정 불명 | id=12 -->
"""

PASS_CHECK = {
    "items": [
        {"url": "https://example.com/a", "url_alive": True, "passed": True},
        {"url": "https://example.com/b", "url_alive": True, "passed": True},
        {"url": "https://example.com/c", "url_alive": True, "passed": True},
        {"url": "https://example.com/d", "url_alive": True, "passed": True},
    ],
    "dropped": [],
    # 계약 v2.1 발송본의 섹션·항목 수 (SAMPLE_MD 와 같은 값이어야 한다 —
    # 발송기는 `item_sections` 로 항목을 세고 그 수를 검증과 대조한다).
    "item_blocks": 4,
    "item_sections": ["✅ 신청하세요 (마감순)", "👀 알아두세요"],
    "commentary_sections": ["🤝 협의회에서", "🏢 회원사 소식"],
    "pass": True,
    "network_checked": True,
    "reason": "",
}


# ─── 미리보기 파싱·렌더 ──────────────────────────────────────────────────
def test_parse_digest_numbers_items_in_document_order():
    parsed = preview_mod.parse_digest(SAMPLE_MD)
    assert [item["number"] for item in parsed["items"]] == [1, 2, 3, 4]
    assert [item["url"] for item in parsed["items"]] == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
        "https://example.com/d",
    ]
    assert parsed["items"][0]["label"] == "D-9"
    assert parsed["items"][0]["author"] == "협동조합포털(기재부)"
    assert parsed["items"][0]["target"] == "대상: 사협·사회적기업"
    assert parsed["items"][0]["deadline"] == "마감 9/22"
    assert parsed["items"][0]["section"] == "신청하세요"
    assert parsed["items"][3]["section"] == "알아두세요"
    assert parsed["items"][3]["label"] == ""
    assert parsed["items"][3]["deadline"] == "의견 10/19까지"
    assert parsed["items"][1]["deadline"] == "접수 9/7부터, 마감 원문 확인"
    assert parsed["period"] == "9/7~9/13"
    assert parsed["has_marker"] is True
    assert parsed["holds"] == [
        {
            "number": 1,
            "title": "국립새만금수목원, 지역민과 함께 만든다",
            "reason": "섹션 판정 불명",
            "id": 12,
        }
    ]


def test_item_urls_matches_parse_order():
    """번호 좌표의 정본: 경량 URL 추출과 전체 파서의 순서가 같아야 한다."""
    parsed = preview_mod.parse_digest(SAMPLE_MD)
    assert preview_mod.item_urls(SAMPLE_MD) == [
        item["url"] for item in parsed["items"]
    ]


def test_render_preview_has_header_numbers_status_usage():
    text = preview_mod.render_preview("2026-W37", SAMPLE_MD, PASS_CHECK)
    assert "협의회 주간 정책브리핑 2026-W37" in text
    assert "기간 9/7~9/13 · 검증 pass · 항목 4건" in text
    # 판정 ⑦: 미리보기는 발송본 그대로 (선정 이유를 따로 붙이지 않는다)
    assert (
        "2. [새 소식] 2026년도 제2차 산림형 예비사회적기업 지정 계획 공고 — "
        "산림청 · 대상: 사회적기업·산림사업자 · 접수 9/7부터, 마감 원문 확인"
    ) in text
    assert "■ ✅ 신청하세요 (마감순)" in text
    assert "보류 1건 (핀 n으로 승격)" in text
    assert "상태: 해설 대기" in text
    assert "제외 2,5" in text
    assert "핀 n" in text


def test_render_preview_marks_sendable_after_commentary():
    annotated, replaced = apply_headline(SAMPLE_MD, "이번 주 의견입니다.")
    assert replaced is True
    text = preview_mod.render_preview("2026-W37", annotated, PASS_CHECK)
    assert "상태: 발송 가능" in text
    assert "이번 주 의견입니다." in text


def test_render_preview_shows_check_failure():
    failing = dict(PASS_CHECK, **{"pass": False, "reason": "생존 항목 없음"})
    text = preview_mod.render_preview("2026-W37", SAMPLE_MD, failing)
    assert "검증 fail" in text
    assert "생존 항목 없음" in text


def test_chunk_text_respects_telegram_limit():
    long_text = "\n".join(f"{i}. " + "가" * 200 for i in range(1, 120))
    chunks = preview_mod.chunk_text(long_text)
    assert len(chunks) > 1
    assert all(len(chunk) <= preview_mod.TELEGRAM_LIMIT for chunk in chunks)
    assert "\n".join(chunks) == long_text


# ─── 해설 치환 ───────────────────────────────────────────────────────────
def test_apply_headline_overwrites_a_confirmed_headline():
    """멱등 덮어쓰기 — 이미 확정된 한 줄에 다시 적용하면 갈아 끼운다 (사이클 7).

    예전에는 마커가 없으면 종료 0 · no-op 이어서, 편집자가 "적용됐다"고 믿는
    사이 본문은 그대로였다 (Codex 3차 MEDIUM #3).
    """
    confirmed = SAMPLE_MD.replace(preview_mod.MARKER, "기존 한 줄")
    updated, replaced = apply_headline(confirmed, "새 한 줄")
    assert replaced is True
    assert "이번 주 한 줄: 새 한 줄" in updated
    assert "기존 한 줄" not in updated
    # 같은 문구를 다시 넣으면 바뀐 것이 없다 (덮어쓸 내용이 없음)
    again, changed = apply_headline(updated, "새 한 줄")
    assert changed is False and again == updated


def test_apply_commentary_writes_the_council_section():
    """`--commentary` 는 `## 🤝 협의회에서` 섹션 본문을 확정한다 (사이클 7).

    상단(이번 주 한 줄)과 **다른 자리**다 — 예전에는 위치 인자 하나가 상단에
    들어가고 의견은 비었다.
    """
    updated, changed = apply_commentary(SAMPLE_MD, "자격 요건 완화를 건의했습니다.")
    assert changed is True
    assert preview_mod.MARKER in updated          # 상단은 건드리지 않는다
    assert "· 자격 요건 완화를 건의했습니다." in updated
    parsed = preview_mod.parse_digest(updated)
    assert "자격 요건 완화를 건의했습니다." in parsed["commentary"]
    # 항목 계수는 그대로 (해설은 항목이 아니다)
    assert blocks_mod.item_block_count(updated) == 4

    # 덮어쓰기 (누적이 아니다)
    again, changed = apply_commentary(updated, "두 번째 확정 의견")
    assert changed is True
    assert "두 번째 확정 의견" in again
    assert "자격 요건 완화를 건의했습니다." not in again


def test_apply_commentary_creates_the_section_when_absent():
    """내용 없는 주차는 composer 가 섹션을 생략한다 — 그때는 만든다."""
    without = "\n".join(
        line for line in SAMPLE_MD.split("\n")
        if line not in ("## 🤝 협의회에서",
                        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)")
    )
    assert "## 🤝 협의회에서" not in without
    updated, changed = apply_commentary(without, "면담 완료")
    assert changed is True
    assert "## 🤝 협의회에서" in updated
    assert "· 면담 완료" in updated
    # 보류 주석보다 앞에 들어간다 (주석은 문서 끝 블록이어야 한다)
    assert updated.index("## 🤝 협의회에서") < updated.index("<!-- 보류:")


def test_apply_commentary_cli_requires_an_explicit_field(tmp_path, monkeypatch):
    """어느 자리를 확정할지 추론하지 않는다 (사이클 7)."""
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv", ["apply_commentary.py", "2026-W37", "--out-dir", str(tmp_path)]
    )
    from scripts.apply_commentary import main as commentary_main

    assert commentary_main() == 2
    assert md.read_text(encoding="utf-8") == SAMPLE_MD


def test_apply_commentary_cli_fills_both_fields(tmp_path, monkeypatch):
    """`--headline` 과 `--commentary` 는 서로 다른 자리를 채운다 (사이클 7)."""
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["apply_commentary.py", "2026-W37",
         "--headline", "이번 주는 인증 공고",
         "--commentary", "자격 요건 완화를 건의했습니다.",
         "--out-dir", str(tmp_path)],
    )
    from scripts.apply_commentary import main as commentary_main

    assert commentary_main() == 0
    body = md.read_text(encoding="utf-8")
    assert "이번 주 한 줄: 이번 주는 인증 공고" in body
    assert preview_mod.MARKER not in body
    assert "· 자격 요건 완화를 건의했습니다." in body
    parsed = preview_mod.parse_digest(body)
    assert parsed["headline"] == "이번 주는 인증 공고"
    assert "자격 요건 완화를 건의했습니다." in parsed["commentary"]
    # 카톡도 같은 본문에서 재생성된다
    kakao = (tmp_path / "2026-W37.kakao.txt").read_text(encoding="utf-8")
    assert "이번 주 한 줄: 이번 주는 인증 공고" in kakao
    assert "· 자격 요건 완화를 건의했습니다." in kakao


def test_apply_commentary_keeps_raw_text():
    """이스케이프는 발송 단계의 일이다 — 저장은 원문 그대로."""
    updated, replaced = apply_headline(SAMPLE_MD, "<b>강조</b> & 인용")
    assert replaced is True
    assert "<b>강조</b> & 인용" in updated


# ─── 상태 파일 ───────────────────────────────────────────────────────────
def test_state_path_derivation():
    assert state_mod.state_path("2026-W37", "digests") == Path(
        "digests/2026-W37.state.json"
    )
    assert state_mod.state_path_for_markdown(
        Path("digests/2026-W37.md")
    ) == Path("digests/2026-W37.state.json")
    assert state_mod.week_from_markdown("digests/2026-W37.md") == "2026-W37"


def test_load_state_missing_file_is_draft(tmp_path):
    state = state_mod.load_state(tmp_path / "2026-W37.state.json", "2026-W37")
    assert state["status"] == "draft"
    assert state["excluded_urls"] == []
    assert set(state) == set(state_mod.STATE_KEYS)


def test_load_state_corrupt_file_fails_closed(tmp_path):
    path = tmp_path / "2026-W37.state.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(state_mod.StateError):
        state_mod.load_state(path, "2026-W37")


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "2026-W37.state.json"
    state = state_mod.add_excluded_urls(
        state_mod.default_state("2026-W37"), ["https://example.com/b"]
    )
    state_mod.save_state(path, state)
    assert json.loads(path.read_text(encoding="utf-8"))["excluded_urls"] == [
        "https://example.com/b"
    ]
    assert state_mod.load_state(path, "2026-W37") == state


def test_add_excluded_urls_dedupes_and_preserves_order():
    state = state_mod.default_state("2026-W37")
    state = state_mod.add_excluded_urls(state, ["a", "b"])
    state = state_mod.add_excluded_urls(state, ["b", "c", ""])
    assert state["excluded_urls"] == ["a", "b", "c"]


def test_can_send_rejects_sent_state():
    sent = state_mod.mark_sent(
        state_mod.default_state("2026-W37"), 1401666801, 3, "2026-09-12T23:10:00"
    )
    assert sent["status"] == "sent"
    assert sent["recipients_count"] == 3
    assert sent["approved_by"] == 1401666801
    allowed, reason = state_mod.can_send(sent)
    assert allowed is False
    assert reason == "이미 발송됨"


def test_mark_held_does_not_override_sent():
    sent = state_mod.mark_sent(
        state_mod.default_state("2026-W37"), 1, 1, "2026-09-12T23:10:00"
    )
    assert state_mod.mark_held(sent)["status"] == "sent"
    assert state_mod.mark_held(state_mod.default_state("2026-W37"))["status"] == "held"


def test_record_preview_stores_message_ids():
    state = state_mod.record_preview(state_mod.default_state("2026-W37"), [11, 12])
    assert state["preview_message_ids"] == [11, 12]


# ─── 발송 게이트 ─────────────────────────────────────────────────────────
def _write_digest(tmp_path, markdown_text=None, check=None):
    """md + check.json. check 에 해시가 없으면 그 파일의 **원시 바이트** 해시를 채운다."""
    text = markdown_text or SAMPLE_MD
    md = tmp_path / "2026-W37.md"
    md.write_text(text, encoding="utf-8")
    payload = dict(check or PASS_CHECK)
    payload.setdefault("markdown_sha256", markdown_sha256(md.read_bytes()))
    # 통합 2 #1: 생존 판정의 나이 — 없으면 발송기가 "검증 만료" 로 막는다.
    payload.setdefault("checked_at", checker_mod._now_utc().isoformat())
    (tmp_path / "2026-W37.check.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    # 통합 1 #1: notify·발송기가 잠금 안에서 정본을 재대조한다 — 픽스처도 정본과
    # DB 를 함께 갖춰야 "검증만 흉내낸 본문" 이 아니게 된다.
    _bind(md)
    return md


class _OkNotifier:
    """SMTP 성공 스텁 (send_html_staged 호출 전 상태를 관찰할 수 있다)."""

    sender = "sender@example.com"
    password = "x"
    recipients = ["a@example.com", "b@example.com"]
    observer = None
    bodies = None

    def send_html_staged(self, subject, body, recipients):
        if self.observer is not None:
            self.observer()
        if self.bodies is not None:
            self.bodies.append(body)
        return True, "done"


def _approval_id(md):
    """현재 state 의 승인 세대 id (없으면 None)."""
    from pathlib import Path as _Path

    md = _Path(md)
    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md),
        state_mod.week_from_markdown(md),
    )
    return state_mod.approval_of(state).get("id")


def _db_for(md):
    """그 발송본에 결속된 대조용 DB (_bind 가 만든다)."""
    from pathlib import Path as _Path

    return str(_Path(md).parent / "bound.db")


def _send(md, **kwargs):
    """실발송 호출 — 현재 승인 세대 id 를 넘긴다 (사이클4 #2)."""
    kwargs.setdefault("approved_by", "1401666801")
    kwargs.setdefault("approval_id", _approval_id(md))
    kwargs.setdefault("db_path", _db_for(md))
    return send_digest(md, dry_run=False, **kwargs)


# ─── PR #1: 검증-본문 바이트 결속 (origin/main 도입 테스트) ──────────────
def test_check_hash_binds_valid_markdown_to_a_passed_gate(tmp_path):
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)

    assert check_fail_closed(md, md.with_suffix(".check.json")) == (True, "")


def test_send_gate_rejects_markdown_changed_after_check(tmp_path):
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    md.write_text(annotated + "\n사후 변경", encoding="utf-8")

    assert send_digest(md, dry_run=True, db_path=_db_for(md)) == 2


def test_send_gate_rejects_line_ending_change_after_check(tmp_path):
    """CRLF 로만 바뀐 본문도 거부 — 해시 기준이 원시 바이트이기 때문이다."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    md.write_bytes(annotated.replace("\n", "\r\n").encode("utf-8"))

    assert send_digest(md, dry_run=True, db_path=_db_for(md)) == 2


def test_send_gate_rejects_check_without_markdown_hash(tmp_path):
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    check_path = md.with_suffix(".check.json")
    check = json.loads(check_path.read_text(encoding="utf-8"))
    del check["markdown_sha256"]
    check_path.write_text(json.dumps(check), encoding="utf-8")

    assert send_digest(md, dry_run=True, db_path=_db_for(md)) == 2


def test_send_digest_refuses_already_sent_week(tmp_path, monkeypatch):
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    state_mod.save_state(
        state_mod.state_path_for_markdown(md),
        state_mod.mark_sent(
            state_mod.load_state(
                state_mod.state_path_for_markdown(md), "2026-W37"
            ), 1401666801, 2, "2026-09-12T23:00:00"
        ),
    )

    _forbid_notifier(monkeypatch, "이미 발송된 주차에서 EmailNotifier가 생성됐다")
    assert _send(md) == 2


def test_send_digest_records_state_on_success(tmp_path, monkeypatch):
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)

    class FakeNotifier:
        sender = "sender@example.com"
        password = "x"
        recipients = ["a@example.com", "b@example.com"]

        def send_html_staged(self, subject, body, recipients):
            assert recipients == self.recipients
            return True, "done"

    monkeypatch.setattr("scripts.send_digest.EmailNotifier", FakeNotifier)
    approval = _approval_id(md)
    assert send_digest(md, dry_run=False, approved_by="1401666801",
                       approval_id=approval, db_path=_db_for(md)) == 0

    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37"
    )
    assert state["status"] == "sent"
    assert state["approved_by"] == "1401666801"
    assert state["recipients_count"] == 2
    assert state["sent_at"]

    # 멱등: 같은 주차 두 번째 발송은 거부된다
    assert send_digest(md, dry_run=False, approved_by="1401666801",
                       approval_id=approval, db_path=_db_for(md)) == 2


def test_send_digest_dry_run_does_not_touch_state(tmp_path):
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    assert send_digest(md, dry_run=True, db_path=_db_for(md)) == 0
    assert not state_mod.state_path_for_markdown(md).exists()


def test_send_digest_still_refuses_unconfirmed_marker(
    tmp_path, monkeypatch, capsys
):
    """마커 게이트 불변 — **승인 세대까지 갖춘 상태**에서 마커만으로 거부된다.

    사이클5 #8: 이전 테스트는 승인 세대를 심지 않아 "승인 세대 없음" 으로 거부됐고,
    마커 검사를 지워도 통과했다. 이제 다른 조건을 전부 충족시켜 마커가 유일한
    거부 사유가 되게 한다.
    """
    md = _write_digest(tmp_path)            # 마커가 남은 본문
    _seed_preview(md)                       # 승인 세대·검증 결속까지 정상
    _forbid_notifier(monkeypatch, "마커 게이트가 뚫렸다")
    assert _send(md) == 2
    assert "미확정 마커" in capsys.readouterr().err


# ─── 재검증 (재조립 없이 check.json만 갱신) ─────────────────────────────
def test_recheck_digest_preserves_commentary(tmp_path, monkeypatch):
    """`/digest 재검토`의 뿌리: 손으로 채운 해설이 재검증으로 사라지지 않아야 한다."""
    annotated, _ = apply_headline(SAMPLE_MD, "손으로 확정한 의견")
    md = tmp_path / "2026-W37.md"
    md.write_text(annotated, encoding="utf-8")
    db = _bind(md)

    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    monkeypatch.setattr(
        "sys.argv", ["recheck_digest.py", str(md), "--db", str(db)]
    )
    assert recheck_main() == 0

    check = json.loads(
        (tmp_path / "2026-W37.check.json").read_text(encoding="utf-8")
    )
    assert check["pass"] is True
    assert check["markdown_sha256"] == hashlib.sha256(md.read_bytes()).hexdigest()
    assert len(check["items"]) == 4
    assert md.read_text(encoding="utf-8") == annotated
    assert preview_mod.MARKER not in md.read_text(encoding="utf-8")


# ─── 크리틱 #1: 중복 발송 (잠금 · sending 상태) ─────────────────────────
def _check_sha(md):
    """그 주차 check.json 의 **바이트** SHA (사이클5 #2 검증 결속)."""
    from pathlib import Path as _Path

    check = _Path(md).with_suffix(".check.json")
    return markdown_sha256(check.read_bytes()) if check.exists() else None


def _seed_preview(md, sha=None, check_sha=None):
    """미리보기를 보낸 것으로 기록 — 새 승인 세대를 발급한다 (사이클4 #2 · 5 #2)."""
    from pathlib import Path as _Path

    md = _Path(md)
    week = state_mod.week_from_markdown(md)
    path = state_mod.state_path_for_markdown(md)
    state = state_mod.load_state(path, week)
    state_mod.save_state(path, state_mod.record_preview(
        state, [2014], preview_mod.item_urls(md.read_text(encoding="utf-8")),
        sha or markdown_sha256(md.read_bytes()),
        check_sha or _check_sha(md),
    ))
    return path


def _annotated_digest(tmp_path):
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    return md


class GateBreached(BaseException):
    """게이트가 뚫려 SMTP 준비까지 갔다는 신호.

    `Exception` 이 아니라 `BaseException` 이어야 한다 — send_digest 의
    `except Exception` 이 AssertionError 를 "초기화 실패"로 삼켜 exit 2 를
    돌려주면, 게이트가 뚫려도 테스트가 통과한다(실제로 그랬다).
    """


def _forbid_notifier(monkeypatch, why):
    def boom(*args, **kwargs):  # pragma: no cover - 호출되면 게이트가 뚫린 것
        raise GateBreached(why)

    monkeypatch.setattr("scripts.send_digest.EmailNotifier", boom)


def test_send_digest_releases_lock_after_success(tmp_path, monkeypatch):
    """발송 후 flock 이 풀려 다음 프로세스가 잡을 수 있다 (파일은 남는다)."""
    md = _annotated_digest(tmp_path)
    monkeypatch.setattr("scripts.send_digest.EmailNotifier", _OkNotifier)
    assert _send(md) == 0
    lock = state_mod.lock_path_for_markdown(md)
    assert lock.exists()
    handle = state_mod.acquire_lock(lock)
    state_mod.release_lock(handle)


def test_send_digest_marks_sending_before_smtp(tmp_path, monkeypatch):
    """SMTP 실행 시점의 상태는 sending 이어야 한다 (기록은 발송보다 먼저)."""
    md = _annotated_digest(tmp_path)
    observed = {}

    class Notifier(_OkNotifier):
        observer = staticmethod(lambda: observed.update(
            state=state_mod.load_state(
                state_mod.state_path_for_markdown(md), "2026-W37"
            )
        ))

    monkeypatch.setattr("scripts.send_digest.EmailNotifier", Notifier)
    assert _send(md) == 0
    assert observed["state"]["status"] == "sending"
    assert observed["state"]["sending_at"]
    final = state_mod.load_state(state_mod.state_path_for_markdown(md), "2026-W37")
    assert final["status"] == "sent"


def test_send_digest_refuses_when_state_is_sending(tmp_path, monkeypatch):
    """sending 이 남아 있으면 자동 재발송하지 않는다."""
    md = _annotated_digest(tmp_path)
    state_path = state_mod.state_path_for_markdown(md)
    state_mod.save_state(state_path, state_mod.mark_sending(
        state_mod.load_state(state_path, "2026-W37"), "2026-09-12T23:00:00"))
    _forbid_notifier(monkeypatch, "sending 상태에서 발송을 시작했다")
    assert _send(md) == 2


def test_send_digest_state_save_failure_leaves_sending(tmp_path, monkeypatch):
    """SMTP 성공 후 기록 실패 → 재시도 → sending 잔존 → 이후 발송 거부."""
    md = _annotated_digest(tmp_path)
    calls = {"n": 0}
    real_save = state_mod.save_state

    def flaky_save(path, state):
        calls["n"] += 1
        if state.get("status") == "sent":
            raise OSError("디스크 쓰기 실패")
        return real_save(path, state)

    monkeypatch.setattr("scripts.send_digest.EmailNotifier", _OkNotifier)
    monkeypatch.setattr(state_mod, "save_state", flaky_save)
    monkeypatch.setattr("scripts.send_digest.STATE_SAVE_BACKOFF", 0)
    assert _send(md) == 1
    # 재시도했는가 (sending 1회 + sent 3회)
    assert calls["n"] == 1 + 3

    monkeypatch.setattr(state_mod, "save_state", real_save)
    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37"
    )
    assert state["status"] == "sending"
    _forbid_notifier(monkeypatch, "sending 잔존 상태에서 재발송했다")
    assert _send(md) == 2


@pytest.mark.parametrize(
    "smtp_mode,expected_rc,expected_status",
    [
        # 확정적 미발송(연결 전) → annotated 로 되돌려 재시도 허용 (사이클2 #4)
        ("connect-error", 2, "annotated"),
        ("starttls-error", 2, "annotated"),
        ("login-error", 2, "annotated"),
        # DATA 이후·불명 → 전달 여부 불확실 → sending 유지(수동 해제)
        ("smtp-error", 2, "sending"),
        ("ok", 0, "sent"),
    ],
)
def test_send_digest_smtp_matrix(
    tmp_path, monkeypatch, smtp_mode, expected_rc, expected_status
):
    """실제 EmailNotifier + smtplib 몽키패치 매트릭스 (단계별 실패 분류)."""
    import smtplib

    from alert.notifiers import email_sender

    md = _annotated_digest(tmp_path)
    monkeypatch.setenv("EMAIL_SENDER", "sender@example.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "app-password")
    monkeypatch.setenv("EMAIL_RECIPIENTS", "a@example.com,b@example.com")

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            if smtp_mode == "connect-error":
                raise OSError("연결 거부")

        def starttls(self):
            if smtp_mode == "starttls-error":
                raise smtplib.SMTPException("STARTTLS 실패")

        def login(self, sender, password):
            if smtp_mode == "login-error":
                raise smtplib.SMTPAuthenticationError(535, b"auth failed")

        def send_message(self, msg):
            if smtp_mode == "smtp-error":
                raise smtplib.SMTPException("550 거부")

        def quit(self):
            return None

    monkeypatch.setattr(email_sender.smtplib, "SMTP", FakeSMTP)
    assert _send(md) == expected_rc
    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37"
    )
    assert state["status"] == expected_status
    # flock 은 해제됐다 (파일은 남는다 — 지우면 배타성이 깨진다)
    released = state_mod.acquire_lock(state_mod.lock_path_for_markdown(md))
    state_mod.release_lock(released)
    if expected_status == "annotated":
        # 되돌려졌으니 같은 본문으로 재시도가 가능하다
        assert state["sending_at"] is None


def test_flock_is_exclusive(tmp_path):
    """flock 은 같은 프로세스의 다른 fd 에도 배타적이다 (사이클4 #1)."""
    path = tmp_path / "2026-W37.lock"
    handle = state_mod.acquire_lock(path)
    with pytest.raises(state_mod.LockBusy):
        state_mod.acquire_lock(path)
    assert state_mod.release_lock(handle) is True
    # 해제 후에는 다시 잡을 수 있다
    again = state_mod.acquire_lock(path)
    assert state_mod.release_lock(again) is True


def test_lock_file_is_never_deleted(tmp_path):
    """잠금 파일을 지우면 같은 경로의 새 inode 에 flock 이 걸려 배타성이 깨진다."""
    path = tmp_path / "2026-W37.lock"
    handle = state_mod.acquire_lock(path)
    assert path.exists()
    state_mod.release_lock(handle)
    assert path.exists()        # 해제해도 파일은 남는다


# ─── 크리틱 #3: 과거 검증 재사용 ─────────────────────────────────────────
def test_send_digest_refuses_stale_check_hash(tmp_path, monkeypatch):
    """검증 이후 본문이 바뀌면 (해시 불일치) 발송을 거부한다."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    md.write_text(annotated + "\n### 몰래 추가한 항목\n", encoding="utf-8")
    _forbid_notifier(monkeypatch, "해시 불일치인데 발송했다")
    assert _send(md) == 2


def test_send_digest_refuses_check_without_hash(tmp_path, monkeypatch):
    """본문 해시가 없는 check.json(구버전)도 거부 — 과거 검증일 수 있다."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(
        tmp_path, annotated, dict(PASS_CHECK, markdown_sha256=None)
    )
    _seed_preview(md)
    (tmp_path / "2026-W37.check.json").write_text(
        json.dumps(PASS_CHECK, ensure_ascii=False), encoding="utf-8"
    )
    _forbid_notifier(monkeypatch, "해시 없는 검증으로 발송했다")
    assert _send(md) == 2


_BIND_SOURCE = "kofpi"


def _classify_for_bind(title):
    """(대상 태그, 지역) — checker 가 DB 에서 재계산하는 것과 같은 입력·같은 함수."""
    classification = composer_mod.classify_item(title, "", _BIND_SOURCE)
    return classification.tags, classification.region


def _bind(markdown_path):
    """md 의 항목 블록에서 **항목 정본 파일과 DB** 를 만든다 (사이클 8 #1 픽스처).

    실제 경로에서는 compose 가 둘을 함께 쓴다. 테스트는 손으로 조립한 본문을 쓰므로,
    그 본문의 마커 id·URL·제목을 정본과 DB 에 심어 결속 사슬을 완성한다 —
    결속을 검사하는 테스트(`id=999` 위조 등)는 이 헬퍼를 쓰지 않는다.
    """
    markdown_path = Path(markdown_path)
    markdown_text = markdown_path.read_text(encoding="utf-8")
    db = markdown_path.parent / "bound.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS announcements "
        "(id INTEGER PRIMARY KEY, url TEXT, period_start TEXT,"
        " period_end TEXT, title TEXT, summary TEXT, source TEXT)"
    )
    entries = []
    block_lines = {
        str(block["item_id"]): block["lines"]
        for block in blocks_mod.parse_blocks(markdown_text)
        if block["kind"] == "item"
    }
    for block in blocks_mod.item_blocks(markdown_text):
        item_id = int(block["item_id"])
        lines = block_lines[str(item_id)]
        entries.append({
            "id": item_id,
            "url": block["url"],
            "title": block["fields"]["title"],
            # 정본의 section 은 **섹션 키**다 (md 의 헤딩이 아니라) — 사이클 9 #4
            "section": composer_mod.HEADING_TO_SECTION.get(
                block["section"], block["section"]
            ),
            "deadline_label": block["fields"]["label"],
            # 사이클 10 #2 + 11 #2: 렌더된 줄 그대로 + DB 기간(시작·마감)
            "line": lines[1],
            "origin_line": lines[2],
            "period_start": "2026-09-08",
            "period_end": "2026-12-31",
            # 사이클 13 #2: 표시 근거 — DB 행에서 재계산한 값과 같아야 한다
            "org": composer_mod.source_display_name(_BIND_SOURCE),
            "target": composer_mod.target_display(
                *_classify_for_bind(block["fields"]["title"])
            ),
            "region": _classify_for_bind(block["fields"]["title"])[1] or "",
        })
        conn.execute(
            "INSERT OR REPLACE INTO announcements"
            " (id, url, period_start, period_end, title, summary, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item_id, block["url"], "2026-09-08", "2026-12-31",
             block["fields"]["title"], "", _BIND_SOURCE),
        )
    conn.commit()
    conn.close()
    composer_mod.write_items_manifest(markdown_path, {
        "week": "2026-W37",
        "schema_version": composer_mod.ITEMS_SCHEMA_VERSION,
        "markdown_sha256": markdown_sha256(markdown_path.read_bytes()),
        "items": entries,
    })
    return db


def test_recheck_records_markdown_sha256(tmp_path, monkeypatch):
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = tmp_path / "2026-W37.md"
    md.write_text(annotated, encoding="utf-8")
    db = _bind(md)
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    monkeypatch.setattr("sys.argv", ["recheck_digest.py", str(md), "--db", str(db)])
    assert recheck_main() == 0
    check = json.loads((tmp_path / "2026-W37.check.json").read_text(encoding="utf-8"))
    assert check["markdown_sha256"] == markdown_sha256(md.read_bytes())


def test_recheck_failure_overwrites_check_as_fail(tmp_path, monkeypatch):
    """재검증 예외(DB 오류·타임아웃)는 과거 pass 를 무효화한다."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    check_path = tmp_path / "2026-W37.check.json"
    assert json.loads(check_path.read_text(encoding="utf-8"))["pass"] is True

    def boom(**kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr("scripts.recheck_digest.check_digest", boom)
    monkeypatch.setattr(
        "sys.argv", ["recheck_digest.py", str(md), "--db", "/no/such/dir/db"]
    )
    assert recheck_main() == 1
    check = json.loads(check_path.read_text(encoding="utf-8"))
    assert check["pass"] is False
    assert check["reason"].startswith("재검증 실패:")

    # 그 뒤의 발송은 거부된다
    _forbid_notifier(monkeypatch, "재검증 실패 상태에서 발송했다")
    assert _send(md) == 2


# ─── 크리틱 #2: 죽은 URL 본문 잔존 ──────────────────────────────────────
DEAD = "https://dead.invalid/x"


def _replace_item_title(markdown_text: str, url: str, new_title: str) -> str:
    """`url` 을 가진 v2.1 항목 블록의 제목 줄을 바꾼다 (픽스처 조립용).

    형식 v2.1 의 항목은 `### ` 머리글이 없다 — 제목 줄 다음 줄이 `  [원문](URL)` 이다.
    """
    lines = markdown_text.split("\n")
    for index, line in enumerate(lines):
        if line.strip() == f"[원문]({url})":
            lines[index - 1] = new_title
            return "\n".join(lines)
    raise AssertionError(f"항목 블록을 찾지 못했습니다: {url}")


def test_checker_fails_when_dead_url_stays_in_body(tmp_path, monkeypatch):
    """본문에 죽은 URL이 남아 있으면 살아 있는 항목이 있어도 pass=false."""
    md = tmp_path / "2026-W37.md"
    md.write_text(
        SAMPLE_MD.replace(preview_mod.MARKER, f"참고 [추가]({DEAD})"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive",
        lambda url, timeout=8: url != DEAD,
    )
    result = check_digest(db_path=str(_bind(md)), markdown_path=md)
    assert result["pass"] is False
    assert "죽은 URL" in result["reason"]
    assert [item["url"] for item in result["dropped"]] == [DEAD]


def test_recheck_removes_dead_item_block(tmp_path, monkeypatch):
    """죽은 항목은 본문에서 블록째 사라지고, 남은 항목으로 pass 한다."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = tmp_path / "2026-W37.md"
    md.write_text(annotated, encoding="utf-8")
    dead_item = "https://example.com/b"
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive",
        lambda url, timeout=8: url != dead_item,
    )
    monkeypatch.setattr(
        "sys.argv",
        ["recheck_digest.py", str(md), "--db", str(_bind(md))],
    )
    assert recheck_main() == 0
    body = md.read_text(encoding="utf-8")
    assert dead_item not in body
    assert "예비사회적기업 모집 공고" not in body
    assert "https://example.com/a" in body
    check = json.loads((tmp_path / "2026-W37.check.json").read_text(encoding="utf-8"))
    assert check["pass"] is True
    assert [item["url"] for item in check["dropped"]] == [dead_item]
    assert check["markdown_sha256"] == markdown_sha256(md.read_bytes())


def test_recheck_strips_dead_link_inside_commentary(tmp_path, monkeypatch):
    """해설(협의회 의견) 안의 죽은 링크도 검사·제거 대상이다."""
    annotated, _ = apply_headline(SAMPLE_MD, f"협의회 의견 [자료]({DEAD}) 참고")
    md = tmp_path / "2026-W37.md"
    md.write_text(annotated, encoding="utf-8")
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive",
        lambda url, timeout=8: url != DEAD,
    )
    monkeypatch.setattr(
        "sys.argv",
        ["recheck_digest.py", str(md), "--db", str(_bind(md))],
    )
    assert recheck_main() == 0
    body = md.read_text(encoding="utf-8")
    assert DEAD not in body
    assert "협의회 의견 자료 참고" in body
    check = json.loads((tmp_path / "2026-W37.check.json").read_text(encoding="utf-8"))
    assert check["pass"] is True
    assert [item["url"] for item in check["dropped"]] == [DEAD]


def test_recheck_fails_when_every_item_is_dead(tmp_path, monkeypatch):
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = tmp_path / "2026-W37.md"
    md.write_text(annotated, encoding="utf-8")
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: False
    )
    monkeypatch.setattr(
        "sys.argv",
        ["recheck_digest.py", str(md), "--db", str(_bind(md))],
    )
    assert recheck_main() == 1
    check = json.loads((tmp_path / "2026-W37.check.json").read_text(encoding="utf-8"))
    assert check["pass"] is False


def test_prune_marks_emptied_section():
    """항목이 전부 빠진 항목 섹션에는 composer 와 같은 빈 표시가 남는다."""
    text, removed, stripped = prune_mod.strip_dead_urls(
        SAMPLE_MD, ["https://example.com/d"]
    )
    assert [item["url"] for item in removed] == ["https://example.com/d"]
    assert stripped == []
    assert "산림재난방지법" not in text
    notice = text.split("## 👀 알아두세요", 1)[1].split("## ", 1)[0]
    assert prune_mod.EMPTY_SECTION_LINE in notice


def test_prune_is_noop_without_dead_urls():
    text, removed, stripped = prune_mod.strip_dead_urls(SAMPLE_MD, [])
    assert text == SAMPLE_MD and removed == [] and stripped == []


# ─── 크리틱 #4: 토큰 로그 유출 ──────────────────────────────────────────
FAKE_TOKEN = "123:TEST_SECRET"


def test_redact_hides_bot_token_in_url():
    message = (
        "HTTPSConnectionPool(host='api.telegram.org', port=443): "
        "Max retries exceeded with url: /bot123:TEST_SECRET/sendMessage"
    )
    cleaned = redact(message)
    assert "TEST_SECRET" not in cleaned
    assert "bot<redacted>" in cleaned


def test_redact_hides_bare_secret_when_known():
    assert "TEST_SECRET" not in redact(
        "login failed for 123:TEST_SECRET_LONG", secrets=("123:TEST_SECRET_LONG",)
    )


def test_notify_send_chunk_redacts_token(monkeypatch):
    """연결 오류 문자열에 토큰이 남아서는 안 된다."""
    import requests

    from scripts import notify_digest

    def boom(url, **kwargs):
        raise requests.exceptions.ConnectionError(
            f"Max retries exceeded with url: /bot{FAKE_TOKEN}/sendMessage"
        )

    monkeypatch.setattr(notify_digest.requests, "post", boom)
    ok, message_id, error = notify_digest.send_chunk(
        FAKE_TOKEN, -100, 1, "본문"
    )
    assert ok is False and message_id is None
    assert "TEST_SECRET" not in error
    assert "bot<redacted>" in error


# ─── 크리틱 #5: 미리보기별 항목 좌표 ────────────────────────────────────
def test_record_preview_keeps_item_urls_per_message():
    state = state_mod.record_preview(
        state_mod.default_state("2026-W37"), [11, 12], ["u1", "u2"]
    )
    assert state["preview_items"] == {"11": ["u1", "u2"], "12": ["u1", "u2"]}
    assert state_mod.preview_urls(state, 11) == ["u1", "u2"]
    assert state_mod.preview_urls(state) == ["u1", "u2"]

    newer = state_mod.record_preview(state, [21], ["u2", "u3"])
    assert state_mod.preview_urls(newer, 11) == ["u1", "u2"]
    assert state_mod.preview_urls(newer) == ["u2", "u3"]
    assert newer["preview_message_ids"] == [21]


def test_record_preview_clears_card_and_prunes_history():
    state = state_mod.record_preview(
        state_mod.default_state("2026-W37"), [1], ["u"], "a" * 64
    )
    state = state_mod.record_card(state, 555)
    assert state_mod.card_message_id(state) == 555
    state = state_mod.record_preview(state, [2], ["u"], "b" * 64)
    assert state_mod.card_message_id(state) is None
    for mid in range(3, 3 + state_mod.PREVIEW_ITEMS_MAX + 5):
        state = state_mod.record_preview(state, [mid], ["u"], "c" * 64)
    assert len(state["preview_items"]) == state_mod.PREVIEW_ITEMS_MAX


# ─── 크리틱 #7: 해설이 조용히 사라지는 문제 ─────────────────────────────
@pytest.mark.parametrize("bad", ["<!-- 중요한 협의회 의견", "의견 --> 끝"])
def test_apply_commentary_rejects_html_comment_tokens(tmp_path, monkeypatch, bad):
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    assert text_error(bad)
    monkeypatch.setattr(
        "sys.argv",
        ["apply_commentary.py", "2026-W37", "--headline", bad,
         "--out-dir", str(tmp_path)],
    )
    from scripts.apply_commentary import main as commentary_main

    assert commentary_main() == 2
    assert md.read_text(encoding="utf-8") == SAMPLE_MD


def test_text_error_allows_html_like_text():
    assert text_error("<b>강조</b> & 인용") == ""


# ─── 크리틱 #8: 생성 실패 후 이전 미리보기 재전송 ───────────────────────
JOB_FAKE_PYTHON = """#!/usr/bin/env bash
echo "$@" >> "$RECORD"
case "$1" in
  scripts/weekly_digest.py) exit "${GEN_EXIT:-0}" ;;
  scripts/notify_digest.py) exit 0 ;;
  -c) echo 2026-W37 ;;
esac
exit 0
"""


def _job_root(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "digests").mkdir()
    (root / "digests" / "2026-W37.md").write_text(SAMPLE_MD, encoding="utf-8")
    job = Path("scripts/digest_job.sh").resolve()
    (root / "scripts" / "digest_job.sh").write_text(
        job.read_text(encoding="utf-8"), encoding="utf-8"
    )
    python = root / "venv" / "bin" / "python"
    python.write_text(JOB_FAKE_PYTHON, encoding="utf-8")
    python.chmod(0o755)
    return root


def _run_job(root, gen_exit):
    import subprocess

    record = root / "calls.log"
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "digest_job.sh"), "2026-W37"],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "RECORD": str(record),
            "GEN_EXIT": str(gen_exit),
        },
    )
    calls = record.read_text(encoding="utf-8") if record.exists() else ""
    return proc, calls


def test_digest_job_skips_notify_when_generation_fails(tmp_path):
    """생성 실패 → notify 미실행(이전 판 재전송 금지) + 실패 종료 코드."""
    root = _job_root(tmp_path)
    proc, calls = _run_job(root, gen_exit=1)
    assert proc.returncode == 1
    assert "weekly_digest.py" in calls
    assert "notify_digest.py" not in calls
    assert "미리보기 전송 생략" in proc.stderr


def test_digest_job_runs_notify_when_generation_succeeds(tmp_path):
    root = _job_root(tmp_path)
    proc, calls = _run_job(root, gen_exit=0)
    assert proc.returncode == 0
    assert "notify_digest.py digests/2026-W37.md" in calls


def test_digest_job_keeps_strict_mode():
    assert "set -euo pipefail" in Path("scripts/digest_job.sh").read_text(
        encoding="utf-8"
    )


# ══ 사이클2: 상태 기계 단일화 (#1) ══════════════════════════════════════
def test_transition_table_is_the_gate():
    assert state_mod.can_transition("draft", "annotated")
    assert state_mod.can_transition("annotated", "sending")
    assert state_mod.can_transition("sending", "sent")
    assert state_mod.can_transition("draft", "held")
    assert state_mod.can_transition("annotated", "held")
    assert state_mod.can_transition("held", "draft")        # 재검토
    # sending 은 sent 로만 나아간다 — 되돌리기는 escape 두 경로뿐
    assert not state_mod.can_transition("sending", "held")
    assert not state_mod.can_transition("sending", "annotated")
    assert not state_mod.can_transition("sending", "draft")
    assert state_mod.can_transition("sending", "annotated", escape=True)
    assert state_mod.can_transition("sending", "draft", escape=True)
    # sent 는 불변
    for target in ("draft", "annotated", "sending", "held"):
        assert not state_mod.can_transition("sent", target)
        assert not state_mod.can_transition("sent", target, escape=True)
    # held 에서 바로 발송은 못 한다 (재검토를 거친다)
    assert not state_mod.can_transition("held", "sending")


def _sending_state():
    return state_mod.mark_sending(
        state_mod.default_state("2026-W37"), "2026-09-13T01:00:00"
    )


def test_mark_held_refuses_sending():
    """미확정 발송을 보류로 풀 수 없다 (Codex 신규 #2)."""
    with pytest.raises(state_mod.TransitionError):
        state_mod.mark_held(_sending_state())


def test_mark_annotated_refuses_sending_and_sent():
    for status in ("sending", "sent"):
        state = dict(state_mod.default_state("2026-W37"), status=status)
        with pytest.raises(state_mod.TransitionError):
            state_mod.mark_annotated(state, "새 의견")


def test_add_excluded_urls_refuses_sending_and_sent():
    for status in ("sending", "sent"):
        state = dict(state_mod.default_state("2026-W37"), status=status)
        with pytest.raises(state_mod.TransitionError):
            state_mod.add_excluded_urls(state, ["https://example.com/a"])


def test_record_preview_never_touches_status():
    """미리보기 기록은 어떤 상태에서도 status 를 바꾸지 않는다 (Codex 신규 #1)."""
    for status in STATUS_SAMPLES:
        state = dict(state_mod.default_state("2026-W37"), status=status)
        updated = state_mod.record_preview(state, [11], ["u1"])
        assert updated["status"] == status
        assert updated["preview_message_ids"] == [11]


STATUS_SAMPLES = ("draft", "annotated", "sending", "sent", "held")


def test_apply_state_rejects_forbidden_transition(tmp_path):
    path = tmp_path / "2026-W37.state.json"
    sending = _sending_state()
    state_mod.save_state(path, sending)
    with pytest.raises(state_mod.TransitionError):
        state_mod.apply_state(
            path, sending, dict(sending, status="held")
        )
    assert state_mod.load_state(path, "2026-W37")["status"] == "sending"


def test_release_sending_needs_escape(tmp_path):
    """`/digest 해제` 만 sending 을 푼다 — 일반 저장 경로로는 안 된다."""
    path = tmp_path / "2026-W37.state.json"
    sending = dict(_sending_state(), commentary="확정 의견",
                   approval={"id": "a" * 12, "sha": "b" * 64,
                             "card_message_id": 777})
    state_mod.save_state(path, sending)
    released = state_mod.release_sending(sending)
    # 사이클3 판정 + 사이클4 #2: draft 로 되돌리고 승인 세대를 폐기한다
    assert released["status"] == "draft"
    assert released["sending_at"] is None
    assert released["approval"] is None
    with pytest.raises(state_mod.TransitionError):
        state_mod.apply_state(path, sending, released)          # escape 없음
    state_mod.apply_state(path, sending, released, escape=True)  # 사람의 해제
    assert state_mod.load_state(path, "2026-W37")["status"] == "draft"

    with pytest.raises(state_mod.TransitionError):
        state_mod.release_sending(state_mod.default_state("2026-W37"))


def test_release_sending_blocks_send_until_new_preview(tmp_path, monkeypatch):
    """해제 후에는 새 미리보기 없이 발송할 수 없다 (사이클3 판정 #3)."""
    md = _annotated_digest(tmp_path)
    state_path = state_mod.state_path_for_markdown(md)
    sending = state_mod.mark_sending(
        state_mod.load_state(state_path, "2026-W37"), "2026-09-13T01:00:00"
    )
    state_mod.save_state(state_path, sending)
    state_mod.apply_state(
        state_path, sending, state_mod.release_sending(sending), escape=True
    )
    _forbid_notifier(monkeypatch, "해제 후 미리보기 없이 발송했다")
    assert _send(md) == 2

    # 새 미리보기가 지문을 다시 남기면 발송 가능
    _seed_preview(md)
    monkeypatch.setattr("scripts.send_digest.EmailNotifier", _OkNotifier)
    assert _send(md) == 0


def test_update_state_rereads_under_lock(tmp_path):
    """잠금을 쥔 뒤 다시 읽으므로, 그 사이의 남의 쓰기를 덮어쓰지 않는다."""
    path = tmp_path / "2026-W37.state.json"
    lock = tmp_path / "2026-W37.lock"
    state_mod.save_state(path, state_mod.default_state("2026-W37"))
    stale = state_mod.load_state(path, "2026-W37")       # 오래된 스냅샷(draft)
    state_mod.save_state(
        path, state_mod.mark_sent(stale, 1, 2, "2026-09-13T01:00:00")
    )
    seen = {}

    def mutate(current):
        seen["status"] = current["status"]
        return state_mod.record_preview(current, [99], ["u"])

    result = state_mod.update_state(path, lock, "2026-W37", mutate)
    assert seen["status"] == "sent"          # 스냅샷(draft)이 아니라 디스크 값
    assert result["status"] == "sent"
    assert result["preview_message_ids"] == [99]


def test_notify_preview_does_not_overwrite_sent(tmp_path, monkeypatch):
    """notify 도중 다른 발송이 sent 를 써도 최종 상태는 sent 다 (Codex 신규 #1)."""
    from scripts import notify_digest

    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    state_path = state_mod.state_path_for_markdown(md)
    state_mod.save_state(state_path, state_mod.default_state("2026-W37"))

    monkeypatch.setattr(
        notify_digest, "resolve_target", lambda topic_key="council": (-100, 2011)
    )
    monkeypatch.setattr(notify_digest, "resolve_token", lambda: "123:FAKE")

    def send_chunk(token, chat_id, thread_id, text):
        # 텔레그램 왕복 도중 다른 프로세스가 발송을 끝냈다
        current = state_mod.load_state(state_path, "2026-W37")
        if current["status"] != "sent":
            state_mod.save_state(
                state_path,
                state_mod.mark_sent(current, 42, 3, "2026-09-13T01:00:00"),
            )
        return True, 2014, ""

    monkeypatch.setattr(notify_digest, "send_chunk", send_chunk)
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 0

    state = state_mod.load_state(state_path, "2026-W37")
    assert state["status"] == "sent"                    # 덮어쓰이지 않았다
    # 통합 2 #2: 차단 안내의 message_id 는 미리보기 자리를 건드리지 않는다
    assert state["notice_message_ids"] == [2014]        # 기록은 됐다
    assert state["preview_message_ids"] == []           # 번호 좌표는 그대로
    assert state["recipients_count"] == 3


def test_apply_commentary_refuses_sending_week(tmp_path, monkeypatch):
    from scripts.apply_commentary import main as commentary_main

    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    state_mod.save_state(
        state_mod.state_path("2026-W37", tmp_path), _sending_state()
    )
    monkeypatch.setattr(
        "sys.argv",
        ["apply_commentary.py", "2026-W37", "--headline", "의견",
         "--out-dir", str(tmp_path)],
    )
    # 사이클3 #6: 거부는 **본문을 쓰기 전**에 일어난다
    assert commentary_main() == 2
    assert md.read_text(encoding="utf-8") == SAMPLE_MD
    assert preview_mod.MARKER in md.read_text(encoding="utf-8")
    assert state_mod.load_state(
        state_mod.state_path("2026-W37", tmp_path), "2026-W37"
    )["status"] == "sending"


# ══ 사이클2: TOCTOU (#2) ════════════════════════════════════════════════
def test_send_digest_requires_approval_id(tmp_path, monkeypatch):
    md = _annotated_digest(tmp_path)
    _forbid_notifier(monkeypatch, "승인 세대 없이 발송했다")
    assert send_digest(md, dry_run=False, approved_by="1") == 2


def test_send_digest_sends_the_text_it_gated(tmp_path, monkeypatch):
    """게이트 통과 직후 본문을 갈아치워도, 발송되는 건 게이트가 본 본문이다."""
    md = _annotated_digest(tmp_path)
    from scripts import send_digest as send_mod

    real_gate = send_mod._check_fail_closed_bytes
    captured = []

    def gate_then_swap(markdown_bytes, check_json_path):
        result = real_gate(markdown_bytes, check_json_path)
        md.write_bytes(
            markdown_bytes
            + "\n### 몰래 끼운 항목\n\n**원문:** [x](https://evil.example/x)\n".encode("utf-8"),
        )
        return result

    class Notifier(_OkNotifier):
        bodies = captured

    monkeypatch.setattr("scripts.send_digest._check_fail_closed_bytes", gate_then_swap)
    monkeypatch.setattr("scripts.send_digest.EmailNotifier", Notifier)
    assert _send(md) == 0
    assert captured and "evil.example" not in captured[0]
    assert "몰래 끼운" not in captured[0]


# ══ 사이클4 #1: flock — 프로세스 경계 실측 ═════════════════════════════
_HOLDER_SNIPPET = (
    "import sys, time\n"
    "sys.path.insert(0, {root!r})\n"
    "from alert.digest import state as st\n"
    "h = st.acquire_lock({lock!r})\n"
    "print('ACQUIRED', flush=True)\n"
    "time.sleep(60)\n"
)


def _spawn_holder(tmp_path, lock):
    """별도 **프로세스**로 잠금을 쥔다 (flock 은 프로세스 경계에서 증명된다)."""
    import subprocess
    from pathlib import Path as _Path

    root = str(_Path(__file__).resolve().parent.parent)
    script = tmp_path / "holder.py"
    script.write_text(
        _HOLDER_SNIPPET.format(root=root, lock=str(lock)), encoding="utf-8"
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout.readline().strip() == "ACQUIRED"
    return proc


def test_two_processes_only_one_acquires(tmp_path):
    """두 프로세스 동시 → 정확히 하나만 획득 (사이클4 #1 수용 기준)."""
    lock = tmp_path / "2026-W37.lock"
    holder = _spawn_holder(tmp_path, lock)
    try:
        with pytest.raises(state_mod.LockBusy):
            state_mod.acquire_lock(lock)
    finally:
        holder.kill()
        holder.wait(10)


def test_killed_holder_releases_lock_immediately(tmp_path):
    """획득자 kill -9 → 즉시 재획득 가능 (크래시 잠금 문제 소멸)."""
    import signal
    import time as _time

    lock = tmp_path / "2026-W37.lock"
    holder = _spawn_holder(tmp_path, lock)
    holder.send_signal(signal.SIGKILL)
    holder.wait(10)
    for _ in range(50):            # OS 가 fd 를 닫을 짧은 틈만 기다린다
        try:
            handle = state_mod.acquire_lock(lock)
        except state_mod.LockBusy:
            _time.sleep(0.02)
            continue
        state_mod.release_lock(handle)
        return
    pytest.fail("kill 후에도 잠금이 풀리지 않았다")


def test_send_digest_refuses_while_another_process_holds_lock(
    tmp_path, monkeypatch
):
    md = _annotated_digest(tmp_path)
    holder = _spawn_holder(tmp_path, state_mod.lock_path_for_markdown(md))
    try:
        _forbid_notifier(monkeypatch, "잠금이 있는데 발송을 시작했다")
        assert _send(md) == 2
    finally:
        holder.kill()
        holder.wait(10)


# ══ 사이클2: prune 범위·항목 수·중복 (#5 #6 #7) ═════════════════════════
COMMENTARY_WITH_BLOCK = SAMPLE_MD.replace(
    preview_mod.MARKER,
    "### 검토 의견\n\n**원문:** [자료](https://dead.invalid/opinion)\n\n"
    "우리 협의회는 이 사업의 자격 요건 완화를 건의했습니다.",
)


def test_prune_keeps_prose_section_blocks(tmp_path, monkeypatch):
    """협의회 의견 안의 `###` 블록은 링크만 떼고 문장을 남긴다 (Codex 신규 #5)."""
    text, removed, stripped = prune_mod.strip_dead_urls(
        COMMENTARY_WITH_BLOCK, ["https://dead.invalid/opinion"]
    )
    assert removed == []
    assert stripped == ["https://dead.invalid/opinion"]
    assert "### 검토 의견" in text
    assert "자격 요건 완화를 건의했습니다" in text
    assert "https://dead.invalid/opinion" not in text
    assert blocks_mod.item_block_count(text) == 4     # 공고 4건 그대로


def test_declared_sections_match_composed_digest(tmp_path):
    """선언 목록 == composer 가 실제로 쓰는 `##` 헤딩 (드리프트 방지, 사이클3 #7).

    composer 에 SECTION_HEADINGS·ITEM_SECTIONS 상수가 있으므로 정본은 계약 v2
    헤딩이다 (sections.from_composer). 빈 DB 에서는 항목 섹션 둘만 실린다 —
    협의회에서·회원사 소식은 내용이 없으면 섹션 자체를 생략한다(개정 v2.4 (c)).
    """
    from alert.digest.composer import compose_digest
    from tests.test_digest import _create_announcements_table

    db = tmp_path / "compose.db"
    _create_announcements_table(db)
    markdown = compose_digest(db_path=str(db), week_str="2026-W37")
    present = sections_mod.headings(markdown)
    item_sections, commentary_sections = sections_mod.declared()
    assert item_sections == sections_mod.V2_ITEM_SECTIONS
    assert [name for name in present if name in item_sections] == list(
        sections_mod.V2_ITEM_SECTIONS
    )
    # 본문에 등장한 헤딩 중 선언 목록에 없는 것은 없다 (= 조용한 오분류 없음)
    assert [name for name in present
            if name not in item_sections and name not in commentary_sections] == []


def test_sections_follow_composer_v2_constants(monkeypatch):
    """composer 의 SECTION_HEADINGS 가 정본이다 (지연 import 로 읽기만 한다)."""
    from alert.digest import composer

    monkeypatch.setattr(composer, "SECTION_HEADINGS", {
        "신청하세요": "✅ 신청하세요 (마감순)",
        "알아두세요": "👀 알아두세요",
        "협의회에서": "🤝 협의회에서",
    }, raising=False)
    monkeypatch.setattr(composer, "ITEM_SECTIONS",
                        ("신청하세요", "알아두세요"), raising=False)
    items, commentary = sections_mod.declared()
    # 병합 3회차: composer 목록이 **정본**이다. 합집합(계약 W10 사이클5)은 v1.2
    # 본문을 세기 위한 것이었는데, 항목 판정이 구조 마커로 바뀌면서 그 목적이
    # 사라졌고 "삭제된 이름이 계속 통하는" 부작용만 남았다.
    assert set(items) == {"✅ 신청하세요 (마감순)", "👀 알아두세요"}
    assert "🤝 협의회에서" in commentary
    assert not set(items) & set(commentary)


def test_item_section_headings_come_from_sections_module():
    """어떤 섹션이 항목 섹션인가는 sections.py 가 정본이다 (정확 일치).

    blocks.py 는 자기 목록을 갖지 않는다 — 부분 일치로 판정하던 `section_key` 는
    폐지됐다(사이클3 #7 + 병합 2회차: 중복 구현 제거).
    """
    assert blocks_mod.item_section_headings(SAMPLE_MD) == (
        "✅ 신청하세요 (마감순)", "👀 알아두세요",
    )
    # check.json 의 목록을 주면 그것이 정본이다
    assert blocks_mod.item_section_headings(
        SAMPLE_MD, ["👀 알아두세요"]
    ) == ("👀 알아두세요",)
    assert not hasattr(blocks_mod, "section_key")
    assert not hasattr(blocks_mod, "FALLBACK_ITEM_SECTION_KEYS")


def test_item_section_headings_follow_composer_rename(monkeypatch):
    """섹션 이름이 바뀌면 blocks 의 판정도 composer 를 따라간다.

    사이클 6 의 `blocks.section_key`(부분 일치) 를 sections.py(정확 일치)로
    합치면서도 "composer 가 정본" 이라는 성질은 그대로 지킨다.
    """
    from alert.digest import composer

    monkeypatch.setattr(composer, "SECTION_HEADINGS", {
        "산림": "산림 정책 동향",
        "지원사업": "지원사업 공고",
        "협의회에서": "협의회에서",
    }, raising=False)
    monkeypatch.setattr(composer, "ITEM_SECTIONS", ("산림", "지원사업"),
                        raising=False)

    renamed = SAMPLE_MD.replace(
        "## ✅ 신청하세요 (마감순)", "## 지원사업 공고"
    ).replace("## 👀 알아두세요", "## 산림 정책 동향")
    assert blocks_mod.item_section_headings(renamed) == (
        "지원사업 공고", "산림 정책 동향",
    )
    assert blocks_mod.item_block_count(renamed) == 4
    # 바뀐 이름을 쓰지 않는 본문은 항목 0건이 된다 (fail-closed, 조용한 오분류 없음)
    assert blocks_mod.item_block_count(SAMPLE_MD) == 0


def test_item_needs_the_structural_marker():
    """마커가 없으면 모양이 맞아도 항목이 아니다 (Codex 3차 HIGH #2 재현 입력).

    양방향으로 닫는다 — ①산문+원문 줄이 항목으로 세어지던 것 ②`*산림 제도 개정`
    같은 위조 항목이 항목 수에는 안 잡히면서 HTML 에는 링크로 실리던 것.
    """
    faked = SAMPLE_MD.replace("<!-- item id=11 -->\n", "")
    assert blocks_mod.item_block_count(faked) == 3
    # 마커를 잃은 줄은 "항목 섹션의 산문"으로 잡힌다
    offending = blocks_mod.prose_lines_in_item_sections(faked)
    assert any("공공조달 1:1 컨설팅" in line for line in offending)
    assert any("example.com/a" in line for line in offending)
    # 링크 감사: 본문 링크 4개 ≠ 항목 3 + 해설 0
    audit = blocks_mod.link_audit(faked)
    assert audit["total"] == 4
    assert audit["items"] == 3
    assert audit["commentary"] == 0
    assert audit["stray"] == 1


def test_prose_that_looks_like_an_item_is_not_an_item():
    """`자료를 참고해 주세요 — …` + 원문 줄은 공고가 아니다 (Codex 3차 #2)."""
    body = (
        "# 📋 협의회 주간 정책브리핑 2026-W37 (9/7~9/13)\n\n"
        f"이번 주 한 줄: {preview_mod.MARKER}\n\n"
        "## ✅ 신청하세요 (마감순)\n\n"
        "자료를 참고해 주세요 — 자세한 내용은 원문에 있습니다.\n"
        "  [원문](https://example.com/live)\n"
    )
    assert blocks_mod.item_block_count(body) == 0
    assert len(preview_mod.parse_digest(body)["items"]) == 0
    assert blocks_mod.prose_lines_in_item_sections(body)


def test_forged_item_line_is_caught_as_prose():
    """`*산림 제도 개정 — 산림청` + 원문 줄은 상한을 우회하지 못한다 (#2)."""
    forged = SAMPLE_MD.rstrip("\n") + (
        "\n\n*산림 제도 개정 — 산림청\n"
        "  [원문](https://example.com/forged)\n"
    )
    # 문서 끝(협의회에서 섹션 뒤)이 아니라 항목 섹션 안에 끼운 경우를 본다
    forged = SAMPLE_MD.replace(
        "## 👀 알아두세요\n",
        "## 👀 알아두세요\n\n*산림 제도 개정 — 산림청\n"
        "  [원문](https://example.com/forged)\n",
    )
    assert blocks_mod.item_block_count(forged) == 4      # 위조 항목은 안 세어진다
    offending = blocks_mod.prose_lines_in_item_sections(forged)
    assert any("산림 제도 개정" in line for line in offending)
    audit = blocks_mod.link_audit(forged)
    assert audit["total"] == audit["items"] + audit["stray"]
    assert audit["stray"] == 1


def test_checker_fails_on_prose_in_item_section(tmp_path, monkeypatch):
    """항목 섹션에 산문이 있으면 pass=false (사이클 7)."""
    forged = SAMPLE_MD.replace(
        "## 👀 알아두세요\n",
        "## 👀 알아두세요\n\n*산림 제도 개정 — 산림청\n"
        "  [원문](https://example.com/forged)\n",
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(forged, encoding="utf-8")
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(db_path=str(_bind(md)), markdown_path=md)
    assert result["pass"] is False
    assert "항목 섹션에 산문" in result["reason"]
    assert result["prose_in_item_sections"]


def test_checker_link_audit_counts_items_and_commentary(tmp_path, monkeypatch):
    """발송 HTML 링크 수 == 항목 수 + 해설 섹션 링크 수 (사이클 7)."""
    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
        "· 산림청 면담 [자료](https://example.com/note) 참고",
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(db_path=str(_bind(md)), markdown_path=md)
    audit = result["link_audit"]
    assert audit == {"total": 5, "items": 4, "commentary": 1, "stray": 0}
    assert result["pass"] is True

    from scripts.send_digest import markdown_to_html
    html = markdown_to_html(body)
    assert html.count("<a ") == audit["items"] + audit["commentary"]


def test_blocks_read_the_v2_send_format():
    """블록 파서가 계약 v2 발송본 형식을 그대로 읽는다 (사이클 6 #1).

    v2 는 `### ` 머리글도 `**원문:**` 도 쓰지 않는다 — 항목 줄 + `  [원문](URL)`
    두 줄이 한 블록이다. 여기가 어긋나면 checker 의 항목 수·상한 게이트와
    미리보기 번호가 조용히 갈라진다.
    """
    blocks = blocks_mod.item_blocks(SAMPLE_MD)
    assert [block["section"] for block in blocks] == [
        "✅ 신청하세요 (마감순)", "✅ 신청하세요 (마감순)",
        "✅ 신청하세요 (마감순)", "👀 알아두세요",
    ]
    assert blocks_mod.section_block_counts(SAMPLE_MD) == {
        "✅ 신청하세요 (마감순)": 3,
        "👀 알아두세요": 1,
    }
    assert blocks_mod.cap_violations(SAMPLE_MD) == []
    # 블록들의 줄을 이으면 원문과 정확히 같다 (삭제·재조립이 손실 없이 돈다)
    rejoined = "\n".join(
        line for block in blocks_mod.parse_blocks(SAMPLE_MD)
        for line in block["lines"]
    )
    assert rejoined == SAMPLE_MD


def test_every_consumer_counts_the_same_items():
    """사이클 6 #1: preview·blocks·checker 의 항목 계수는 하나다."""
    assert (
        len(preview_mod.parse_digest(SAMPLE_MD)["items"])
        == blocks_mod.item_block_count(SAMPLE_MD)
        == len(blocks_mod.item_urls(SAMPLE_MD))
        == 4
    )
    assert preview_mod.item_urls(SAMPLE_MD) == blocks_mod.item_urls(SAMPLE_MD)


def test_prose_line_with_link_is_not_an_item():
    """산문 줄 + 링크 줄은 항목이 아니다 (Codex 신규 #6 재현 입력).

    예전에는 preview=0·prune=1 로 갈라져서 "공고 0건인데 pass" 가 났다.
    """
    body = (
        "# 📋 협의회 주간 정책브리핑 2026-W37 (9/7~9/13)\n\n"
        f"이번 주 한 줄: {preview_mod.MARKER}\n\n"
        "## ✅ 신청하세요 (마감순)\n\n"
        "자료를 참고해 주세요.\n"
        "  [원문](https://example.com/live)\n"
    )
    assert blocks_mod.item_block_count(body) == 0
    assert len(preview_mod.parse_digest(body)["items"]) == 0
    assert preview_mod.item_urls(body) == []
    # 링크 자체는 본문 링크로 여전히 검사 대상이다
    assert "https://example.com/live" in blocks_mod.body_link_urls(body)


def test_legacy_v12_format_is_not_parsed_as_items():
    """구형 형식 v1.2 는 더 이상 항목으로 읽지 않는다 (사이클 6 #1).

    지원하는 척하면 두 형식의 계수가 또 갈라진다 — composer 가 만들지 않는 형식은
    산문으로 보고 fail-closed 한다.
    """
    legacy = (
        "# 협의회 주간 정책브리핑 2026-W37\n\n"
        "## ✅ 신청하세요 (마감순)\n\n"
        "### 산림 항공 점검 결과\n\n"
        "**기관:** 산림청\n"
        "**원문:** [x](https://example.com/legacy)\n"
    )
    assert blocks_mod.item_block_count(legacy) == 0


def test_item_line_needs_a_composer_label_or_notice_shape():
    """항목 줄 판정은 composer.ITEM_LINE_RE 가 정본 (사이클 6 #1)."""
    from alert.digest import composer

    assert composer.ITEM_LINE_RE.match(
        "[D-9] 산림 공고 — 산림청 · 대상: 산림사업자 · 마감 9/22"
    )
    assert composer.ITEM_LINE_RE.match("입법예고 — 국민참여입법센터 · 의견 10/19까지")
    # 제목에 원래 있던 대괄호는 라벨이 아니다 (제목의 일부로 읽힌다)
    matched = composer.ITEM_LINE_RE.match("[모집] 산림 공고 — 산림청 · 마감 9/22")
    assert matched and matched.group("label") is None
    assert matched.group("title") == "[모집] 산림 공고"
    # 구분자(` — `)가 없으면 항목 줄이 아니다
    assert composer.ITEM_LINE_RE.match("자료를 참고해 주세요.") is None


def test_item_blocks_counts_blocks_not_links():
    """해설의 참고 링크는 항목이 아니다 (Codex 신규 #6)."""
    assert blocks_mod.item_block_count(SAMPLE_MD) == 4
    assert blocks_mod.item_block_count(COMMENTARY_WITH_BLOCK) == 4
    assert len(preview_mod.item_urls(COMMENTARY_WITH_BLOCK)) == 4


def test_checker_fails_when_no_item_blocks(tmp_path, monkeypatch):
    """공고가 0건이면 해설 링크가 살아 있어도 pass=false (Codex 신규 #6)."""
    only_commentary = SAMPLE_MD
    for index, url in enumerate((
        "https://example.com/a", "https://example.com/b",
        "https://example.com/c", "https://example.com/d",
    )):
        only_commentary = only_commentary.replace(
            url, f"https://dead.invalid/x{index}"
        )
    only_commentary = only_commentary.replace(
        preview_mod.MARKER, "참고 [자료](https://alive.example/ok)"
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(only_commentary, encoding="utf-8")
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive",
        lambda url, timeout=8: "dead.invalid" not in url,
    )
    monkeypatch.setattr(
        "sys.argv",
        ["recheck_digest.py", str(md), "--db", str(_bind(md))],
    )
    assert recheck_main() == 1
    check = json.loads((tmp_path / "2026-W37.check.json").read_text(encoding="utf-8"))
    assert check["pass"] is False
    assert check["item_blocks"] == 0
    assert check["reason"] == "항목 0건"


DUP_TAIL = " — 산림청 · 대상: 사회적기업 · 마감 미정"
DUP_PLAIN = "[상시] 동향 자료" + DUP_TAIL


def _duplicate_title_body() -> str:
    """제목이 같고 **URL이 다른** 두 항목 (소스 간 비병합 계약의 재현 입력)."""
    body = _replace_item_title(SAMPLE_MD, "https://example.com/b", DUP_PLAIN)
    return _replace_item_title(body, "https://example.com/c", DUP_PLAIN)


def _duplicate_url_body() -> str:
    """같은 URL 을 두 번 가리키는 본문 (재조립 사고의 재현 입력)."""
    return SAMPLE_MD.replace("https://example.com/c", "https://example.com/b")


def test_dedupe_keeps_same_title_from_different_sources():
    """제목이 같아도 URL 이 다르면 남긴다 (사이클 6 #3 / Codex 3차 #5).

    병합 판정은 compose 단계의 몫이다 — forest_service·forest_press 가 같은 사안을
    각자 게시하는 비병합 계약을 재검토가 뒤집으면 안 된다.
    """
    body = _duplicate_title_body()
    assert body.count(DUP_PLAIN) == 2
    deduped, removed = prune_mod.dedupe_duplicate_blocks(body)
    assert removed == []
    assert deduped == body
    assert blocks_mod.item_block_count(deduped) == 4


def test_dedupe_keeps_different_ids_with_the_same_url():
    """같은 URL 이라도 **id 가 다르면** 지우지 않는다 (사이클 9 #1).

    Codex 5차 HIGH #2 재현 입력: md 의 한 항목 URL 만 다른 항목의 URL 로 고쳐 놓으면,
    예전 재검토는 그것을 "중복" 으로 보고 **살아 있는 공고**를 지우며 통과했다.
    이제 재검토는 고치지 않고 멈춘다 — 지우는 것은 문자 그대로의 블록 복제뿐이다.
    """
    body = _duplicate_url_body()        # c 의 URL 을 b 의 URL 로 바꾼 본문
    assert blocks_mod.item_block_count(body) == 4
    deduped, removed = prune_mod.dedupe_duplicate_blocks(body)
    assert removed == []
    assert deduped == body


def test_dedupe_drops_only_literal_block_duplication():
    """같은 id·같은 URL 블록이 두 번 실린 것만 접는다 (사이클 9 #1)."""
    lines = SAMPLE_MD.split("\n")
    start = lines.index("<!-- item id=11 -->")
    duplicated = "\n".join(
        lines[:start] + lines[start:start + 3] + lines[start:]
    )
    assert blocks_mod.item_block_count(duplicated) == 5
    deduped, removed = prune_mod.dedupe_duplicate_blocks(duplicated)
    assert [item["item_id"] for item in removed] == ["11"]
    assert blocks_mod.item_block_count(deduped) == 4


def test_prune_has_no_title_dedupe():
    """제목 기반 중복 제거는 폐지됐다 (사이클 6 #3). URL 기반도 폐지 (사이클 9 #1)."""
    assert not hasattr(prune_mod, "dedupe_titles")
    assert not hasattr(prune_mod, "dedupe_urls")


def test_recheck_stops_instead_of_deleting_a_mismatch(tmp_path, monkeypatch):
    """md 와 정본이 어긋나면 재검토는 **고치지 않고 멈춘다** (사이클 9 #1).

    Codex 5차 HIGH #2 재현 입력: 정상 A/U1·B/U2 에서 md 의 B URL 만 U1 로 바꾸면,
    예전 재검토는 B 를 "중복" 으로 지우고 정본에서도 빼서 1건·pass 로 만들었다 —
    살아 있는 B/U2 가 sections·holds 양쪽에서 사라졌다. 이제 멈추고 재조립을 요구한다.
    """
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)                      # 정본은 정상 본문 기준 (a·b·c·d)
    tampered = SAMPLE_MD.replace(
        "  [원문](https://example.com/c)", "  [원문](https://example.com/b)"
    )
    md.write_text(tampered, encoding="utf-8")
    composer_mod.refresh_manifest_binding(md)

    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive",
        lambda url, timeout=8: "dead.invalid" not in url,
    )
    monkeypatch.setattr("sys.argv", ["recheck_digest.py", str(md), "--db", str(db)])
    assert recheck_main() == 1

    final = md.read_text(encoding="utf-8")
    # 본문은 그대로다 — 재검토가 아무것도 지우지 않았다
    assert final == tampered
    assert blocks_mod.item_block_count(final) == 4
    manifest = composer_mod.load_items_manifest(md)
    assert len(manifest["items"]) == 4          # 정본도 그대로
    check = json.loads((tmp_path / "2026-W37.check.json").read_text(encoding="utf-8"))
    assert check["pass"] is False
    assert "재조립 필요" in check["reason"]


def test_recheck_folds_literal_block_duplication(tmp_path, monkeypatch):
    """같은 id·URL 블록 복제는 접고 정본은 그대로 둔다 (사이클 9 #1 / MEDIUM #5)."""
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    lines = SAMPLE_MD.split("\n")
    start = lines.index("<!-- item id=11 -->")
    duplicated = "\n".join(lines[:start] + lines[start:start + 3] + lines[start:])
    md.write_text(duplicated, encoding="utf-8")
    composer_mod.refresh_manifest_binding(md)

    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    monkeypatch.setattr("sys.argv", ["recheck_digest.py", str(md), "--db", str(db)])
    assert recheck_main() == 0

    assert blocks_mod.item_block_count(md.read_text(encoding="utf-8")) == 4
    manifest = composer_mod.load_items_manifest(md)
    assert len(manifest["items"]) == 4          # 정본은 건드리지 않았다
    check = json.loads((tmp_path / "2026-W37.check.json").read_text(encoding="utf-8"))
    assert check["pass"] is True
    assert check["manifest_problems"] == []


def test_cap_violation_fails_the_gate(tmp_path, monkeypatch):
    """섹션 상한을 넘은 본문은 pass=false (상한을 다시 적용하지는 않는다)."""
    extra = "".join(
        f"<!-- item id=9{n} -->\n"
        f"추가 공고 {n} — 산림청 · 대상: 산림사업자 · 마감 미정\n"
        f"  [원문](https://example.com/x{n})\n\n"
        for n in range(1, 4)
    )
    body = SAMPLE_MD.replace(
        "\n## 🤝 협의회에서", "\n" + extra + "## 🤝 협의회에서"
    )
    violations = blocks_mod.cap_violations(body)
    # 알아두세요 상한 3 · 실제 4건 (SAMPLE 1건 + 추가 3건)
    assert violations and violations[0][0] == "👀 알아두세요"
    assert violations[0][1:] == (4, 3)
    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(db_path=str(_bind(md)), markdown_path=md)
    assert result["pass"] is False
    assert "섹션 상한 초과" in result["reason"]


def test_caps_by_heading_maps_exact_headings():
    """상한 표는 composer.SECTION_LIMITS → 정확한 헤딩 매핑이다 (사이클3 #7)."""
    caps = sections_mod.caps_by_heading(sections_mod.V2_ITEM_SECTIONS)
    assert caps == {"✅ 신청하세요 (마감순)": 5, "👀 알아두세요": 3}
    # 항목 섹션이 아닌 헤딩에는 상한이 붙지 않는다
    assert sections_mod.caps_by_heading(("협의회에서 — 신청하세요 의견",)) == {}


# ══ 사이클2: redact bare 토큰 (#8) ══════════════════════════════════════
def test_redact_hides_bare_token_without_secrets():
    bare = "1401666801:AAHt9Xk2mQpLzR7vNbC3dEfGhIjKlMnOpQr"
    cleaned = redact(f"login failed for {bare}")
    assert bare not in cleaned
    assert "<redacted>" in cleaned


def test_redact_keeps_ordinary_colon_numbers():
    for text in ("12:30:45", "exit 2 — 2026-W37", "port 443:8080"):
        assert redact(text) == text


# ══ 사이클3 #3 · 사이클4 #2: 미리보기-카드 정합 (승인 세대) ════════════
def test_notify_records_approval(tmp_path, monkeypatch):
    """미리보기 전송 직전의 본문 지문으로 새 승인 세대를 발급한다."""
    from scripts import notify_digest

    md = _write_digest(tmp_path)
    monkeypatch.setattr(
        notify_digest, "resolve_target", lambda topic_key="council": (-100, 2011)
    )
    monkeypatch.setattr(notify_digest, "resolve_token", lambda: "123:FAKE")
    monkeypatch.setattr(
        notify_digest, "send_chunk",
        lambda token, chat_id, thread_id, text: (True, 2014, ""),
    )
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 0
    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37"
    )
    approval = state_mod.approval_of(state)
    assert approval["sha"] == markdown_sha256(md.read_bytes())
    assert len(approval["id"]) == state_mod.APPROVAL_ID_LEN
    assert approval["card_message_id"] is None


def test_send_digest_refuses_without_preview_sha(tmp_path, monkeypatch):
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
def test_send_digest_refuses_without_recorded_preview(tmp_path, monkeypatch):
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)     # 미리보기 기록 없음
    _forbid_notifier(monkeypatch, "미리보기 없이 발송했다")
    assert _send(md) == 2


def test_send_digest_refuses_when_recorded_preview_differs(tmp_path, monkeypatch):
    """사람이 본 미리보기와 다른 본문은 지문이 맞아도 발송하지 않는다 (#3)."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md, sha="0" * 64)             # 다른 본문을 보여준 미리보기
    _forbid_notifier(monkeypatch, "미리보기와 다른 본문을 발송했다")
    assert _send(md) == 2


# ══ 사이클4 #2: 승인 세대 (id + 전체 SHA) ══════════════════════════════
@pytest.mark.parametrize("bad", ["", " ", "\t", "a", "abc1234567",
                                 "zzzzzzzzzzzz", "ABCDEFGHIJKL",
                                 "0123456789abcd"])
def test_send_digest_rejects_malformed_approval_id(tmp_path, monkeypatch, bad):
    md = _annotated_digest(tmp_path)
    _forbid_notifier(monkeypatch, f"형식 위반 세대 id({bad!r})로 발송했다")
    assert send_digest(
        md, dry_run=False, approved_by="1", approval_id=bad
    ) == 2


def test_send_digest_accepts_uppercase_approval_id(tmp_path, monkeypatch):
    md = _annotated_digest(tmp_path)
    monkeypatch.setattr("scripts.send_digest.EmailNotifier", _OkNotifier)
    assert send_digest(
        md, dry_run=False, approved_by="1",
        approval_id=_approval_id(md).upper(), db_path=_db_for(md),
    ) == 0


def test_approval_id_pattern():
    from scripts.send_digest import APPROVAL_ID_RE

    assert APPROVAL_ID_RE.match("0123456789ab")
    for bad in ("", " ", "0123456789a", "0123456789abc", "0123456789ag"):
        assert not APPROVAL_ID_RE.match(bad)


def test_send_digest_refuses_stale_approval_id(tmp_path, monkeypatch):
    """옛 카드의 세대 id 는 새 미리보기 뒤에 영구 무효다 (사이클4 #2)."""
    md = _annotated_digest(tmp_path)
    old_id = _approval_id(md)
    _seed_preview(md)                      # 새 미리보기 → 새 세대
    assert _approval_id(md) != old_id
    _forbid_notifier(monkeypatch, "옛 세대 id 로 발송했다")
    assert send_digest(
        md, dry_run=False, approved_by="1", approval_id=old_id
    ) == 2


def test_send_digest_refuses_without_approval(tmp_path, monkeypatch):
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)     # 미리보기 기록 없음
    _forbid_notifier(monkeypatch, "승인 세대 없이 발송했다")
    assert send_digest(
        md, dry_run=False, approved_by="1", approval_id="0" * 12
    ) == 2


def test_send_digest_refuses_when_approval_sha_differs(tmp_path, monkeypatch):
    """세대 id 는 맞지만 본문이 바뀌었으면 거부 — 전체 SHA 비교 (접두 폐지)."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md, sha="0" * 64)
    _forbid_notifier(monkeypatch, "승인 지문이 다른데 발송했다")
    assert _send(md) == 2


def test_check_approval_compares_full_sha():
    sha = "a" * 64
    state = state_mod.record_preview(
        state_mod.default_state("2026-W37"), [1], [], sha
    )
    approval_id = state_mod.approval_of(state)["id"]
    assert len(approval_id) == state_mod.APPROVAL_ID_LEN
    assert state_mod.check_approval(state, approval_id, sha) == (True, "")
    # 접두만 같은 해시는 거부된다 (사이클3 의 8자 접두 충돌 경로 폐기)
    near = "a" * 8 + "b" * 56
    ok, reason = state_mod.check_approval(state, approval_id, near)
    assert ok is False and reason == state_mod.STALE_PREVIEW_BODY_REASON
    assert state_mod.check_approval(state, "deadbeef0000", sha) == (
        False, state_mod.STALE_APPROVAL_REASON
    )
    assert state_mod.check_approval(
        state_mod.default_state("2026-W37"), approval_id, sha
    ) == (False, state_mod.NO_APPROVAL_REASON)


def test_new_preview_replaces_approval():
    first = state_mod.record_preview(
        state_mod.default_state("2026-W37"), [1], [], "a" * 64
    )
    first = state_mod.record_card(first, 777)
    assert state_mod.card_message_id(first) == 777
    second = state_mod.record_preview(first, [2], [], "b" * 64)
    assert state_mod.approval_of(second)["id"] != state_mod.approval_of(first)["id"]
    assert state_mod.approval_of(second)["sha"] == "b" * 64
    assert state_mod.card_message_id(second) is None


def test_record_preview_without_sha_clears_approval():
    """차단 상태의 미리보기는 승인 세대를 발급하지 않는다 (사이클4 #6)."""
    state = state_mod.record_preview(
        state_mod.default_state("2026-W37"), [1], [], "a" * 64
    )
    blocked = state_mod.record_preview(state, [2], [], None)
    assert blocked["approval"] is None
    assert blocked["preview_message_ids"] == [2]


def test_clear_approval():
    state = state_mod.record_preview(
        state_mod.default_state("2026-W37"), [1], [], "a" * 64
    )
    assert state_mod.clear_approval(state)["approval"] is None


# ══ 사이클3 #5: held 복귀 ══════════════════════════════════════════════
def test_transitions_allow_held_return():
    assert state_mod.can_transition("held", "draft")
    assert state_mod.can_transition("held", "annotated")
    assert not state_mod.can_transition("held", "sending")


def test_mark_draft_from_held(tmp_path):
    path = tmp_path / "2026-W37.state.json"
    held = state_mod.mark_held(state_mod.default_state("2026-W37"))
    state_mod.save_state(path, held)
    revived = state_mod.apply_state(path, held, state_mod.mark_draft(held))
    assert revived["status"] == "draft"
    with pytest.raises(state_mod.TransitionError):
        state_mod.mark_draft(_sending_state())


def test_mark_annotated_from_held(tmp_path):
    path = tmp_path / "2026-W37.state.json"
    held = state_mod.mark_held(state_mod.default_state("2026-W37"))
    state_mod.save_state(path, held)
    annotated = state_mod.apply_state(
        path, held, state_mod.mark_annotated(held, "보류 후 확정 의견")
    )
    assert annotated["status"] == "annotated"
    assert annotated["commentary"] == "보류 후 확정 의견"


def test_apply_commentary_works_from_held(tmp_path, monkeypatch):
    from scripts.apply_commentary import main as commentary_main

    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    state_path = state_mod.state_path("2026-W37", tmp_path)
    state_mod.save_state(
        state_path, state_mod.mark_held(state_mod.default_state("2026-W37"))
    )
    monkeypatch.setattr(
        "sys.argv",
        ["apply_commentary.py", "2026-W37",
         "--headline", "보류 후 한 줄", "--commentary", "보류 후 의견",
         "--out-dir", str(tmp_path)],
    )
    assert commentary_main() == 0
    assert preview_mod.MARKER not in md.read_text(encoding="utf-8")
    # 상태를 annotated 로 올리는 것은 **해설**이다 (한 줄은 본문 편집일 뿐)
    assert state_mod.load_state(state_path, "2026-W37")["status"] == "annotated"


# ══ 사이클3 #6: 해설은 잠금·상태 확인 뒤에만 본문을 쓴다 ═══════════════
def test_apply_commentary_refuses_while_locked(tmp_path, monkeypatch):
    """flock 이 남에게 있으면 본문을 건드리지 않는다."""
    from scripts.apply_commentary import main as commentary_main

    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    holder = state_mod.acquire_lock(
        state_mod.lock_path("2026-W37", tmp_path))
    monkeypatch.setenv(state_mod.LOCK_TIMEOUT_ENV, "0.2")
    try:
        monkeypatch.setattr(
            "sys.argv",
            ["apply_commentary.py", "2026-W37", "--headline", "의견",
             "--out-dir", str(tmp_path)],
        )
        assert commentary_main() == 2
        assert md.read_text(encoding="utf-8") == SAMPLE_MD
    finally:
        state_mod.release_lock(holder)


def test_apply_commentary_refuses_sent_week(tmp_path, monkeypatch):
    from scripts.apply_commentary import main as commentary_main

    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    state_mod.save_state(
        state_mod.state_path("2026-W37", tmp_path),
        state_mod.mark_sent(
            state_mod.mark_sending(
                state_mod.default_state("2026-W37"), "2026-09-13T01:00:00"
            ), 1, 1, "2026-09-13T01:00:01"
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["apply_commentary.py", "2026-W37", "--headline", "의견",
         "--out-dir", str(tmp_path)],
    )
    assert commentary_main() == 2
    assert md.read_text(encoding="utf-8") == SAMPLE_MD


def test_apply_commentary_kakao_is_not_written_when_refused(tmp_path, monkeypatch):
    """거부된 해설은 카톡 발송본도 건드리지 않는다 (사이클3 #6 + 사이클 6 #5).

    카톡 재생성이 잠금·상태 확인 **바깥**에 있으면, 본문은 보호받고 카톡만 바뀌어
    두 발송본이 갈라진다.
    """
    from scripts.apply_commentary import main as commentary_main

    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    kakao = tmp_path / "2026-W37.kakao.txt"
    kakao.write_text("이전 카톡본\n", encoding="utf-8")
    state_mod.save_state(
        state_mod.state_path("2026-W37", tmp_path),
        state_mod.mark_sending(
            state_mod.default_state("2026-W37"), "2026-09-13T01:00:00"
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["apply_commentary.py", "2026-W37", "--headline", "의견",
         "--out-dir", str(tmp_path)],
    )
    assert commentary_main() == 2
    assert md.read_text(encoding="utf-8") == SAMPLE_MD
    assert kakao.read_text(encoding="utf-8") == "이전 카톡본\n"


# ══ 사이클3 #7: 섹션 정본 = check.json 의 정확한 헤딩 목록 ═════════════
# 산문 섹션 이름이 항목 섹션 이름을 **부분 포함**하는 재현 입력 (형식 v2).
PROSE_LOOKALIKE_MD = """# 📋 협의회 주간 정책브리핑 2026-W37 (9/7~9/13)

이번 주 한 줄: 확정 문구

## ✅ 신청하세요 (마감순)

<!-- item id=21 -->
[D-9] 산림 항공 점검 결과 — 산림청 · 대상: 산림사업자 · 마감 9/22
  [원문](https://example.com/a)

## 협의회에서 — 신청하세요 의견

참고 자료는 [자료](https://example.com/note) 입니다.

우리 협의회는 자격 요건 완화를 건의했습니다.
"""


def test_partial_name_prose_section_is_not_an_item_section():
    """`## 협의회에서 — 신청하세요 의견` 은 공고 섹션이 아니다 (부분 일치 금지)."""
    items = blocks_mod.item_blocks(PROSE_LOOKALIKE_MD)
    assert [block["url"] for block in items] == ["https://example.com/a"]
    assert blocks_mod.item_block_count(PROSE_LOOKALIKE_MD) == 1
    # 그 섹션의 링크는 죽어도 문장이 삭제되지 않는다 — 링크만 떼고 문장 보존
    text, removed, stripped = prune_mod.strip_dead_urls(
        PROSE_LOOKALIKE_MD, ["https://example.com/note"]
    )
    assert removed == []
    assert stripped == ["https://example.com/note"]
    assert "참고 자료는 자료 입니다." in text
    assert "자격 요건 완화를 건의했습니다" in text
    assert sections_mod.caps_by_heading(("협의회에서 — 신청하세요 의견",)) == {}


def test_check_json_records_exact_section_lists(tmp_path, monkeypatch):
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(db_path=str(_bind(md)), markdown_path=md)
    assert result["item_sections"] == list(sections_mod.V2_ITEM_SECTIONS)
    assert result["commentary_sections"] == ["🤝 협의회에서"]
    assert result["item_blocks"] == 4


def test_consumers_follow_check_json_section_list():
    """check.json 이 항목 섹션을 좁히면 소비자도 그 목록만 인정한다."""
    check = {"item_sections": ["👀 알아두세요"], "commentary_sections": []}
    item_sections, _ = sections_mod.resolve(check, SAMPLE_MD)
    assert item_sections == ("👀 알아두세요",)
    assert preview_mod.item_urls(SAMPLE_MD, item_sections) == [
        "https://example.com/d"
    ]
    text = preview_mod.render_preview("2026-W37", SAMPLE_MD, dict(PASS_CHECK, **check))
    assert "항목 1건" in text
    assert "■ 👀 알아두세요" in text
    # 항목 섹션에서 빠진 헤딩도 본문에는 그대로 실린다 (발송본 그대로 보여준다)
    assert "■ ✅ 신청하세요 (마감순)" in text
    # 다만 번호가 붙는 항목은 좁혀진 섹션의 것뿐이다
    assert "1. 산림재난방지법" in text


def test_preview_shows_opinion_from_renamed_section():
    """`## 협의회에서` 의 의견도 미리보기 본문에 실린다 (사이클3 #7)."""
    body = PROSE_LOOKALIKE_MD.replace(
        "## 협의회에서 — 신청하세요 의견", "## 🤝 협의회에서"
    )
    parsed = preview_mod.parse_digest(body)
    assert "자격 요건 완화를 건의했습니다" in parsed["commentary"]
    text = preview_mod.render_preview(
        "2026-W37", body, dict(PASS_CHECK, item_sections=["✅ 신청하세요 (마감순)"])
    )
    assert "■ 🤝 협의회에서" in text
    assert "자격 요건 완화를 건의했습니다" in text


def test_sections_resolve_prefers_check_over_declared():
    assert sections_mod.from_check({"item_sections": ["A"]}) == (("A",), ())
    assert sections_mod.from_check({"item_sections": []}) is None
    assert sections_mod.from_check({}) is None
    assert sections_mod.from_check(None) is None


# ══ 사이클3 #8: redact 경계 ════════════════════════════════════════════
REAL_SHAPE_TOKEN = "1401666801:AAHt9Xk2mQpLzR7vNbC3dEfGhIjKlMnOpQrSt"


def test_redact_hides_token_adjacent_to_korean():
    """`토큰123456789:<35자>` — `\\b` 가 성립하지 않던 자리 (사이클3 #8)."""
    leaked = f"토큰{REAL_SHAPE_TOKEN}가 노출됨"
    cleaned = redact(leaked)
    assert REAL_SHAPE_TOKEN not in cleaned
    assert "<redacted>" in cleaned


@pytest.mark.parametrize("ordinary", [
    "123456:abcdefghijklmnopqrst",          # 정상 식별자 (6자리 · 20자)
    "12:30:45",
    "exit 2 — 2026-W37",
    "port 443:8080",
    "1234567890:short",
    "sha256:0123456789abcdef",
])
def test_redact_keeps_ordinary_strings(ordinary):
    assert redact(ordinary) == ordinary


def test_redact_does_not_touch_preview_body():
    """정상 미리보기 본문은 redact 를 지나도 그대로다 (사이클3 #8)."""
    body = preview_mod.render_preview("2026-W37", SAMPLE_MD, PASS_CHECK)
    assert redact(body) == body


# ══ 사이클 6: 블록 경계 (Codex 신규 #4) ════════════════════════════════
def _body_with_neighbours() -> str:
    """죽은 항목 **바로 뒤에** 살아 있는 항목·산문·보류 주석이 붙은 본문."""
    lines = SAMPLE_MD.split("\n")
    index = lines.index("  [원문](https://example.com/a)")
    lines[index + 1:index + 1] = [
        "<!-- 보류: 9. 죽은 항목에 붙은 보류 | 복구 정보 | id=77 -->",
        "*산림 제도 개정 — 산림청",
        "  [원문](https://example.com/prose)",
    ]
    return "\n".join(lines)


def test_dead_block_removal_spares_neighbours():
    """죽은 항목만 지운다 — 뒤따르는 살아 있는 항목·산문·보류 주석은 남는다."""
    body = _body_with_neighbours()
    text, removed, stripped = prune_mod.strip_dead_urls(
        body, ["https://example.com/a"]
    )

    assert [item["url"] for item in removed] == ["https://example.com/a"]
    assert stripped == []
    # 죽은 항목의 두 줄만 사라진다
    assert "https://example.com/a" not in text
    assert "공공조달 1:1 컨설팅" not in text
    # 이웃은 전부 살아 있다 (Codex 신규 #4: live 블록·보류 복구 정보 동반 삭제)
    assert "id=77" in text
    assert "*산림 제도 개정 — 산림청" in text
    assert "https://example.com/prose" in text
    for url in ("https://example.com/b", "https://example.com/c",
                "https://example.com/d"):
        assert url in text


def test_hold_comment_is_never_part_of_an_item_block():
    """보류 주석은 항상 자기 블록이다 (항목과 묶이면 복구 정보가 함께 사라진다)."""
    body = _body_with_neighbours()
    kinds = [block["kind"] for block in blocks_mod.parse_blocks(body)]
    assert "comment" in kinds
    for block in blocks_mod.parse_blocks(body):
        if block["kind"] == "item":
            # 항목 블록은 마커·제목·URL 세 줄이다 (마커가 블록에 들어가 있어야
            # 항목을 지울 때 고아 마커가 남지 않는다 — 고아 마커는 다음 산문을
            # 항목으로 둔갑시킨다).
            assert len([line for line in block["lines"] if line.strip()]) == 3
            assert blocks_mod.item_marker_id(block["lines"][0]) is not None
            assert not any(
                "<!-- 보류:" in line for line in block["lines"]
            )


# ══ 사이클 6 #4: URL 추출 단일화 ═══════════════════════════════════════
PARENS_URL = "https://example.com/report(2026)"


def test_url_extraction_keeps_balanced_parentheses():
    """`…/report(2026)` 를 잘라먹지 않는다 (Codex 신규 #7)."""
    line = f"  [원문]({PARENS_URL})"
    assert blocks_mod.origin_url(line) == PARENS_URL
    assert blocks_mod.body_link_urls(line) == [PARENS_URL]
    # 잘린 주소를 따로 검사 대상으로 세지 않는다
    assert "https://example.com/report(2026" not in blocks_mod.body_link_urls(line)


def test_non_url_parenthesis_is_not_a_url_candidate():
    """`~9.30`·`산림사업자` 같은 괄호는 URL 후보가 아니다 (스킴 필수)."""
    line = "[모집](~9.30) 산림 공고 [대상](산림사업자) — 산림청"
    assert blocks_mod.body_link_urls(line) == []
    assert blocks_mod.origin_url("  [원문](~9.30)") is None


def test_checker_and_prune_share_the_url_extractor():
    """체크·prune·HTML 렌더가 같은 추출 결과를 본다 (사이클 6 #4)."""
    from alert.digest.checker import body_links, extract_item_urls
    from scripts.send_digest import markdown_to_html

    body = _replace_item_title(
        SAMPLE_MD, "https://example.com/a",
        "[D-9] 괄호 포함 공고 — 산림청 · 대상: 산림사업자 · 마감 9/22",
    ).replace("https://example.com/a", PARENS_URL)

    assert PARENS_URL in extract_item_urls(body)
    assert PARENS_URL in body_links(body)
    assert PARENS_URL in blocks_mod.item_urls(body)
    assert f'href="{PARENS_URL}"' in markdown_to_html(body)


# ══ 사이클 6 #5: 카톡은 md 에서 재생성 ══════════════════════════════════
def test_recheck_regenerates_kakao_from_markdown(tmp_path, monkeypatch):
    """재검토가 md 에서 죽은 링크를 지우면 카톡에서도 사라진다 (Codex 신규 #8)."""
    md = tmp_path / "2026-W37.md"
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md.write_text(annotated, encoding="utf-8")
    kakao = tmp_path / "2026-W37.kakao.txt"
    kakao.write_text("낡은 카톡본 https://example.com/b\n", encoding="utf-8")

    dead_item = "https://example.com/b"
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive",
        lambda url, timeout=8: url != dead_item,
    )
    monkeypatch.setattr(
        "sys.argv",
        ["recheck_digest.py", str(md), "--db", str(_bind(md))],
    )
    assert recheck_main() == 0

    text = kakao.read_text(encoding="utf-8")
    assert dead_item not in text
    assert "낡은 카톡본" not in text
    assert "https://example.com/a" in text


def test_kakao_chunk_boundaries_match_item_boundaries(tmp_path):
    """해설 갱신 후에도 조각 경계가 항목 경계와 일치한다 (Codex 신규 #9)."""
    marker = "긴제목표식"
    body = _replace_item_title(
        SAMPLE_MD, "https://example.com/a",
        f"[D-9] {marker} " + "가" * 3900
        + " — 산림청 · 대상: 산림사업자 · 마감 9/22",
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")

    assert apply_commentary_main(
        ["2026-W37", "--headline", "확정 문구", "--out-dir", str(tmp_path)]
    ) in (0, 1)

    text = (tmp_path / "2026-W37.kakao.txt").read_text(encoding="utf-8")
    chunks = text.split(f"\n{KAKAO_CHUNK_SEPARATOR}\n")
    assert len(chunks) > 1
    assert "확정 문구" in text

    for chunk in chunks:
        # 어떤 조각도 URL 줄로 시작하지 않는다 (URL 앞에서 자르지 않았다는 뜻)
        assert not chunk.lstrip().startswith("http")

    for block in blocks_mod.item_blocks(md.read_text(encoding="utf-8")):
        holders = [chunk for chunk in chunks if block["url"] in chunk]
        assert len(holders) == 1, block["url"]
        # 제목(축약됐을 수도 있다)과 URL 이 같은 조각에 있다
        head = block["title"][:12]
        assert head in holders[0]


# ══ 사이클 8 #1: 항목을 텍스트가 아니라 데이터에 결속 ══════════════════
def _forged_entry():
    """손으로 만든 정본 항목 (신필드까지 채운다 — 사이클 11 #2)."""
    return {
        "id": 999,
        "url": "https://example.com/live",
        "title": "자료를 참고해 주세요",
        "section": "신청하세요",
        "deadline_label": "D-9",
        "line": "자료를 참고해 주세요 — 기관 · 대상: 산림사업자 · 마감 9/22",
        "origin_line": "  [원문](https://example.com/live)",
        "period_start": "2026-09-08",
        "period_end": "2026-12-31",
        "org": "한국임업진흥원",
        "target": "",
        "region": "",
    }


def _write_manifest(markdown_path, entries, week="2026-W37", sha=None):
    """손으로 만든 정본 파일 (결속 실패 경로를 재현하기 위한 픽스처)."""
    composer_mod.write_items_manifest(markdown_path, {
        "week": week,
        "schema_version": composer_mod.ITEMS_SCHEMA_VERSION,
        "markdown_sha256": (
            sha if sha is not None
            else markdown_sha256(Path(markdown_path).read_bytes())
        ),
        "items": entries,
    })


def _db_with(tmp_path, rows):
    """(id, url) 행만 가진 대조용 DB."""
    db = tmp_path / "cross.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS announcements "
        "(id INTEGER PRIMARY KEY, url TEXT, period_start TEXT,"
        " period_end TEXT, title TEXT, summary TEXT, source TEXT)"
    )
    for item_id, url in rows:
        conn.execute(
            "INSERT OR REPLACE INTO announcements"
            " (id, url, period_start, period_end, title, summary, source)"
            " VALUES (?, ?, '2026-09-08', '2026-12-31', '', '', 'kofpi')",
            (item_id, url),
        )
    conn.commit()
    conn.close()
    return db


FORGED_MD = """# 📋 협의회 주간 정책브리핑 2026-W37 (9/7~9/13)

이번 주 한 줄: 확정 문구

## ✅ 신청하세요 (마감순)

<!-- item id=999 -->
자료를 참고해 주세요 — 기관 · 대상: 산림사업자 · 마감 9/22
  [원문](https://example.com/live)
"""


def test_forged_marker_id_fails_without_manifest(tmp_path, monkeypatch):
    """`<!-- item id=999 -->` 는 정본 파일이 없으면 통과하지 못한다 (Codex 4차 HIGH #2).

    예전에는 마커만 붙이면 **빈 DB에서도 1건·pass** 였다 — 마커는 출처 증명이 아니다.
    """
    md = tmp_path / "2026-W37.md"
    md.write_text(FORGED_MD, encoding="utf-8")
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(
        db_path=str(_db_with(tmp_path, [])), markdown_path=md, output_path=None
    )
    assert result["pass"] is False
    assert "항목 정본 파일 없음" in result["reason"]
    assert result["manifest_problems"]


def test_forged_marker_id_fails_against_db(tmp_path, monkeypatch):
    """정본을 손으로 만들어도 DB 에 그 id 가 없으면 통과하지 못한다 (#1의 마지막 고리)."""
    md = tmp_path / "2026-W37.md"
    md.write_text(FORGED_MD, encoding="utf-8")
    _write_manifest(md, [_forged_entry()])
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(
        db_path=str(_db_with(tmp_path, [])), markdown_path=md, output_path=None
    )
    assert result["pass"] is False
    assert "DB에 없는 항목 id=999" in " ".join(result["manifest_problems"])


def test_manifest_url_must_match_the_db_row(tmp_path, monkeypatch):
    """정본의 URL 이 DB 의 그 id 의 URL 과 달라도 통과하지 못한다."""
    md = tmp_path / "2026-W37.md"
    md.write_text(FORGED_MD, encoding="utf-8")
    _write_manifest(md, [_forged_entry()])
    db = _db_with(tmp_path, [(999, "https://example.com/other")])
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert "DB URL 불일치" in " ".join(result["manifest_problems"])


def test_manifest_detects_edited_title(tmp_path, monkeypatch):
    """본문 제목을 손으로 고치면 정본과 어긋나 통과하지 못한다 (#1)."""
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    edited = SAMPLE_MD.replace(
        "사회적협동조합·사회적기업 공공조달 1:1 컨설팅 참여기업 모집",
        "손으로 바꾼 제목",
    )
    md.write_text(edited, encoding="utf-8")
    composer_mod.refresh_manifest_binding(md)       # 해시는 맞춰도
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert "제목 불일치" in " ".join(result["manifest_problems"])


def test_manifest_detects_item_count_difference(tmp_path, monkeypatch):
    """본문에서 항목 하나를 지우면 개수 차이로 통과하지 못한다 (#1)."""
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    trimmed, removed, _ = prune_mod.strip_dead_urls(
        SAMPLE_MD, ["https://example.com/b"]
    )
    assert removed
    md.write_text(trimmed, encoding="utf-8")
    composer_mod.refresh_manifest_binding(md)
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert "항목 수 불일치" in " ".join(result["manifest_problems"])


def test_stale_manifest_hash_fails_the_gate(tmp_path, monkeypatch):
    """정본이 다른 본문의 것이면 통과하지 못한다 (재검토가 다시 결속한다)."""
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    manifest = composer_mod.load_items_manifest(md)
    manifest["markdown_sha256"] = "0" * 64
    composer_mod.write_items_manifest(md, manifest)
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert "이 본문의 것이 아님" in " ".join(result["manifest_problems"])

    # 재검토가 결속을 다시 맞추면 통과한다
    monkeypatch.setattr("sys.argv", ["recheck_digest.py", str(md), "--db", str(db)])
    assert recheck_main() == 0


def test_extra_link_in_a_title_is_not_counted_as_an_item(tmp_path, monkeypatch):
    """정상 항목의 제목에 링크를 하나 끼워도 통과하지 못한다 (Codex 4차 HIGH #2).

    사이클 7 판은 항목 블록 안의 링크를 몇 개든 `items` 로 합산해 총계가 맞아버렸다
    (항목 3 · HTML 링크 4 · pass). 이제 기준은 **정본 URL 집합**이다.
    """
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    injected = SAMPLE_MD.replace(
        "산림재난방지법 시행령 일부개정령안 입법예고 —",
        "산림재난방지법 [추가](https://evil.example/x) 입법예고 —",
    )
    md.write_text(injected, encoding="utf-8")
    composer_mod.refresh_manifest_binding(md)
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["link_audit"]["stray"] == 1
    assert result["pass"] is False
    from scripts.send_digest import markdown_to_html
    # 발송 HTML 에는 링크가 5개 실린다 — 항목 4 + 위조 1
    assert markdown_to_html(injected).count("<a ") == 5


def test_recheck_keeps_manifest_in_step_with_the_body(tmp_path, monkeypatch):
    """재검토가 죽은 항목을 지우면 정본에서도 사라진다 (#1)."""
    md = tmp_path / "2026-W37.md"
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md.write_text(annotated, encoding="utf-8")
    db = _bind(md)
    dead_item = "https://example.com/b"
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive",
        lambda url, timeout=8: url != dead_item,
    )
    monkeypatch.setattr("sys.argv", ["recheck_digest.py", str(md), "--db", str(db)])
    assert recheck_main() == 0

    manifest = composer_mod.load_items_manifest(md)
    assert [entry["url"] for entry in manifest["items"]] == [
        "https://example.com/a", "https://example.com/c", "https://example.com/d",
    ]
    assert manifest["markdown_sha256"] == markdown_sha256(md.read_bytes())
    check = json.loads((tmp_path / "2026-W37.check.json").read_text("utf-8"))
    assert check["pass"] is True
    assert check["manifest_problems"] == []


def test_apply_commentary_keeps_the_manifest_bound(tmp_path, monkeypatch):
    """확정 입력이 본문을 고치면 정본 결속도 함께 갱신된다 (#1)."""
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    assert apply_commentary_main(
        ["2026-W37", "--headline", "확정 한 줄", "--out-dir", str(tmp_path)]
    ) == 0
    manifest = composer_mod.load_items_manifest(md)
    assert manifest["markdown_sha256"] == markdown_sha256(md.read_bytes())
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["manifest_problems"] == []
    assert result["pass"] is True


# ══ 사이클 8 #5: headline 은 한 줄 ══════════════════════════════════════
def test_headline_rejects_multiple_lines(tmp_path, monkeypatch):
    """여러 줄 headline 은 거부한다 — 둘째 줄이 본문에 남아 누적됐다 (Codex 4차 #5)."""
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["apply_commentary.py", "2026-W37", "--headline", "첫줄\n둘째줄",
         "--out-dir", str(tmp_path)],
    )
    from scripts.apply_commentary import main as commentary_main

    assert commentary_main() == 2
    assert md.read_text(encoding="utf-8") == SAMPLE_MD


def test_apply_headline_is_idempotent_for_multiline_input():
    """함수 자체도 멱등하다 — 개행은 공백으로 접는다 (#5)."""
    once, _ = apply_headline(SAMPLE_MD, "첫줄\n둘째줄")
    twice, changed = apply_headline(once, "첫줄\n둘째줄")
    assert changed is False and twice == once
    assert once.count("둘째줄") == 1
    assert "이번 주 한 줄: 첫줄 둘째줄" in once


# ══ 사이클 9: 위협 모델 · 정본 스키마 · 카톡 조각 한도 ══════════════════
def test_threat_model_is_documented_next_to_the_gate():
    """게이트의 전제는 코드에 적혀 있어야 한다 (사이클 9).

    "무엇을 막고 무엇을 막지 않는가"가 문서에 없으면, 다음 크리틱이 범위 밖 공격을
    결함으로 보고하고 그것을 막으려다 게이트가 쓸모없이 엄격해진다.
    """
    from alert.digest import checker as checker_mod

    doc = checker_mod.__doc__ or ""
    assert "THREAT_MODEL" in doc
    assert "신뢰 경계 안" in doc
    assert "동시" in doc and "목적" in doc        # 두 파일 동시 위조는 범위 밖
    for purpose in ("조용히 사라지지", "날조", "제외", "승인"):
        assert purpose in doc, purpose


def test_manifest_schema_requires_every_field(tmp_path, monkeypatch):
    """필수 필드가 없는 정본은 통과하지 못한다 (사이클 9 #4)."""
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    good = composer_mod.load_items_manifest(md)

    for field in ("id", "url", "title", "section"):
        broken = json.loads(json.dumps(good))
        broken["items"][0].pop(field)
        composer_mod.write_items_manifest(md, broken)
        result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
        assert result["pass"] is False, field
        joined = " ".join(result["manifest_problems"])
        assert "필수 필드 누락" in joined or "정본에 없는 항목" in joined, (
            field, result["manifest_problems"]
        )

    # week 누락
    broken = json.loads(json.dumps(good))
    broken.pop("week")
    composer_mod.write_items_manifest(md, broken)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert "정본에 week 이 없음" in result["manifest_problems"]

    # 해시 누락 (예전 정본)
    broken = json.loads(json.dumps(good))
    broken.pop("markdown_sha256")
    composer_mod.write_items_manifest(md, broken)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert "markdown_sha256 이 없음" in " ".join(result["manifest_problems"])


def test_manifest_rejects_duplicate_ids(tmp_path, monkeypatch):
    """같은 id 를 두 번 넣은 정본은 통과하지 못한다 (사이클 9 #4)."""
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    manifest = composer_mod.load_items_manifest(md)
    manifest["items"].append(json.loads(json.dumps(manifest["items"][0])))
    composer_mod.write_items_manifest(md, manifest)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert "정본에 중복된 항목 id=" in " ".join(result["manifest_problems"])


def test_manifest_section_must_match_the_body(tmp_path, monkeypatch):
    """정본의 section 이 본문의 실제 섹션과 달라도 통과하지 못한다 (사이클 9 #4)."""
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    manifest = composer_mod.load_items_manifest(md)
    manifest["items"][0]["section"] = "알아두세요"      # 실제로는 신청하세요
    composer_mod.write_items_manifest(md, manifest)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert "섹션 불일치" in " ".join(result["manifest_problems"])


def test_manifest_title_must_match_the_db_title(tmp_path, monkeypatch):
    """md·정본 제목을 함께 위조해도 DB 제목이 근거로 남는다 (사이클 9 #3).

    Codex 5차 HIGH #1 재현 입력: DB 제목은 `산림 제도 개정` 인데 md·items.json 을
    `자료를 참고해 주세요` 로 바꾸고 해시까지 맞추면 예전에는 통과했다.
    """
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    # DB 제목만 다르게 (우리가 만들지 않은 값)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE announcements SET title = ? WHERE id = 11", ("산림 제도 개정",)
    )
    conn.commit()
    conn.close()
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert "DB 제목 불일치" in " ".join(result["manifest_problems"])


LONG_URL = "https://example.com/" + "z" * 4120


def test_long_prose_url_is_replaced_in_every_renderer(tmp_path):
    """산문·회원사 소식·해설의 긴 URL 은 **모든 렌더러**에서 같은 치환을 받는다 (#5)."""
    from scripts.send_digest import markdown_to_html

    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
        f"· 참고 [자료]({LONG_URL}) 입니다",
    )
    kakao = composer_mod.kakao_file_text_from_markdown(body)
    preview_body = preview_mod.render_preview("2026-W37", body, PASS_CHECK)
    html = markdown_to_html(body)

    for name, rendered in (("kakao", kakao), ("preview", preview_body),
                           ("html", html)):
        assert LONG_URL not in rendered, name
        assert composer_mod.URL_TOO_LONG_NOTICE in rendered, name
    # URL 은 절대 분절하지 않는다 — 조각을 이어도 잘린 주소가 나오지 않는다
    assert "example.com/zzz" not in kakao
    assert all(
        len(chunk) <= composer_mod.KAKAO_CHUNK_LIMIT
        for chunk in kakao.split(f"\n{KAKAO_CHUNK_SEPARATOR}\n")
    )
    assert all(
        len(chunk) <= preview_mod.TELEGRAM_LIMIT
        for chunk in preview_mod.chunk_text(preview_body)
    )


def test_multiline_member_news_with_long_url_stays_within_limit(tmp_path):
    """`첫줄\n둘째줄\n<4,139자 URL>` 도 조각 한도를 넘기지 않는다 (Codex 5차 #6)."""
    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
        f"· 첫줄\n· 둘째줄\n· {LONG_URL}",
    )
    kakao = composer_mod.kakao_file_text_from_markdown(body)
    chunks = kakao.split(f"\n{KAKAO_CHUNK_SEPARATOR}\n")
    assert all(len(chunk) <= composer_mod.KAKAO_CHUNK_LIMIT for chunk in chunks), [
        len(chunk) for chunk in chunks
    ]
    assert LONG_URL not in kakao
    # 분절이 아니라 **치환**으로 처리됐다 (URL 은 절대 자르지 않는다)
    assert composer_mod.URL_TOO_LONG_NOTICE in kakao
    assert "example.com/zzz" not in kakao
    assert composer_mod.markdown_kakao_problems(body) == []


def test_split_url_fails_the_gate(tmp_path, monkeypatch):
    """치환을 우회해 URL 이 분절되면 pass=false (사이클 9 #5).

    조각 길이만 보면 "URL 을 잘라서 한도를 맞춘" 출력이 통과한다 — Codex 5차가
    측정한 것이 정확히 그 상태였다(4,092자 산문 URL 분절 · pass). 그래서 생성 후
    확인은 조각 길이와 **URL 온전성**을 함께 본다.
    """
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    # 치환을 무력화해 분절이 실제로 일어나게 한다
    monkeypatch.setattr(
        "alert.digest.composer.fit_prose_urls",
        lambda text, limit=composer_mod.KAKAO_CHUNK_LIMIT: (text, []),
    )
    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
        f"· 참고 [자료]({LONG_URL}) 입니다",
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    db = _bind(md)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["kakao_problems"], result["kakao_problems"]
    assert any("분절" in problem for problem in result["kakao_problems"])
    assert result["pass"] is False
    assert "카톡 조각 초과" in result["reason"]


def test_normal_body_has_no_kakao_problems(tmp_path, monkeypatch):
    """정상 본문에서는 조각 길이·URL 온전성 둘 다 문제 없다 (기준선)."""
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["kakao_problems"] == []
    assert result["pass"] is True


# ══ 사이클 10: 항목 줄 편집 불가 · 본문 전체 URL · 채널 동일성 ══════════
def _alive(monkeypatch, dead=()):
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive",
        lambda url, timeout=8: url not in set(dead),
    )


@pytest.mark.parametrize("edit", [
    ("[D-9] 사회적협동조합", "[D-109] 사회적협동조합"),        # 라벨·마감 조작
    ("· 마감 9/22", "· 마감 12/31"),                          # 마감 표기
    ("· 대상: 사협·사회적기업", "· 대상: 누구나"),             # 대상
    ("협동조합포털(기재부)", "산림청"),                        # 기관
])
def test_item_line_edit_requires_recompose(tmp_path, monkeypatch, edit):
    """항목 줄은 **편집 불가 영역**이다 (사이클 10 #2 / Codex 6차 HIGH #2).

    md 만 고쳐 마감·대상·기관·라벨을 바꾸고 재검토해도 통과했고, 바뀐 마감이
    카톡에 실렸다. 이제 정본이 담은 렌더 결과와 문자열이 다르면 멈춘다.
    """
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    before, after = edit
    md.write_text(SAMPLE_MD.replace(before, after, 1), encoding="utf-8")
    composer_mod.refresh_manifest_binding(md)       # 해시를 맞춰도
    _alive(monkeypatch)

    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False, edit
    assert any("항목 줄이 정본과 다름" in problem
               for problem in result["manifest_problems"]), result["manifest_problems"]

    # 재검토도 결속해주지 않는다
    monkeypatch.setattr("sys.argv", ["recheck_digest.py", str(md), "--db", str(db)])
    assert recheck_main() == 1
    check = json.loads((tmp_path / "2026-W37.check.json").read_text("utf-8"))
    assert check["pass"] is False
    assert "재조립 필요" in check["reason"]


def test_origin_line_edit_requires_recompose(tmp_path, monkeypatch):
    """원문 줄도 정본과 문자열이 같아야 한다 (사이클 10 #2)."""
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    md.write_text(
        SAMPLE_MD.replace("  [원문](https://example.com/a)",
                          "[원문](https://example.com/a)", 1),
        encoding="utf-8",
    )
    composer_mod.refresh_manifest_binding(md)
    _alive(monkeypatch)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert any("원문 줄이 정본과 다름" in problem
               for problem in result["manifest_problems"])


def test_db_deadline_change_requires_recompose(tmp_path, monkeypatch):
    """DB 마감이 바뀌면 정본이 낡았다 — 재조립을 요구한다 (사이클 10 #2)."""
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE announcements SET period_end = '2026-09-12' WHERE id = 11")
    conn.commit()
    conn.close()
    _alive(monkeypatch)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert any("DB 마감 변경" in problem
               for problem in result["manifest_problems"])


def test_editable_areas_still_pass(tmp_path, monkeypatch):
    """이번 주 한 줄·협의회에서·회원사 소식은 여전히 편집할 수 있다 (사이클 10 #2)."""
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    edited, _ = apply_headline(SAMPLE_MD, "이번 주는 인증 공고")
    edited, _ = apply_commentary(edited, "자격 요건 완화를 건의했습니다.")
    edited = edited.rstrip("\n") + "\n\n## 🏢 회원사 소식\n\n· 회원사A: 신규 사업 개시\n"
    md.write_text(edited, encoding="utf-8")
    composer_mod.refresh_manifest_binding(md)
    _alive(monkeypatch)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["manifest_problems"] == []
    assert result["pass"] is True


# ══ #3 맨몸 산문 URL 도 생존 검사 대상 ══════════════════════════════════
def test_bare_prose_url_is_checked_for_liveness(tmp_path, monkeypatch):
    """해설에 그냥 붙여넣은 죽은 주소도 검사한다 (Codex 6차 HIGH #3)."""
    dead = "https://dead.invalid/resource"
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    edited, _ = apply_headline(SAMPLE_MD, f"참고 {dead}")
    md.write_text(edited, encoding="utf-8")
    composer_mod.refresh_manifest_binding(md)

    assert dead in blocks_mod.bare_urls(edited)
    assert dead in blocks_mod.body_urls(edited)
    _alive(monkeypatch, dead=[dead])
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert dead in [item["url"] for item in result["dropped"]]


def test_bare_url_in_member_news_is_checked(tmp_path, monkeypatch):
    """회원사 소식의 맨몸 URL 도 마찬가지다 (#3)."""
    dead = "https://dead.invalid/member"
    body = SAMPLE_MD.rstrip("\n") + (
        f"\n\n## 🏢 회원사 소식\n\n· 회원사A: 자료는 {dead} 입니다\n"
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    db = _bind(md)
    _alive(monkeypatch, dead=[dead])
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert dead in [item["url"] for item in result["dropped"]]


# ══ #4 한 줄에 긴 링크 둘 ══════════════════════════════════════════════
LONG_A = "https://example.com/a" + "a" * 4180
LONG_B = "https://example.com/b" + "b" * 4080


def test_two_long_links_on_one_line_are_both_replaced(tmp_path, monkeypatch):
    """한 줄의 긴 링크 두 개가 모두 치환된다 (Codex 6차 MEDIUM #4).

    첫 치환 뒤 옛 좌표로 두 번째를 처리해, B 는 실제로 남았는데 "치환 완료" 로
    기록됐다 — 카톡에서는 분절되고 HTML 에는 B 링크가 그대로 실렸다.
    """
    from scripts.send_digest import markdown_to_html

    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
        f"· [A]({LONG_A}) [B]({LONG_B})",
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    db = _bind(md)
    _alive(monkeypatch)

    kakao = composer_mod.kakao_file_text_from_markdown(body)
    html = markdown_to_html(body)
    preview_body = preview_mod.render_preview("2026-W37", body, PASS_CHECK)
    for name, rendered in (("kakao", kakao), ("html", html),
                           ("preview", preview_body)):
        assert LONG_A not in rendered, name
        assert LONG_B not in rendered, name
    assert composer_mod.markdown_kakao_problems(body) == []
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert set(result["long_prose_urls"]) == {LONG_A, LONG_B}
    assert all(
        len(chunk) <= composer_mod.KAKAO_CHUNK_LIMIT
        for chunk in kakao.split(f"\n{KAKAO_CHUNK_SEPARATOR}\n")
    )


# ══ #5 항목 원문 URL 은 채널 간 동일 ═══════════════════════════════════
def test_item_origin_url_is_identical_across_channels(tmp_path):
    """md·미리보기·카톡·HTML 이 **같은 항목 URL** 을 싣는다 (Codex 6차 MEDIUM #5).

    공통 치환 함수가 항목 원문 줄까지 산문으로 처리해, 긴 항목 URL 이 채널마다
    달라졌다(md·미리보기엔 원문, HTML·재생성 카톡엔 안내 문구).
    """
    from scripts.send_digest import markdown_to_html

    long_item_url = "https://example.com/item" + "c" * 4060   # 항목으로 게시 가능
    body = SAMPLE_MD.replace("https://example.com/a", long_item_url)
    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")

    kakao = composer_mod.kakao_file_text_from_markdown(body)
    html = markdown_to_html(body)
    preview_body = preview_mod.render_preview("2026-W37", body, PASS_CHECK)
    for name, rendered in (("md", body), ("kakao", kakao), ("html", html),
                           ("preview", preview_body)):
        assert long_item_url in rendered, name
        assert composer_mod.URL_TOO_LONG_NOTICE not in rendered, name
    # 짧은 항목 URL 도 네 채널에서 같다
    for url in blocks_mod.item_urls(body):
        assert all(url in rendered for rendered in (body, kakao, html,
                                                    preview_body)), url


# ══ #6 선두 괄호의 장소 문맥 ═══════════════════════════════════════════
def test_leading_venue_bracket_is_not_a_region():
    """`[설명회 장소: 서울]` 은 선두에 있어도 자격 지역이 아니다 (Codex 6차 #6)."""
    from alert.digest.composer import infer_region, target_display, infer_target_tags

    for title in (
        "[설명회 장소: 서울] 사회적기업 지원사업 모집",
        "사회적기업 지원사업 모집 [설명회 장소: 서울]",
        "[개최 장소: 대전] 사회적기업 지원사업 모집",
        # 사이클 13 #4: `에서` 단독은 장소가 아니다 — 장소 동사가 따라야 한다
        "[서울에서 개최] 사회적기업 지원사업 모집",
    ):
        assert infer_region(title) is None, title
    # 자격 지역은 여전히 잡는다
    assert infer_region("[경기] 사회적기업 지원사업 모집") == "경기"
    tags = infer_target_tags("[설명회 장소: 서울] 사회적기업 지원사업 모집")
    assert "(서울)" not in target_display(tags, infer_region(
        "[설명회 장소: 서울] 사회적기업 지원사업 모집"
    ))


# ══ 사이클 11: 정본 구버전 · 표시문 URL · URL 단위 검증 ═════════════════
@pytest.mark.parametrize("field", [
    "line", "origin_line", "period_start", "period_end", "deadline_label",
])
def test_legacy_manifest_requires_recompose(tmp_path, monkeypatch, field):
    """신필드가 없는 **구버전 정본**은 통과하지 못한다 (사이클 11 #2).

    Codex 7차 HIGH #2 재현 입력: 사이클 9 이전 산출물의 정본을 그대로 두고 md 만
    마감을 고쳐 재검토하면 통과했다 — 없는 필드를 조용히 건너뛰었기 때문이다.
    """
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    manifest = composer_mod.load_items_manifest(md)
    for entry in manifest["items"]:
        entry.pop(field, None)
    composer_mod.write_items_manifest(md, manifest)

    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False, field
    assert any("정본 구버전 — 재조립 필요" in problem
               for problem in result["manifest_problems"]), result["manifest_problems"]

    # 재검토도 결속해주지 않는다
    monkeypatch.setattr("sys.argv", ["recheck_digest.py", str(md), "--db", str(db)])
    assert recheck_main() == 1


def test_legacy_manifest_blocks_deadline_edit(tmp_path, monkeypatch):
    """구버전 정본 + md 마감 편집 → 통과하지 못한다 (사이클 11 #2)."""
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    manifest = composer_mod.load_items_manifest(md)
    for entry in manifest["items"]:            # 사이클 9 형태로 되돌린다
        for field in ("line", "origin_line", "period_start", "period_end"):
            entry.pop(field, None)
    composer_mod.write_items_manifest(md, manifest)
    md.write_text(SAMPLE_MD.replace("· 마감 9/22", "· 마감 12/31", 1),
                  encoding="utf-8")
    composer_mod.refresh_manifest_binding(md)

    monkeypatch.setattr("sys.argv", ["recheck_digest.py", str(md), "--db", str(db)])
    assert recheck_main() == 1
    check = json.loads((tmp_path / "2026-W37.check.json").read_text("utf-8"))
    assert check["pass"] is False
    assert "재조립 필요" in check["reason"]


def test_db_period_start_change_requires_recompose(tmp_path, monkeypatch):
    """DB 게시일만 바뀌어도 재조립을 요구한다 (사이클 11 #2).

    Codex 7차 HIGH #2: `period_end` 는 그대로 두고 `period_start` 만 바꾸면
    통과했지만, 같은 DB 로 재조립하면 유효 마감이 달라져 배제된다.
    """
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE announcements SET period_start = '2026-09-01' WHERE id = 11")
    conn.commit()
    conn.close()
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert any("DB 게시일 변경" in problem
               for problem in result["manifest_problems"]), result["manifest_problems"]


DEAD_IN_TEXT = "https://dead.invalid/resource"


def test_dead_url_inside_link_text_is_checked(tmp_path, monkeypatch):
    """링크 **표시문** 안의 죽은 주소도 검사한다 (사이클 11 #3).

    Codex 7차 HIGH #3 재현 입력: `[https://dead/x](https://live/y)` 는 목적지만
    검사돼 통과했고, 사람 눈에 보이는(그리고 카톡에 그대로 실리는) dead 주소는
    검사되지 않았다.
    """
    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
        f"· 참고 [{DEAD_IN_TEXT}](https://live.example/ref)",
    )
    assert DEAD_IN_TEXT in blocks_mod.bare_urls(body)
    assert DEAD_IN_TEXT in blocks_mod.body_urls(body)
    assert "https://live.example/ref" in blocks_mod.body_urls(body)

    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    db = _bind(md)
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive",
        lambda url, timeout=8: url != DEAD_IN_TEXT,
    )
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert DEAD_IN_TEXT in [item["url"] for item in result["dropped"]]


def test_quoted_scheme_is_not_a_url(tmp_path, monkeypatch):
    """`"http://"` 같은 스킴 설명은 URL 후보가 아니다 (사이클 11 #6)."""
    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
        '· 스킴은 "http://" 또는 "https://" 입니다',
    )
    assert blocks_mod.bare_urls(body) == []
    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    db = _bind(md)
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is True, result["reason"]


LONG_1 = "https://example.com/one" + "1" * 4180
LONG_2 = "https://example.com/two" + "2" * 4080


def test_mixed_long_urls_are_verified_per_url(tmp_path, monkeypatch):
    """긴 URL 검증은 **URL 단위**다 (사이클 11 #4).

    Codex 7차 MEDIUM #4 재현 입력: 한 줄은 치환되고 다른 줄(베어 URL)은 분절된
    상태가, 출력 어딘가의 안내 문구 하나로 면제돼 통과했다.
    """
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
        f"· 참고 [A]({LONG_1})\n· 참고: {LONG_2}",
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    db = _bind(md)

    kakao = composer_mod.kakao_file_text_from_markdown(body)
    # 둘 다 치환되고 분절 조각이 남지 않는다
    for url in (LONG_1, LONG_2):
        assert url not in kakao, url[:40]
        assert url[:60] not in kakao, url[:40]
    assert composer_mod.markdown_kakao_problems(body) == []
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert set(result["long_prose_urls"]) == {LONG_1, LONG_2}
    assert result["kakao_problems"] == []
    assert result["pass"] is True, result["reason"]


def test_properly_replaced_long_link_is_not_flagged(tmp_path, monkeypatch):
    """정상 치환된 `[자료이름](긴 URL)` 은 분절로 오판되지 않는다 (사이클 11 #4)."""
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    url = "https://example.com/" + "q" * 4072       # 4,092자
    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
        f"· 참고 [자료이름]({url}) 입니다",
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    db = _bind(md)
    assert composer_mod.markdown_kakao_problems(body) == []
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["kakao_problems"] == []
    assert result["long_prose_urls"] == [url]
    assert result["pass"] is True, result["reason"]


# ══ 사이클 12: 괄호 URL · 카톡 단일 경로 · 출현별 검증 ═════════════════
PAREN_URL = "https://live.example/report(dead)"


def test_bare_url_with_parentheses_is_extracted_whole(tmp_path, monkeypatch):
    """괄호가 든 맨몸 URL 은 **전체**가 검사 대상이다 (Codex 8차 HIGH #2).

    예전에는 `[^\\s"\'<>()\\[\\]]+` 로 잘라서 `…/report` 만 검사했고, 실제로 죽어 있는
    전체 주소는 카톡에 그대로 실린 채 통과했다.
    """
    assert blocks_mod.bare_urls(f"참고 {PAREN_URL} 입니다") == [PAREN_URL]
    # 괄호 밖의 끝 구두점만 떼어낸다
    assert blocks_mod.bare_urls("참고 https://live.example/x. 끝") == [
        "https://live.example/x"
    ]
    assert blocks_mod.bare_urls(f"참고 {PAREN_URL}. 끝") == [PAREN_URL]

    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)", f"· 참고 {PAREN_URL}"
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    db = _bind(md)
    # 전체 주소는 dead, 접두(`/report`)는 alive 로 모킹한다
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive",
        lambda url, timeout=8: url != PAREN_URL,
    )
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert PAREN_URL in [item["url"] for item in result["dropped"]]


def test_kakao_first_render_equals_regeneration(tmp_path):
    """최초 카톡본 == 재생성본 (바이트 동일 — 사이클 12 #4).

    Codex 8차 MEDIUM #4: 회원사명 접두를 붙이는 별도 렌더 경로에서 길이 판정이
    달라져, 최초 카톡만 안내 문구로 치환되고 md·미리보기·HTML·재생성본에는 URL 이
    남았다. 카톡 파일을 만드는 경로를 md 렌더러 하나로 합쳤다.
    """
    from alert.digest.composer import compose_digest, kakao_file_text_from_markdown
    from tests.test_digest import _create_announcements_table, _insert_one

    long_url = "https://example.com/" + "m" * 4050
    forms = tmp_path / "forms.csv"
    forms.write_text(
        "접수일,회원사,유형,내용,관련정책\n"
        f"2026-09-10,{'회원사이름' * 6},동정,자료는 [자료]({long_url}) 입니다,\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "c12_kakao.db"
    _create_announcements_table(db_path)
    _insert_one(
        db_path, source="kofpi", source_id="k1",
        title="산림 지원사업 참여기업 모집 공고",
        url="https://example.com/k1", period_end="2026-12-31",
    )
    out = tmp_path / "2026-W13.md"
    markdown = compose_digest(
        db_path=str(db_path), week_str="2026-W13", output_path=out,
        forms_csv_path=forms,
    )
    first = (tmp_path / "2026-W13.kakao.txt").read_bytes()
    regenerated = kakao_file_text_from_markdown(markdown).encode("utf-8")
    assert first == regenerated

    # 회원사명 접두 때문에 길이 판정이 갈라지지 않는다 — 모든 채널이 **같은 결정**을 한다
    from scripts.send_digest import markdown_to_html
    kakao = first.decode("utf-8")
    html = markdown_to_html(markdown)
    preview_body = preview_mod.render_preview("2026-W13", markdown, PASS_CHECK)
    assert "https://example.com/k1" in markdown
    for rendered in (kakao, html, preview_body):
        assert "https://example.com/k1" in rendered
        # 회원사 소식의 URL 은 네 채널에서 같은 처분을 받는다 (보존 or 치환)
        assert (long_url in rendered) == (long_url in markdown), rendered[:40]
    assert composer_mod.markdown_kakao_problems(markdown) == []

    # 같은 소식이 한도를 넘기면 **모든 채널에서** 치환된다
    huge_url = "https://example.com/" + "h" * 4200
    huge = markdown.replace(long_url, huge_url)
    huge_kakao = kakao_file_text_from_markdown(huge)
    huge_html = markdown_to_html(huge)
    for rendered in (huge_kakao, huge_html):
        assert huge_url not in rendered
        assert composer_mod.URL_TOO_LONG_NOTICE in rendered
    assert composer_mod.markdown_kakao_problems(huge) == []


REPEAT_URL = "https://example.com/repeat" + "r" * 4060


def test_repeated_long_url_is_verified_per_occurrence(tmp_path, monkeypatch):
    """같은 URL 이 여러 번 나와도 **출현별**로 검증한다 (Codex 8차 MEDIUM #5).

    예전에는 첫 출현이 온전하면 두 번째 출현의 분절을 면제했다.
    판정을 토큰 경계로 바꿔, 결과물에 원본에 없는 URL 토큰이 있으면 분절로 본다.
    """
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
        f"· 참고자료 원문주소: {REPEAT_URL}\n· 다시: {REPEAT_URL}",
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    db = _bind(md)

    kakao = composer_mod.kakao_file_text_from_markdown(body)
    # 분절 조각(원본에 없는 URL 토큰)이 없다
    assert composer_mod.markdown_kakao_problems(body) == []
    assert all(
        len(chunk) <= composer_mod.KAKAO_CHUNK_LIMIT
        for chunk in kakao.split(f"\n{KAKAO_CHUNK_SEPARATOR}\n")
    )
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["kakao_problems"] == []
    assert result["pass"] is True, result["reason"]


def test_shared_prefix_urls_are_not_false_positives(tmp_path, monkeypatch):
    """접두를 공유하는 정상 URL 이 조각으로 오판되지 않는다 (사이클 12 #5)."""
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    prefix = "https://example.com/shared/prefix/that/is/long/enough/to/matter"
    short = prefix + "/ok"
    long_url = prefix + "/" + "z" * 4100
    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
        f"· 짧은 [A]({short})\n· 긴 [B]({long_url})",
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    db = _bind(md)

    kakao = composer_mod.kakao_file_text_from_markdown(body)
    assert short in kakao                  # 짧은 URL 은 온전히 실린다
    assert long_url not in kakao           # 긴 URL 만 치환된다
    assert composer_mod.markdown_kakao_problems(body) == []
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["kakao_problems"] == []
    assert result["long_prose_urls"] == [long_url]
    assert result["pass"] is True, result["reason"]


SPLIT_URL = "https://example.com/split" + "s" * 4200


def test_split_url_still_fails_with_token_rule(tmp_path, monkeypatch):
    """치환을 무력화하면 분절 조각이 잡힌다 (사이클 12 #5 — 규칙이 비지 않았다)."""
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    monkeypatch.setattr(
        "alert.digest.composer.fit_prose_urls",
        lambda text, limit=composer_mod.KAKAO_CHUNK_LIMIT: (text, []),
    )
    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
        f"· 참고 [자료]({SPLIT_URL}) 입니다",
    )
    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    db = _bind(md)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["kakao_problems"], result["kakao_problems"]
    assert any("분절" in problem for problem in result["kakao_problems"])
    assert result["pass"] is False


# ══ 사이클 13: 정본 스키마 버전 · DB 재계산 · 출현별 URL · 미리보기 ══════
def test_previous_schema_version_manifest_fails(tmp_path, monkeypatch):
    """렌더 규칙이 바뀌기 전 버전의 정본은 재조립을 요구한다 (사이클 13 #1).

    Codex 9차 HIGH #1 재현: `fe7f365` 에서 조립한 정본(필수 필드는 모두 있음)을
    최신 코드로 재검토하면 통과했다 — 그 사이 마감 근거 규칙이 바뀌었는데도.
    필드의 **존재**가 아니라 **규칙 버전**이 정본의 유효기간을 정한다.
    """
    _alive(monkeypatch)
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    manifest = composer_mod.load_items_manifest(md)
    assert manifest["schema_version"] == composer_mod.ITEMS_SCHEMA_VERSION

    manifest["schema_version"] = composer_mod.ITEMS_SCHEMA_VERSION - 1
    composer_mod.write_items_manifest(md, manifest)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert "정본 구버전 — 재조립 필요" in " ".join(result["manifest_problems"])


def test_manifest_without_schema_version_fails(tmp_path, monkeypatch):
    """버전 키가 아예 없는 예전 정본도 구버전이다 (사이클 13 #1)."""
    _alive(monkeypatch)
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    manifest = composer_mod.load_items_manifest(md)
    manifest.pop("schema_version")
    composer_mod.write_items_manifest(md, manifest)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert "정본 구버전 — 재조립 필요" in " ".join(result["manifest_problems"])


def test_db_summary_change_requires_recompose(tmp_path, monkeypatch):
    """대상 태그의 근거가 바뀌면 정본은 무효다 (사이클 13 #2).

    Codex 9차 MEDIUM #4 재현: summary 만 고쳐 새 조립의 대상이 달라졌는데도
    재검토는 예전 대상을 그대로 pass 했다. 항목 줄 렌더 동일성 검사의 입력은
    정본이 아니라 **DB 에서 재계산한 값**이어야 한다.
    """
    _alive(monkeypatch)
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    assert check_digest(
        db_path=str(db), markdown_path=md, output_path=None
    )["pass"] is True

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE announcements SET summary = ? WHERE id = 11", ("협동조합 대상",)
    )
    conn.commit()
    conn.close()
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    joined = " ".join(result["manifest_problems"])
    assert "DB 대상 변경" in joined and "재조립 필요" in joined
    assert "협동조합" in joined


def test_manifest_region_must_match_the_db_recomputation(tmp_path, monkeypatch):
    """정본이 적어 둔 지역도 DB 재계산과 같아야 한다 (사이클 13 #2)."""
    _alive(monkeypatch)
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    manifest = composer_mod.load_items_manifest(md)
    manifest["items"][0]["region"] = "경기"        # DB 제목에는 지역이 없다
    composer_mod.write_items_manifest(md, manifest)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert "DB 지역 변경" in " ".join(result["manifest_problems"])


OCCUR_URL = "https://example.com/occur/" + "o" * 4100


def _replace_only_the_first(text, limit=None):
    """첫 출현만 치환하는 가짜 치환기 — 집합 비교의 맹점을 그대로 만든다."""
    index = text.find(OCCUR_URL)
    if index < 0:
        return text, []
    replaced = (
        text[:index]
        + composer_mod.URL_TOO_LONG_NOTICE
        + text[index + len(OCCUR_URL):]
    )
    return replaced, [OCCUR_URL]


def test_url_occurrences_are_counted_not_deduped(tmp_path, monkeypatch):
    """같은 URL 이 두 번 나오면 두 번 다 확인한다 (사이클 13 #3).

    Codex 9차 MEDIUM #2 재현: 첫 출현이 치환 로그를 만족시키면 두 번째 출현이
    조각 경계에서 잘려도 집합 비교가 통과했다(온전한 출현 2→1). 이제 출현 수를
    센다 — `치환된 수 + 온전히 남은 수 == 원본 출현 수` 여야 한다.
    """
    _alive(monkeypatch)
    monkeypatch.setattr(
        "alert.digest.composer.fit_prose_urls", _replace_only_the_first
    )
    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
        f"· 첫 출현 {OCCUR_URL} 입니다\n· 둘째 출현 {OCCUR_URL} 입니다",
    )
    problems = composer_mod.markdown_kakao_problems(body)
    assert any("URL 출현 불일치(2→1)" in problem for problem in problems), problems

    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    db = _bind(md)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert any(
        "출현 불일치" in problem for problem in result["kakao_problems"]
    ), result["kakao_problems"]


DOT_URL = "https://example.com/dot/" + "d" * 4200


def test_trailing_dot_bare_url_substitution_passes(tmp_path, monkeypatch):
    """끝 구두점이 붙은 4,200자 베어 URL 의 정상 치환은 pass 다 (사이클 13 #3).

    Codex 9차 MEDIUM #2 후단 재현: `<URL>.` 의 마침표까지 URL 로 세면 치환 로그와
    원본 토큰이 어긋나 **정상 치환이 유실로 오판**됐다.
    """
    _alive(monkeypatch)
    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
        f"· 자세한 내용은 {DOT_URL}. 참고하세요",
    )
    assert composer_mod.markdown_kakao_problems(body) == []
    kakao = composer_mod.kakao_file_text_from_markdown(body)
    assert DOT_URL not in kakao
    assert composer_mod.URL_TOO_LONG_NOTICE in kakao

    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    db = _bind(md)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["kakao_problems"] == []
    assert result["pass"] is True, result["reason"]


def test_preview_keeps_prose_headings(tmp_path):
    """해설·회원사 소식의 `# `·`## ` 줄은 미리보기에도 남는다 (사이클 13 #5).

    Codex 9차 MEDIUM #5 재현: 협의회 섹션에 `# 이번 지원은 경기 소재 기업만…` 을
    넣으면 카톡에는 실리는데 미리보기에는 없었다 — 승인한 화면과 발송물이 다르다.
    생략 대상은 **문서 제목 하나**뿐이다.
    """
    text = "이번 지원은 경기 소재 기업만 신청 가능합니다"
    sub = "참고 자료"
    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
        f"# {text}\n## {sub}\n· 면담 2건",
    )
    rendered = preview_mod.render_preview("2026-W37", body, PASS_CHECK)
    kakao = composer_mod.kakao_file_text_from_markdown(body)

    # 카톡과 같은 텍스트 — 둘 다 `#` 표기만 걷어낸다
    assert text in kakao
    assert text in rendered
    assert rendered.count(text) == 1
    assert f"■ {sub}" in rendered              # 섹션 제목 표기는 종전대로
    # 문서 제목 **하나만** 생략된다 (미리보기 자체 머리글 뒤에는 없다)
    assert "협의회 주간 정책브리핑 2026-W37 (9/7~9/13)" not in rendered.split("\n", 1)[1]


# ══ 사이클 14: 기관 대조 · 중간 괄호 지역 · 링크 표시문 치환 기록 ═══════
def test_manifest_org_must_match_the_db_source(tmp_path, monkeypatch):
    """정본의 기관 표시명도 DB source 에서 재계산한 값이어야 한다 (사이클 14 #1).

    항목 줄에 실리는 세 값(기관·대상·지역) 중 기관만 정본을 그대로 믿으면, 그 값은
    검증 밖이다 — 기관은 독자가 신뢰를 거는 이름이라 바뀌면 안 된다.
    """
    _alive(monkeypatch)
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    manifest = composer_mod.load_items_manifest(md)
    manifest["items"][0]["org"] = "산림청"        # DB source 는 kofpi
    composer_mod.write_items_manifest(md, manifest)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    joined = " ".join(result["manifest_problems"])
    assert "DB 기관 변경" in joined and "재조립 필요" in joined
    assert "한국임업진흥원" in joined


def test_db_source_change_requires_recompose(tmp_path, monkeypatch):
    """DB 의 source 가 바뀌면(기관 표시명이 달라지면) 재조립을 요구한다 (사이클 14 #1)."""
    _alive(monkeypatch)
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE announcements SET source = ? WHERE id = 11", ("fowi",))
    conn.commit()
    conn.close()
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert "DB 기관 변경" in " ".join(result["manifest_problems"])


SELF_LINK_URL = "https://example.com/self/" + "s" * 4100


def test_link_whose_text_is_the_url_passes(tmp_path, monkeypatch):
    """`[U](U)` 의 정상 치환은 pass 다 (사이클 14 #3).

    표시문에도 URL 이 있으면 원본 출현은 2 인데 치환 기록은 목적지 1 뿐이어서,
    사이클 13 의 출현 수 검증이 **정상 치환을 유실로** 잡았다. 기록 단위를
    "지워지는 구간의 모든 URL 출현" 으로 바꿔 맞춘다.
    """
    _alive(monkeypatch)
    body = SAMPLE_MD.replace(
        "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
        f"· 참고 [{SELF_LINK_URL}]({SELF_LINK_URL}) 입니다",
    )
    source_count = blocks_mod.body_urls(body, unique=False).count(SELF_LINK_URL)
    assert source_count == 2, source_count
    _, replaced = composer_mod.fit_prose_urls(body)
    assert replaced.count(SELF_LINK_URL) == 2, replaced

    assert composer_mod.markdown_kakao_problems(body) == []
    kakao = composer_mod.kakao_file_text_from_markdown(body)
    assert SELF_LINK_URL not in kakao
    assert composer_mod.URL_TOO_LONG_NOTICE in kakao

    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    db = _bind(md)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["kakao_problems"] == []
    assert result["pass"] is True, result["reason"]


def test_middle_bracket_region_reaches_the_item_line(tmp_path):
    """중간 괄호의 지역이 항목 줄 대상 표기까지 간다 (사이클 14 #2)."""
    title = "사회적기업 지원사업 [경기 소재 기업] 모집"
    classification = composer_mod.classify_item(title, "", "kofpi")
    assert classification.region == "경기"
    assert composer_mod.target_display(
        classification.tags, classification.region
    ) == "사회적기업(경기)"


# ══ 테스트 하네스 자체 검증 ════════════════════════════════════════════
def test_forbid_notifier_propagates_when_gate_is_open(tmp_path, monkeypatch):
    """게이트가 열려 있으면 `_forbid_notifier` 가 **반드시 터진다**.

    이전 하네스는 `AssertionError` 를 던졌고 send_digest 의 `except Exception` 이
    그것을 "초기화 실패"로 삼켜 exit 2 를 돌려줬다 — 게이트가 뚫려도 테스트가
    통과했다. `BaseException` 으로 바꾼 뒤 그 창이 닫혔는지 여기서 확인한다.
    """
    md = _annotated_digest(tmp_path)        # 통과 조건을 모두 갖춘 상태
    _forbid_notifier(monkeypatch, "게이트가 열려 있다")
    with pytest.raises(GateBreached):
        _send(md)


# ══ 사이클5 #1: 제외 검사는 본문 전체 URL 기준 ═════════════════════════
HIDDEN = "https://example.com/hidden"


def test_body_links_covers_every_url():
    """항목·해설·산문·머리말의 모든 링크를 센다 (사이클5 #1)."""
    body = SAMPLE_MD.replace(
        preview_mod.MARKER, f"협의회 의견 [참고]({HIDDEN}) 였습니다"
    ).replace(
        "  [원문](https://example.com/c)",
        f"  [원문](https://example.com/c)\n· 부록 [자료]({HIDDEN}2)",
    )
    links = prune_mod.body_links(body)
    assert HIDDEN in links              # 해설의 참고 링크
    assert f"{HIDDEN}2" in links        # 항목 제목의 두 번째 링크
    assert "https://example.com/a" in links
    # 항목 URL 목록보다 넓다
    assert set(prune_mod.body_links(body)) > {
        block["url"] for block in prune_mod.item_blocks(body)
    }


def test_send_digest_refuses_excluded_url_hidden_in_commentary(
    tmp_path, monkeypatch, capsys
):
    """제외 URL 을 해설 참고 링크로 옮겨도 거부된다 (Codex 사이클5 #1 재현)."""
    annotated = SAMPLE_MD.replace(
        preview_mod.MARKER, f"확정 의견 — 참고 [자료]({HIDDEN})"
    )
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    state_path = state_mod.state_path_for_markdown(md)
    state = state_mod.load_state(state_path, "2026-W37")
    state_mod.save_state(state_path, dict(state, excluded_urls=[HIDDEN]))
    _forbid_notifier(monkeypatch, "제외 URL 이 해설에 남았는데 발송했다")
    assert _send(md) == 2
    assert state_mod.EXCLUDED_NOT_APPLIED_REASON in capsys.readouterr().err


def test_send_digest_refuses_excluded_url_as_second_item_link(
    tmp_path, monkeypatch
):
    annotated, _ = apply_commentary(
        SAMPLE_MD.replace(
            "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
            f"· 관련 [부록]({HIDDEN})"
        ),
        "확정 의견",
    )
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    state_path = state_mod.state_path_for_markdown(md)
    state_mod.save_state(state_path, dict(
        state_mod.load_state(state_path, "2026-W37"), excluded_urls=[HIDDEN]))
    _forbid_notifier(monkeypatch, "제외 URL 이 두 번째 링크로 남았는데 발송했다")
    assert _send(md) == 2


# ══ 사이클5 #2: 승인 세대의 검증 결속 ══════════════════════════════════
def test_send_digest_refuses_when_check_bytes_changed(
    tmp_path, monkeypatch, capsys
):
    """본문 SHA 가 같아도 렌더에 쓴 검증이 바뀌면 거부 (Codex 사이클5 #3 재현)."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    check_path = md.with_suffix(".check.json")
    payload = json.loads(check_path.read_text(encoding="utf-8"))
    payload["reason"] = ""          # 같은 본문 SHA, 다른 check 바이트
    payload["items"] = payload["items"] + [
        {"url": "https://example.com/a", "url_alive": True, "passed": True}
    ]
    check_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _forbid_notifier(monkeypatch, "검증 파일이 바뀌었는데 발송했다")
    assert _send(md) == 2
    assert state_mod.STALE_CHECK_APPROVAL_REASON in capsys.readouterr().err


def test_check_approval_requires_check_sha():
    state = state_mod.record_preview(
        state_mod.default_state("2026-W37"), [1], [], "a" * 64, "c" * 64
    )
    approval_id = state_mod.approval_of(state)["id"]
    assert state_mod.check_approval(state, approval_id, "a" * 64, "c" * 64) == (
        True, ""
    )
    ok, reason = state_mod.check_approval(state, approval_id, "a" * 64, "d" * 64)
    assert ok is False and reason == state_mod.STALE_CHECK_APPROVAL_REASON
    # check_sha 를 발급하지 않은 세대는 검증 결속을 요구하면 거부된다
    legacy = state_mod.record_preview(
        state_mod.default_state("2026-W37"), [1], [], "a" * 64
    )
    legacy_id = state_mod.approval_of(legacy)["id"]
    assert state_mod.check_approval(legacy, legacy_id, "a" * 64, "c" * 64) == (
        False, state_mod.STALE_CHECK_APPROVAL_REASON
    )


@pytest.mark.parametrize("patch,expected_block", [
    ({"pass": False, "reason": "생존 항목 없음"}, "검증 미통과"),
    ({"items": []}, "검증에 항목이 0건"),
    ({"item_blocks": 0}, "항목 0건"),
])
def test_notify_issues_no_approval_for_failed_check(
    tmp_path, monkeypatch, patch, expected_block
):
    """pass=false·항목 0건이면 승인 세대를 발급하지 않는다 (사이클5 #2)."""
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated, dict(PASS_CHECK, **patch))
    sent = []
    monkeypatch.setattr(
        notify_digest, "resolve_target", lambda topic_key="council": (-100, 2011)
    )
    monkeypatch.setattr(notify_digest, "resolve_token", lambda: "123:FAKE")
    monkeypatch.setattr(
        notify_digest, "send_chunk",
        lambda token, chat, thread, text: (sent.append(text), (True, 2014, ""))[1],
    )
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 0
    assert sent and expected_block in sent[0]
    assert "1. [" not in sent[0]         # 항목 미리보기 없음
    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37"
    )
    assert state["approval"] is None


# ══ 사이클5 #3: tombstone ══════════════════════════════════════════════
def test_tombstone_blocks_send_and_preview(tmp_path, monkeypatch):
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    tombstone = state_mod.tombstone_path_for_markdown(md)
    state_mod.write_tombstone(
        tombstone, "무효화 실패: 디스크 오류", "2026-09-13T03:00:00"
    )
    assert state_mod.tombstone_reason(tombstone)

    _forbid_notifier(monkeypatch, "tombstone 이 있는데 발송했다")
    assert _send(md) == 2
    assert send_digest(md, dry_run=True, db_path=_db_for(md)) == 2       # 드라이런도 거부

    sent = []
    monkeypatch.setattr(
        notify_digest, "resolve_target", lambda topic_key="council": (-100, 2011)
    )
    monkeypatch.setattr(notify_digest, "resolve_token", lambda: "123:FAKE")
    monkeypatch.setattr(
        notify_digest, "send_chunk",
        lambda token, chat, thread, text: (sent.append(text), (True, 2014, ""))[1],
    )
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 0
    assert "무효화 실패" in sent[0] and "1. [" not in sent[0]

    assert state_mod.clear_tombstone(tombstone) is True
    assert state_mod.tombstone_reason(tombstone) is None


# ══ 사이클5 #4: redact 중앙화 ══════════════════════════════════════════
LEAK_TOKEN = "1401666801:AAHt9Xk2mQpLzR7vNbC3dEfGhIjKlMnOpQrSt"


def test_sender_stderr_redacts_check_reason(tmp_path, monkeypatch, capsys):
    """check.reason 의 토큰이 발송기 stderr 로 새지 않는다 (사이클5 #4)."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(
        tmp_path, annotated,
        dict(PASS_CHECK, **{"pass": False, "reason": f"검증 실패 {LEAK_TOKEN}"}),
    )
    _seed_preview(md)
    _forbid_notifier(monkeypatch, "pass=false 인데 발송했다")
    assert _send(md) == 2
    err = capsys.readouterr().err
    assert LEAK_TOKEN not in err
    assert "<redacted>" in err


def test_notify_preview_body_redacts_check_reason(tmp_path, monkeypatch):
    """미리보기 본문에 실리는 check.reason 도 가려진다 (사이클5 #4)."""
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(
        tmp_path, annotated,
        dict(PASS_CHECK, **{"pass": False, "reason": f"생존 항목 없음 {LEAK_TOKEN}"}),
    )
    sent = []
    monkeypatch.setattr(
        notify_digest, "resolve_target", lambda topic_key="council": (-100, 2011)
    )
    monkeypatch.setattr(notify_digest, "resolve_token", lambda: "123:FAKE")
    monkeypatch.setattr(
        notify_digest, "send_chunk",
        lambda token, chat, thread, text: (sent.append(text), (True, 2014, ""))[1],
    )
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 0
    assert sent and LEAK_TOKEN not in sent[0]
    assert "<redacted>" in sent[0]


# ══ 사이클5 #5: 발송 중에는 미리보기를 보내지 않는다 ═══════════════════
def test_notify_skips_preview_while_send_holds_lock(tmp_path, monkeypatch):
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    holder = state_mod.acquire_lock(state_mod.lock_path_for_markdown(md))
    monkeypatch.setenv(state_mod.LOCK_TIMEOUT_ENV, "0.2")
    sent = []
    monkeypatch.setattr(
        notify_digest, "resolve_target", lambda topic_key="council": (-100, 2011)
    )
    monkeypatch.setattr(notify_digest, "resolve_token", lambda: "123:FAKE")
    monkeypatch.setattr(
        notify_digest, "send_chunk",
        lambda token, chat, thread, text: (sent.append(text), (True, 2014, ""))[1],
    )
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    try:
        # 사이클8 #5: 잠금 타임아웃 종료 코드는 다섯 CLI 모두 2 다.
        assert notify_digest.main() == 2
    finally:
        state_mod.release_lock(holder)
    assert len(sent) == 1
    assert "발송 진행 중" in sent[0]
    assert "1. [" not in sent[0]         # 미리보기 본문은 나가지 않았다


# ══ 사이클6 #3: URL 추출 단일 함수 ══════════════════════════════════════
@pytest.mark.parametrize("line,expected", [
    ("참고 https://example.com/hidden 입니다", ["https://example.com/hidden"]),
    ("[자료](https://example.com/report(1))", ["https://example.com/report(1)"]),
    ("**원문:** [https://x/c](https://x/c)", ["https://x/c"]),
    ("**원문:** https://raw.example/p", ["https://raw.example/p"]),
    ("끝 https://x/y. 그리고 (https://x/z) 끝", ["https://x/y", "https://x/z"]),
    ("문장 https://x/q), 다음", ["https://x/q"]),
    ("링크 없음", []),
])
def test_body_urls_extraction(line, expected):
    assert prune_mod.body_urls(line) == expected


@pytest.mark.parametrize("hidden,injected", [
    ("https://example.com/bare", "확정 의견 — 참고 https://example.com/bare"),
    ("https://example.com/report(1)", "확정 의견 [자료](https://example.com/report(1))"),
])
def test_send_digest_refuses_excluded_url_in_any_form(
    tmp_path, monkeypatch, hidden, injected
):
    """베어 URL·괄호 링크로 숨긴 제외 URL 도 거부된다 (사이클6 #3)."""
    md = _write_digest(tmp_path, SAMPLE_MD.replace(preview_mod.MARKER, injected))
    _seed_preview(md)
    state_path = state_mod.state_path_for_markdown(md)
    state_mod.save_state(state_path, dict(
        state_mod.load_state(state_path, "2026-W37"), excluded_urls=[hidden]))
    _forbid_notifier(monkeypatch, f"제외 URL({hidden})이 남았는데 발송했다")
    assert _send(md) == 2


# ══ 사이클6 #2: 제어 문자 fail-closed ══════════════════════════════════
def _empty_db(tmp_path):
    """항목이 없는 DB — 제어 문자 게이트는 DB 조회 **전에** 멈춘다."""
    return _db_with(tmp_path, [])



CTRL_BODY = SAMPLE_MD.replace(
    "· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
    "· 기록 없음\x0b## 기타\x0b[자료](https://x/y)",
)


def test_control_chars_detected():
    assert prune_mod.control_chars(CTRL_BODY) == ["\x0b"]
    assert "\\x0b" in prune_mod.control_chars_label(CTRL_BODY)
    assert prune_mod.control_chars(SAMPLE_MD) == []
    assert prune_mod.control_chars("탭\t과 개행\n은 허용") == []
    for char in "\r\v\f\x1c\x1d\x1e\x85  ":
        assert prune_mod.control_chars(f"a{char}b") == [char]


def test_checker_fails_on_control_chars(tmp_path, monkeypatch):
    md = tmp_path / "2026-W37.md"
    md.write_text(CTRL_BODY, encoding="utf-8")
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(db_path=str(_empty_db(tmp_path)), markdown_path=md)
    assert result["pass"] is False
    assert "제어 문자 포함" in result["reason"]
    assert result["item_blocks"] == 0


def test_parsers_agree_on_control_char_body():
    """split 과 splitlines 가 달라지던 본문 — 이제 모든 파서가 split 만 쓴다."""
    assert prune_mod.item_block_count(CTRL_BODY) == 4
    assert len(preview_mod.item_urls(CTRL_BODY)) == 4
    assert len(preview_mod.parse_digest(CTRL_BODY)["items"]) == 4


def test_send_digest_refuses_control_chars(tmp_path, monkeypatch, capsys):
    md = _write_digest(tmp_path, CTRL_BODY.replace(preview_mod.MARKER, "확정 의견"))
    _seed_preview(md)
    _forbid_notifier(monkeypatch, "제어 문자 본문을 발송했다")
    assert _send(md) == 2
    assert "제어 문자" in capsys.readouterr().err


def test_send_digest_refuses_rendered_count_mismatch(
    tmp_path, monkeypatch, capsys
):
    """렌더될 항목 수가 정본과 다르면 거부 (사이클6 → 통합 1 #1).

    근거는 check.json 의 숫자가 아니라 **정본(items.json)** 이다. 본문에서 항목
    블록 하나를 지우면 정본(4) ≠ 본문(3) 이 되어 멈춘다.
    """
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    body = md.read_text(encoding="utf-8")
    start = body.index("<!-- item id=13 -->")
    end = body.index("\n\n", body.index("example.com/c")) + 2
    md.write_text(body[:start] + body[end:], encoding="utf-8")
    composer_mod.refresh_manifest_binding(md)   # 해시만 다시 맞춘다(항목은 그대로)
    check_path = md.with_suffix(".check.json")
    check = json.loads(check_path.read_text(encoding="utf-8"))
    check["markdown_sha256"] = markdown_sha256(md.read_bytes())
    check_path.write_text(json.dumps(check, ensure_ascii=False), encoding="utf-8")
    _seed_preview(md)
    _forbid_notifier(monkeypatch, "항목 수가 다른데 발송했다")
    assert _send(md) == 2
    assert "항목 수" in capsys.readouterr().err


# ══ 사이클6 #1: 잠금 밖 선행 검사 없음 ═════════════════════════════════
def test_send_digest_checks_everything_inside_lock(tmp_path, monkeypatch):
    """잠금을 남이 쥐고 있으면 **어떤 판정도 하지 않고** LockBusy 로 끝난다."""
    md = _annotated_digest(tmp_path)
    holder = state_mod.acquire_lock(state_mod.lock_path_for_markdown(md))
    read_calls = []
    real_read = Path.read_bytes

    def counted(self, *args, **kwargs):
        read_calls.append(str(self))
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", counted)
    _forbid_notifier(monkeypatch, "잠금 없이 발송을 시작했다")
    try:
        assert _send(md) == 2
    finally:
        state_mod.release_lock(holder)
    # 잠금을 못 얻었으므로 md·check 바이트를 읽지 않았다
    assert not [c for c in read_calls if c.endswith(".md")]
    assert not [c for c in read_calls if c.endswith(".check.json")]


def test_tombstone_is_checked_inside_lock(tmp_path, monkeypatch):
    """표식을 잠금 획득 직후에 본다 — 잠금 전 검사 후 생성되는 경합이 없다."""
    md = _annotated_digest(tmp_path)
    tombstone = state_mod.tombstone_path_for_markdown(md)
    real_acquire = state_mod.acquire_lock

    def acquire_then_break(path, *args, **kwargs):
        handle = real_acquire(path, *args, **kwargs)
        # 잠금 획득과 판정 사이에 표식이 생기는 최악의 순간을 만든다
        state_mod.write_tombstone(tombstone, "경합 중 생성", "2026-09-13T05:00:00")
        return handle

    monkeypatch.setattr("scripts.send_digest.state_mod.acquire_lock",
                        acquire_then_break)
    _forbid_notifier(monkeypatch, "표식이 생겼는데 발송했다")
    assert _send(md) == 2


# ══ 사이클6 #8: apply_commentary 플래그 인터페이스 ══════════════════════
def _run_apply(tmp_path, *args):
    import subprocess

    return subprocess.run(
        [sys.executable, "scripts/apply_commentary.py", "2026-W37",
         *args, "--out-dir", str(tmp_path)],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )


def test_apply_commentary_flag_interface(tmp_path):
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    proc = _run_apply(tmp_path, "--commentary", "플래그로 넣은 의견")
    assert proc.returncode == 0, proc.stderr
    body = md.read_text(encoding="utf-8")
    # 해설은 `## 🤝 협의회에서` 섹션 본문이다 — 한 줄 자리의 마커는 그대로 남는다
    assert "· 플래그로 넣은 의견" in body
    assert preview_mod.MARKER in body
    # 한 줄 자리는 --headline 이 채운다 (서로 다른 자리, 사이클 7 #3)
    proc = _run_apply(tmp_path, "--headline", "한 줄 확정")
    assert proc.returncode == 0, proc.stderr
    body = md.read_text(encoding="utf-8")
    assert "이번 주 한 줄: 한 줄 확정" in body
    assert preview_mod.MARKER not in body


def test_apply_commentary_rejects_positional_and_missing_flag(tmp_path):
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    assert _run_apply(tmp_path, "위치인자").returncode == 2
    assert _run_apply(tmp_path).returncode == 2
    assert md.read_text(encoding="utf-8") == SAMPLE_MD
    # 둘을 한 번에 주는 것은 **허용**이다 — 서로 다른 자리라 섞이지 않는다
    # (사이클 7 #3: 추론 금지의 대상은 위치 인자이지 플래그 조합이 아니다).
    assert _run_apply(
        tmp_path, "--commentary", "의견", "--headline", "한 줄"
    ).returncode == 0


HEADLINE_MD = SAMPLE_MD


def test_headline_and_commentary_target_different_markers():
    """`--headline` 은 한 줄 자리만, `--commentary` 는 협의회에서 섹션만 채운다."""
    headlined, ok = apply_headline(HEADLINE_MD, "이번 주 요지")
    assert ok is True
    assert "이번 주 한 줄: 이번 주 요지" in headlined
    assert preview_mod.MARKER not in headlined          # 한 줄 자리의 마커만 사라진다
    assert "· (면담·건의·수렴 현황 — 이번 주 기록 없음)" in headlined

    commented, ok = apply_commentary(HEADLINE_MD, "협의회 의견 본문")
    assert ok is True
    assert "이번 주 한 줄: <!-- 상민 확정 필요 -->" in commented   # 한 줄은 그대로
    assert "· 협의회 의견 본문" in commented

    both, _ = apply_commentary(headlined, "협의회 의견 본문")
    assert preview_mod.MARKER not in both
    assert "· 협의회 의견 본문" in both


def test_apply_headline_via_cli(tmp_path):
    md = tmp_path / "2026-W37.md"
    md.write_text(HEADLINE_MD, encoding="utf-8")
    proc = _run_apply(tmp_path, "--headline", "한 줄 확정")
    assert proc.returncode == 0, proc.stderr
    body = md.read_text(encoding="utf-8")
    assert "이번 주 한 줄: 한 줄 확정" in body
    # v2.1 본문의 마커는 한 줄 자리에 하나뿐이다 — 그 자리만 채워졌다
    assert body.count(preview_mod.MARKER) == 0
    assert "· (면담·건의·수렴 현황 — 이번 주 기록 없음)" in body
    # 한 줄은 본문 편집이므로 status 를 annotated 로 올리지 않는다
    assert state_mod.load_state(
        state_mod.state_path("2026-W37", tmp_path), "2026-W37"
    )["status"] == "draft"


def test_apply_commentary_rejects_control_chars(tmp_path):
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    proc = _run_apply(tmp_path, "--commentary", "의견\x0b숨김")
    assert proc.returncode == 2
    assert md.read_text(encoding="utf-8") == SAMPLE_MD


# ══ 사이클6 #9: 엄격 주차 ══════════════════════════════════════════════
@pytest.mark.parametrize("week,ok", [
    ("2026-W01", True), ("2026-W53", True), ("2026-W37", True),
    ("2026-W00", False), ("2026-W54", False), ("2026-W99", False),
    ("2026-W37\n", False), ("2026-w37", False), ("26-W37", False),
    ("", False), (None, False),
])
def test_valid_week(week, ok):
    assert state_mod.valid_week(week) is ok


def test_send_digest_refuses_non_week_filename(tmp_path, monkeypatch):
    md = tmp_path / "digest.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    _forbid_notifier(monkeypatch, "주차 이름이 아닌 파일을 발송했다")
    assert send_digest(
        md, dry_run=False, approved_by="1", approval_id="0" * 12
    ) == 2


# ══ 사이클6 #7: main() 전체 redact ══════════════════════════════════════
def test_guarded_main_redacts_traceback(monkeypatch, capsys):
    from scripts import send_digest as send_mod

    leak = "1401666801:AAHt9Xk2mQpLzR7vNbC3dEfGhIjKlMnOpQrSt"

    def boom():
        raise RuntimeError(f"터짐 {leak}")

    monkeypatch.setattr(send_mod, "main", boom)
    assert send_mod.guarded_main() == 70
    err = capsys.readouterr().err
    assert leak not in err and "<redacted>" in err
    assert "Traceback" in err


def test_notify_guarded_main_redacts_traceback(monkeypatch, capsys):
    from scripts import notify_digest

    leak = "1401666801:AAHt9Xk2mQpLzR7vNbC3dEfGhIjKlMnOpQrSt"

    def boom():
        raise RuntimeError(f"터짐 {leak}")

    monkeypatch.setattr(notify_digest, "main", boom)
    assert notify_digest.guarded_main() == 70
    err = capsys.readouterr().err
    assert leak not in err and "<redacted>" in err


def test_notify_dry_run_redacts_check_reason(tmp_path, monkeypatch, capsys):
    """드라이런 출력도 redact 를 지난다 (사이클6 #7)."""
    from scripts import notify_digest

    leak = "1401666801:AAHt9Xk2mQpLzR7vNbC3dEfGhIjKlMnOpQrSt"
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(
        tmp_path, annotated,
        dict(PASS_CHECK, **{"pass": False, "reason": f"생존 항목 없음 {leak}"}),
    )
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md), "--dry-run"])
    assert notify_digest.main() == 0
    out = capsys.readouterr().out
    assert leak not in out and "<redacted>" in out


# ══════════════════════════════════════════════════════════════════════════
# 사이클7 — Codex w10fix6 크리틱 재현을 테스트로
# ══════════════════════════════════════════════════════════════════════════

# ══ #1 모든 작성자가 같은 flock 을 따른다 ═══════════════════════════════
def test_recheck_cli_times_out_and_writes_nothing_while_lock_held(
    tmp_path, monkeypatch, capsys
):
    """발송기가 잠금을 쥐면 재검증은 **기다리다 실패**한다 — 표식도 만들지 않는다.

    사이클6 크리틱 #1 재현: 예전 recheck 는 잠금을 무시하고 돌아, 무효화 실패 시
    `.broken` 을 잠금 밖에서 만들었다(발송 중 표식 생성 → 발송 후 영구 차단).
    """
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    before_check = md.with_suffix(".check.json").read_bytes()
    before_md = md.read_bytes()
    approval_before = _approval_id(md)

    holder = _spawn_holder(tmp_path, state_mod.lock_path_for_markdown(md))
    monkeypatch.setenv(state_mod.LOCK_TIMEOUT_ENV, "0.3")

    def forbid(*args, **kwargs):    # pragma: no cover - 불려도 안 된다
        raise GateBreached("잠금 없이 재검증 본체가 돌았다")

    monkeypatch.setattr("scripts.recheck_digest.recheck", forbid)
    monkeypatch.setattr(
        "sys.argv", ["recheck_digest.py", str(md), "--db", str(tmp_path / "x.db")]
    )
    try:
        assert recheck_main() == 2
    finally:
        holder.kill()
        holder.wait(10)

    assert not state_mod.tombstone_path_for_markdown(md).exists()   # 표식 없음
    assert md.with_suffix(".check.json").read_bytes() == before_check
    assert md.read_bytes() == before_md
    assert _approval_id(md) == approval_before
    assert "잠금 대기 실패" in capsys.readouterr().err


def test_weekly_digest_times_out_while_lock_held(tmp_path, monkeypatch):
    """생성기도 같은 잠금을 따른다 — 발송 중에 본문을 재조립하지 않는다."""
    from scripts import weekly_digest

    md = _write_digest(tmp_path, SAMPLE_MD)
    before = md.read_bytes()
    holder = _spawn_holder(tmp_path, state_mod.lock_path("2026-W37", tmp_path))
    monkeypatch.setenv(state_mod.LOCK_TIMEOUT_ENV, "0.3")

    def forbid(*args, **kwargs):    # pragma: no cover
        raise GateBreached("잠금 없이 재조립했다")

    monkeypatch.setattr(weekly_digest, "compose_digest", forbid)
    monkeypatch.setattr("sys.argv", [
        "weekly_digest.py", "--week", "2026-W37",
        "--out-dir", str(tmp_path), "--db", str(tmp_path / "x.db"),
    ])
    try:
        assert weekly_digest.main() == 2
    finally:
        holder.kill()
        holder.wait(10)
    assert md.read_bytes() == before


def test_notify_reads_body_after_acquiring_lock(tmp_path, monkeypatch):
    """notify 는 **잠금을 쥔 뒤** 본문·검증을 읽는다 (사이클6 크리틱 MEDIUM #6).

    예전에는 읽고 판정한 뒤 잠금을 쥐어서, 그 사이에 본문이 바뀌면 낡은 미리보기를
    보내고 옛 바이트로 승인을 발급했다.

    통합 2 #1 이후 기대값이 한 칸 더 엄해진다: 생존 스냅샷은 잠금 **밖**에서
    옛 바이트를 봤으므로, 잠금 안에서 새 바이트를 읽으면 스냅샷이 무효다 →
    승인을 발급하지 않고 "재검토 필요" 로 막는다. 잠금 전에 읽었다면 스냅샷과
    본문이 같아 그냥 통과했을 것이므로, 이 차단 자체가 "잠금 뒤에 읽었다" 는 증거다.
    """
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    swapped, _ = apply_headline(SAMPLE_MD, "확정 의견 — 잠금 뒤 교체")
    check_path = md.with_suffix(".check.json")

    real_acquire = state_mod.acquire_lock

    def acquire_then_swap(path, **kwargs):
        handle = real_acquire(path, **kwargs)
        md.write_text(swapped, encoding="utf-8")
        # 교체된 본문에도 정본을 결속한다 — 이 테스트가 보는 것은 "언제 읽는가"
        # 이지 정본 대조가 아니다(그건 별도 테스트가 본다).
        composer_mod.refresh_manifest_binding(md)
        check_path.write_text(json.dumps(dict(
            PASS_CHECK,
            markdown_sha256=markdown_sha256(md.read_bytes()),
            checked_at=checker_mod._now_utc().isoformat(),
        ), ensure_ascii=False), encoding="utf-8")
        return handle

    monkeypatch.setattr("scripts.notify_digest.state_mod.acquire_lock",
                        acquire_then_swap)
    monkeypatch.setattr(
        notify_digest, "resolve_target", lambda topic_key="council": (-100, 2011)
    )
    monkeypatch.setattr(notify_digest, "resolve_token", lambda: "123:FAKE")
    sent = []
    monkeypatch.setattr(
        notify_digest, "send_chunk",
        lambda token, chat, thread, text: (sent.append(text), (True, 2014, ""))[1],
    )
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 0

    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37")
    approval = state_mod.approval_of(state)
    assert approval == {}, approval            # 옛 바이트의 승인은 발급되지 않는다
    assert any("본문이 바뀌었습니다" in text for text in sent), sent
    # 안내만 나갔으므로 번호 좌표도 생기지 않는다 (통합 2 #2)
    assert state["preview_message_ids"] == []
    assert state["notice_message_ids"] == [2014]


# ══ #2 승인 폐기 실패 = 중단 ════════════════════════════════════════════
def test_recheck_cli_aborts_when_approval_drop_fails(tmp_path, monkeypatch,
                                                     capsys):
    """첫 동작(승인 폐기)이 실패하면 **아무것도 진행하지 않는다** (exit 2)."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    before_check = md.with_suffix(".check.json").read_bytes()

    def drop_boom(*args, **kwargs):
        raise OSError("상태 저장 실패")

    def forbid(*args, **kwargs):    # pragma: no cover
        raise GateBreached("승인 폐기 실패 후에도 재검증이 돌았다")

    monkeypatch.setattr(
        "scripts.recheck_digest.state_mod.update_state_locked", drop_boom)
    monkeypatch.setattr("scripts.recheck_digest.recheck", forbid)
    monkeypatch.setattr(
        "sys.argv", ["recheck_digest.py", str(md), "--db", str(tmp_path / "x.db")]
    )
    assert recheck_main() == 2
    assert md.with_suffix(".check.json").read_bytes() == before_check
    assert not state_mod.tombstone_path_for_markdown(md).exists()
    assert "승인 폐기 실패" in capsys.readouterr().err


def test_weekly_digest_aborts_when_approval_drop_fails(tmp_path, monkeypatch):
    from scripts import weekly_digest

    md = _write_digest(tmp_path, SAMPLE_MD)
    before = md.read_bytes()

    def drop_boom(*args, **kwargs):
        raise OSError("상태 저장 실패")

    def forbid(*args, **kwargs):    # pragma: no cover
        raise GateBreached("승인 폐기 실패 후에도 조립이 돌았다")

    monkeypatch.setattr(
        "scripts.weekly_digest.state_mod.update_state_locked", drop_boom)
    monkeypatch.setattr(weekly_digest, "compose_digest", forbid)
    monkeypatch.setattr("sys.argv", [
        "weekly_digest.py", "--week", "2026-W37",
        "--out-dir", str(tmp_path), "--db", str(tmp_path / "x.db"),
    ])
    assert weekly_digest.main() == 2
    assert md.read_bytes() == before


def test_recheck_cli_drops_approval_before_rechecking(tmp_path, monkeypatch):
    """재검증은 **성공하든 실패하든** 옛 승인을 먼저 없앤다 (부활 금지)."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    assert _approval_id(md)

    seen = {}

    def fake_recheck(markdown_path, db_path, check_path):
        seen["approval_during"] = _approval_id(md)
        raise OSError("검증 불가")

    monkeypatch.setattr("scripts.recheck_digest.recheck", fake_recheck)
    monkeypatch.setattr(
        "sys.argv", ["recheck_digest.py", str(md), "--db", str(tmp_path / "x.db")]
    )
    assert recheck_main() == 1
    assert seen["approval_during"] is None      # 본체가 돌기 전에 이미 없었다
    assert _approval_id(md) is None


def test_weekly_digest_drops_approval_even_when_generation_fails(
    tmp_path, monkeypatch
):
    """생성 실패(rc=1) 뒤에도 옛 승인으로 발송할 수 없다 (사이클6 크리틱 #2)."""
    from scripts import weekly_digest

    md = _write_digest(tmp_path, SAMPLE_MD)
    _seed_preview(md)
    assert _approval_id(md)

    def compose_boom(**kwargs):
        raise RuntimeError("조립 실패")

    monkeypatch.setattr(weekly_digest, "compose_digest", compose_boom)
    monkeypatch.setattr("sys.argv", [
        "weekly_digest.py", "--week", "2026-W37",
        "--out-dir", str(tmp_path), "--db", str(tmp_path / "x.db"),
    ])
    assert weekly_digest.main() == 1
    assert _approval_id(md) is None


# ══ #3 링크 파서는 하나 — 검사한 URL = 실제 목적지 ═══════════════════════
def test_body_urls_covers_display_string_and_trailing_punctuation():
    """사이클6 크리틱 #3 재현 두 건."""
    assert prune_mod.body_urls(
        "[https://example.com/hidden](https://example.com/ok)"
    ) == ["https://example.com/ok", "https://example.com/hidden"]
    assert prune_mod.body_urls("[자료](https://example.com/report!)") == [
        "https://example.com/report!"
    ]


def test_renderer_href_is_exactly_what_the_checker_sees():
    """렌더러의 `<a href>` 와 body_urls 가 같은 문자열을 낸다."""
    from scripts.send_digest import markdown_to_html

    for line in (
        "[자료](https://example.com/report!)",
        "[자료](https://example.com/report(1))",
        "[https://example.com/hidden](https://example.com/ok)",
    ):
        html = markdown_to_html(line)
        hrefs = [href for _display, href in prune_mod.markdown_links(line)]
        for href in hrefs:
            assert f'href="{href}"' in html
            assert href in prune_mod.body_urls(line)


@pytest.mark.parametrize("hidden,injected", [
    ("https://example.com/hidden",
     "확정 의견 — [https://example.com/hidden](https://example.com/ok)"),
    ("https://example.com/report!",
     "확정 의견 — [자료](https://example.com/report!)"),
])
def test_send_digest_refuses_excluded_url_hidden_in_link(
    tmp_path, monkeypatch, hidden, injected
):
    """표시문자열에 숨긴 URL·끝 구두점이 붙은 href 도 제외 검사를 피하지 못한다."""
    md = _write_digest(tmp_path, SAMPLE_MD.replace(preview_mod.MARKER, injected))
    _seed_preview(md)
    state_path = state_mod.state_path_for_markdown(md)
    state_mod.save_state(state_path, dict(
        state_mod.load_state(state_path, "2026-W37"), excluded_urls=[hidden]))
    _forbid_notifier(monkeypatch, f"제외 URL({hidden})이 남았는데 발송했다")
    assert _send(md) == 2


# ══ #4 제어 문자 = 허용 목록 밖 전부 ════════════════════════════════════
@pytest.mark.parametrize("char,label", [
    ("\ufeff", "BOM"),
    ("\u200b", "ZWSP"),
    ("\u200e", "LRM"),
    ("\u202e", "RLO"),
    ("\u2066", "LRI"),
    ("\u2069", "PDI"),
    ("\x80", "C1 시작"),
    ("\x9f", "C1 끝"),
    ("\u2028", "줄 구분"),
    ("\u2029", "단락 구분"),
    ("\x00", "NUL"),
    ("\x0b", "수직 탭"),
])
def test_control_chars_allowlist_rejects_format_and_bidi(char, label):
    """블랙리스트가 계속 놓쳤던 것들 — Cc·Cf·Zl·Zp 전부 거부 (사이클6 크리틱 #5)."""
    assert prune_mod.control_chars(f"a{char}b") == [char], label
    assert prune_mod.strip_control_chars(f"a{char}b") == "ab", label


def test_control_chars_allowlist_keeps_ordinary_text():
    assert prune_mod.control_chars("한글 abc 123 \t탭 \n개행 …—“”") == []
    assert prune_mod.control_chars(SAMPLE_MD) == []


@pytest.mark.parametrize("char", ["\ufeff", "\u200b", "\u202e", "\x9f"])
def test_send_digest_refuses_format_control_chars(tmp_path, monkeypatch, char):
    body = SAMPLE_MD.replace(preview_mod.MARKER, f"확정 의견{char}")
    md = _write_digest(tmp_path, body)
    _seed_preview(md)
    _forbid_notifier(monkeypatch, f"제어 문자({char!r}) 본문을 발송했다")
    assert _send(md) == 2


@pytest.mark.parametrize("char", ["\ufeff", "\u200b", "\u202e", "\x9f"])
def test_checker_refuses_format_control_chars(tmp_path, monkeypatch, char):
    md = tmp_path / "2026-W37.md"
    md.write_text(
        SAMPLE_MD.replace("· (면담·건의·수렴 현황 — 이번 주 기록 없음)",
                          f"· 기록 없음{char}"),
        encoding="utf-8")
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(db_path=str(_empty_db(tmp_path)), markdown_path=md)
    assert result["pass"] is False
    assert "제어 문자 포함" in result["reason"]


# ══ #5 recheck CLI 도 redact 를 지난다 ══════════════════════════════════
def test_recheck_cli_redacts_token_in_stderr(tmp_path, monkeypatch, capsys):
    """사이클6 크리틱 #7 재현: check_digest 예외에 실린 토큰이 stderr 로 샜다."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)

    def boom(**kwargs):
        raise OSError(f"DB 오류 https://api.telegram.org/bot{LEAK_TOKEN}/x")

    monkeypatch.setattr("scripts.recheck_digest.check_digest", boom)
    monkeypatch.setattr(
        "sys.argv", ["recheck_digest.py", str(md), "--db", str(tmp_path / "x.db")]
    )
    assert recheck_main() == 1
    captured = capsys.readouterr()
    assert LEAK_TOKEN not in captured.err + captured.out
    assert "bot<redacted>" in captured.err
    # check.json 의 사유에도 남지 않는다 (미리보기로 흘러 나가는 경로)
    assert LEAK_TOKEN not in md.with_suffix(".check.json").read_text(
        encoding="utf-8")


def test_recheck_cli_guarded_main_redacts_traceback(tmp_path, monkeypatch,
                                                    capsys):
    from scripts import recheck_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)

    def boom(*args, **kwargs):
        raise MemoryError(f"토큰 {LEAK_TOKEN} 유출")

    monkeypatch.setattr(recheck_digest.state_mod, "acquire_lock", boom)
    monkeypatch.setattr(
        "sys.argv", ["recheck_digest.py", str(md), "--db", str(tmp_path / "x.db")]
    )
    assert recheck_digest.guarded_main() == 70
    captured = capsys.readouterr()
    assert LEAK_TOKEN not in captured.err
    assert "<redacted>" in captured.err


# ══════════════════════════════════════════════════════════════════════════
# 사이클8 — Codex w10fix7 크리틱 재현을 테스트로
# ══════════════════════════════════════════════════════════════════════════

# ══ #2 HTML 렌더 순서: 이스케이프 → 굵게(텍스트만) → 앵커 복원 ══════════
class _HrefProbe(HTMLParser):
    """실제 HTML 파서가 읽는 href·strong (렌더러 자체 문자열 검사 금지)."""

    def __init__(self):
        super().__init__()
        self.hrefs = []
        self.strong_text = []
        self._in_strong = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.append(dict(attrs).get("href"))
        elif tag == "strong":
            self._in_strong = True

    def handle_endtag(self, tag):
        if tag == "strong":
            self._in_strong = False

    def handle_data(self, data):
        if self._in_strong:
            self.strong_text.append(data)


def _rendered(line):
    from scripts.send_digest import markdown_to_html

    probe = _HrefProbe()
    probe.feed(markdown_to_html(line))
    return probe


def test_renderer_does_not_rewrite_asterisks_inside_href():
    """사이클7 크리틱 #2 재현: 앵커 복원 뒤 굵게를 걸어 href 가 바뀌었다.

    `[자료](https://example.com/**report**)` 의 실제 목적지가
    `https://example.com/<strong>report</strong>` 가 되어, 검사한 URL 과
    발송되는 URL 이 갈렸다 (checker pass · sender rc=0 · SMTP 1회).
    """
    line = "[자료](https://example.com/**report**)"
    probe = _rendered(line)
    assert probe.hrefs == ["https://example.com/**report**"]
    assert "<strong>" not in (probe.hrefs[0] or "")
    assert probe.strong_text == []          # 굵게는 href 를 건드리지 않았다


def test_renderer_applies_bold_to_text_only():
    probe = _rendered("**원문:** [자료](https://example.com/**report**)")
    assert probe.hrefs == ["https://example.com/**report**"]
    assert probe.strong_text == ["원문:"]   # 텍스트에만 <strong>


@pytest.mark.parametrize("line", [
    "[자료](https://example.com/**report**)",
    "**원문:** [자료](https://example.com/**report**)",
    "[자료](https://example.com/report(1))",
    "[자료](https://example.com/report!)",
    "[https://example.com/hidden](https://example.com/ok)",
    "**원문:** [https://x/**h**](https://x/**o**)",
])
def test_rendered_href_is_always_a_checked_url(line):
    """실제 파서가 읽은 href 는 **반드시** body_urls 안에 있다."""
    probe = _rendered(line)
    checked = prune_mod.body_urls(line)
    for href in probe.hrefs:
        assert href in checked


@pytest.mark.parametrize("hidden,injected", [
    ("https://example.com/**report**",
     "확정 의견 — [자료](https://example.com/**report**)"),
    ("https://example.com/a**b",
     "확정 의견 — [자료](https://example.com/a**b)"),
])
def test_send_digest_refuses_excluded_url_with_asterisks(
    tmp_path, monkeypatch, hidden, injected
):
    """`**` 를 품은 URL 도 제외 검사를 피하지 못한다 (렌더·검사 동일 문자열)."""
    md = _write_digest(tmp_path, SAMPLE_MD.replace(preview_mod.MARKER, injected))
    _seed_preview(md)
    state_path = state_mod.state_path_for_markdown(md)
    state_mod.save_state(state_path, dict(
        state_mod.load_state(state_path, "2026-W37"), excluded_urls=[hidden]))
    _forbid_notifier(monkeypatch, f"제외 URL({hidden})이 남았는데 발송했다")
    assert _send(md) == 2


# ══ #3 argparse 출력도 redact 를 지난다 ═════════════════════════════════
_CLI_SCRIPTS = [
    "scripts/send_digest.py",
    "scripts/notify_digest.py",
    "scripts/recheck_digest.py",
    "scripts/weekly_digest.py",
    "scripts/apply_commentary.py",
]


def _run_cli(*args):
    import subprocess

    return subprocess.run(
        [sys.executable, *args],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )


@pytest.mark.parametrize("script", _CLI_SCRIPTS)
def test_cli_argparse_does_not_leak_token_on_unknown_flag(script):
    """사이클7 크리틱 #3 재현: argparse 가 guarded_main 보다 먼저 원문을 찍었다."""
    proc = _run_cli(script, f"--invalid={LEAK_TOKEN}")
    assert proc.returncode == 2, proc.stderr
    assert LEAK_TOKEN not in proc.stderr
    assert LEAK_TOKEN not in proc.stdout


@pytest.mark.parametrize("script", _CLI_SCRIPTS)
def test_cli_rejects_token_shaped_argv_before_parsing(script):
    """토큰 형태 인자는 **내용을 출력하지 않고** 일반 오류로 끝낸다."""
    proc = _run_cli(script, LEAK_TOKEN)
    assert proc.returncode == 2
    assert LEAK_TOKEN not in proc.stderr + proc.stdout
    assert "토큰 형태" in proc.stderr


@pytest.mark.parametrize("script", _CLI_SCRIPTS)
def test_cli_usage_output_has_no_token(script):
    """--help 경로도 같은 파서를 쓴다 (usage 출력 자체는 정상)."""
    proc = _run_cli(script, "--help")
    assert proc.returncode == 0
    assert LEAK_TOKEN not in proc.stdout


def test_safe_argparse_detects_token_shaped_argv():
    from alert.utils.safe_argparse import argv_has_secret

    assert argv_has_secret([f"--token={LEAK_TOKEN}"]) is True
    assert argv_has_secret([f"https://api.telegram.org/bot{LEAK_TOKEN}/x"]) is True
    assert argv_has_secret(["--week", "2026-W37", "--db", "a.db"]) is False


# ══ #4 잠금 범위 최소화 — 텔레그램 왕복은 잠금 밖 ═══════════════════════
def _lock_free(md):
    """지금 그 주차의 잠금이 비어 있는가 (별 fd 로 즉시 획득 시도)."""
    try:
        handle = state_mod.acquire_lock(
            state_mod.lock_path_for_markdown(md), blocking=False)
    except state_mod.LockBusy:
        return False
    state_mod.release_lock(handle)
    return True


def _notify_stubs(monkeypatch, notify_digest, send_chunk):
    monkeypatch.setattr(
        notify_digest, "resolve_target", lambda topic_key="council": (-100, 2011)
    )
    monkeypatch.setattr(notify_digest, "resolve_token", lambda: "123:FAKE")
    monkeypatch.setattr(notify_digest, "send_chunk", send_chunk)
    monkeypatch.setattr(
        notify_digest, "clear_card", lambda token, chat, mid: (True, ""))


def test_notify_releases_lock_during_telegram_roundtrip(tmp_path, monkeypatch):
    """사이클7 크리틱 #4 재현: 카드 제거·청크 전송 내내 flock 을 쥐고 있었다."""
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    observed = []

    def send_chunk(token, chat, thread, text):
        observed.append(_lock_free(md))
        return True, 2013 + len(observed), ""

    _notify_stubs(monkeypatch, notify_digest, send_chunk)
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 0
    assert observed and all(observed)        # 전송 중 잠금은 비어 있었다
    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37")
    assert state["preview_message_ids"] == [2014]
    assert state_mod.approval_of(state)["sha"] == markdown_sha256(
        md.read_bytes())


def test_notify_issues_approval_before_sending(tmp_path, monkeypatch):
    """승인 초안은 전송 **전에** 잠금 안에서 발급된다 (사이클8 #4)."""
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    state_path = state_mod.state_path_for_markdown(md)
    seen = {}

    def send_chunk(token, chat, thread, text):
        state = state_mod.load_state(state_path, "2026-W37")
        seen["approval_during_send"] = state_mod.approval_of(state).get("id")
        seen["ids_during_send"] = list(state.get("preview_message_ids") or [])
        return True, 2014, ""

    _notify_stubs(monkeypatch, notify_digest, send_chunk)
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 0
    assert seen["approval_during_send"]              # 이미 발급돼 있다
    assert seen["ids_during_send"] == []             # message_id 는 아직 없다
    state = state_mod.load_state(state_path, "2026-W37")
    assert state_mod.approval_of(state)["id"] == seen["approval_during_send"]
    assert state["preview_message_ids"] == [2014]


def test_notify_drops_approval_when_generation_changes_mid_send(
    tmp_path, monkeypatch
):
    """전송 중 다른 작성자가 세대를 바꿨으면 message_id 를 붙이지 않고 폐기한다."""
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    state_path = state_mod.state_path_for_markdown(md)
    dropped_cards = []

    def send_chunk(token, chat, thread, text):
        # 전송 왕복 중 recheck 가 승인을 폐기했다 (잠금이 비어 있으니 가능하다)
        state_mod.save_state(state_path, state_mod.clear_approval(
            state_mod.load_state(state_path, "2026-W37")))
        return True, 2014, ""

    _notify_stubs(monkeypatch, notify_digest, send_chunk)
    monkeypatch.setattr(
        notify_digest, "clear_card",
        lambda token, chat, mid: (dropped_cards.append(mid), (True, ""))[1])
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 1

    state = state_mod.load_state(state_path, "2026-W37")
    assert state_mod.approval_of(state) == {}        # 세대는 폐기됐다
    assert state["preview_message_ids"] == []        # 기록하지 않았다


def test_notify_drops_approval_when_send_fails(tmp_path, monkeypatch):
    """전송이 깨지면 발급해 둔 초안 승인을 폐기한다 (카드 없는 승인 방지)."""
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    state_path = state_mod.state_path_for_markdown(md)

    _notify_stubs(monkeypatch, notify_digest,
                  lambda token, chat, thread, text: (False, None, "API 실패"))
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 2
    state = state_mod.load_state(state_path, "2026-W37")
    assert state_mod.approval_of(state) == {}


def test_notify_blocked_notice_drops_old_approval(tmp_path, monkeypatch):
    """차단 안내 경로도 옛 승인을 폐기하고, 안내 message_id 를 최신으로 남긴다."""
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    md.write_text(annotated + "\n사후 변경", encoding="utf-8")   # 검증과 불일치
    state_path = state_mod.state_path_for_markdown(md)
    assert state_mod.approval_of(
        state_mod.load_state(state_path, "2026-W37")).get("id")

    sent = []
    _notify_stubs(
        monkeypatch, notify_digest,
        lambda token, chat, thread, text: (
            sent.append(text), (True, 3014, ""))[1])
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 0

    state = state_mod.load_state(state_path, "2026-W37")
    assert state_mod.approval_of(state) == {}            # 옛 승인 폐기
    # 통합 2 #2: 안내는 별도 자리에 남고, 마지막 **실제 미리보기**의 번호 좌표는
    # 그대로다 — 늦게 끝난 안내가 최신 좌표를 `[]` 로 지우던 경로를 닫았다.
    assert state["notice_message_ids"] == [3014]
    assert state["preview_message_ids"] == [2014]
    # 통합 2 #2: 안내는 좌표를 만들지도 지우지도 않는다 — 마지막 실제 미리보기의
    # 좌표가 남는다(제외 n 이 가리킬 곳이 사라지지 않는다).
    assert state_mod.preview_urls(state) == blocks_mod.item_urls(
        md.read_text(encoding="utf-8"))
    assert any("재검증 필요" in text or "검증" in text for text in sent)


def test_notify_aborts_when_blocked_and_approval_drop_fails(
    tmp_path, monkeypatch
):
    """차단인데 승인을 못 지우면 안내조차 보내지 않는다 (사이클7 #2 규율)."""
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    md.write_text(annotated + "\n사후 변경", encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("상태 저장 실패")

    sent = []
    _notify_stubs(
        monkeypatch, notify_digest,
        lambda token, chat, thread, text: (
            sent.append(text), (True, 3014, ""))[1])
    monkeypatch.setattr(
        "scripts.notify_digest.state_mod.update_state_locked", boom)
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 2
    assert sent == []


# ══ #5 잠금 타임아웃 종료 코드 2 로 통일 ════════════════════════════════
def test_notify_lock_timeout_exit_code_is_two(tmp_path, monkeypatch):
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    holder = state_mod.acquire_lock(state_mod.lock_path_for_markdown(md))
    monkeypatch.setenv(state_mod.LOCK_TIMEOUT_ENV, "0.2")
    _notify_stubs(monkeypatch, notify_digest,
                  lambda token, chat, thread, text: (True, 2014, ""))
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    try:
        assert notify_digest.main() == 2
    finally:
        state_mod.release_lock(holder)


def test_all_cli_lock_timeouts_exit_two(tmp_path, monkeypatch):
    """recheck·weekly·apply·notify 가 같은 종료 코드(2)로 실패한다."""
    from scripts import notify_digest, recheck_digest, weekly_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    holder = state_mod.acquire_lock(state_mod.lock_path_for_markdown(md))
    monkeypatch.setenv(state_mod.LOCK_TIMEOUT_ENV, "0.2")
    _notify_stubs(monkeypatch, notify_digest,
                  lambda token, chat, thread, text: (True, 2014, ""))
    try:
        monkeypatch.setattr(
            "sys.argv",
            ["recheck_digest.py", str(md), "--db", str(tmp_path / "x.db")])
        assert recheck_digest.main() == 2
        monkeypatch.setattr("sys.argv", [
            "weekly_digest.py", "--week", "2026-W37",
            "--out-dir", str(tmp_path), "--db", str(tmp_path / "x.db")])
        assert weekly_digest.main() == 2
        monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
        assert notify_digest.main() == 2
        assert _run_apply(tmp_path, "--commentary", "의견").returncode == 2
    finally:
        state_mod.release_lock(holder)


# ══ 병합 3회차: 두 정본을 하나의 흐름으로 ═══════════════════════════════
def test_manifest_order_is_the_number_coordinate(tmp_path, monkeypatch):
    """정본의 항목 **순서**가 본문과 다르면 멈춘다 (번호 좌표의 근거).

    봇의 `제외 2,5` 는 미리보기 번호를 가리키고, 그 번호는 정본 순서에서 나온다.
    순서가 본문과 어긋나면 사람이 승인한 번호와 실제 항목이 달라진다 —
    고치지 않고 재조립을 요구한다(자동 교정 금지, 사이클 9 #1).
    """
    _alive(monkeypatch)
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    assert check_digest(db_path=str(db), markdown_path=md,
                        output_path=None)["pass"] is True

    manifest = composer_mod.load_items_manifest(md)
    manifest["items"] = [manifest["items"][1], manifest["items"][0]] + \
        manifest["items"][2:]
    composer_mod.write_items_manifest(md, manifest)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert "정본 항목 순서가 본문과 다름" in " ".join(result["manifest_problems"])


def test_notify_numbers_come_from_the_manifest(tmp_path):
    """미리보기가 기록하는 번호→URL 좌표는 정본 순서다 (W10 이 미룬 좌표 문제)."""
    import scripts.notify_digest as notify_digest

    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    _bind(md)
    manifest = composer_mod.load_items_manifest(md)
    assert notify_digest._manifest_item_urls(md) == [
        entry["url"] for entry in manifest["items"]
    ]
    # 정본이 없으면 None — 호출자는 본문 순서로 물러선다
    composer_mod.items_json_path(md).unlink()
    assert notify_digest._manifest_item_urls(md) is None


def test_send_counts_items_from_the_manifest(tmp_path, monkeypatch, capsys):
    """발송기의 항목 수 근거는 items.json 이다 (check.json 의 숫자가 아니라)."""
    _alive(monkeypatch)
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = tmp_path / "2026-W37.md"
    md.write_text(annotated, encoding="utf-8")
    db = _bind(md)
    result = check_digest(db_path=str(db), markdown_path=md,
                          output_path=md.with_suffix(".check.json"))
    assert result["pass"] is True
    _seed_preview(md)

    # 정본에서 항목 하나를 빼면 본문(4) ≠ 정본(3) 이므로 거부한다
    manifest = composer_mod.load_items_manifest(md)
    manifest["items"] = manifest["items"][:-1]
    composer_mod.write_items_manifest(md, manifest)
    _forbid_notifier(monkeypatch, "정본과 본문의 항목 수가 다른데 발송했다")
    assert _send(md) == 2
    err = capsys.readouterr().err
    assert "항목 수" in err and "(3)" in err


def test_apply_commentary_cli_matches_the_bot_verbs(tmp_path):
    """봇의 `상단:`·`해설:` 이 부르는 CLI 가 이 브랜치의 인터페이스와 같다.

    미리보기 사용법 줄(preview.USAGE_LINES)이 사람에게 약속하는 두 동사와,
    apply_commentary 가 받는 두 플래그가 1:1 이어야 한다 — 봇 배선은 W10 레인의
    파일에 있고(이 저장소 밖), 여기서는 **CLI 계약**을 고정한다.
    """
    usage = "\n".join(preview_mod.USAGE_LINES)
    assert "상단: <한 줄>" in usage and "해설: <본문>" in usage

    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    assert _run_apply(tmp_path, "--headline", "한 줄").returncode == 0
    assert _run_apply(tmp_path, "--commentary", "해설 본문").returncode == 0
    body = md.read_text(encoding="utf-8")
    assert "이번 주 한 줄: 한 줄" in body
    assert "· 해설 본문" in body
    assert preview_mod.MARKER not in body


def test_one_parser_for_links_and_urls():
    """링크 파서·URL 추출은 하나다 — prune 의 이름은 blocks 로의 위임이다."""
    line = "참고 [자료](https://example.com/report(1)) 와 https://example.com/bare."
    assert prune_mod.body_urls(line) == blocks_mod.body_urls(line)
    assert prune_mod.body_links(line) == blocks_mod.body_link_urls(line)
    assert [link.url for link in prune_mod.link_matches(line)] == [
        link.url for link in blocks_mod.find_links(line)
    ]
    assert prune_mod.markdown_links(line) == [
        (link.text, link.url) for link in blocks_mod.find_links(line)
    ]
    # 발송기의 렌더도 같은 파서를 쓴다 — href 는 검사한 URL 그대로다
    from scripts.send_digest import markdown_to_html
    html = markdown_to_html(line)
    assert 'href="https://example.com/report(1)"' in html


# ══ 통합 사이클 1: 정본 재대조 · 마감 재판정 · 승인 세대 보호 ═══════════
def _swap_manifest_order(md):
    """items.json 의 첫 두 항목 순서만 뒤집는다 (본문·개수는 그대로)."""
    manifest = composer_mod.load_items_manifest(md)
    manifest["items"] = ([manifest["items"][1], manifest["items"][0]]
                         + manifest["items"][2:])
    composer_mod.write_items_manifest(md, manifest)
    return manifest


def test_notify_blocks_when_manifest_order_changed(tmp_path, monkeypatch):
    """items.json 순서만 바뀌어도 notify 는 승인을 발급하지 않는다 (통합 1 #1).

    Codex 통합 게이트 HIGH #1 재현: 개수는 같으므로 예전 검사는 통과했고,
    미리보기의 `1=A` 와 제외 좌표의 `1=B` 가 갈렸다. 번호는 사람이 승인하는
    좌표다 — 어긋나면 승인 자체를 만들지 않는다.
    """
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _swap_manifest_order(md)

    sent = []
    _notify_stubs(monkeypatch, notify_digest,
                  lambda token, chat, thread, text: (
                      sent.append(text), (True, 2014, ""))[1])
    monkeypatch.setattr("sys.argv",
                        ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 0
    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37")
    assert state_mod.approval_of(state).get("id") is None      # 승인 미발급
    assert "정본 대조 실패" in sent[0]
    assert "1. [" not in sent[0]                               # 항목 미리보기 없음


def test_send_refuses_when_manifest_order_changed(tmp_path, monkeypatch, capsys):
    """발송기도 같은 대조를 잠금 안에서 다시 한다 (통합 1 #1)."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)                      # 승인까지 정상으로 갖춘 뒤
    _swap_manifest_order(md)               # 정본 순서만 뒤집는다
    _forbid_notifier(monkeypatch, "번호 좌표가 어긋났는데 발송했다")
    assert _send(md) == 2
    err = capsys.readouterr().err
    assert "정본 대조 실패" in err and "순서" in err


def _past_deadline(md, db, item_id=11):
    """DB 의 마감을 어제로 바꾸고 정본도 같이 맞춘다 (= 정상 조립 후 날짜 경과)."""
    from datetime import date, timedelta

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE announcements SET period_start = '2026-09-01', period_end = ?"
        " WHERE id = ?", (yesterday, item_id))
    conn.commit()
    conn.close()
    manifest = composer_mod.load_items_manifest(md)
    for entry in manifest["items"]:
        if str(entry["id"]) == str(item_id):
            entry["period_start"] = "2026-09-01"
            entry["period_end"] = yesterday
    composer_mod.write_items_manifest(md, manifest)
    return yesterday


def test_recheck_fails_on_expired_deadline(tmp_path, monkeypatch):
    """지난 마감은 재검토에서 pass=false 다 (통합 1 #2).

    Codex 통합 게이트 HIGH #2 재현: 9/12 에 조립한 `마감 9/13` 항목을 9/14 에
    재검토하면 예전에는 pass 였고 `[D-1]` 이 그대로 실렸다. 같은 DB 로 재조립하면
    "마감 경과" 로 빠지는 항목이다 — 죽은 정보를 보내지 않는 것이 위협 모델 ②다.
    """
    _alive(monkeypatch)
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    assert check_digest(db_path=str(db), markdown_path=md,
                        output_path=None)["pass"] is True

    _past_deadline(md, db)
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is False
    joined = " ".join(result["manifest_problems"])
    assert "마감 경과" in joined and "재조립 필요" in joined


def test_send_refuses_expired_deadline(tmp_path, monkeypatch, capsys):
    """발송기도 **오늘 기준**으로 다시 판정한다 (통합 1 #2)."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    _past_deadline(md, _db_for(md))
    _forbid_notifier(monkeypatch, "마감이 지난 항목을 발송했다")
    assert _send(md) == 2
    assert "마감 경과" in capsys.readouterr().err


def test_send_refuses_when_db_period_changed_after_check(
    tmp_path, monkeypatch, capsys
):
    """검증 뒤 DB 기간이 바뀌면 발송하지 않는다 (통합 1 #2 후단)."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    conn = sqlite3.connect(_db_for(md))
    conn.execute(
        "UPDATE announcements SET period_end = '2027-01-31' WHERE id = 11")
    conn.commit()
    conn.close()
    _forbid_notifier(monkeypatch, "DB 가 바뀌었는데 발송했다")
    assert _send(md) == 2
    err = capsys.readouterr().err
    assert "정본 대조 실패" in err and "마감" in err


def test_notice_section_is_not_expired(tmp_path, monkeypatch):
    """알아두세요 항목은 마감으로 내리지 않는다 (compose 와 같은 규칙)."""
    _alive(monkeypatch)
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    _past_deadline(md, db, item_id=14)          # 14 = 알아두세요 항목
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is True, result["manifest_problems"]


def test_finish_locked_keeps_another_notifys_approval(tmp_path):
    """A 가 준비한 회차는 B 의 최신 승인을 건드리지 않는다 (통합 1 #3)."""
    from scripts import notify_digest

    md = _write_digest(tmp_path)
    state_path = state_mod.state_path_for_markdown(md)
    week = "2026-W37"

    prepared_a = state_mod.approval_of(state_mod.record_preview(
        state_mod.default_state(week), [], [], "sha-a", "check-a"))
    live_b = state_mod.record_preview(
        state_mod.default_state(week), [2020], [], "sha-b", "check-b")
    state_mod.save_state(state_path, live_b)
    approval_b = state_mod.approval_of(live_b)

    code, drop_card, message = notify_digest._finish_locked(
        state_path, week, prepared_a, [2030], [], None)

    assert code == 1                       # 이 회차는 실패로 끝나고
    assert drop_card is None               # 카드도 회수하지 않으며
    assert "최신 승인은 그대로" in message
    live = state_mod.approval_of(state_mod.load_state(state_path, week))
    assert live.get("id") == approval_b.get("id")      # B 의 승인은 남아 있다
    assert live.get("sha") == "sha-b"
    assert prepared_a.get("id") != approval_b.get("id")


def test_recheck_kakao_failure_is_redacted(tmp_path, monkeypatch, capsys):
    """카톡 재생성 실패 메시지도 redact 를 지난다 (통합 1 #4)."""
    import scripts.recheck_digest as recheck_mod

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = tmp_path / "2026-W37.md"
    md.write_text(annotated, encoding="utf-8")
    db = _bind(md)
    _alive(monkeypatch)

    def boom(markdown_text):
        raise OSError(f"디스크 오류 bot{LEAK_TOKEN}")

    monkeypatch.setattr(recheck_mod, "kakao_file_text_from_markdown", boom)
    monkeypatch.setattr("sys.argv",
                        ["recheck_digest.py", str(md), "--db", str(db)])
    recheck_mod.main()
    err = capsys.readouterr().err
    assert "카톡 평문 재생성 실패" in err
    assert LEAK_TOKEN not in err


# ══ 통합 사이클 2: 발송 직전 생존 재검사 · 검증 만료 · 안내 좌표 ═══════
def _dead(monkeypatch, dead_urls):
    """주어진 URL 만 죽은 것으로 응답 (네트워크 없음)."""
    dead_set = set(dead_urls)
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive",
        lambda url, timeout=8: url not in dead_set,
    )


def test_send_refuses_when_url_died_after_check(tmp_path, monkeypatch, capsys):
    """검증 뒤 링크가 죽으면 발송하지 않는다 (통합 2 #1).

    Codex 통합 게이트 HIGH 재현: 파일·DB 는 그대로 두고 생존 응답만 실패로 바꾸면
    새 checker 는 pass=false 였지만 발송기는 **네트워크를 한 번도 보지 않고**
    rc=0 이었다. 검증은 "그때 살아 있었다" 는 기록일 뿐이다.
    """
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    _dead(monkeypatch, ["https://example.com/b"])
    _forbid_notifier(monkeypatch, "죽은 링크가 있는데 발송했다")
    assert _send(md) == 2
    err = capsys.readouterr().err
    assert "죽은 링크" in err and "example.com/b" in err


def test_notify_blocks_when_url_died_after_check(tmp_path, monkeypatch):
    """notify 도 승인 발급 전에 같은 검사를 한다 (통합 2 #1)."""
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _dead(monkeypatch, ["https://example.com/a"])
    sent = []
    _notify_stubs(monkeypatch, notify_digest,
                  lambda token, chat, thread, text: (
                      sent.append(text), (True, 2014, ""))[1])
    monkeypatch.setattr("sys.argv",
                        ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 0
    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37")
    assert state_mod.approval_of(state).get("id") is None
    assert "죽은 링크" in sent[0]


def test_send_refuses_when_body_changed_after_liveness_snapshot(
    tmp_path, monkeypatch, capsys
):
    """생존 스냅샷과 발송 바이트가 다르면 거부 (통합 2 #1 — TOCTOU 를 바이트로)."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    real_snapshot = checker_mod.liveness_snapshot

    def snapshot_of_other_bytes(markdown_path, timeout=8):
        snap = real_snapshot(markdown_path, timeout=timeout)
        snap["sha"] = markdown_sha256(b"another body")    # 다른 바이트를 본 척
        return snap

    monkeypatch.setattr("scripts.send_digest.liveness_snapshot",
                        snapshot_of_other_bytes)
    _forbid_notifier(monkeypatch, "스냅샷과 다른 바이트를 발송했다")
    assert _send(md) == 2
    assert "생존 검사 뒤 본문이 바뀌었습니다" in capsys.readouterr().err


def test_send_refuses_expired_check(tmp_path, monkeypatch, capsys):
    """25시간 전 검증은 만료다 (통합 2 #1)."""
    from datetime import timedelta

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    stale = (checker_mod._now_utc() - timedelta(hours=25)).isoformat()
    md = _write_digest(tmp_path, annotated,
                       dict(PASS_CHECK, checked_at=stale))
    _seed_preview(md)
    _forbid_notifier(monkeypatch, "만료된 검증으로 발송했다")
    assert _send(md) == 2
    assert checker_mod.CHECK_EXPIRED_REASON in capsys.readouterr().err


def test_send_refuses_check_without_checked_at(tmp_path, monkeypatch, capsys):
    """검증 시각이 없으면 fail-closed (언제 본 판정인지 모른다)."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    check_path = md.with_suffix(".check.json")
    check = json.loads(check_path.read_text(encoding="utf-8"))
    del check["checked_at"]
    check_path.write_text(json.dumps(check, ensure_ascii=False),
                          encoding="utf-8")
    _seed_preview(md)
    _forbid_notifier(monkeypatch, "시각 없는 검증으로 발송했다")
    assert _send(md) == 2
    assert checker_mod.CHECK_EXPIRED_REASON in capsys.readouterr().err


def test_fresh_check_records_checked_at(tmp_path, monkeypatch):
    """checker 가 생존을 본 시각을 남긴다 (만료 판정의 근거)."""
    _alive(monkeypatch)
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    db = _bind(md)
    result = check_digest(db_path=str(db), markdown_path=md,
                          output_path=md.with_suffix(".check.json"))
    assert result["checked_at"]
    assert checker_mod.check_expired(result) is False
    stored = json.loads(
        md.with_suffix(".check.json").read_text(encoding="utf-8"))
    assert stored["checked_at"] == result["checked_at"]


def test_notice_does_not_overwrite_preview_coordinates(tmp_path):
    """늦게 끝난 차단 안내가 최신 번호 좌표를 지우지 않는다 (통합 2 #2).

    Codex 통합 게이트 MEDIUM 재현: A 차단 안내 준비 → B 정상 미리보기 완료 →
    A 완료 순서에서 `preview_message_ids` 가 A 로 교체되고 좌표가 `[]` 가 됐다.
    """
    from scripts import notify_digest

    md = _write_digest(tmp_path)
    state_path = state_mod.state_path_for_markdown(md)
    urls = blocks_mod.item_urls(md.read_text(encoding="utf-8"))
    live_b = state_mod.record_preview_messages(
        state_mod.record_preview(
            state_mod.default_state("2026-W37"), [], urls, "sha-b", "check-b"),
        [2020], urls)
    state_mod.save_state(state_path, live_b)

    code, drop_card, message = notify_digest._finish_locked(
        state_path, "2026-W37", None, [3030], [], None)

    assert code == 0 and drop_card is None
    state = state_mod.load_state(state_path, "2026-W37")
    assert state["notice_message_ids"] == [3030]     # 안내는 자기 자리에
    assert state["preview_message_ids"] == [2020]    # 좌표는 그대로
    assert state_mod.preview_urls(state) == urls
    assert state_mod.approval_of(state).get("id") == \
        state_mod.approval_of(live_b).get("id")      # B 승인도 그대로


# ══ 통합 사이클 3: probe 는 잠금 밖에서 끝난다 ══════════════════════════
def test_liveness_probe_finishes_before_the_lock(tmp_path, monkeypatch):
    """URL 생존 검사는 **잠금을 쥐기 전에** 끝난다 (통합 3 #1).

    Codex 통합 3차 MEDIUM 실측: 호출 순서가 `acquire_lock → probe(잠금 보유)
    → release_lock` 이었다. URL 4개가 HEAD·GET 8초씩이면 검사만 64초 —
    다른 작성자의 60초 대기 한도를 통째로 먹는다. 주석은 "잠금 밖" 이라고
    적혀 있었고 코드는 잠금 안이었다.

    순서를 기록해 **잠금 보유 중 네트워크 0회**를 단언한다.
    """
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)

    events = []
    held = {"count": 0}
    real_acquire = state_mod.acquire_lock
    real_release = state_mod.release_lock

    def acquire(path, **kwargs):
        handle = real_acquire(path, **kwargs)
        held["count"] += 1
        events.append("acquire")
        return handle

    def release(handle):
        held["count"] -= 1
        events.append("release")
        return real_release(handle)

    def probe(url, timeout=8):
        events.append(f"probe:{held['count']}")
        return True

    monkeypatch.setattr("scripts.send_digest.state_mod.acquire_lock", acquire)
    monkeypatch.setattr("scripts.send_digest.state_mod.release_lock", release)
    monkeypatch.setattr("alert.digest.checker.check_url_alive", probe)
    monkeypatch.setattr("scripts.send_digest.EmailNotifier", _OkNotifier)

    assert _send(md) == 0

    probes = [event for event in events if event.startswith("probe")]
    assert probes, events
    # ① 잠금을 쥔 채로 찍은 probe 가 하나도 없다
    assert all(event == "probe:0" for event in probes), events
    # ② 마지막 probe 가 **마지막 acquire 앞**에 있다 (판정 구간 진입 전에 끝났다)
    assert events.index(probes[-1]) < len(events) - 1 - events[::-1].index("acquire")


def test_busy_lock_skips_both_reads_and_probes(tmp_path, monkeypatch):
    """남이 잠금을 쥐고 있으면 바이트도 읽지 않고 네트워크도 쓰지 않는다.

    통합 3 에서 probe 를 잠금 앞으로 옮기면서도 계약 W10 사이클6 #1
    ("잠금 밖 판정 0")을 지킨다 — 잠금이 비어 있는지 먼저 보고, 차 있으면
    거기서 끝낸다.
    """
    md = _annotated_digest(tmp_path)
    holder = state_mod.acquire_lock(state_mod.lock_path_for_markdown(md))
    probes = []
    monkeypatch.setattr("alert.digest.checker.check_url_alive",
                        lambda url, timeout=8: probes.append(url) or True)
    read_calls = []
    real_read = Path.read_bytes

    def counted(self, *args, **kwargs):
        read_calls.append(str(self))
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", counted)
    _forbid_notifier(monkeypatch, "잠금 없이 발송을 시작했다")
    try:
        assert _send(md) == 2
    finally:
        state_mod.release_lock(holder)
    assert probes == []
    assert not [c for c in read_calls if c.endswith(".md")]
    assert not [c for c in read_calls if c.endswith(".check.json")]


def test_notice_log_reports_the_live_approval(tmp_path):
    """안내 완료 로그가 최신 승인 상태를 그대로 말한다 (통합 3 #2)."""
    from scripts import notify_digest

    md = _write_digest(tmp_path)
    state_path = state_mod.state_path_for_markdown(md)
    live_b = state_mod.record_preview(
        state_mod.default_state("2026-W37"), [2020], [], "sha-b", "check-b")
    state_mod.save_state(state_path, live_b)
    approval_b = state_mod.approval_of(live_b)["id"]

    code, _drop, message = notify_digest._finish_locked(
        state_path, "2026-W37", None, [3030], [], None)
    assert code == 0
    # 예전에는 이 자리에서 언제나 "approval=없음(검증 필요)" 라고 적었다 —
    # B 의 승인이 살아 있는데도 없다고 오보했다.
    assert approval_b in message
    assert "없음(검증 필요)" not in message

    state_mod.save_state(state_path, state_mod.clear_approval(
        state_mod.load_state(state_path, "2026-W37")))
    _code, _drop, message = notify_digest._finish_locked(
        state_path, "2026-W37", None, [3031], [], None)
    assert "없음(검증 필요)" in message       # 진짜 없을 때만 그렇게 적는다


# ══ V4 계약 ①②: 보류 좌표 기록 · 옛 미리보기 삭제 ═══════════════════════
def _delete_stub(monkeypatch, notify_digest, deleted, ok=True):
    def delete_message(token, chat_id, message_id):
        deleted.append(message_id)
        return (True, "") if ok else (False, "Bad Request: message can't be deleted")
    monkeypatch.setattr(notify_digest, "delete_message", delete_message)


def _run_notify(monkeypatch, notify_digest, md, sent=None, message_id=2014):
    def send_chunk(token, chat, thread, text):
        if sent is not None:
            sent.append(text)
        return True, message_id, ""
    _notify_stubs(monkeypatch, notify_digest, send_chunk)
    monkeypatch.setattr(
        "sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    return notify_digest.main()


def test_notify_records_hold_ids_as_pin_coordinates(tmp_path, monkeypatch):
    """미리보기는 `핀 n` 의 좌표(보류 id, 번호 순서)를 함께 기록한다 (계약 ①)."""
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _delete_stub(monkeypatch, notify_digest, [])
    assert _run_notify(monkeypatch, notify_digest, md) == 0

    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37")
    # SAMPLE_MD 의 보류 주석은 한 건(id=12)이다
    assert state["preview_holds"] == {"2014": [12]}
    assert state_mod.preview_hold_ids(state) == [12]


def test_notify_hold_ids_are_none_without_ids(tmp_path):
    """id 없는 구버전 보류 주석이면 좌표를 만들지 않는다 (fail-closed)."""
    from scripts import notify_digest

    body = SAMPLE_MD.replace(" | id=12 -->", " -->")
    assert notify_digest._hold_ids(body, None) is None
    assert notify_digest._hold_ids(SAMPLE_MD, None) == [12]


def test_notify_deletes_previous_preview_and_notice(tmp_path, monkeypatch):
    """새 미리보기가 도착하면 옛 미리보기·안내를 지운다 (계약 ②)."""
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    state_path = state_mod.state_path_for_markdown(md)
    state_mod.save_state(state_path, dict(
        state_mod.default_state("2026-W37"),
        preview_message_ids=[1000],
        preview_items={"1000": []},
        notice_message_ids=[1001]))

    deleted = []
    _delete_stub(monkeypatch, notify_digest, deleted)
    assert _run_notify(monkeypatch, notify_digest, md) == 0

    assert deleted == [1000, 1001]
    state = state_mod.load_state(state_path, "2026-W37")
    assert state["superseded_message_ids"] == [1000, 1001]
    assert state["preview_message_ids"] == [2014]       # 새 좌표만 남는다


def test_notify_delete_failure_does_not_stop_preview(tmp_path, monkeypatch):
    """삭제 실패는 진행을 막지 않는다 — 다만 무엇을 밀어냈는지는 기록한다 (계약 ②)."""
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    state_path = state_mod.state_path_for_markdown(md)
    state_mod.save_state(state_path, dict(
        state_mod.default_state("2026-W37"), preview_message_ids=[1000]))

    deleted = []
    _delete_stub(monkeypatch, notify_digest, deleted, ok=False)
    assert _run_notify(monkeypatch, notify_digest, md) == 0
    assert deleted == [1000]
    state = state_mod.load_state(state_path, "2026-W37")
    assert state["superseded_message_ids"] == [1000]
    assert state_mod.approval_of(state)["id"]           # 승인은 정상 발급


def test_notify_block_notice_keeps_previous_preview(tmp_path, monkeypatch):
    """차단 안내는 **안내만** 밀어낸다 — 미리보기를 지우면 번호 좌표가 사라진다."""
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated,
                       dict(PASS_CHECK, **{"pass": False, "reason": "생존 항목 없음"}))
    state_path = state_mod.state_path_for_markdown(md)
    state_mod.save_state(state_path, dict(
        state_mod.default_state("2026-W37"),
        preview_message_ids=[1000],
        preview_items={"1000": ["https://example.com/a"]},
        notice_message_ids=[1001]))

    deleted = []
    _delete_stub(monkeypatch, notify_digest, deleted)
    assert _run_notify(monkeypatch, notify_digest, md, message_id=2020) == 0

    assert deleted == [1001]                            # 안내만 지웠다
    state = state_mod.load_state(state_path, "2026-W37")
    assert state["preview_message_ids"] == [1000]       # 좌표는 그대로
    assert state["preview_items"]["1000"] == ["https://example.com/a"]
    assert state["notice_message_ids"] == [2020]


def test_notify_keeps_old_preview_when_send_fails(tmp_path, monkeypatch):
    """전송이 깨지면 아무것도 지우지 않는다 — 편집자 화면을 비워 두지 않는다."""
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    state_path = state_mod.state_path_for_markdown(md)
    state_mod.save_state(state_path, dict(
        state_mod.default_state("2026-W37"), preview_message_ids=[1000]))

    deleted = []
    _delete_stub(monkeypatch, notify_digest, deleted)
    _notify_stubs(monkeypatch, notify_digest,
                  lambda token, chat, thread, text: (False, None, "API 실패"))
    monkeypatch.setattr(
        "sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 2

    assert deleted == []                    # 옛 미리보기는 텔레그램에 그대로 남는다
    state = state_mod.load_state(state_path, "2026-W37")
    assert state["approval"] is None        # 실패한 회차의 승인은 폐기된다
    # 번호 좌표는 승인 초안 발급(잠금 ①) 때 이미 비워졌다 — 기존 동작이며
    # fail-closed 다: 옛 미리보기에 답장한 `제외 n`·`핀 n` 은 "최신 미리보기에
    # 답장하세요" 로 거부된다. 지우지 않는 이유는 화면을 비우지 않기 위해서다.
    assert state["preview_message_ids"] == []


# ══ V4 계약 ③: 상태 스키마 (승격) ═════════════════════════════════════
def test_pinned_ids_roundtrip(tmp_path):
    path = tmp_path / "2026-W37.state.json"
    state = state_mod.add_pinned_ids(state_mod.default_state("2026-W37"),
                                     [11, 12, 11])
    state_mod.save_state(path, state)
    loaded = state_mod.load_state(path, "2026-W37")
    assert state_mod.pinned_ids(loaded) == [11, 12]
    assert state_mod.pinned_ids(state_mod.drop_pinned_ids(loaded, [11])) == [12]


@pytest.mark.parametrize("status", ["sending", "sent"])
def test_pin_refused_while_frozen(status):
    """발송 중·완료 주차는 승격도 거부한다 (제외와 같은 규율)."""
    with pytest.raises(state_mod.TransitionError):
        state_mod.add_pinned_ids(
            dict(state_mod.default_state("2026-W37"), status=status), [11])


@pytest.mark.parametrize("broken", [
    {"pinned_ids": {}},
    {"preview_holds": []},
    {"superseded_message_ids": "1,2"},
])
def test_new_state_keys_are_type_checked(tmp_path, broken):
    """손상된 새 키는 조용히 넘어가지 않는다 (fail-closed)."""
    path = tmp_path / "2026-W37.state.json"
    payload = dict(state_mod.default_state("2026-W37"), **broken)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(state_mod.StateError):
        state_mod.load_state(path, "2026-W37")


def test_superseded_history_is_bounded():
    """이력은 무한히 자라지 않는다 (상태 파일이 커지면 잠금 구간이 길어진다)."""
    state = state_mod.default_state("2026-W37")
    state = state_mod.record_superseded(
        state, range(state_mod.SUPERSEDED_MAX + 10))
    assert len(state["superseded_message_ids"]) == state_mod.SUPERSEDED_MAX
    assert state["superseded_message_ids"][-1] == state_mod.SUPERSEDED_MAX + 9


# ══ V4 라운드 2 (Codex MEDIUM): 부분 전송에서는 옛 미리보기를 지우지 않는다 ══
def _two_chunk_preview(monkeypatch):
    """미리보기를 2조각으로 강제한다 (부분 전송을 만들 유일한 손잡이)."""
    monkeypatch.setattr(preview_mod, "chunk_text",
                        lambda text, limit=None: ["조각 1", "조각 2"])


def test_notify_keeps_old_preview_on_partial_send(tmp_path, monkeypatch):
    """2조각 중 1조각만 도착하면 아무것도 지우지 않는다.

    라운드 1 의 조건은 `if message_ids:` 였다 — 반쪽 미리보기만 남기고 온전한 옛
    미리보기를 지워, 편집자에게 완전한 미리보기가 하나도 없는 상태를 만들었다.
    """
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    state_path = state_mod.state_path_for_markdown(md)
    state_mod.save_state(state_path, dict(
        state_mod.default_state("2026-W37"),
        preview_message_ids=[1000],
        preview_items={"1000": ["https://example.com/a"]},
        notice_message_ids=[1001]))

    deleted = []
    _delete_stub(monkeypatch, notify_digest, deleted)
    _two_chunk_preview(monkeypatch)
    calls = []

    def send_chunk(token, chat, thread, text):
        calls.append(text)
        if len(calls) == 1:
            return True, 3000, ""
        return False, None, "API 실패: too many requests"

    _notify_stubs(monkeypatch, notify_digest, send_chunk)
    monkeypatch.setattr(
        "sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 2

    assert len(calls) == 2                 # 두 번째에서 깨졌다
    assert deleted == []                   # 옛 미리보기·안내 모두 살아 있다
    state = state_mod.load_state(state_path, "2026-W37")
    # 도착해 버린 새 조각은 superseded 후보로만 적는다 (지우지는 않는다)
    assert state["superseded_message_ids"] == [3000]
    assert state["approval"] is None       # 반쪽 미리보기는 승인 대상이 아니다


def test_notify_deletes_only_after_every_chunk_arrives(tmp_path, monkeypatch):
    """2조각이 **전부** 도착하면 그때 지운다 (부분 전송 가드의 반대 분기)."""
    from scripts import notify_digest

    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    state_path = state_mod.state_path_for_markdown(md)
    state_mod.save_state(state_path, dict(
        state_mod.default_state("2026-W37"),
        preview_message_ids=[1000], notice_message_ids=[1001]))

    deleted = []
    _delete_stub(monkeypatch, notify_digest, deleted)
    _two_chunk_preview(monkeypatch)
    ids = iter([3000, 3001])
    _notify_stubs(monkeypatch, notify_digest,
                  lambda token, chat, thread, text: (True, next(ids), ""))
    monkeypatch.setattr(
        "sys.argv", ["notify_digest.py", str(md), "--db", _db_for(md)])
    assert notify_digest.main() == 0

    assert deleted == [1000, 1001]
    state = state_mod.load_state(state_path, "2026-W37")
    assert state["preview_message_ids"] == [3000, 3001]
    assert state["superseded_message_ids"] == [1000, 1001]
