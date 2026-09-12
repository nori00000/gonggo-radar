"""텔레그램 승인 게이트: 상태 파일·미리보기 렌더·해설 치환·발송 기록 테스트 (계약 W10)."""

import json
import sqlite3
from pathlib import Path

import pytest

from alert.digest import blocks as blocks_mod
from alert.digest import prune as prune_mod
from alert.digest import state as state_mod
from alert.digest import preview as preview_mod
from alert.digest.checker import check_digest, markdown_sha256
from alert.digest.composer import KAKAO_CHUNK_SEPARATOR
from alert.utils.redact import redact
from scripts.apply_commentary import (
    apply_commentary,
    commentary_error,
    main as apply_commentary_main,
)
from scripts.recheck_digest import main as recheck_main
from scripts.send_digest import send_digest


SAMPLE_MD = """<!-- lane: Claude opus executor -->

# 📋 협의회 주간 정책브리핑 2026-W37 (9/7~9/13)

이번 주 한 줄: <!-- 상민 확정 필요 -->

## ✅ 신청하세요 (마감순)

[D-9] 사회적협동조합·사회적기업 공공조달 1:1 컨설팅 참여기업 모집 — 협동조합포털(기재부) · 대상: 사협·사회적기업 · 마감 9/22
  [원문](https://example.com/a)

[새 소식] 2026년도 제2차 산림형 예비사회적기업 지정 계획 공고 — 산림청 · 대상: 사회적기업·산림사업자 · 접수 9/7부터, 마감 원문 확인
  [원문](https://example.com/b)

[상시] 산양삼 등 산촌자원 활용 시제품 개발 지원 참여자 모집 — 한국임업진흥원 · 대상: 산림사업자 · 마감 연중 상시 모집
  [원문](https://example.com/c)

## 👀 알아두세요

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
    """md + check.json. check 에 md_sha256 이 없으면 이 본문의 해시를 채운다."""
    text = markdown_text or SAMPLE_MD
    md = tmp_path / "2026-W37.md"
    md.write_text(text, encoding="utf-8")
    payload = dict(check or PASS_CHECK)
    payload.setdefault("md_sha256", markdown_sha256(text))
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
    """그 파일 본문의 승인 지문 (계약 W10 사이클2 #2)."""
    from pathlib import Path as _Path
    return markdown_sha256(_Path(md).read_text(encoding="utf-8"))[:8]


def _send(md, **kwargs):
    """실발송 호출 — 현재 본문 지문을 승인 지문으로 넘긴다."""
    kwargs.setdefault("approved_by", "1401666801")
    kwargs.setdefault("approved_sha", _sha8(md))
    return send_digest(md, dry_run=False, **kwargs)


def test_send_digest_refuses_already_sent_week(tmp_path, monkeypatch):
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    state_mod.save_state(
        state_mod.state_path_for_markdown(md),
        state_mod.mark_sent(
            state_mod.default_state("2026-W37"), 1401666801, 2, "2026-09-12T23:00:00"
        ),
    )

    def boom():  # pragma: no cover - 호출되면 게이트가 뚫린 것
        raise AssertionError("이미 발송된 주차에서 EmailNotifier가 생성됐다")

    monkeypatch.setattr("scripts.send_digest.EmailNotifier", boom)
    assert _send(md) == 2


def test_send_digest_records_state_on_success(tmp_path, monkeypatch):
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)

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
    assert len(check["items"]) == 4
    assert md.read_text(encoding="utf-8") == annotated
    assert preview_mod.MARKER not in md.read_text(encoding="utf-8")


# ─── 크리틱 #1: 중복 발송 (잠금 · sending 상태) ─────────────────────────
def _annotated_digest(tmp_path):
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    return _write_digest(tmp_path, annotated)


def _forbid_notifier(monkeypatch, why):
    def boom(*args, **kwargs):  # pragma: no cover - 호출되면 게이트가 뚫린 것
        raise AssertionError(why)

    monkeypatch.setattr("scripts.send_digest.EmailNotifier", boom)


def test_send_digest_refuses_when_lock_exists(tmp_path, monkeypatch):
    """동시 실행: 잠금 파일이 있으면 SMTP까지 가지 않는다."""
    md = _annotated_digest(tmp_path)
    lock = state_mod.lock_path_for_markdown(md)
    lock.write_text("99999\n", encoding="utf-8")
    _forbid_notifier(monkeypatch, "잠금이 있는데 발송을 시작했다")
    assert _send(md) == 2


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
    state_mod.save_state(
        state_mod.state_path_for_markdown(md),
        state_mod.mark_sending(
            state_mod.default_state("2026-W37"), "2026-09-12T23:00:00"
        ),
    )
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
    fd = state_mod.acquire_lock(path)
    with pytest.raises(state_mod.LockBusy):
        state_mod.acquire_lock(path)
    state_mod.release_lock(path, fd)
    assert not path.exists()
    # 해제 후에는 다시 잡을 수 있다
    state_mod.release_lock(path, state_mod.acquire_lock(path))


# ─── 크리틱 #3: 과거 검증 재사용 ─────────────────────────────────────────
def test_send_digest_refuses_stale_check_hash(tmp_path, monkeypatch):
    """검증 이후 본문이 바뀌면 (해시 불일치) 발송을 거부한다."""
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
    md.write_text(annotated + "\n### 몰래 추가한 항목\n", encoding="utf-8")
    _forbid_notifier(monkeypatch, "해시 불일치인데 발송했다")
    assert _send(md) == 2


def test_send_digest_refuses_check_without_hash(tmp_path, monkeypatch):
    """본문 해시가 없는 check.json(구버전)도 거부 — 과거 검증일 수 있다."""
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(
        tmp_path, annotated, dict(PASS_CHECK, md_sha256=None)
    )
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


def test_recheck_records_md_sha256(tmp_path, monkeypatch):
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
    assert check["md_sha256"] == markdown_sha256(md.read_text(encoding="utf-8"))


def test_recheck_failure_overwrites_check_as_fail(tmp_path, monkeypatch):
    """재검증 예외(DB 오류·타임아웃)는 과거 pass 를 무효화한다."""
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)
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
    assert check["md_sha256"] == markdown_sha256(body)


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
    sending = dict(_sending_state(), commentary="확정 의견")
    state_mod.save_state(path, sending)
    released = state_mod.release_sending(sending)
    assert released["status"] == "annotated" and released["sending_at"] is None
    # 해설 기록이 없어도 annotated 로 푼다 (마커 게이트를 통과한 본문이므로)
    assert state_mod.release_sending(_sending_state())["status"] == "annotated"
    with pytest.raises(state_mod.TransitionError):
        state_mod.apply_state(path, sending, released)          # escape 없음
    state_mod.apply_state(path, sending, released, escape=True)  # 사람의 해제
    assert state_mod.load_state(path, "2026-W37")["status"] == "annotated"

    with pytest.raises(state_mod.TransitionError):
        state_mod.release_sending(state_mod.default_state("2026-W37"))


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
    assert commentary_main() == 1
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

    real_gate = send_mod.check_gate
    captured = []

    def gate_then_swap(markdown_text, check_json_path):
        result = real_gate(markdown_text, check_json_path)
        md.write_text(
            markdown_text
            + "\n### 몰래 끼운 항목\n\n**원문:** [x](https://evil.example/x)\n",
            encoding="utf-8",
        )
        return result

    class Notifier(_OkNotifier):
        bodies = captured

    monkeypatch.setattr("scripts.send_digest.check_gate", gate_then_swap)
    monkeypatch.setattr("scripts.send_digest.EmailNotifier", Notifier)
    assert _send(md) == 0
    assert captured and "evil.example" not in captured[0]
    assert "몰래 끼운" not in captured[0]


# ══ 사이클2: 잠금 회수 (#3) ═════════════════════════════════════════════
def test_acquire_lock_records_pid_and_timestamp(tmp_path):
    import os

    path = tmp_path / "2026-W37.lock"
    fd = state_mod.acquire_lock(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["started_at"] > 0
    state_mod.release_lock(path, fd)


def test_acquire_lock_reclaims_dead_pid(tmp_path):
    """크래시로 남은 잠금은 회수하고 진행한다 (Codex 신규 #3)."""
    path = tmp_path / "2026-W37.lock"
    path.write_text(
        json.dumps({"pid": 999999, "started_at": 9e9}), encoding="utf-8"
    )
    reclaimed = []
    fd = state_mod.acquire_lock(path, on_reclaim=reclaimed.append)
    assert reclaimed and "보유 프로세스 없음" in reclaimed[0]
    state_mod.release_lock(path, fd)


def test_acquire_lock_reclaims_timed_out_lock(tmp_path):
    import os

    path = tmp_path / "2026-W37.lock"
    path.write_text(
        json.dumps({"pid": os.getpid(), "started_at": 0}), encoding="utf-8"
    )
    reclaimed = []
    fd = state_mod.acquire_lock(path, on_reclaim=reclaimed.append)
    assert reclaimed and "보유 시간 초과" in reclaimed[0]
    state_mod.release_lock(path, fd)


def test_acquire_lock_reclaims_corrupt_lock(tmp_path):
    path = tmp_path / "2026-W37.lock"
    path.write_text("not json", encoding="utf-8")
    reclaimed = []
    fd = state_mod.acquire_lock(path, on_reclaim=reclaimed.append)
    assert reclaimed and "pid 없음" in reclaimed[0]
    state_mod.release_lock(path, fd)


def test_acquire_lock_refuses_live_lock(tmp_path):
    import os
    import time as _time

    path = tmp_path / "2026-W37.lock"
    path.write_text(
        json.dumps({"pid": os.getpid(), "started_at": _time.time()}),
        encoding="utf-8",
    )
    with pytest.raises(state_mod.LockBusy):
        state_mod.acquire_lock(path)


def test_send_digest_reclaims_stale_lock(tmp_path, monkeypatch):
    md = _annotated_digest(tmp_path)
    state_mod.lock_path_for_markdown(md).write_text(
        json.dumps({"pid": 999999, "started_at": 9e9}), encoding="utf-8"
    )
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


def test_section_key_only_matches_item_sections():
    """항목 섹션은 composer.SECTION_LIMITS 의 키가 정본 (계약 v2 현재 이름).

    이모지·부제가 붙은 발송본 헤딩(`## ✅ 신청하세요 (마감순)`)에서도 키를 찾아야
    한다 — 못 찾으면 게이트가 조용히 "항목 0건"을 센다.
    """
    assert blocks_mod.section_key("✅ 신청하세요 (마감순)") == "신청하세요"
    assert blocks_mod.section_key("👀 알아두세요") == "알아두세요"
    for prose in ("🤝 협의회에서", "🏢 회원사 소식", "협의회 의견",
                  "회원사 동정", "이번 주 한 줄", ""):
        assert blocks_mod.section_key(prose) is None


def test_section_key_follows_composer_rename(monkeypatch):
    """섹션 이름이 바뀌면 composer 를 따라간다 (임의의 이름으로 갈아도)."""
    from alert.digest import composer

    monkeypatch.setattr(
        composer, "SECTION_LIMITS", {"산림 정책 동향": 5, "지원사업 공고": 5}
    )
    assert blocks_mod.section_key("산림 정책 동향") == "산림 정책 동향"
    assert blocks_mod.section_key("지원사업 공고") == "지원사업 공고"
    for prose in ("✅ 신청하세요 (마감순)", "🏢 회원사 소식", "이번 주 한 줄"):
        assert blocks_mod.section_key(prose) is None


def test_blocks_read_the_v2_send_format():
    """블록 파서가 계약 v2 발송본 형식을 그대로 읽는다 (사이클 6 #1).

    v2 는 `### ` 머리글도 `**원문:**` 도 쓰지 않는다 — 항목 줄 + `  [원문](URL)`
    두 줄이 한 블록이다. 여기가 어긋나면 checker 의 항목 수·상한 게이트와
    미리보기 번호가 조용히 갈라진다.
    """
    blocks = blocks_mod.item_blocks(SAMPLE_MD)
    assert [block["key"] for block in blocks] == [
        "신청하세요", "신청하세요", "신청하세요", "알아두세요",
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


def test_fallback_item_keys_are_the_v2_sections():
    """composer 를 못 읽어도 계약 v2 항목 섹션 이름은 알아본다.

    구형 v1.2 이름은 더 이상 생성되지 않으므로 폴백에서 뺐다 (사이클 6 #1) —
    지원하는 척하면 두 형식의 계수가 또 갈라진다.
    """
    assert blocks_mod.FALLBACK_ITEM_SECTION_KEYS == ("신청하세요", "알아두세요")


def test_item_blocks_counts_blocks_not_links():
    """해설의 참고 링크는 항목이 아니다 (Codex 신규 #6)."""
    assert blocks_mod.item_block_count(SAMPLE_MD) == 4
    assert blocks_mod.item_block_count(COMMENTARY_WITH_BLOCK) == 4
    assert len(preview_mod.item_urls(COMMENTARY_WITH_BLOCK)) == 4


def test_checker_fails_when_no_item_blocks(tmp_path, monkeypatch):
    """공고가 0건이면 해설 링크가 살아 있어도 pass=false (Codex 신규 #6)."""
    only_commentary = SAMPLE_MD
    for url in ("https://example.com/a", "https://example.com/b",
                "https://example.com/c", "https://example.com/d"):
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


DUP_TAIL = " — 산림청 · 대상: 사회적기업 · 마감 미정"
DUP_PLAIN = "[상시] 동향 자료" + DUP_TAIL


def _duplicate_title_body() -> str:
    """제목이 같고 **URL이 다른** 두 항목 (소스 간 비병합 계약의 재현 입력)."""
    body = _replace_item_title(SAMPLE_MD, "https://example.com/b", DUP_PLAIN)
    return _replace_item_title(body, "https://example.com/c", DUP_PLAIN)


def _duplicate_url_body() -> str:
    """같은 URL 을 두 번 가리키는 본문 (재조립 사고의 재현 입력)."""
    return SAMPLE_MD.replace("https://example.com/c", "https://example.com/b")


def test_dedupe_urls_keeps_same_title_from_different_sources():
    """제목이 같아도 URL 이 다르면 남긴다 (사이클 6 #3 / Codex 신규 #5).

    병합 판정은 compose 단계의 몫이다 — forest_service·forest_press 가 같은 사안을
    각자 게시하는 비병합 계약을 재검토가 뒤집으면 안 된다.
    """
    body = _duplicate_title_body()
    assert body.count(DUP_PLAIN) == 2
    deduped, removed = prune_mod.dedupe_urls(body)
    assert removed == []
    assert deduped == body
    assert blocks_mod.item_block_count(deduped) == 4


def test_dedupe_urls_drops_later_same_url_block():
    """같은 URL 을 가리키는 뒤쪽 블록만 지운다 (사이클 6 #3)."""
    body = _duplicate_url_body()
    assert blocks_mod.item_block_count(body) == 4
    deduped, removed = prune_mod.dedupe_urls(body)
    assert [item["url"] for item in removed] == ["https://example.com/b"]
    assert blocks_mod.item_block_count(deduped) == 3
    assert blocks_mod.item_urls(deduped) == [
        "https://example.com/a", "https://example.com/b", "https://example.com/d",
    ]


def test_prune_has_no_title_dedupe():
    """제목 기반 중복 제거는 폐지됐다 (사이클 6 #3)."""
    assert not hasattr(prune_mod, "dedupe_titles")


def test_recheck_dedupes_same_url_after_link_strip(tmp_path, monkeypatch):
    body = _duplicate_url_body()
    annotated, _ = apply_commentary(body, f"확정 의견 [자료]({DEAD}) 참고")
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
    assert DEAD not in final
    assert "확정 의견 자료 참고" in final       # 산문은 문장을 보존한다
    assert final.count("[원문](https://example.com/b)") == 1
    check = json.loads((tmp_path / "2026-W37.check.json").read_text(encoding="utf-8"))
    assert check["pass"] is True
    assert check["item_blocks"] == 3
    assert "https://example.com/b" in [item["url"] for item in check["dropped"]]


def test_cap_violation_fails_the_gate(tmp_path, monkeypatch):
    """섹션 상한을 넘은 본문은 pass=false (상한을 다시 적용하지는 않는다)."""
    extra = "".join(
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
    result = check_digest(db_path=str(_empty_db(tmp_path)), markdown_path=md)
    assert result["pass"] is False
    assert "섹션 상한 초과" in result["reason"]


def test_section_caps_come_from_composer():
    from alert.digest import composer

    assert blocks_mod.section_caps() == {
        str(key): int(value) for key, value in composer.SECTION_LIMITS.items()
    }
    assert set(blocks_mod.item_section_keys()) == set(composer.SECTION_LIMITS)


# ══ 사이클2: redact bare 토큰 (#8) ══════════════════════════════════════
def test_redact_hides_bare_token_without_secrets():
    bare = "1401666801:AAHt9Xk2mQpLzR7vNbC3dEfGhIjKlMnOpQr"
    cleaned = redact(f"login failed for {bare}")
    assert bare not in cleaned
    assert "<redacted>" in cleaned


def test_redact_keeps_ordinary_colon_numbers():
    for text in ("12:30:45", "exit 2 — 2026-W37", "port 443:8080"):
        assert redact(text) == text


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
            assert not any("<!--" in line for line in block["lines"])
            assert len([line for line in block["lines"] if line.strip()]) == 2


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
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
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
        ["recheck_digest.py", str(md), "--db", str(_empty_db(tmp_path))],
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
        ["2026-W37", "확정 문구", "--out-dir", str(tmp_path)]
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
