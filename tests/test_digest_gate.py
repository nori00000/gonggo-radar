"""텔레그램 승인 게이트: 상태 파일·미리보기 렌더·해설 치환·발송 기록 테스트 (계약 W10)."""

import hashlib
import json
from pathlib import Path

import pytest

from alert.digest import state as state_mod
from alert.digest import preview as preview_mod
from scripts.apply_commentary import apply_commentary
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
    md = tmp_path / "2026-W37.md"
    md.write_text(markdown_text or SAMPLE_MD, encoding="utf-8")
    result = dict(check or PASS_CHECK)
    result.setdefault(
        "markdown_sha256",
        hashlib.sha256(md.read_bytes()).hexdigest(),
    )
    (tmp_path / "2026-W37.check.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    return md


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
    state_mod.save_state(
        state_mod.state_path_for_markdown(md),
        state_mod.mark_sent(
            state_mod.default_state("2026-W37"), 1401666801, 2, "2026-09-12T23:00:00"
        ),
    )

    def boom():  # pragma: no cover - 호출되면 게이트가 뚫린 것
        raise AssertionError("이미 발송된 주차에서 EmailNotifier가 생성됐다")

    monkeypatch.setattr("scripts.send_digest.EmailNotifier", boom)
    assert send_digest(md, dry_run=False, approved_by="1401666801") == 2


def test_send_digest_records_state_on_success(tmp_path, monkeypatch):
    annotated, _ = apply_commentary(SAMPLE_MD, "확정 의견")
    md = _write_digest(tmp_path, annotated)

    class FakeNotifier:
        sender = "sender@example.com"
        password = "x"
        recipients = ["a@example.com", "b@example.com"]

        def send_html(self, subject, body, recipients):
            assert recipients == self.recipients
            return True

    monkeypatch.setattr("scripts.send_digest.EmailNotifier", FakeNotifier)
    assert send_digest(md, dry_run=False, approved_by="1401666801") == 0

    state = state_mod.load_state(
        state_mod.state_path_for_markdown(md), "2026-W37"
    )
    assert state["status"] == "sent"
    assert state["approved_by"] == "1401666801"
    assert state["recipients_count"] == 2
    assert state["sent_at"]

    # 멱등: 같은 주차 두 번째 발송은 거부된다
    assert send_digest(md, dry_run=False, approved_by="1401666801") == 2


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
    assert send_digest(md, dry_run=False, approved_by="1") == 2


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
