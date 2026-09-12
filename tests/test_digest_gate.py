"""텔레그램 승인 게이트: 상태 파일·미리보기 렌더·해설 치환·발송 기록 테스트 (계약 W10)."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from alert.digest import prune as prune_mod
from alert.digest import sections as sections_mod
from alert.digest import state as state_mod
from alert.digest import preview as preview_mod
from alert.digest.checker import check_digest, markdown_sha256
from alert.utils.redact import redact
from scripts.apply_commentary import apply_commentary, commentary_error
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

    def boom():  # pragma: no cover - 호출되면 게이트가 뚫린 것
        raise AssertionError("이미 발송된 주차에서 EmailNotifier가 생성됐다")

    monkeypatch.setattr("scripts.send_digest.EmailNotifier", boom)
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
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
        ["apply_commentary.py", "2026-W37", bad, "--out-dir", str(tmp_path)],
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
        ["apply_commentary.py", "2026-W37", "의견", "--out-dir", str(tmp_path)],
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
    assert items == ("✅ 신청하세요 (마감순)", "👀 알아두세요")
    assert commentary == ("🤝 협의회에서",)


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
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)     # 미리보기 기록 없음
    _forbid_notifier(monkeypatch, "미리보기 없이 발송했다")
    assert _send(md) == 2


def test_send_digest_refuses_when_preview_sha_differs(tmp_path, monkeypatch):
    """사람이 본 미리보기와 다른 본문은 지문이 맞아도 발송하지 않는다 (#3)."""
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
        ["apply_commentary.py", "2026-W37", "보류 후 의견", "--out-dir", str(tmp_path)],
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
        ["apply_commentary.py", "2026-W37", "의견", "--out-dir", str(tmp_path)],
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
        ["apply_commentary.py", "2026-W37", "의견", "--out-dir", str(tmp_path)],
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
