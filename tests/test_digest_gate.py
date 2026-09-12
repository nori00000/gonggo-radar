"""텔레그램 승인 게이트: 상태 파일·미리보기 렌더·해설 치환·발송 기록 테스트 (계약 W10)."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from alert.digest import blocks as blocks_mod
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
    commentary_error,
    main as apply_commentary_main,
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
    (tmp_path / "2026-W37.check.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
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


def _sha8(md):
    """그 파일의 승인 지문 — **원시 바이트** 해시 앞 8자 (PR #1 기준)."""
    from pathlib import Path as _Path
    return markdown_sha256(_Path(md).read_bytes())[:8]


def _send(md, **kwargs):
    """실발송 호출 — 현재 본문 지문을 승인 지문으로 넘긴다."""
    kwargs.setdefault("approved_by", "1401666801")
    kwargs.setdefault("approved_sha", _sha8(md))
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

    assert send_digest(md, dry_run=True) == 2


def test_send_gate_rejects_line_ending_change_after_check(tmp_path):
    """CRLF 로만 바뀐 본문도 거부 — 해시 기준이 원시 바이트이기 때문이다."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    md.write_bytes(annotated.replace("\n", "\r\n").encode("utf-8"))

    assert send_digest(md, dry_run=True) == 2


def test_send_gate_rejects_check_without_markdown_hash(tmp_path):
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    check_path = md.with_suffix(".check.json")
    check = json.loads(check_path.read_text(encoding="utf-8"))
    del check["markdown_sha256"]
    check_path.write_text(json.dumps(check), encoding="utf-8")

    assert send_digest(md, dry_run=True) == 2


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

    def boom():  # pragma: no cover - 호출되면 게이트가 뚫린 것
        raise AssertionError("이미 발송된 주차에서 EmailNotifier가 생성됐다")

    monkeypatch.setattr("scripts.send_digest.EmailNotifier", boom)
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
    sha = _sha8(md)
    assert send_digest(md, dry_run=False, approved_by="1401666801",
                       approved_sha=sha) == 0

    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37"
    )
    assert state["status"] == "sent"
    assert state["approved_by"] == "1401666801"
    assert state["recipients_count"] == 2
    assert state["sent_at"]

    # 멱등: 같은 주차 두 번째 발송은 거부된다
    assert send_digest(md, dry_run=False, approved_by="1401666801",
                       approved_sha=sha) == 2


def test_send_digest_dry_run_does_not_touch_state(tmp_path):
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    assert send_digest(md, dry_run=True) == 0
    assert not state_mod.state_path_for_markdown(md).exists()


def test_send_digest_still_refuses_unconfirmed_marker(tmp_path, monkeypatch):
    """기존 게이트(마커) 불변 — 상태 파일이 생겨도 마커가 남으면 거부."""
    md = _write_digest(tmp_path)
    monkeypatch.setattr(
        "scripts.send_digest.EmailNotifier",
        lambda: (_ for _ in ()).throw(AssertionError("마커 게이트가 뚫렸다")),
    )
    assert _send(md) == 2


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
def _seed_preview(md, sha=None):
    """미리보기를 보낸 것으로 기록 (사이클3 #3: 발송에는 preview_sha 가 필요하다)."""
    from pathlib import Path as _Path

    md = _Path(md)
    week = state_mod.week_from_markdown(md)
    path = state_mod.state_path_for_markdown(md)
    state = state_mod.load_state(path, week)
    state_mod.save_state(path, state_mod.record_preview(
        state, [2014], preview_mod.item_urls(md.read_text(encoding="utf-8")),
        sha or markdown_sha256(md.read_bytes()),
    ))
    return path


def _annotated_digest(tmp_path):
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    return md


def _forbid_notifier(monkeypatch, why):
    def boom(*args, **kwargs):  # pragma: no cover - 호출되면 게이트가 뚫린 것
        raise AssertionError(why)

    monkeypatch.setattr("scripts.send_digest.EmailNotifier", boom)


def test_send_digest_refuses_when_lock_exists(tmp_path, monkeypatch):
    """동시 실행: 살아 있는 잠금이 있으면 SMTP까지 가지 않는다."""
    import os as _os
    import time as _time

    md = _annotated_digest(tmp_path)
    lock = state_mod.lock_path_for_markdown(md)
    lock.write_text(json.dumps(
        {"pid": _os.getpid(), "nonce": "other", "started_at": _time.time()}
    ), encoding="utf-8")
    _forbid_notifier(monkeypatch, "잠금이 있는데 발송을 시작했다")
    assert _send(md) == 2
    assert lock.exists()        # 남의 잠금을 지우지 않았다


def test_send_digest_releases_lock_after_success(tmp_path, monkeypatch):
    md = _annotated_digest(tmp_path)
    monkeypatch.setattr("scripts.send_digest.EmailNotifier", _OkNotifier)
    assert _send(md) == 0
    assert not state_mod.lock_path_for_markdown(md).exists()


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
    assert not state_mod.lock_path_for_markdown(md).exists()
    if expected_status == "annotated":
        # 되돌려졌으니 같은 본문으로 재시도가 가능하다
        assert state["sending_at"] is None


def test_acquire_lock_is_exclusive(tmp_path):
    path = tmp_path / "2026-W37.lock"
    handle = state_mod.acquire_lock(path)
    with pytest.raises(state_mod.LockBusy):
        state_mod.acquire_lock(path)
    assert state_mod.release_lock(handle) is True
    assert not path.exists()
    # 해제 후에는 다시 잡을 수 있다
    assert state_mod.release_lock(state_mod.acquire_lock(path)) is True


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
    state = state_mod.record_card(state_mod.default_state("2026-W37"), 555)
    assert state["card_message_id"] == 555
    state = state_mod.record_preview(state, [1], ["u"])
    assert state["card_message_id"] is None
    for mid in range(2, 2 + state_mod.PREVIEW_ITEMS_MAX + 5):
        state = state_mod.record_preview(state, [mid], ["u"])
    assert len(state["preview_items"]) == state_mod.PREVIEW_ITEMS_MAX


# ─── 크리틱 #7: 해설이 조용히 사라지는 문제 ─────────────────────────────
@pytest.mark.parametrize("bad", ["<!-- 중요한 협의회 의견", "의견 --> 끝"])
def test_apply_commentary_rejects_html_comment_tokens(tmp_path, monkeypatch, bad):
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    assert commentary_error(bad)
    monkeypatch.setattr(
        "sys.argv",
        ["apply_commentary.py", "2026-W37", "--headline", bad,
         "--out-dir", str(tmp_path)],
    )
    from scripts.apply_commentary import main as commentary_main

    assert commentary_main() == 2
    assert md.read_text(encoding="utf-8") == SAMPLE_MD


def test_commentary_error_allows_html_like_text():
    assert commentary_error("<b>강조</b> & 인용") == ""


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
                   preview_sha="a" * 64, card_message_id=777)
    state_mod.save_state(path, sending)
    released = state_mod.release_sending(sending)
    # 사이클3 판정: draft 로 되돌리고 미리보기·카드도 무효화한다
    assert released["status"] == "draft"
    assert released["sending_at"] is None
    assert released["preview_sha"] is None
    assert released["card_message_id"] is None
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
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md)])
    assert notify_digest.main() == 0

    state = state_mod.load_state(state_path, "2026-W37")
    assert state["status"] == "sent"                    # 덮어쓰이지 않았다
    assert state["preview_message_ids"] == [2014]       # 기록은 됐다
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
def test_send_digest_requires_approved_sha(tmp_path, monkeypatch):
    md = _annotated_digest(tmp_path)
    _forbid_notifier(monkeypatch, "승인 지문 없이 발송했다")
    assert send_digest(md, dry_run=False, approved_by="1") == 2


def test_send_digest_refuses_wrong_approved_sha(tmp_path, monkeypatch):
    md = _annotated_digest(tmp_path)
    _forbid_notifier(monkeypatch, "다른 지문으로 발송했다")
    assert send_digest(
        md, dry_run=False, approved_by="1", approved_sha="deadbeef"
    ) == 2


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


# ══ 사이클2 #3 · 사이클3 #1: 잠금 소유권·회수 ════════════════════════
def _lock_payload(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_acquire_lock_records_pid_and_nonce(tmp_path):
    import os as _os

    path = tmp_path / "2026-W37.lock"
    handle = state_mod.acquire_lock(path)
    payload = _lock_payload(path)
    assert payload["pid"] == _os.getpid()
    assert payload["nonce"] == handle.nonce and len(handle.nonce) == 32
    assert payload["started_at"] > 0
    state_mod.release_lock(handle)


def test_release_lock_only_deletes_own_lock(tmp_path):
    """회수 경합 뒤 남의 잠금을 지우지 않는다 (Codex 사이클3 #1)."""
    import os as _os
    import time as _time

    path = tmp_path / "2026-W37.lock"
    mine = state_mod.acquire_lock(path)
    # 남이 회수해 새로 잡은 상황을 흉내낸다 (nonce 가 다르다)
    path.write_text(json.dumps(
        {"pid": _os.getpid(), "nonce": "someone-else", "started_at": _time.time()}
    ), encoding="utf-8")
    assert state_mod.release_lock(mine) is False
    assert _lock_payload(path)["nonce"] == "someone-else"   # 남의 잠금 그대로


def test_acquire_lock_reclaims_dead_pid(tmp_path):
    """크래시로 남은 잠금은 회수하고 진행한다."""
    import time as _time

    path = tmp_path / "2026-W37.lock"
    path.write_text(json.dumps(
        {"pid": 999999, "nonce": "dead", "started_at": _time.time()}
    ), encoding="utf-8")
    reclaimed = []
    handle = state_mod.acquire_lock(path, on_reclaim=reclaimed.append)
    assert reclaimed and "보유 프로세스 없음" in reclaimed[0]
    assert _lock_payload(path)["nonce"] == handle.nonce
    # 회수 흔적(rename 산출물)이 남는다
    assert list(tmp_path.glob("2026-W37.lock.stale-*"))
    state_mod.release_lock(handle)


def test_live_pid_lock_is_never_reclaimed_even_when_old(tmp_path):
    """살아 있는 pid 의 잠금은 나이와 무관하게 회수하지 않는다 (경고만)."""
    import os as _os

    path = tmp_path / "2026-W37.lock"
    path.write_text(json.dumps(
        {"pid": _os.getpid(), "nonce": "live", "started_at": 0}
    ), encoding="utf-8")
    warned = []
    with pytest.raises(state_mod.LockBusy):
        state_mod.acquire_lock(path, on_warn=warned.append)
    assert warned and "회수하지 않음" in warned[0]
    assert _lock_payload(path)["nonce"] == "live"
    assert state_mod.stale_reason(path) is None


def test_partial_lock_file_has_grace_then_reclaims(tmp_path):
    """O_EXCL 생성과 JSON 쓰기 사이의 빈 파일은 유예 뒤에만 회수한다."""
    path = tmp_path / "2026-W37.lock"
    path.write_text("", encoding="utf-8")
    assert state_mod.stale_reason(path) is None             # 생성 직후 5초 유예
    with pytest.raises(state_mod.LockBusy):
        state_mod.acquire_lock(path)
    reason = state_mod.stale_reason(path, partial_grace=0)
    assert reason and "손상/미완성" in reason
    handle = state_mod.acquire_lock(path, partial_grace=0)
    state_mod.release_lock(handle)


def test_two_reclaimers_only_one_wins(tmp_path):
    """두 회수자가 동시에 달려들면 정확히 하나만 획득한다 (Codex 사이클3 #1)."""
    import threading
    import time as _time

    path = tmp_path / "2026-W37.lock"
    path.write_text(json.dumps(
        {"pid": 999999, "nonce": "dead", "started_at": _time.time()}
    ), encoding="utf-8")

    start = threading.Barrier(2)
    results = []
    errors = []

    def contend():
        start.wait()
        try:
            results.append(state_mod.acquire_lock(path))
        except state_mod.LockBusy as exc:
            errors.append(exc)

    threads = [threading.Thread(target=contend) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert len(results) == 1, f"동시 획득 {len(results)}건"
    assert len(errors) == 1
    winner = results[0]
    assert _lock_payload(path)["nonce"] == winner.nonce
    assert state_mod.release_lock(winner) is True
    assert not path.exists()


def test_send_digest_reclaims_stale_lock(tmp_path, monkeypatch):
    import time as _time

    md = _annotated_digest(tmp_path)
    state_mod.lock_path_for_markdown(md).write_text(json.dumps(
        {"pid": 999999, "nonce": "dead", "started_at": _time.time()}
    ), encoding="utf-8")
    monkeypatch.setattr("scripts.send_digest.EmailNotifier", _OkNotifier)
    assert _send(md) == 0
    assert state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37"
    )["status"] == "sent"


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
    assert items == ("✅ 신청하세요 (마감순)", "👀 알아두세요")
    assert commentary == ("🤝 협의회에서",)


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


# ══ 사이클3 #3: 미리보기-카드 정합 (preview_sha) ═══════════════════════
def test_notify_records_preview_sha(tmp_path, monkeypatch):
    """미리보기 전송 직전의 본문 지문을 state 에 남긴다."""
    from scripts import notify_digest

    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    monkeypatch.setattr(
        notify_digest, "resolve_target", lambda topic_key="council": (-100, 2011)
    )
    monkeypatch.setattr(notify_digest, "resolve_token", lambda: "123:FAKE")
    monkeypatch.setattr(
        notify_digest, "send_chunk",
        lambda token, chat_id, thread_id, text: (True, 2014, ""),
    )
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md)])
    assert notify_digest.main() == 0
    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37"
    )
    assert state["preview_sha"] == markdown_sha256(md.read_bytes())
    assert state["card_message_id"] is None


def test_send_digest_refuses_without_preview_sha(tmp_path, monkeypatch):
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)     # 미리보기 기록 없음
    _forbid_notifier(monkeypatch, "미리보기 없이 발송했다")
    assert _send(md) == 2


def test_send_digest_refuses_when_preview_sha_differs(tmp_path, monkeypatch):
    """사람이 본 미리보기와 다른 본문은 지문이 맞아도 발송하지 않는다 (#3)."""
    annotated, _ = apply_headline(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md, sha="0" * 64)             # 다른 본문을 보여준 미리보기
    _forbid_notifier(monkeypatch, "미리보기와 다른 본문을 발송했다")
    assert _send(md) == 2


def test_record_preview_keeps_previous_sha_when_omitted():
    state = state_mod.record_preview(
        state_mod.default_state("2026-W37"), [1], ["u"], "abc123"
    )
    assert state["preview_sha"] == "abc123"
    later = state_mod.record_preview(state, [2], ["u"])
    assert later["preview_sha"] == "abc123"


# ══ 사이클3 #4: 승인 지문 형식 검증 ════════════════════════════════════
@pytest.mark.parametrize("bad", ["", " ", "\t", "a", "abc1234", "zzzzzzzz",
                                 "ABCDEFG", "0123456789" * 7])
def test_send_digest_rejects_malformed_approved_sha(tmp_path, monkeypatch, bad):
    md = _annotated_digest(tmp_path)
    _forbid_notifier(monkeypatch, f"형식 위반 지문({bad!r})으로 발송했다")
    assert send_digest(
        md, dry_run=False, approved_by="1", approved_sha=bad
    ) == 2


def test_send_digest_accepts_uppercase_hex(tmp_path, monkeypatch):
    md = _annotated_digest(tmp_path)
    monkeypatch.setattr("scripts.send_digest.EmailNotifier", _OkNotifier)
    assert send_digest(
        md, dry_run=False, approved_by="1", approved_sha=_sha8(md).upper()
    ) == 0


def test_approved_sha_pattern():
    from scripts.send_digest import APPROVED_SHA_RE

    assert APPROVED_SHA_RE.match("0123abcd")
    assert APPROVED_SHA_RE.match("f" * 64)
    for bad in ("", " ", "0123abc", "0123abcg", "f" * 65):
        assert not APPROVED_SHA_RE.match(bad)


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
        ["apply_commentary.py", "2026-W37", "--headline", "보류 후 의견",
         "--out-dir", str(tmp_path)],
    )
    assert commentary_main() == 0
    assert preview_mod.MARKER not in md.read_text(encoding="utf-8")
    assert state_mod.load_state(state_path, "2026-W37")["status"] == "annotated"


# ══ 사이클3 #6: 해설은 잠금·상태 확인 뒤에만 본문을 쓴다 ═══════════════
def test_apply_commentary_refuses_while_locked(tmp_path, monkeypatch):
    import os as _os
    import time as _time

    from scripts.apply_commentary import main as commentary_main

    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    state_mod.lock_path("2026-W37", tmp_path).write_text(json.dumps(
        {"pid": _os.getpid(), "nonce": "other", "started_at": _time.time()}
    ), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["apply_commentary.py", "2026-W37", "--headline", "의견",
         "--out-dir", str(tmp_path)],
    )
    assert commentary_main() == 2
    assert md.read_text(encoding="utf-8") == SAMPLE_MD


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
