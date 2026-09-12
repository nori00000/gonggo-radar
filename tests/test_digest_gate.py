"""텔레그램 승인 게이트: 상태 파일·미리보기 렌더·해설 치환·발송 기록 테스트 (계약 W10)."""

import hashlib
import json
import sqlite3
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

from alert.digest import prune as prune_mod
from alert.digest import sections as sections_mod
from alert.digest import state as state_mod
from alert.digest import preview as preview_mod
from alert.digest.checker import check_digest, markdown_sha256
from alert.utils.redact import redact
from scripts.apply_commentary import (
    apply_commentary,
    apply_headline,
    text_error,
)
from scripts.recheck_digest import main as recheck_main
from scripts.send_digest import check_fail_closed, send_digest


SAMPLE_MD = """<!-- lane: Codex(gpt-5.6) -->

# 협의회 주간 정책브리핑 2026-W37

**기간:** 2026-09-07 ~ 2026-09-13

## 산림 정책 동향

### 산림 항공 점검 결과

**기관:** 산림청
**마감:** 미정
**원문:** [https://example.com/a](https://example.com/a)

요약 한 줄.

## 지원사업 공고

### 예비사회적기업 모집 공고

**기관:** 농식품부
**마감:** 2026-09-30
**원문:** [https://example.com/b](https://example.com/b)

*(요약 없음)*

### 스마트팜 의견 조사

**기관:** 스마트팜코리아
**마감:** 미정
**원문:** [https://example.com/c](https://example.com/c)

*(요약 없음)*

## 사회연대경제 동향

*(항목 없음)*

## 회원사 동정

*(항목 없음)*

## 협의회 의견

<!-- 상민 확정 필요 -->
"""

PASS_CHECK = {
    "items": [
        {"url": "https://example.com/a", "url_alive": True, "passed": True},
        {"url": "https://example.com/b", "url_alive": True, "passed": True},
        {"url": "https://example.com/c", "url_alive": True, "passed": True},
    ],
    "dropped": [],
    "item_blocks": 3,
    "item_sections": ["산림 정책 동향", "지원사업 공고", "사회연대경제 동향"],
    "commentary_sections": ["회원사 동정", "협의회 의견"],
    "pass": True,
    "network_checked": True,
    "reason": "",
}


# ─── 미리보기 파싱·렌더 ──────────────────────────────────────────────────
def test_parse_digest_numbers_items_in_document_order():
    parsed = preview_mod.parse_digest(SAMPLE_MD)
    assert [item["number"] for item in parsed["items"]] == [1, 2, 3]
    assert [item["url"] for item in parsed["items"]] == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]
    assert parsed["items"][1]["author"] == "농식품부"
    assert parsed["items"][1]["deadline"] == "2026-09-30"
    assert parsed["items"][0]["section"] == "산림 정책 동향"
    assert parsed["period"] == "2026-09-07 ~ 2026-09-13"
    assert parsed["has_marker"] is True


def test_item_urls_matches_parse_order():
    """번호 좌표의 정본: 경량 URL 추출과 전체 파서의 순서가 같아야 한다."""
    parsed = preview_mod.parse_digest(SAMPLE_MD)
    assert preview_mod.item_urls(SAMPLE_MD) == [
        item["url"] for item in parsed["items"]
    ]


def test_render_preview_has_header_numbers_status_usage():
    text = preview_mod.render_preview("2026-W37", SAMPLE_MD, PASS_CHECK)
    assert "협의회 주간 정책브리핑 2026-W37" in text
    assert "기간 2026-09-07 ~ 2026-09-13 · 검증 pass · 항목 3건" in text
    assert "2. [농식품부] 예비사회적기업 모집 공고 — 2026-09-30 — https://example.com/b" in text
    assert "■ 산림 정책 동향" in text
    assert "상태: 해설 대기" in text
    assert "제외 2,5" in text


def test_render_preview_marks_sendable_after_commentary():
    annotated, replaced = apply_commentary(SAMPLE_MD, "이번 주 의견입니다.")
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
def test_apply_commentary_is_noop_without_marker():
    without_marker = SAMPLE_MD.replace(preview_mod.MARKER, "기존 의견")
    updated, replaced = apply_commentary(without_marker, "새 의견")
    assert replaced is False
    assert updated == without_marker


def test_apply_commentary_keeps_raw_text():
    """이스케이프는 발송 단계의 일이다 — 저장은 원문 그대로."""
    updated, replaced = apply_commentary(SAMPLE_MD, "<b>강조</b> & 인용")
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


def _approval_id(md):
    """현재 state 의 승인 세대 id (없으면 None)."""
    from pathlib import Path as _Path

    md = _Path(md)
    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md),
        state_mod.week_from_markdown(md),
    )
    return state_mod.approval_of(state).get("id")


def _send(md, **kwargs):
    """실발송 호출 — 현재 승인 세대 id 를 넘긴다 (사이클4 #2)."""
    kwargs.setdefault("approved_by", "1401666801")
    kwargs.setdefault("approval_id", _approval_id(md))
    return send_digest(md, dry_run=False, **kwargs)


# ─── PR #1: 검증-본문 바이트 결속 (origin/main 도입 테스트) ──────────────
def test_check_hash_binds_valid_markdown_to_a_passed_gate(tmp_path):
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)

    assert check_fail_closed(md, md.with_suffix(".check.json")) == (True, "")


def test_send_gate_rejects_markdown_changed_after_check(tmp_path):
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    md.write_text(annotated + "\n사후 변경", encoding="utf-8")

    assert send_digest(md, dry_run=True) == 2


def test_send_gate_rejects_line_ending_change_after_check(tmp_path):
    """CRLF 로만 바뀐 본문도 거부 — 해시 기준이 원시 바이트이기 때문이다."""
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    md.write_bytes(annotated.replace("\n", "\r\n").encode("utf-8"))

    assert send_digest(md, dry_run=True) == 2


def test_send_gate_rejects_check_without_markdown_hash(tmp_path):
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    check_path = md.with_suffix(".check.json")
    check = json.loads(check_path.read_text(encoding="utf-8"))
    del check["markdown_sha256"]
    check_path.write_text(json.dumps(check), encoding="utf-8")

    assert send_digest(md, dry_run=True) == 2


def test_send_digest_refuses_already_sent_week(tmp_path, monkeypatch):
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
                       approval_id=approval) == 0

    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37"
    )
    assert state["status"] == "sent"
    assert state["approved_by"] == "1401666801"
    assert state["recipients_count"] == 2
    assert state["sent_at"]

    # 멱등: 같은 주차 두 번째 발송은 거부된다
    assert send_digest(md, dry_run=False, approved_by="1401666801",
                       approval_id=approval) == 2


def test_send_digest_dry_run_does_not_touch_state(tmp_path):
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    assert send_digest(md, dry_run=True) == 0
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
    annotated, _ = apply_commentary(SAMPLE_MD, "손으로 확정한 의견")
    md = tmp_path / "2026-W37.md"
    md.write_text(annotated, encoding="utf-8")
    db = tmp_path / "empty.db"
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE announcements (url TEXT, period_end TEXT, title TEXT)"
    )
    conn.commit()
    conn.close()

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
    assert len(check["items"]) == 3
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
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    md.write_text(annotated + "\n### 몰래 추가한 항목\n", encoding="utf-8")
    _forbid_notifier(monkeypatch, "해시 불일치인데 발송했다")
    assert _send(md) == 2


def test_send_digest_refuses_check_without_hash(tmp_path, monkeypatch):
    """본문 해시가 없는 check.json(구버전)도 거부 — 과거 검증일 수 있다."""
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(
        tmp_path, annotated, dict(PASS_CHECK, markdown_sha256=None)
    )
    _seed_preview(md)
    (tmp_path / "2026-W37.check.json").write_text(
        json.dumps(PASS_CHECK, ensure_ascii=False), encoding="utf-8"
    )
    _forbid_notifier(monkeypatch, "해시 없는 검증으로 발송했다")
    assert _send(md) == 2


def _empty_db(tmp_path):
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE announcements (url TEXT, period_end TEXT, title TEXT)")
    conn.commit()
    conn.close()
    return db


def test_recheck_records_markdown_sha256(tmp_path, monkeypatch):
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = tmp_path / "2026-W37.md"
    md.write_text(annotated, encoding="utf-8")
    db = _empty_db(tmp_path)
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    monkeypatch.setattr("sys.argv", ["recheck_digest.py", str(md), "--db", str(db)])
    assert recheck_main() == 0
    check = json.loads((tmp_path / "2026-W37.check.json").read_text(encoding="utf-8"))
    assert check["markdown_sha256"] == markdown_sha256(md.read_bytes())


def test_recheck_failure_overwrites_check_as_fail(tmp_path, monkeypatch):
    """재검증 예외(DB 오류·타임아웃)는 과거 pass 를 무효화한다."""
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
    result = check_digest(db_path=str(_empty_db(tmp_path)), markdown_path=md)
    assert result["pass"] is False
    assert "죽은 URL" in result["reason"]
    assert [item["url"] for item in result["dropped"]] == [DEAD]


def test_recheck_removes_dead_item_block(tmp_path, monkeypatch):
    """죽은 항목은 본문에서 블록째 사라지고, 남은 항목으로 pass 한다."""
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = tmp_path / "2026-W37.md"
    md.write_text(annotated, encoding="utf-8")
    dead_item = "https://example.com/b"
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive",
        lambda url, timeout=8: url != dead_item,
    )
    monkeypatch.setattr(
        "sys.argv",
        ["recheck_digest.py", str(md), "--db", str(_empty_db(tmp_path))],
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
    annotated, _ = apply_commentary(SAMPLE_MD, f"협의회 의견 [자료]({DEAD}) 참고")
    md = tmp_path / "2026-W37.md"
    md.write_text(annotated, encoding="utf-8")
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive",
        lambda url, timeout=8: url != DEAD,
    )
    monkeypatch.setattr(
        "sys.argv",
        ["recheck_digest.py", str(md), "--db", str(_empty_db(tmp_path))],
    )
    assert recheck_main() == 0
    body = md.read_text(encoding="utf-8")
    assert DEAD not in body
    assert "협의회 의견 자료 참고" in body
    check = json.loads((tmp_path / "2026-W37.check.json").read_text(encoding="utf-8"))
    assert check["pass"] is True
    assert [item["url"] for item in check["dropped"]] == [DEAD]


def test_recheck_fails_when_every_item_is_dead(tmp_path, monkeypatch):
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = tmp_path / "2026-W37.md"
    md.write_text(annotated, encoding="utf-8")
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: False
    )
    monkeypatch.setattr(
        "sys.argv",
        ["recheck_digest.py", str(md), "--db", str(_empty_db(tmp_path))],
    )
    assert recheck_main() == 1
    check = json.loads((tmp_path / "2026-W37.check.json").read_text(encoding="utf-8"))
    assert check["pass"] is False


def test_prune_marks_emptied_section(tmp_path):
    text, removed, stripped = prune_mod.strip_dead_urls(
        SAMPLE_MD, ["https://example.com/a"]
    )
    assert [item["url"] for item in removed] == ["https://example.com/a"]
    assert stripped == []
    assert "산림 항공 점검 결과" not in text
    forest = text.split("## 산림 정책 동향", 1)[1].split("## ", 1)[0]
    assert prune_mod.EMPTY_SECTION_LINE in forest


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
        ["apply_commentary.py", "2026-W37", "--commentary", bad,
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
        ["apply_commentary.py", "2026-W37", "--commentary", "의견",
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
    assert prune_mod.item_block_count(text) == 3     # 공고 3건 그대로


def test_declared_sections_match_composed_digest(tmp_path):
    """선언 목록 == composer 가 실제로 쓰는 `##` 헤딩 (드리프트 방지, 사이클3 #7)."""
    from alert.digest.composer import compose_digest
    from tests.test_digest import _create_announcements_table

    db = tmp_path / "compose.db"
    _create_announcements_table(db)
    markdown = compose_digest(db_path=str(db), week_str="2026-W37")
    present = sections_mod.headings(markdown)
    item_sections, commentary_sections = sections_mod.declared()
    assert [name for name in present if name in item_sections] == list(
        sections_mod.V1_ITEM_SECTIONS
    )
    assert [name for name in present if name in commentary_sections] == list(
        sections_mod.V1_COMMENTARY_SECTIONS
    )
    assert [name for name in present
            if name not in item_sections and name not in commentary_sections] == []


def test_sections_follow_composer_v2_constants(monkeypatch):
    """composer 에 SECTION_HEADINGS 가 생기면 그쪽이 정본이 된다."""
    from alert.digest import composer

    monkeypatch.setattr(composer, "SECTION_HEADINGS", {
        "신청하세요": "✅ 신청하세요 (마감순)",
        "알아두세요": "👀 알아두세요",
        "협의회에서": "🤝 협의회에서",
    }, raising=False)
    monkeypatch.setattr(composer, "ITEM_SECTIONS",
                        ("신청하세요", "알아두세요"), raising=False)
    items, commentary = sections_mod.declared()
    # 사이클5: composer 목록을 v1.2·v2 선언 목록과 **합집합**으로 쓴다 —
    # composer 를 v2 로 바꾼 뒤에도 디스크의 v1 본문이 "항목 0건" 이 되지 않게.
    assert set(items) >= {"✅ 신청하세요 (마감순)", "👀 알아두세요"}
    assert set(items) >= set(sections_mod.V1_ITEM_SECTIONS)
    assert "🤝 협의회에서" in commentary
    assert not set(items) & set(commentary)


def test_item_blocks_counts_blocks_not_links():
    """해설의 참고 링크는 항목이 아니다 (Codex 신규 #6)."""
    assert prune_mod.item_block_count(SAMPLE_MD) == 3
    assert prune_mod.item_block_count(COMMENTARY_WITH_BLOCK) == 3
    assert len(preview_mod.item_urls(COMMENTARY_WITH_BLOCK)) == 3


def test_checker_fails_when_no_item_blocks(tmp_path, monkeypatch):
    """공고가 0건이면 해설 링크가 살아 있어도 pass=false (Codex 신규 #6)."""
    only_commentary = SAMPLE_MD
    for url in ("https://example.com/a", "https://example.com/b",
                "https://example.com/c"):
        only_commentary = only_commentary.replace(url, "https://dead.invalid/x")
    only_commentary = only_commentary.replace(
        preview_mod.MARKER, "참고 [자료](https://alive.example/ok)"
    )
    md = tmp_path / "2026-W37.md"
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive",
        lambda url, timeout=8: "dead.invalid" not in url,
    )
    monkeypatch.setattr(
        "sys.argv",
        ["recheck_digest.py", str(md), "--db", str(_empty_db(tmp_path))],
    )
    md.write_text(only_commentary, encoding="utf-8")
    assert recheck_main() == 1
    check = json.loads((tmp_path / "2026-W37.check.json").read_text(encoding="utf-8"))
    assert check["pass"] is False
    assert check["item_blocks"] == 0
    assert check["reason"] == "항목 0건"


def test_dedupe_titles_drops_later_duplicate():
    """링크를 떼어내 제목이 겹치면 뒤 블록을 지운다 (Codex 신규 #7)."""
    body = SAMPLE_MD.replace(
        "### 예비사회적기업 모집 공고", "### 동향 [자료](https://dead.invalid/t)"
    ).replace("### 스마트팜 의견 조사", "### 동향 자료")
    pruned, _, stripped = prune_mod.strip_dead_urls(body, ["https://dead.invalid/t"])
    assert stripped == ["https://dead.invalid/t"]
    assert pruned.count("### 동향 자료") == 2
    deduped, removed = prune_mod.dedupe_titles(pruned)
    assert [item["title"] for item in removed] == ["동향 자료"]
    assert deduped.count("### 동향 자료") == 1
    assert prune_mod.item_block_count(deduped) == 2


def test_recheck_dedupes_after_link_strip(tmp_path, monkeypatch):
    body = SAMPLE_MD.replace(
        "### 예비사회적기업 모집 공고", "### 동향 [자료](https://dead.invalid/t)"
    ).replace("### 스마트팜 의견 조사", "### 동향 자료")
    annotated, _ = apply_commentary(body, "확정 의견")
    md = tmp_path / "2026-W37.md"
    md.write_text(annotated, encoding="utf-8")
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive",
        lambda url, timeout=8: "dead.invalid" not in url,
    )
    monkeypatch.setattr(
        "sys.argv",
        ["recheck_digest.py", str(md), "--db", str(_empty_db(tmp_path))],
    )
    assert recheck_main() == 0
    final = md.read_text(encoding="utf-8")
    assert final.count("### 동향 자료") == 1
    check = json.loads((tmp_path / "2026-W37.check.json").read_text(encoding="utf-8"))
    assert check["pass"] is True
    assert check["item_blocks"] == 2
    assert "동향 자료" in [item["title"] for item in check["dropped"]]


def test_cap_violation_fails_the_gate(tmp_path, monkeypatch):
    """섹션 상한을 넘은 본문은 pass=false (상한을 다시 적용하지는 않는다)."""
    extra = "".join(
        f"\n### 추가 공고 {n}\n\n**기관:** 기관\n**마감:** 미정\n"
        f"**원문:** [https://example.com/x{n}](https://example.com/x{n})\n"
        for n in range(1, 6)
    )
    body = SAMPLE_MD.replace(
        "\n## 사회연대경제 동향", extra + "\n## 사회연대경제 동향"
    )
    violations = prune_mod.cap_violations(body)
    assert violations and violations[0][0] == "지원사업 공고"
    md = tmp_path / "2026-W37.md"
    md.write_text(body, encoding="utf-8")
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(db_path=str(_empty_db(tmp_path)), markdown_path=md)
    assert result["pass"] is False
    assert "섹션 상한 초과" in result["reason"]


def test_caps_by_heading_maps_exact_headings():
    caps = sections_mod.caps_by_heading(sections_mod.V1_ITEM_SECTIONS)
    assert caps == {"산림 정책 동향": 3, "지원사업 공고": 5, "사회연대경제 동향": 3}
    # 항목 섹션이 아닌 헤딩에는 상한이 붙지 않는다
    assert sections_mod.caps_by_heading(("협의회에서 — 지원사업 의견",)) == {}


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
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md)])
    assert notify_digest.main() == 0
    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37"
    )
    approval = state_mod.approval_of(state)
    assert approval["sha"] == markdown_sha256(md.read_bytes())
    assert len(approval["id"]) == state_mod.APPROVAL_ID_LEN
    assert approval["card_message_id"] is None


def test_send_digest_refuses_without_recorded_preview(tmp_path, monkeypatch):
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)     # 미리보기 기록 없음
    _forbid_notifier(monkeypatch, "미리보기 없이 발송했다")
    assert _send(md) == 2


def test_send_digest_refuses_when_recorded_preview_differs(tmp_path, monkeypatch):
    """사람이 본 미리보기와 다른 본문은 지문이 맞아도 발송하지 않는다 (#3)."""
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
        approval_id=_approval_id(md).upper(),
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
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)     # 미리보기 기록 없음
    _forbid_notifier(monkeypatch, "승인 세대 없이 발송했다")
    assert send_digest(
        md, dry_run=False, approved_by="1", approval_id="0" * 12
    ) == 2


def test_send_digest_refuses_when_approval_sha_differs(tmp_path, monkeypatch):
    """세대 id 는 맞지만 본문이 바뀌었으면 거부 — 전체 SHA 비교 (접두 폐지)."""
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
        ["apply_commentary.py", "2026-W37", "--commentary", "보류 후 의견",
         "--out-dir", str(tmp_path)],
    )
    assert commentary_main() == 0
    assert preview_mod.MARKER not in md.read_text(encoding="utf-8")
    assert state_mod.load_state(state_path, "2026-W37")["status"] == "annotated"


# ══ 사이클3 #6: 해설은 잠금·상태 확인 뒤에만 본문을 쓴다 ═══════════════
def test_apply_commentary_refuses_while_locked(tmp_path, monkeypatch):
    """flock 이 남에게 있으면 본문을 건드리지 않는다."""
    from scripts.apply_commentary import main as commentary_main

    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    holder = state_mod.acquire_lock(state_mod.lock_path("2026-W37", tmp_path))
    monkeypatch.setenv(state_mod.LOCK_TIMEOUT_ENV, "0.2")
    try:
        monkeypatch.setattr(
            "sys.argv",
            ["apply_commentary.py", "2026-W37", "--commentary", "의견",
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
        ["apply_commentary.py", "2026-W37", "--commentary", "의견",
         "--out-dir", str(tmp_path)],
    )
    assert commentary_main() == 2
    assert md.read_text(encoding="utf-8") == SAMPLE_MD


# ══ 사이클3 #7: 섹션 정본 = check.json 의 정확한 헤딩 목록 ═════════════
PROSE_LOOKALIKE_MD = """# 협의회 주간 정책브리핑 2026-W37

**기간:** 2026-09-07 ~ 2026-09-13

## 산림 정책 동향

### 산림 항공 점검 결과

**기관:** 산림청
**마감:** 미정
**원문:** [https://example.com/a](https://example.com/a)

## 협의회에서 — 지원사업 의견

### 참고 자료

**원문:** [https://example.com/note](https://example.com/note)

우리 협의회는 자격 요건 완화를 건의했습니다.
"""


def test_partial_name_prose_section_is_not_an_item_section():
    """`## 협의회에서 — 지원사업 의견` 은 공고 섹션이 아니다 (부분 일치 금지)."""
    items = prune_mod.item_blocks(PROSE_LOOKALIKE_MD)
    assert [block["url"] for block in items] == ["https://example.com/a"]
    assert prune_mod.item_block_count(PROSE_LOOKALIKE_MD) == 1
    # 그 섹션의 블록은 죽어도 삭제되지 않는다 — 링크만 떼고 문장 보존
    text, removed, stripped = prune_mod.strip_dead_urls(
        PROSE_LOOKALIKE_MD, ["https://example.com/note"]
    )
    assert removed == []
    assert stripped == ["https://example.com/note"]
    assert "### 참고 자료" in text
    assert "자격 요건 완화를 건의했습니다" in text
    assert sections_mod.caps_by_heading(("협의회에서 — 지원사업 의견",)) == {}


def test_check_json_records_exact_section_lists(tmp_path, monkeypatch):
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    monkeypatch.setattr(
        "alert.digest.checker.check_url_alive", lambda url, timeout=8: True
    )
    result = check_digest(db_path=str(_empty_db(tmp_path)), markdown_path=md)
    assert result["item_sections"] == list(sections_mod.V1_ITEM_SECTIONS)
    assert result["commentary_sections"] == list(sections_mod.V1_COMMENTARY_SECTIONS)
    assert result["item_blocks"] == 3


def test_consumers_follow_check_json_section_list():
    """check.json 이 항목 섹션을 좁히면 소비자도 그 목록만 인정한다."""
    check = {"item_sections": ["지원사업 공고"], "commentary_sections": []}
    item_sections, _ = sections_mod.resolve(check, SAMPLE_MD)
    assert item_sections == ("지원사업 공고",)
    assert preview_mod.item_urls(SAMPLE_MD, item_sections) == [
        "https://example.com/b", "https://example.com/c"
    ]
    text = preview_mod.render_preview("2026-W37", SAMPLE_MD, dict(PASS_CHECK, **check))
    assert "항목 2건" in text
    assert "■ 지원사업 공고" in text
    assert "■ 산림 정책 동향" not in text


def test_preview_shows_opinion_from_renamed_section():
    """`## 협의회에서` 의 의견도 미리보기 본문에 실린다 (사이클3 #7)."""
    body = PROSE_LOOKALIKE_MD.replace(
        "## 협의회에서 — 지원사업 의견", "## 협의회에서"
    )
    parsed = preview_mod.parse_digest(body)
    assert "자격 요건 완화를 건의했습니다" in parsed["commentary"]
    text = preview_mod.render_preview(
        "2026-W37", body, dict(PASS_CHECK, item_sections=["산림 정책 동향"])
    )
    assert "■ 협의회 의견" in text
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
        "### 스마트팜 의견 조사",
        f"### 스마트팜 의견 조사 [부록]({HIDDEN}2)",
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
            "요약 한 줄.", f"요약 한 줄. 관련 [부록]({HIDDEN})"
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
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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

    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md)])
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

    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    tombstone = state_mod.tombstone_path_for_markdown(md)
    state_mod.write_tombstone(
        tombstone, "무효화 실패: 디스크 오류", "2026-09-13T03:00:00"
    )
    assert state_mod.tombstone_reason(tombstone)

    _forbid_notifier(monkeypatch, "tombstone 이 있는데 발송했다")
    assert _send(md) == 2
    assert send_digest(md, dry_run=True) == 2       # 드라이런도 거부

    sent = []
    monkeypatch.setattr(
        notify_digest, "resolve_target", lambda topic_key="council": (-100, 2011)
    )
    monkeypatch.setattr(notify_digest, "resolve_token", lambda: "123:FAKE")
    monkeypatch.setattr(
        notify_digest, "send_chunk",
        lambda token, chat, thread, text: (sent.append(text), (True, 2014, ""))[1],
    )
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md)])
    assert notify_digest.main() == 0
    assert "무효화 실패" in sent[0] and "1. [" not in sent[0]

    assert state_mod.clear_tombstone(tombstone) is True
    assert state_mod.tombstone_reason(tombstone) is None


# ══ 사이클5 #4: redact 중앙화 ══════════════════════════════════════════
LEAK_TOKEN = "1401666801:AAHt9Xk2mQpLzR7vNbC3dEfGhIjKlMnOpQrSt"


def test_sender_stderr_redacts_check_reason(tmp_path, monkeypatch, capsys):
    """check.reason 의 토큰이 발송기 stderr 로 새지 않는다 (사이클5 #4)."""
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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

    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md)])
    assert notify_digest.main() == 0
    assert sent and LEAK_TOKEN not in sent[0]
    assert "<redacted>" in sent[0]


# ══ 사이클5 #5: 발송 중에는 미리보기를 보내지 않는다 ═══════════════════
def test_notify_skips_preview_while_send_holds_lock(tmp_path, monkeypatch):
    from scripts import notify_digest

    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md)])
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
CTRL_BODY = SAMPLE_MD.replace(
    "요약 한 줄.", "요약 한 줄.\x0b## 기타\x0b[자료](https://x/y)"
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
    assert prune_mod.item_block_count(CTRL_BODY) == 3
    assert len(preview_mod.item_urls(CTRL_BODY)) == 3
    assert len(preview_mod.parse_digest(CTRL_BODY)["items"]) == 3


def test_send_digest_refuses_control_chars(tmp_path, monkeypatch, capsys):
    md = _write_digest(tmp_path, CTRL_BODY.replace(preview_mod.MARKER, "확정 의견"))
    _seed_preview(md)
    _forbid_notifier(monkeypatch, "제어 문자 본문을 발송했다")
    assert _send(md) == 2
    assert "제어 문자" in capsys.readouterr().err


def test_send_digest_refuses_rendered_count_mismatch(
    tmp_path, monkeypatch, capsys
):
    """검증이 말하는 항목 수와 렌더될 항목 수가 다르면 거부 (사이클6)."""
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated, dict(PASS_CHECK, item_blocks=99))
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
    assert "플래그로 넣은 의견" in md.read_text(encoding="utf-8")
    assert preview_mod.MARKER not in md.read_text(encoding="utf-8")


def test_apply_commentary_rejects_positional_and_missing_flag(tmp_path):
    md = tmp_path / "2026-W37.md"
    md.write_text(SAMPLE_MD, encoding="utf-8")
    assert _run_apply(tmp_path, "위치인자").returncode == 2
    assert _run_apply(tmp_path).returncode == 2
    assert _run_apply(
        tmp_path, "--commentary", "a", "--headline", "b"
    ).returncode == 2
    assert md.read_text(encoding="utf-8") == SAMPLE_MD


HEADLINE_MD = """# 협의회 주간 정책브리핑 2026-W37

이번 주 한 줄: <!-- 상민 확정 필요 -->

## 지원사업 공고

### 공고 하나

**기관:** 기관
**마감:** 미정
**원문:** [https://example.com/a](https://example.com/a)

## 협의회 의견

<!-- 상민 확정 필요 -->
"""


def test_headline_and_commentary_target_different_markers():
    """`--headline` 은 한 줄 줄의 마커만, `--commentary` 는 그 밖의 첫 마커만."""
    headlined, ok = apply_headline(HEADLINE_MD, "이번 주 요지")
    assert ok is True
    assert "이번 주 한 줄: 이번 주 요지" in headlined
    assert headlined.count(preview_mod.MARKER) == 1     # 협의회 의견은 그대로

    commented, ok = apply_commentary(HEADLINE_MD, "협의회 의견 본문")
    assert ok is True
    assert "이번 주 한 줄: <!-- 상민 확정 필요 -->" in commented
    assert "협의회 의견 본문" in commented

    both, _ = apply_commentary(headlined, "협의회 의견 본문")
    assert preview_mod.MARKER not in both


def test_apply_headline_via_cli(tmp_path):
    md = tmp_path / "2026-W37.md"
    md.write_text(HEADLINE_MD, encoding="utf-8")
    proc = _run_apply(tmp_path, "--headline", "한 줄 확정")
    assert proc.returncode == 0, proc.stderr
    body = md.read_text(encoding="utf-8")
    assert "이번 주 한 줄: 한 줄 확정" in body
    assert body.count(preview_mod.MARKER) == 1
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
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(
        tmp_path, annotated,
        dict(PASS_CHECK, **{"pass": False, "reason": f"생존 항목 없음 {leak}"}),
    )
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md), "--dry-run"])
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
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
    보내고 옛 바이트로 승인을 발급했다. 잠금 획득 직후 본문을 교체해 두면,
    발급된 승인의 sha 가 **새 바이트**여야 한다.
    """
    from scripts import notify_digest

    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    swapped, _ = apply_commentary(SAMPLE_MD, "확정 의견 — 잠금 뒤 교체")
    check_path = md.with_suffix(".check.json")

    real_acquire = state_mod.acquire_lock

    def acquire_then_swap(path, **kwargs):
        handle = real_acquire(path, **kwargs)
        md.write_text(swapped, encoding="utf-8")
        check_path.write_text(json.dumps(dict(
            PASS_CHECK,
            markdown_sha256=markdown_sha256(md.read_bytes()),
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
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md)])
    assert notify_digest.main() == 0

    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37")
    approval = state_mod.approval_of(state)
    assert approval["sha"] == markdown_sha256(swapped.encode("utf-8"))
    assert approval["check_sha"] == markdown_sha256(check_path.read_bytes())
    assert any("잠금 뒤 교체" in chunk for chunk in sent)


# ══ #2 승인 폐기 실패 = 중단 ════════════════════════════════════════════
def test_recheck_cli_aborts_when_approval_drop_fails(tmp_path, monkeypatch,
                                                     capsys):
    """첫 동작(승인 폐기)이 실패하면 **아무것도 진행하지 않는다** (exit 2)."""
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
    md.write_text(SAMPLE_MD.replace("요약 한 줄.", f"요약 한 줄.{char}"),
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
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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

    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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

    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    observed = []

    def send_chunk(token, chat, thread, text):
        observed.append(_lock_free(md))
        return True, 2013 + len(observed), ""

    _notify_stubs(monkeypatch, notify_digest, send_chunk)
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md)])
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

    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    state_path = state_mod.state_path_for_markdown(md)
    seen = {}

    def send_chunk(token, chat, thread, text):
        state = state_mod.load_state(state_path, "2026-W37")
        seen["approval_during_send"] = state_mod.approval_of(state).get("id")
        seen["ids_during_send"] = list(state.get("preview_message_ids") or [])
        return True, 2014, ""

    _notify_stubs(monkeypatch, notify_digest, send_chunk)
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md)])
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

    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md)])
    assert notify_digest.main() == 1

    state = state_mod.load_state(state_path, "2026-W37")
    assert state_mod.approval_of(state) == {}        # 세대는 폐기됐다
    assert state["preview_message_ids"] == []        # 기록하지 않았다


def test_notify_drops_approval_when_send_fails(tmp_path, monkeypatch):
    """전송이 깨지면 발급해 둔 초안 승인을 폐기한다 (카드 없는 승인 방지)."""
    from scripts import notify_digest

    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    state_path = state_mod.state_path_for_markdown(md)

    _notify_stubs(monkeypatch, notify_digest,
                  lambda token, chat, thread, text: (False, None, "API 실패"))
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md)])
    assert notify_digest.main() == 2
    state = state_mod.load_state(state_path, "2026-W37")
    assert state_mod.approval_of(state) == {}


def test_notify_blocked_notice_drops_old_approval(tmp_path, monkeypatch):
    """차단 안내 경로도 옛 승인을 폐기하고, 안내 message_id 를 최신으로 남긴다."""
    from scripts import notify_digest

    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md)])
    assert notify_digest.main() == 0

    state = state_mod.load_state(state_path, "2026-W37")
    assert state_mod.approval_of(state) == {}            # 옛 승인 폐기
    assert state["preview_message_ids"] == [3014]         # 최신은 안내(항목 0건)
    assert state_mod.preview_urls(state) == []
    assert any("재검증 필요" in text or "검증" in text for text in sent)


def test_notify_aborts_when_blocked_and_approval_drop_fails(
    tmp_path, monkeypatch
):
    """차단인데 승인을 못 지우면 안내조차 보내지 않는다 (사이클7 #2 규율)."""
    from scripts import notify_digest

    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md)])
    assert notify_digest.main() == 2
    assert sent == []


# ══ #5 잠금 타임아웃 종료 코드 2 로 통일 ════════════════════════════════
def test_notify_lock_timeout_exit_code_is_two(tmp_path, monkeypatch):
    from scripts import notify_digest

    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    _seed_preview(md)
    holder = state_mod.acquire_lock(state_mod.lock_path_for_markdown(md))
    monkeypatch.setenv(state_mod.LOCK_TIMEOUT_ENV, "0.2")
    _notify_stubs(monkeypatch, notify_digest,
                  lambda token, chat, thread, text: (True, 2014, ""))
    monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md)])
    try:
        assert notify_digest.main() == 2
    finally:
        state_mod.release_lock(holder)


def test_all_cli_lock_timeouts_exit_two(tmp_path, monkeypatch):
    """recheck·weekly·apply·notify 가 같은 종료 코드(2)로 실패한다."""
    from scripts import notify_digest, recheck_digest, weekly_digest

    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
        monkeypatch.setattr("sys.argv", ["notify_digest.py", str(md)])
        assert notify_digest.main() == 2
        assert _run_apply(tmp_path, "--commentary", "의견").returncode == 2
    finally:
        state_mod.release_lock(holder)
