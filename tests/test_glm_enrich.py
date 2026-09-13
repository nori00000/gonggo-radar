"""GLM 야간 요약 레인(V3) — 게이트·보강 줄 부착 테스트."""

import codecs
import json
import os
import sqlite3
import time

import pytest

from alert.digest import blocks as blocks_mod
from alert.digest import composer as composer_mod
from alert.digest import preview as preview_mod
from alert.digest.checker import check_digest
from alert.digest.composer import compose_digest, load_items_manifest
from alert.utils import http_fetch
from scripts import glm_enrich as glm_mod

W13 = "2026-W13"
W13_CREATED_AT = "2026-03-26T12:00:00"


@pytest.fixture(autouse=True)
def stub_detail_fetch(monkeypatch):
    """상세 텍스트 수집은 **절대 실호출하지 않는다** — 기본 스텁은 빈 문자열이다.

    개별 테스트가 근거 텍스트를 공급하려면 같은 이름을 다시 갈아끼운다.
    """
    monkeypatch.setattr(glm_mod, "fetch_detail_text", lambda url, **kwargs: "")


def _label(entry) -> str:
    """항목 제목을 그대로 옮긴 **추출형** 한 줄 의미 (r3b 문법 통과).

    r3b 부터 `한 줄 의미`는 «구절» **하나**이거나 정확히 `원문 확인` 이다 —
    인용 밖에는 한 글자도 올 수 없다.
    """
    return "«{}»".format(entry["title"])


def _create_table(db_path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE announcements (
            id INTEGER PRIMARY KEY, source TEXT, source_id TEXT,
            title TEXT, summary TEXT DEFAULT '', url TEXT, author TEXT,
            category TEXT DEFAULT '', target TEXT DEFAULT '',
            period_start TEXT, period_end TEXT,
            relevance_score REAL DEFAULT 0.0,
            relevance_reason TEXT DEFAULT '',
            matched_keywords TEXT DEFAULT '[]',
            is_notified INTEGER DEFAULT 0,
            raw_data TEXT DEFAULT '', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, business_domain TEXT DEFAULT '',
            domain_confidence REAL DEFAULT 0.0,
            obsidian_path TEXT DEFAULT '', embedding_id INTEGER DEFAULT NULL,
            UNIQUE(source, source_id)
        )
        """
    )
    conn.commit()
    conn.close()


def _insert(db_path, **overrides) -> None:
    row = {
        "source": "kofpi",
        "source_id": "test_001",
        "title": "산림 분야 지원사업 참여기업 모집 공고",
        "summary": "임업 기업 대상 접수기간 9월 22일까지 신청 가능합니다.",
        "url": "https://example.com/test-001",
        "author": "기관",
        "period_start": None,
        "period_end": "2026-12-31",
        "raw_data": "",
        "relevance_score": 0.9,
        "created_at": W13_CREATED_AT,
    }
    row.update(overrides)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO announcements
        (source, source_id, title, summary, url, author, period_start,
         period_end, raw_data, relevance_score, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["source"], row["source_id"], row["title"], row["summary"],
            row["url"], row["author"], row["period_start"],
            row["period_end"], row["raw_data"],
            row["relevance_score"], row["created_at"], row["created_at"],
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def digest_fixture(tmp_path):
    """W13 다이제스트(md + items.json + check.json)를 실제로 조립해 돌려준다."""
    db_path = tmp_path / "announcements.db"
    _create_table(db_path)
    _insert(
        db_path,
        source="kofpi", source_id="a1",
        title="산림분야 오픈이노베이션 참여기업 모집 공고",
        summary="임업 기업 대상 접수기간 9월 22일까지 신청 가능합니다.",
        url="https://example.com/kofpi/1",
        period_end="2026-09-22",
    )
    _insert(
        db_path,
        source="seis", source_id="a2",
        title="경기도 사회적기업 사회보험료 지원사업 참여기업 모집 공고",
        summary="경기도 소재 사회적기업 사회보험료 일부 지원",
        url="https://example.com/seis/2",
        period_end="2026-12-30",
    )

    out_dir = tmp_path / "digests"
    markdown_path = out_dir / f"{W13}.md"
    compose_digest(db_path=str(db_path), week_str=W13, output_path=markdown_path)
    result = check_digest(
        db_path=str(db_path), markdown_path=markdown_path,
        output_path=out_dir / f"{W13}.check.json", skip_network=False,
    )
    assert result["pass"], result.get("reason")
    return {"db_path": str(db_path), "out_dir": out_dir, "markdown_path": markdown_path}


def _manifest_items(markdown_path):
    manifest = load_items_manifest(markdown_path)
    assert manifest is not None
    return manifest["items"]


# ─── 게이트 단위 테스트 (가짜 GLM 출력 4종) ───────────────────────────────
def _one_input_item(n=1, **overrides):
    item = {
        "n": n,
        "id": 46,
        "title": "테스트 공고",
        "summary": "9월 22일까지 접수, 지원금 300만원",
        "quote_deadline": "2026-09-22",
        "quote_eligibility": "원문 확인",
        "quote_amount": "원문 확인",
        "url": "https://example.com/x",
    }
    item.update(overrides)
    return item


def test_gate_output_normal_case_applies_all_fields():
    items = [_one_input_item()]
    raw = json.dumps([{
        "n": 1, "대상 태그": "사회적기업(경기)",
        "마감": "2026-09-22 «9월 22일까지»",
        "자격": "원문 확인", "금액": "300만원 «지원금 300만원»",
        "한 줄 의미": "«9월 22일까지 접수»",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert warnings == []
    assert results[1]["한 줄 의미"] == "«9월 22일까지 접수»"
    assert results[1]["마감"] == "2026-09-22 «9월 22일까지»"
    assert results[1]["금액"] == "300만원 «지원금 300만원»"
    assert results[1]["대상 태그"] == "사회적기업(경기)"


def test_gate_output_quote_not_substring_falls_back_and_warns():
    items = [_one_input_item()]
    raw = json.dumps([{
        "n": 1, "대상 태그": "사회적기업",
        "마감": "2026-09-22 «날조된 인용문»",  # 입력 텍스트에 없는 인용
        "자격": "원문 확인", "금액": "원문 확인",
        "한 줄 의미": "«9월 22일까지 접수»",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["마감"] == glm_mod.FALLBACK
    assert any("마감" in w and "인용" in w for w in warnings)
    # 인용 실패가 다른 필드(한 줄 의미)까지 통째로 버리지 않는다
    assert results[1]["한 줄 의미"] == "«9월 22일까지 접수»"


def test_gate_output_parse_failure_returns_no_results():
    items = [_one_input_item()]
    raw = "이것은 JSON이 아닙니다 — 죄송합니다."

    results, warnings = glm_mod.gate_output(raw, items)

    assert results == {}
    assert any("파싱" in w for w in warnings)


def test_gate_output_disallowed_tag_falls_back_to_hold():
    items = [_one_input_item()]
    raw = json.dumps([{
        "n": 1, "대상 태그": "일반기업",  # 허용값 밖
        "마감": "원문 확인", "자격": "원문 확인", "금액": "원문 확인",
        "한 줄 의미": "신청 가능",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["대상 태그"] == "보류"
    assert any("허용값" in w for w in warnings)


def test_gate_output_missing_n_discards_every_item():
    """V3.1 B3: n 집합이 입력과 다르면 **부분 적용 금지** — 출력 전체를 버린다."""
    items = [_one_input_item(n=1), _one_input_item(n=2, id=47)]
    raw = json.dumps([{
        "n": 1, "대상 태그": "전체", "마감": "원문 확인",
        "자격": "원문 확인", "금액": "원문 확인", "한 줄 의미": "확인 가능",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results == {}
    assert any("[2]" in w and "폐기" in w for w in warnings)


# ─── --dry-run: ds 호출 없이 입력 JSON만 생성 ──────────────────────────────
def test_dry_run_writes_input_json_without_touching_markdown(digest_fixture):
    markdown_before = digest_fixture["markdown_path"].read_text(encoding="utf-8")

    class Args:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = True
        apply_json = None

    rc = glm_mod.run(Args())

    assert rc == 0
    assert digest_fixture["markdown_path"].read_text(encoding="utf-8") == markdown_before
    input_json_path = digest_fixture["out_dir"] / f"{W13}.glm_input.json"
    assert input_json_path.exists()
    payload = json.loads(input_json_path.read_text(encoding="utf-8"))
    assert "today" in payload
    assert len(payload["items"]) == len(_manifest_items(digest_fixture["markdown_path"]))
    # GLM 에 보내는 페이로드에는 내부 부기 필드(id)가 없다
    assert "id" not in payload["items"][0]
    assert set(payload["items"][0]) == {
        "n", "title", "source_name", "summary", "detail_text",
        "quote_deadline", "quote_eligibility", "quote_amount", "url",
    }


# ─── --apply-json: 가짜 출력 적용 → 정본·checker 가 → 줄을 허용 ───────────
def test_apply_json_adds_enrich_line_without_changing_item_line(digest_fixture, tmp_path):
    markdown_path = digest_fixture["markdown_path"]
    items = _manifest_items(markdown_path)
    assert len(items) >= 1
    original_lines = {entry["id"]: entry["line"] for entry in items}
    original_origins = {entry["id"]: entry["origin_line"] for entry in items}
    # 정본 순서 == n 순서(1-based) — payload_for_glm 이 id 를 빼므로 이 매핑으로
    # 되짚는다.
    id_to_n = {entry["id"]: idx + 1 for idx, entry in enumerate(items)}

    # 입력 JSON을 먼저 만들어 n <-> id 매핑을 그대로 재사용한다.
    class Args:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = True
        apply_json = None

    glm_mod.run(Args())
    input_payload = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_input.json").read_text(encoding="utf-8")
    )
    fake_output = [
        {
            "n": entry["n"], "대상 태그": "전체", "마감": "원문 확인",
            "자격": "원문 확인", "금액": "원문 확인",
            "한 줄 의미": _label(entry),
        }
        for entry in input_payload["items"]
    ]
    fake_output_path = tmp_path / "fake_glm_output.json"
    fake_output_path.write_text(json.dumps(fake_output, ensure_ascii=False), encoding="utf-8")

    class ApplyArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = str(fake_output_path)

    rc = glm_mod.run(ApplyArgs())
    assert rc == 0

    new_markdown = markdown_path.read_text(encoding="utf-8")
    assert "  → " + _label(input_payload["items"][0]) in new_markdown

    # 항목 줄·원문 줄은 정본과 여전히 동일 문자열(편집 불가 영역 불변)
    refreshed_items = _manifest_items(markdown_path)
    for entry in refreshed_items:
        assert entry["line"] == original_lines[entry["id"]]
        assert entry["origin_line"] == original_origins[entry["id"]]
        assert entry["enrich_line"] == blocks_mod.enrich_line_text(
            {item["n"]: _label(item) for item in input_payload["items"]}[
                id_to_n[entry["id"]]
            ]
        )

    # blocks 파서가 보강 줄을 항목 블록의 일부로 인정한다 (산문으로 오판하지 않음)
    item_sections = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.check.json").read_text(encoding="utf-8")
    ).get("item_sections")
    assert blocks_mod.prose_lines_in_item_sections(new_markdown, item_sections) == []
    parsed_blocks = blocks_mod.item_blocks(new_markdown, item_sections)
    assert all(b["enrich_line"] for b in parsed_blocks)

    # recheck 가 보강 후 본문에서도 pass=True (해시·항목 대조가 새 본문을 본다)
    recheck_result = check_digest(
        db_path=digest_fixture["db_path"], markdown_path=markdown_path,
        output_path=None, skip_network=False,
    )
    assert recheck_result["pass"], recheck_result.get("reason")
    assert recheck_result.get("manifest_problems") == []


def test_apply_json_is_idempotent(digest_fixture, tmp_path):
    """같은 출력을 두 번 적용해도 본문이 더 늘어나지 않는다(교체, 누적 아님)."""
    markdown_path = digest_fixture["markdown_path"]

    class DryArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = True
        apply_json = None

    glm_mod.run(DryArgs())
    input_payload = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_input.json").read_text(encoding="utf-8")
    )
    fake_output = [
        {
            "n": entry["n"], "대상 태그": "전체", "마감": "원문 확인",
            "자격": "원문 확인", "금액": "원문 확인", "한 줄 의미": _label(entry),
        }
        for entry in input_payload["items"]
    ]
    fake_output_path = tmp_path / "fake.json"
    fake_output_path.write_text(json.dumps(fake_output, ensure_ascii=False), encoding="utf-8")

    class ApplyArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = str(fake_output_path)

    glm_mod.run(ApplyArgs())
    once = markdown_path.read_text(encoding="utf-8")
    glm_mod.run(ApplyArgs())
    twice = markdown_path.read_text(encoding="utf-8")

    assert once == twice
    assert once.count("  → «") == len(input_payload["items"])


def test_manifest_problems_flag_hand_edited_enrich_line(digest_fixture):
    """보강 줄을 md 에서만 손으로 고치면(정본 미갱신) 게이트가 fail-closed."""
    markdown_path = digest_fixture["markdown_path"]
    manifest_items = _manifest_items(markdown_path)
    item_id = manifest_items[0]["id"]

    text = markdown_path.read_text(encoding="utf-8")
    new_text, changed = blocks_mod.set_enrich_lines(
        text, {str(item_id): blocks_mod.enrich_line_text("몰래 고친 문구")}
    )
    assert changed == [str(item_id)]
    markdown_path.write_text(new_text, encoding="utf-8")
    # 정본(items.json)의 enrich_line 은 갱신하지 않는다 — 대조가 어긋나야 한다.

    result = check_digest(
        db_path=digest_fixture["db_path"], markdown_path=markdown_path,
        output_path=None, skip_network=False,
    )
    assert result["pass"] is False
    assert any("보강 줄" in p for p in result["manifest_problems"])


# ─── 헤드라인 초안 삽입 ────────────────────────────────────────────────────
def test_apply_headline_draft_inserts_comment_after_marker():
    text = "# 제목\n\n이번 주 한 줄: <!-- 상민 확정 필요 -->\n\n## 섹션\n"
    new_text, changed = glm_mod.apply_headline_draft(text, "신청 2건, 마감 9/22까지")
    assert changed is True
    assert "<!-- GLM 초안: 신청 2건, 마감 9/22까지 -->" in new_text
    lines = new_text.split("\n")
    headline_index = next(i for i, line in enumerate(lines) if line.startswith("이번 주 한 줄:"))
    assert lines[headline_index + 1] == "<!-- GLM 초안: 신청 2건, 마감 9/22까지 -->"


def test_apply_headline_draft_replaces_existing_draft_idempotently():
    text = (
        "# 제목\n\n이번 주 한 줄: <!-- 상민 확정 필요 -->\n"
        "<!-- GLM 초안: 옛 문구 -->\n\n## 섹션\n"
    )
    new_text, changed = glm_mod.apply_headline_draft(text, "새 문구")
    assert changed is True
    assert new_text.count("<!-- GLM 초안:") == 1
    assert "<!-- GLM 초안: 새 문구 -->" in new_text

    same_text, changed_again = glm_mod.apply_headline_draft(new_text, "새 문구")
    assert changed_again is False
    assert same_text == new_text


# ══ V3.1 ═══════════════════════════════════════════════════════════════════
# A. 근거 공급 — 상세 텍스트 수집 (공용 헬퍼 alert/utils/http_fetch)
# ─────────────────────────────────────────────────────────────────────────
class _FakeResponse:
    """urllib3 HTTPResponse 흉내 — `_open` 이 돌려주는 것."""

    def __init__(self, status=200, headers=None, chunks=()):
        self.status = status
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.released = False

    def stream(self, amt=None, decode_content=True):
        return iter(self._chunks)

    def release_conn(self):
        self.released = True

    def close(self):
        pass


def _use_open(monkeypatch, *responses):
    """`http_fetch._open` 을 갈아끼우고 요청 URL 을 기록한다 (실호출 금지)."""
    queue = list(responses)
    calls = []

    def fake_open(url):
        calls.append(url)
        if not queue:
            raise AssertionError("예상보다 많은 요청")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(http_fetch, "_open", fake_open)
    return calls


def _html_response(body: str):
    return _FakeResponse(
        headers={"content-type": "text/html; charset=utf-8"},
        chunks=[body.encode("utf-8")],
    )


def _redirect_response(location, status=302):
    headers = {"location": location} if location is not None else {}
    return _FakeResponse(status=status, headers=headers)


def test_visible_text_drops_script_style_nav_and_normalizes_whitespace():
    html_text = (
        "<html><head><style>.a{color:red}</style></head><body>"
        "<nav>메뉴 메뉴</nav><script>alert('x')</script>"
        "<h1>공고\n제목</h1><p>접수기간&nbsp;9월 22일까지</p>"
        "<!-- 숨은 주석 --></body></html>"
    )
    text = http_fetch.visible_text(html_text)
    assert "alert(" not in text
    assert "color:red" not in text
    assert "메뉴" not in text
    assert "숨은 주석" not in text
    assert "공고 제목 접수기간 9월 22일까지" in text


def test_visible_text_truncates_to_limit():
    assert len(http_fetch.visible_text("<p>" + "가" * 5000 + "</p>")) == (
        http_fetch.DETAIL_TEXT_CHARS
    )


def test_fetch_detail_text_reads_html_through_the_shared_open_helper(monkeypatch):
    calls = _use_open(monkeypatch, _html_response("<html><body><p>공고 본문</p></body></html>"))
    assert http_fetch.fetch_detail_text("https://example.test/a") == "공고 본문"
    assert calls == ["https://example.test/a"]


def test_fetch_detail_text_is_fail_open_on_error_status_non_html_and_exception(monkeypatch):
    _use_open(monkeypatch, _FakeResponse(status=404))
    assert http_fetch.fetch_detail_text("https://example.test/a") == ""

    _use_open(monkeypatch, _FakeResponse(
        headers={"content-type": "application/pdf"}, chunks=[b"%PDF-1.4"],
    ))
    assert http_fetch.fetch_detail_text("https://example.test/a") == ""

    def boom(url):
        raise RuntimeError("boom")

    monkeypatch.setattr(http_fetch, "_open", boom)
    assert http_fetch.fetch_detail_text("https://example.test/a") == ""

    assert http_fetch.fetch_detail_text("ftp://example.test/a") == ""
    assert http_fetch.fetch_detail_text("") == ""


def test_fetch_detail_text_stops_at_max_bytes(monkeypatch):
    _use_open(monkeypatch, _FakeResponse(
        headers={"content-type": "text/html"},
        chunks=[b"a" * 400000, b"b" * 400000, b"c" * 400000],
    ))
    text = http_fetch.fetch_detail_text(
        "https://example.test/a", limit=http_fetch.MAX_DETAIL_BYTES
    )
    assert len(text) <= http_fetch.MAX_DETAIL_BYTES
    assert "c" not in text


def test_checker_probe_and_detail_fetch_share_the_same_user_agent():
    from alert.digest import checker as checker_mod

    assert checker_mod.PROBE_HEADERS is http_fetch.PROBE_HEADERS
    assert checker_mod.BROWSER_USER_AGENT == http_fetch.BROWSER_USER_AGENT


def test_build_input_items_carries_detail_text_into_the_payload(
    digest_fixture, monkeypatch
):
    monkeypatch.setattr(
        glm_mod, "fetch_detail_text",
        lambda url, **kwargs: f"상세 본문 {url} 지원금 300만원",
    )

    class Args:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = True
        apply_json = None

    assert glm_mod.run(Args()) == 0
    payload = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_input.json").read_text(encoding="utf-8")
    )
    for entry in payload["items"]:
        assert entry["detail_text"].startswith("상세 본문 https://")
        assert "300만원" in entry["detail_text"]


def test_persona_prompt_states_the_evidence_rule():
    assert "detail_text" in glm_mod.PERSONA_SYSTEM_PROMPT
    assert "추정 금지" in glm_mod.PERSONA_SYSTEM_PROMPT
    assert "원문 확인" in glm_mod.PERSONA_SYSTEM_PROMPT


# ─── B1. 한 줄 의미 근거 검증 (숫자 토큰 · 인용 전부) ──────────────────────
def test_gate_summary_replaces_a_fabricated_claim_with_no_quoted_span():
    """근거에서 오려 온 «인용» 이 하나도 없는 자유 문장은 통과할 수 없다."""
    items = [_one_input_item()]
    raw = json.dumps([{
        "n": 1, "대상 태그": "전체", "마감": "원문 확인",
        "자격": "원문 확인", "금액": "원문 확인",
        "한 줄 의미": "전 기업 1억원 지급 확정",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["한 줄 의미"] == glm_mod.FALLBACK
    assert any("인용 0개" in w for w in warnings)


def test_gate_summary_keeps_a_valid_extractive_line_from_detail_text():
    items = [_one_input_item(detail_text="사업비 1억원, 9.30 까지 접수합니다")]
    raw = json.dumps([{
        "n": 1, "대상 태그": "전체", "마감": "원문 확인",
        "자격": "원문 확인", "금액": "원문 확인",
        "한 줄 의미": "«사업비 1억원, 9.30 까지 접수합니다»",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["한 줄 의미"] == "«사업비 1억원, 9.30 까지 접수합니다»"
    assert warnings == []


def test_gate_summary_checks_every_quote_not_only_the_first():
    items = [_one_input_item()]
    raw = json.dumps([{
        "n": 1, "대상 태그": "전체", "마감": "원문 확인",
        "자격": "원문 확인", "금액": "원문 확인",
        "한 줄 의미": "«지원금 300만원» «날조»",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["한 줄 의미"] == glm_mod.FALLBACK
    assert any("인용 2개" in w for w in warnings)


def test_gate_quoted_field_checks_every_quote_not_only_the_first():
    items = [_one_input_item()]
    raw = json.dumps([{
        "n": 1, "대상 태그": "전체",
        "마감": "내일 «지원금 300만원» «날조»",
        "자격": "원문 확인", "금액": "원문 확인", "한 줄 의미": "신청 가능",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["마감"] == glm_mod.FALLBACK
    assert any("마감" in w and "인용" in w for w in warnings)


def test_number_tokens_keeps_the_unit_with_the_digits():
    assert glm_mod.number_tokens("9.30까지 300만원, 2026-09-30 마감 1억") == [
        "9.30", "300만원", "2026-09-30", "1억",
    ]


# ─── B2. 형식 게이트 (저장 전 · 필드 단위 대체) ───────────────────────────
@pytest.mark.parametrize("bad", [
    "[\ub9c1\ud06c](https://evil.test)",        # 마크다운 링크
    "**\uc804\uc561 \uc9c0\uc6d0**",                  # 강조
    "_\uac15\uc870_",                          # 밑줄 강조
    "<b>\uc2e0\uccad</b>",                     # 꺾쇠
    "\ud655\uc778\u0000\ud544\uc694",                    # NUL 제어문자
    "\ud655\uc778\u2028\ud544\uc694",                    # 줄 구분자
    "\ud655\uc778\u00a0\ud544\uc694",                    # NBSP
    "\ud655\uc778\u200b\ud544\uc694",                    # 제로폭 공백
])
def test_gate_summary_replaces_non_plaintext_before_saving(bad):
    items = [_one_input_item()]
    raw = json.dumps([{
        "n": 1, "대상 태그": "전체", "마감": "원문 확인",
        "자격": "원문 확인", "금액": "원문 확인", "한 줄 의미": bad,
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["한 줄 의미"] == glm_mod.FALLBACK
    assert any("한 줄 의미" in w for w in warnings)


def test_gate_summary_still_replaces_overlong_text_after_normalization():
    items = [_one_input_item()]
    raw = json.dumps([{
        "n": 1, "대상 태그": "전체", "마감": "원문 확인",
        "자격": "원문 확인", "금액": "원문 확인",
        "한 줄 의미": "확인\n" + "가" * 100,
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["한 줄 의미"] == glm_mod.FALLBACK
    assert any("초과" in w for w in warnings)


def test_format_violation_never_reaches_the_markdown(digest_fixture, tmp_path):
    """저장 **전에** 필드를 대체하므로 링크·제어문자가 본문에 남지 않는다."""
    markdown_path = digest_fixture["markdown_path"]

    class DryArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = True
        apply_json = None

    glm_mod.run(DryArgs())
    payload = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_input.json").read_text(encoding="utf-8")
    )
    fake = [
        {
            "n": entry["n"], "대상 태그": "전체", "마감": "원문 확인",
            "자격": "원문 확인", "금액": "원문 확인",
            "한 줄 의미": "[신청](https://evil.test)",
        }
        for entry in payload["items"]
    ]
    output_path = tmp_path / "bad_format.json"
    output_path.write_text(json.dumps(fake, ensure_ascii=False), encoding="utf-8")

    class ApplyArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = str(output_path)

    assert glm_mod.run(ApplyArgs()) == 0
    text = markdown_path.read_text(encoding="utf-8")
    assert "evil.test" not in text
    assert f"  → {glm_mod.FALLBACK}" in text
    result = check_digest(
        db_path=digest_fixture["db_path"], markdown_path=markdown_path,
        output_path=None, skip_network=False,
    )
    assert result["pass"], result.get("reason")


# ─── B3. n 집합 정확 일치 (부분 적용 금지) ────────────────────────────────
def test_gate_output_rejects_duplicate_n():
    items = [_one_input_item(n=1)]
    raw = json.dumps([
        {"n": 1, "대상 태그": "전체", "마감": "원문 확인", "자격": "원문 확인",
         "금액": "원문 확인", "한 줄 의미": "첫 값"},
        {"n": 1, "대상 태그": "전체", "마감": "원문 확인", "자격": "원문 확인",
         "금액": "원문 확인", "한 줄 의미": "덮어쓴 값"},
    ], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results == {}
    assert any("중복" in w and "폐기" in w for w in warnings)


def test_gate_output_rejects_boolean_n():
    items = [_one_input_item(n=1)]
    raw = json.dumps([{
        "n": True, "대상 태그": "전체", "마감": "원문 확인",
        "자격": "원문 확인", "금액": "원문 확인", "한 줄 의미": "신청 가능",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results == {}
    assert any("정수" in w and "폐기" in w for w in warnings)


def test_gate_output_rejects_unknown_n():
    items = [_one_input_item(n=1)]
    raw = json.dumps([
        {"n": 1, "대상 태그": "전체", "마감": "원문 확인", "자격": "원문 확인",
         "금액": "원문 확인", "한 줄 의미": "신청 가능"},
        {"n": 9, "대상 태그": "전체", "마감": "원문 확인", "자격": "원문 확인",
         "금액": "원문 확인", "한 줄 의미": "신청 가능"},
    ], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results == {}
    assert any("[9]" in w and "폐기" in w for w in warnings)


# ─── B4. 실패 재실행 잔존 ──────────────────────────────────────────────────
def _apply_fake_enrichment(digest_fixture, tmp_path, name="fake.json"):
    class DryArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = True
        apply_json = None

    glm_mod.run(DryArgs())
    payload = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_input.json").read_text(encoding="utf-8")
    )
    fake = [
        {
            "n": entry["n"], "대상 태그": "전체", "마감": "원문 확인",
            "자격": "원문 확인", "금액": "원문 확인",
            "한 줄 의미": _label(entry),
        }
        for entry in payload["items"]
    ]
    output_path = tmp_path / name
    output_path.write_text(json.dumps(fake, ensure_ascii=False), encoding="utf-8")

    class ApplyArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = str(output_path)

    assert glm_mod.run(ApplyArgs()) == 0
    return payload


def _enrich_line_count(markdown_path) -> int:
    return len([
        block for block in blocks_mod.item_blocks(
            markdown_path.read_text(encoding="utf-8"))
        if block.get("enrich_line")
    ])


def test_failed_rerun_after_apply_leaves_zero_enrich_lines(digest_fixture, tmp_path):
    """적용 → 실패 재실행 → 보강 0건 (md·정본 둘 다)."""
    markdown_path = digest_fixture["markdown_path"]
    _apply_fake_enrichment(digest_fixture, tmp_path)
    assert _enrich_line_count(markdown_path) == 2

    broken = tmp_path / "broken.json"
    broken.write_text("이것은 JSON 이 아닙니다", encoding="utf-8")

    class BrokenArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = str(broken)

    assert glm_mod.run(BrokenArgs()) == 0

    text = markdown_path.read_text(encoding="utf-8")
    assert _enrich_line_count(markdown_path) == 0
    assert "  → " not in text
    assert all(
        (entry.get("enrich_line") or "") == ""
        for entry in _manifest_items(markdown_path)
    )
    result = check_digest(
        db_path=digest_fixture["db_path"], markdown_path=markdown_path,
        output_path=None, skip_network=False,
    )
    assert result["pass"], result.get("reason")
    assert result["manifest_problems"] == []


def test_failed_rerun_with_missing_ds_binary_also_clears(digest_fixture, tmp_path,
                                                        monkeypatch):
    markdown_path = digest_fixture["markdown_path"]
    _apply_fake_enrichment(digest_fixture, tmp_path)
    assert _enrich_line_count(markdown_path) == 2

    monkeypatch.setattr(glm_mod, "DS_BIN", tmp_path / "no-such-ds")

    class NoDsArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = None

    assert glm_mod.run(NoDsArgs()) == 0
    assert _enrich_line_count(markdown_path) == 0


def test_failed_rerun_drops_the_stale_headline_draft_and_warnings(
    digest_fixture, tmp_path
):
    markdown_path = digest_fixture["markdown_path"]
    _apply_fake_enrichment(digest_fixture, tmp_path)
    text, changed = glm_mod.apply_headline_draft(
        markdown_path.read_text(encoding="utf-8"), "지난 주 초안"
    )
    assert changed
    markdown_path.write_text(text, encoding="utf-8")
    warn_path = digest_fixture["out_dir"] / f"{W13}.glm_warnings.json"
    warn_path.write_text('{"warnings": ["지난 실행 경고"]}\n', encoding="utf-8")

    broken = tmp_path / "broken2.json"
    broken.write_text("[", encoding="utf-8")

    class BrokenArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = str(broken)

    assert glm_mod.run(BrokenArgs()) == 0
    assert glm_mod.GLM_DRAFT_PREFIX not in markdown_path.read_text(encoding="utf-8")
    # 지난 실행의 경고도 남지 않는다 — 이번 실행이 낸 경고만 있다
    warnings_now = json.loads(warn_path.read_text(encoding="utf-8"))["warnings"]
    assert "지난 실행 경고" not in warnings_now
    assert any("파싱 실패" in w for w in warnings_now)


def test_dry_run_does_not_clear_existing_enrichment(digest_fixture, tmp_path):
    """`--dry-run` 은 본문을 건드리지 않는다 — 제거는 실제 적용 경로에서만."""
    markdown_path = digest_fixture["markdown_path"]
    _apply_fake_enrichment(digest_fixture, tmp_path)
    before = markdown_path.read_text(encoding="utf-8")

    class DryArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = True
        apply_json = None

    assert glm_mod.run(DryArgs()) == 0
    assert markdown_path.read_text(encoding="utf-8") == before


# ─── B5. 카톡 렌더 ────────────────────────────────────────────────────────
def test_kakao_render_carries_the_enrich_line_with_the_item(digest_fixture, tmp_path):
    markdown_path = digest_fixture["markdown_path"]
    payload = _apply_fake_enrichment(digest_fixture, tmp_path)
    text = markdown_path.read_text(encoding="utf-8")

    blocks = composer_mod.kakao_blocks_from_markdown(text)
    item_chunks = [b for b in blocks if "http" in b and "\n" in b]
    assert item_chunks
    for chunk in item_chunks:
        lines = chunk.split("\n")
        assert len(lines) == 3
        assert lines[2].strip().startswith("→")
    assert _label(payload["items"][0]) in composer_mod.kakao_file_text_from_markdown(text)
    assert composer_mod.markdown_kakao_problems(text) == []


def test_markdown_kakao_problems_flags_a_dropped_enrich_line(
    digest_fixture, tmp_path, monkeypatch
):
    markdown_path = digest_fixture["markdown_path"]
    _apply_fake_enrichment(digest_fixture, tmp_path)
    text = markdown_path.read_text(encoding="utf-8")
    original = composer_mod.kakao_blocks_from_markdown

    def drop_enrich(markdown_text):
        return [
            "\n".join(
                line for line in chunk.split("\n")
                if not line.strip().startswith("→")
            )
            for chunk in original(markdown_text)
        ]

    monkeypatch.setattr(composer_mod, "kakao_blocks_from_markdown", drop_enrich)
    problems = composer_mod.markdown_kakao_problems(text)
    assert any("보강 줄 누락" in p for p in problems)


# ─── B6. 헤드라인 초안 격리 ────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [
    "-->허위 문장<!--",
    "신청 2건 -- 마감 임박",
    "<b>신청</b> 2건",
    "신청 2건 > 마감",
])
def test_gate_headline_draft_rejects_comment_escapes(bad):
    draft, ok = glm_mod.gate_headline_draft(bad)
    assert ok is False
    assert draft == ""


def test_gate_headline_draft_accepts_plain_sentence():
    draft, ok = glm_mod.gate_headline_draft('  "신청 2건, 가장 빠른 마감 9/22."  ')
    assert ok is True
    assert draft == "신청 2건, 가장 빠른 마감 9/22."


def test_gate_headline_draft_rejects_overlong_text():
    draft, ok = glm_mod.gate_headline_draft("가" * (glm_mod._MAX_HEADLINE_CHARS + 1))
    assert ok is False
    assert draft == ""


# ─── B7. 경고 노출 (check.json → 미리보기 상단) ───────────────────────────
def test_check_digest_counts_glm_warnings(digest_fixture):
    markdown_path = digest_fixture["markdown_path"]
    (digest_fixture["out_dir"] / f"{W13}.glm_warnings.json").write_text(
        json.dumps({"warnings": ["n=1 한 줄 의미 근거 없는 1억",
                                 "n=2 마감 인용 검증 실패"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = check_digest(
        db_path=digest_fixture["db_path"], markdown_path=markdown_path,
        output_path=None, skip_network=False,
    )
    assert result["glm_warnings"] == 2
    # 표시만 한다 — 발송을 막지 않는다
    assert result["pass"], result.get("reason")


def test_preview_shows_glm_warning_count_and_enrich_line(digest_fixture, tmp_path):
    markdown_path = digest_fixture["markdown_path"]
    payload = _apply_fake_enrichment(digest_fixture, tmp_path)
    (digest_fixture["out_dir"] / f"{W13}.glm_warnings.json").write_text(
        json.dumps({"warnings": ["n=1 한 줄 의미 근거 없는 1억"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    check = check_digest(
        db_path=digest_fixture["db_path"], markdown_path=markdown_path,
        output_path=None, skip_network=False,
    )
    preview = preview_mod.render_preview(
        W13, markdown_path.read_text(encoding="utf-8"), check
    )
    assert "GLM 보강 경고 1건" in preview
    assert _label(payload["items"][0]) in preview


def test_preview_has_no_glm_warning_line_when_there_are_none(digest_fixture):
    check = check_digest(
        db_path=digest_fixture["db_path"],
        markdown_path=digest_fixture["markdown_path"],
        output_path=None, skip_network=False,
    )
    preview = preview_mod.render_preview(
        W13, digest_fixture["markdown_path"].read_text(encoding="utf-8"), check
    )
    assert "GLM 보강 경고" not in preview


# ─── B8. recheck 가 정본 해시를 갱신하는가 (확인만) ────────────────────────
def test_recheck_refreshes_the_manifest_hash_after_enrichment(digest_fixture, tmp_path):
    """`recheck_digest.recheck()` 의 첫 동작이 `refresh_manifest_binding` 이다."""
    import inspect

    from scripts import recheck_digest

    source = inspect.getsource(recheck_digest.recheck)
    assert "refresh_manifest_binding(markdown_path)" in source

    markdown_path = digest_fixture["markdown_path"]
    _apply_fake_enrichment(digest_fixture, tmp_path)
    text, changed = glm_mod.apply_headline_draft(
        markdown_path.read_text(encoding="utf-8"), "초안 문장"
    )
    assert changed
    markdown_path.write_text(text, encoding="utf-8")
    stale = load_items_manifest(markdown_path)["markdown_sha256"]

    composer_mod.refresh_manifest_binding(markdown_path)
    fresh = load_items_manifest(markdown_path)["markdown_sha256"]
    assert fresh != stale
    result = check_digest(
        db_path=digest_fixture["db_path"], markdown_path=markdown_path,
        output_path=None, skip_network=False,
    )
    assert result["manifest_problems"] == []


# ─── V3.1a. 창을 기사 본문에 앵커한다 (제목 마지막 출현 기준) ──────────────
_NAV = "공지사항 주메뉴 바로가기 본문 바로가기 로그인 회원가입 정보공개 "
_TITLE = "2026 산림분야 오픈이노베이션 참여기업 모집"
_BODY = " 접수기간 9월 22일까지, 지원금 300만원 규모로 임업 기업을 모집합니다."


def test_anchored_window_starts_at_the_title_when_found_once():
    text = _NAV + _TITLE + _BODY
    window = http_fetch.anchored_window(text, _TITLE, limit=50)
    assert window.startswith(_TITLE)
    assert "주메뉴 바로가기" not in window


def test_anchored_window_starts_at_the_last_occurrence_of_the_title():
    """목록·빵부스러기에도 제목이 박혀 있다 — 본문 제목은 대개 **마지막**이다."""
    text = _NAV + _TITLE + " 목록으로 " + _NAV + _TITLE + _BODY
    window = http_fetch.anchored_window(text, _TITLE, limit=200)
    assert window.startswith(_TITLE + _BODY)
    assert window.count(_TITLE) == 1


def test_anchored_window_falls_back_to_the_start_when_the_title_is_absent():
    text = _NAV + "전혀 다른 기사 본문입니다."
    window = http_fetch.anchored_window(text, "레포에 없는 제목 문자열", limit=30)
    assert window == text[:30]


def test_anchored_window_falls_back_to_the_first_20_chars_of_the_title():
    """목록 제목이 말줄임·괄호 정리로 정본과 달라도 앞 20자로 붙잡는다."""
    text = _NAV + _TITLE + _BODY
    manifest_title = _TITLE + " (2차 연장, ~9.30)"
    assert manifest_title not in text
    window = http_fetch.anchored_window(text, manifest_title, limit=200)
    assert window.startswith(_TITLE[:http_fetch.ANCHOR_PARTIAL_CHARS])
    assert "주메뉴 바로가기" not in window


def test_anchored_window_normalizes_whitespace_on_both_sides():
    text = http_fetch.normalize_space(_NAV + "공고\n제목   여기" + _BODY)
    window = http_fetch.anchored_window(text, "  공고\t제목\n여기  ", limit=40)
    assert window.startswith("공고 제목 여기")


def test_anchored_window_with_no_anchor_keeps_the_old_start_window():
    text = _NAV + _TITLE + _BODY
    assert http_fetch.anchored_window(text, "", limit=25) == text[:25]


def test_fetch_detail_text_anchors_the_window_at_the_article_title(monkeypatch):
    html_body = (
        "<html><body><ul><li>" + _TITLE + "</li></ul>"
        "<div class='view'><h2>" + _TITLE + "</h2><p>" + _BODY + "</p></div>"
        "</body></html>"
    )
    _use_open(monkeypatch, _html_response(html_body))
    text = http_fetch.fetch_detail_text("https://example.test/a", anchor=_TITLE)
    assert text.startswith(_TITLE)
    assert text.count(_TITLE) == 1
    assert "접수기간 9월 22일까지" in text


def test_build_input_items_passes_the_item_title_as_the_anchor(
    digest_fixture, monkeypatch
):
    seen = {}

    def _stub(url, anchor="", **kwargs):
        seen[url] = anchor
        return f"{anchor} 본문"

    monkeypatch.setattr(glm_mod, "fetch_detail_text", _stub)

    class Args:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = True
        apply_json = None

    assert glm_mod.run(Args()) == 0
    payload = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_input.json").read_text(encoding="utf-8")
    )
    assert seen
    for entry in payload["items"]:
        assert seen[entry["url"]] == entry["title"]
        assert entry["detail_text"] == f"{entry['title']} 본문"


# ══ V3.1 r2 (Codex 실검토 v3.1 수렴) ══════════════════════════════════════
# 1. 추출형 `한 줄 의미` — 토큰 경계·문법·정규화
# ─────────────────────────────────────────────────────────────────────────
def test_gate_summary_rejects_a_span_cut_from_the_middle_of_a_token():
    """근거 `사업비 11억원` 에서 `1억원` 을 오려 새 금액을 만들 수 없다."""
    items = [_one_input_item(summary="사업비 11억원 규모입니다")]
    raw = json.dumps([{
        "n": 1, "대상 태그": "전체", "마감": "원문 확인",
        "자격": "원문 확인", "금액": "원문 확인",
        "한 줄 의미": "«1억원 규모입니다»",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["한 줄 의미"] == glm_mod.FALLBACK
    assert any("경계로 없는 인용" in w for w in warnings)


def test_gate_summary_keeps_a_span_that_crosses_a_newline_in_the_evidence():
    """근거의 개행을 가로지르는 정상 인용은 오거절되지 않는다 (양쪽 정규화)."""
    items = [_one_input_item(summary="접수기간\n9월 22일까지\n신청")]
    raw = json.dumps([{
        "n": 1, "대상 태그": "전체", "마감": "원문 확인",
        "자격": "원문 확인", "금액": "원문 확인",
        "한 줄 의미": "«접수기간 9월 22일까지»",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["한 줄 의미"] == "«접수기간 9월 22일까지»"
    assert warnings == []


def test_gate_summary_rejects_any_character_outside_the_span():
    """r3b: 구절 밖에는 조사 한 글자도 올 수 없다."""
    items = [_one_input_item()]
    raw = json.dumps([{
        "n": 1, "대상 태그": "전체", "마감": "원문 확인",
        "자격": "원문 확인", "금액": "원문 확인",
        "한 줄 의미": "«9월 22일까지 접수» 무조건 신청",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["한 줄 의미"] == glm_mod.FALLBACK
    assert any("인용 밖 글자" in w and "무조건" in w for w in warnings)


def test_gate_summary_grammar_requires_exactly_one_span_of_bounded_length():
    evidence = glm_mod.summary_normalize("가나다라마바사아 " + "바" * 90)
    two_spans = "«가나다라마바사아» «가나다라마바사아»"
    assert "인용 2개" in (glm_mod.gate_summary_grammar(two_spans, evidence) or "")
    assert "인용 0개" in (glm_mod.gate_summary_grammar("상시 접수", evidence) or "")
    assert "3자" in (glm_mod.gate_summary_grammar("«가나다»", evidence) or "")
    long_span = "«" + "바" * 90 + "»"
    assert "90자" in (glm_mod.gate_summary_grammar(long_span, evidence) or "")


def test_gate_summary_keeps_the_plain_fallback_string():
    items = [_one_input_item()]
    raw = json.dumps([{
        "n": 1, "대상 태그": "전체", "마감": "원문 확인",
        "자격": "원문 확인", "금액": "원문 확인", "한 줄 의미": "원문 확인",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["한 줄 의미"] == glm_mod.FALLBACK
    assert warnings == []


def test_persona_prompt_teaches_the_single_span_grammar():
    prompt = glm_mod.PERSONA_SYSTEM_PROMPT
    assert "한 구절(8~80자) 하나" in prompt
    assert "두 구절 금지" in prompt and "구절 밖 글자 금지" in prompt
    assert prompt.count("예1:") == 1 and prompt.count("예2:") == 1
    assert "낱말 경계" in prompt


# ─── 2. 형식 게이트 — 맨몸 URL · 양방향 제어문자 ──────────────────────────
@pytest.mark.parametrize("bad", [
    "https://evil.test \uc2e0\uccad",            # \ub9e8\ubab8 URL
    "\uc2e0\uccad www.evil.test/a http://x.y",   # \uc2a4\ud0b4 \uc788\ub294 \ub9e8\ubab8 URL
    "\uc2e0\uccad\u202e\ud655\uc778",                       # RIGHT-TO-LEFT OVERRIDE
    "\uc2e0\uccad\u2068\ud655\uc778",                       # FIRST STRONG ISOLATE
    "\uc2e0\uccad\u202a\ud655\uc778",                       # LEFT-TO-RIGHT EMBEDDING
])
def test_format_problem_rejects_bare_urls_and_bidi_controls(bad):
    assert glm_mod.format_problem(bad) is not None


def test_format_problem_accepts_a_plain_extractive_line():
    assert glm_mod.format_problem("«9월 22일까지» 접수") is None


def test_gate_headline_draft_rejects_a_bare_url_and_bidi_control():
    assert glm_mod.gate_headline_draft("신청 2건 https://evil.test") == ("", False)
    assert glm_mod.gate_headline_draft("\uc2e0\uccad\u202e2\uac74") == ("", False)


# ─── 3. 제거는 외부 I/O **앞**에서 일어난다 ───────────────────────────────
def test_clear_runs_before_the_db_query_and_http_collection(
    digest_fixture, tmp_path, monkeypatch
):
    """DB 조회가 터져도 지난 보강은 이미 지워져 있다 (수집 중 종료도 같다)."""
    markdown_path = digest_fixture["markdown_path"]
    _apply_fake_enrichment(digest_fixture, tmp_path)
    assert _enrich_line_count(markdown_path) == 2

    def boom(*args, **kwargs):
        raise RuntimeError("DB 조회 실패")

    monkeypatch.setattr(glm_mod, "fetch_summary_raw", boom)
    monkeypatch.setattr(
        glm_mod, "fetch_detail_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("수집 실패")),
    )

    class Args:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = None

    with pytest.raises(RuntimeError):
        glm_mod.run(Args())

    assert _enrich_line_count(markdown_path) == 0
    assert all(
        (entry.get("enrich_line") or "") == ""
        for entry in _manifest_items(markdown_path)
    )


# ─── 4. 원자성 — 정본에만 남은 보강을 다음 실행이 치유한다 ────────────────
def test_clear_heals_a_manifest_that_kept_enrich_lines_after_a_crash(
    digest_fixture, tmp_path, monkeypatch
):
    """md 를 쓴 뒤 정본을 쓰기 전에 죽은 상태 → 다음 실행이 둘 다 비운다."""
    import hashlib

    markdown_path = digest_fixture["markdown_path"]
    _apply_fake_enrichment(digest_fixture, tmp_path)

    # 중단 창 재현: md 의 보강 줄만 지우고 정본은 그대로 둔다.
    text = markdown_path.read_text(encoding="utf-8")
    present = [
        str(block["item_id"])
        for block in blocks_mod.item_blocks(text)
        if block.get("enrich_line")
    ]
    stripped, changed = blocks_mod.set_enrich_lines(
        text, {item_id: None for item_id in present}
    )
    assert changed
    markdown_path.write_text(stripped, encoding="utf-8")
    assert any(
        (entry.get("enrich_line") or "") for entry in _manifest_items(markdown_path)
    )

    monkeypatch.setattr(glm_mod, "DS_BIN", tmp_path / "no-such-ds")

    class NoDsArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = None

    assert glm_mod.run(NoDsArgs()) == 0

    manifest = load_items_manifest(markdown_path)
    assert all((entry.get("enrich_line") or "") == "" for entry in manifest["items"])
    assert manifest["markdown_sha256"] == hashlib.sha256(
        markdown_path.read_bytes()
    ).hexdigest()
    result = check_digest(
        db_path=digest_fixture["db_path"], markdown_path=markdown_path,
        output_path=None, skip_network=False,
    )
    assert result["manifest_problems"] == []
    assert result["pass"], result.get("reason")


def test_write_items_manifest_is_atomic_and_leaves_no_temp_file(digest_fixture):
    markdown_path = digest_fixture["markdown_path"]
    manifest = load_items_manifest(markdown_path)
    composer_mod.write_items_manifest(markdown_path, manifest)
    json_path = composer_mod.items_json_path(markdown_path)
    assert json_path.exists()
    assert not json_path.with_name(json_path.name + ".tmp").exists()


# ─── 5. HTTP — 벽시계 데드라인 · 잡 예산 · 수동 리다이렉트 · Content-Type ──
class _Clock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_fetch_detail_text_aborts_on_the_item_wall_clock_deadline(monkeypatch):
    """9초마다 조금씩 보내는 서버가 read timeout 을 영원히 리셋하지 못한다."""
    clock = _Clock()

    class _Slow(_FakeResponse):
        def stream(self, amt=None, decode_content=True):
            def generate():
                while True:
                    clock.now += 4.0
                    yield b"<p>a</p>"
            return generate()

    _use_open(monkeypatch, _Slow(headers={"content-type": "text/html"}))
    assert http_fetch.fetch_detail_text(
        "https://example.test/a", clock=clock
    ) == ""
    assert clock.now - 1000.0 <= http_fetch.DETAIL_TIMEOUT + 4.0


def test_fetch_detail_text_cuts_a_server_that_never_sends_a_first_chunk(monkeypatch):
    """첫 청크 전에는 청크 루프가 돌지 않는다 — 워커 스레드를 벽시계로 자른다."""
    import threading

    blocked = threading.Event()

    def never_returns(url):
        blocked.wait(30)
        raise AssertionError("도달하면 안 된다")

    monkeypatch.setattr(http_fetch, "_open", never_returns)
    started = time.monotonic()
    assert http_fetch.fetch_detail_text("https://example.test/a", timeout=0.2) == ""
    elapsed = time.monotonic() - started
    blocked.set()
    assert elapsed < 3.0


def test_fetch_detail_text_skips_items_once_the_job_budget_is_spent(monkeypatch):
    clock = _Clock()
    calls = _use_open(monkeypatch, _html_response("<p>본문</p>"))
    budget = http_fetch.FetchBudget(seconds=5, clock=clock)

    assert http_fetch.fetch_detail_text(
        "https://example.test/a", budget=budget, clock=clock
    ) == "본문"

    clock.now += 10  # 예산 소진
    assert budget.exhausted()
    before = len(calls)
    assert http_fetch.fetch_detail_text(
        "https://example.test/b", budget=budget, clock=clock
    ) == ""
    assert len(calls) == before  # 요청조차 하지 않는다


def test_fetch_detail_text_follows_redirects_manually_up_to_the_limit(monkeypatch):
    calls = _use_open(
        monkeypatch,
        _redirect_response("/b"), _redirect_response("/c"),
        _html_response("<p>도착</p>"),
    )
    assert http_fetch.fetch_detail_text("https://example.test/a") == "도착"
    assert calls == [
        "https://example.test/a", "https://example.test/b", "https://example.test/c",
    ]


def test_fetch_detail_text_gives_up_after_too_many_redirects(monkeypatch):
    calls = _use_open(
        monkeypatch, *[_redirect_response(f"/{n}") for n in range(6)]
    )
    assert http_fetch.fetch_detail_text("https://example.test/a") == ""
    assert len(calls) == http_fetch.MAX_REDIRECTS + 1


def test_fetch_detail_text_refuses_a_non_http_redirect_target(monkeypatch):
    _use_open(monkeypatch, _redirect_response("ftp://example.test/a"))
    assert http_fetch.fetch_detail_text("https://example.test/a") == ""


def test_a_redirect_without_location_is_a_failure_not_a_retry(monkeypatch):
    """Location 없는 3xx 로 같은 URL 을 다시 부르지 않는다 (Codex r3)."""
    calls = _use_open(monkeypatch, _redirect_response(None))
    assert http_fetch.fetch_detail_text("https://example.test/a") == ""
    assert calls == ["https://example.test/a"]


def test_a_multiple_choices_response_is_not_accepted_as_evidence(monkeypatch):
    _use_open(monkeypatch, _FakeResponse(
        status=300, headers={"content-type": "text/html"},
        chunks=[b"<p>\xea\xb3\xa8\xeb\x9d\xbc</p>"],
    ))
    assert http_fetch.fetch_detail_text("https://example.test/a") == ""


@pytest.mark.parametrize("headers", [
    {},                                    # Content-Type 누락
    {"content-type": "text/plain"},        # 평문
    {"content-type": "application/json"},
])
def test_fetch_detail_text_requires_an_html_content_type(monkeypatch, headers):
    _use_open(monkeypatch, _FakeResponse(headers=headers, chunks=[b"<p>x</p>"]))
    assert http_fetch.fetch_detail_text("https://example.test/a") == ""


# ─── 6. 본문 선택 — 파서 기반 ─────────────────────────────────────────────
def test_visible_text_reaches_the_article_behind_a_huge_navigation_block():
    """메뉴가 2,000자를 넘어도 `<article>` 본문이 창에 들어온다."""
    nav = "<div id='gnb'>" + ("메뉴 바로가기 " * 400) + "</div>"
    assert len(nav) > 2000
    html_text = (
        "<html><body>" + nav
        + "<article><h2>공고 제목</h2><p>접수기간 9월 22일까지</p></article>"
        + "</body></html>"
    )
    text = http_fetch.visible_text(html_text)
    assert text.startswith("공고 제목")
    assert "메뉴" not in text


def test_visible_text_prefers_main_then_the_largest_content_container():
    main_html = (
        "<body><div id='gnb'>메뉴</div>"
        "<main><p>메인 본문</p></main></body>"
    )
    assert http_fetch.visible_text(main_html) == "메인 본문"

    container_html = (
        "<body><div class='side'>짧음</div>"
        "<div class='board-view'><p>게시판 본문 " + ("가" * 50) + "</p></div>"
        "<div id='contents'>짧은 본문</div></body>"
    )
    assert http_fetch.visible_text(container_html).startswith("게시판 본문")


def test_visible_text_drops_hidden_elements_and_unclosed_scripts():
    html_text = (
        "<body><div hidden>숨김1</div>"
        "<div aria-hidden='true'>숨김2</div>"
        "<div style='display:none'>숨김3</div>"
        "<p>보이는 본문</p>"
        "<script>var leak = '유출';"
    )
    text = http_fetch.visible_text(html_text)
    assert "보이는 본문" in text
    for hidden in ("숨김1", "숨김2", "숨김3", "유출"):
        assert hidden not in text


def test_visible_text_keeps_inline_markup_from_splitting_tokens():
    assert http_fetch.visible_text("<body><p>9월 <b>22</b>일까지</p></body>") == (
        "9월 22일까지"
    )


def test_visible_text_is_linear_on_pathological_input():
    import time as _time

    start = _time.monotonic()
    http_fetch.visible_text("<" * 32000)
    assert _time.monotonic() - start < 1.0


def test_decode_prefers_bom_then_meta_charset_over_a_wrong_header():
    body = "<html><head><meta charset='utf-8'></head><body><p>한글</p></body></html>"
    raw = body.encode("utf-8")
    # 헤더는 iso-8859-1 이라고 우기지만 meta 가 이긴다
    assert "한글" in http_fetch._decode(raw, "text/html; charset=iso-8859-1")
    assert "한글" in http_fetch._decode(
        codecs.BOM_UTF8 + raw, "text/html; charset=iso-8859-1"
    )


# ─── 7. 카톡 — 출현별 대조 · 보강 줄만 떼기 ───────────────────────────────
def _apply_fallback_enrichment(digest_fixture, tmp_path):
    """두 항목 모두 `→ 원문 확인` 으로 만든다 (같은 문자열 2개)."""
    class DryArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = True
        apply_json = None

    glm_mod.run(DryArgs())
    payload = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_input.json").read_text(encoding="utf-8")
    )
    fake = [
        {
            "n": entry["n"], "대상 태그": "전체", "마감": "원문 확인",
            "자격": "원문 확인", "금액": "원문 확인", "한 줄 의미": "원문 확인",
        }
        for entry in payload["items"]
    ]
    path = tmp_path / "fallback.json"
    path.write_text(json.dumps(fake, ensure_ascii=False), encoding="utf-8")

    class ApplyArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = str(path)

    assert glm_mod.run(ApplyArgs()) == 0
    return payload


def test_markdown_kakao_problems_counts_identical_enrich_lines_per_item(
    digest_fixture, tmp_path, monkeypatch
):
    """두 항목이 같은 `→ 원문 확인` 이면 하나만 사라져도 잡아야 한다."""
    markdown_path = digest_fixture["markdown_path"]
    _apply_fallback_enrichment(digest_fixture, tmp_path)
    text = markdown_path.read_text(encoding="utf-8")
    assert text.count("  → 원문 확인") == 2
    assert composer_mod.markdown_kakao_problems(text) == []

    original = composer_mod.kakao_blocks_from_markdown

    def drop_one(markdown_text):
        blocks = original(markdown_text)
        dropped = False
        out = []
        for block in blocks:
            lines = block.split("\n")
            if not dropped and len(lines) == 3 and lines[2].strip().startswith("→"):
                out.append("\n".join(lines[:2]))
                dropped = True
                continue
            out.append(block)
        assert dropped
        return out

    monkeypatch.setattr(composer_mod, "kakao_blocks_from_markdown", drop_one)
    problems = composer_mod.markdown_kakao_problems(text)
    assert any("보강 줄 누락(2→1)" in problem for problem in problems)


def test_pack_blocks_drops_only_the_enrich_line_when_a_block_overflows():
    """보강 줄 때문에 넘치면 **보강 줄만** 뗀다 — 원문 URL 은 살린다."""
    url = "https://e.test/" + "x" * 17
    block = f"제목\n  {url}\n  → 원문 확인"
    limit = 40
    assert len(block) > limit

    chunks = composer_mod._pack_blocks([block], limit)

    joined = "\n".join(chunks)
    assert url in joined
    assert "→ 원문 확인" not in joined
    assert composer_mod.URL_TOO_LONG_NOTICE not in joined


def test_url_too_long_block_keeps_the_enrich_line():
    block = "제목\n  https://e.test/" + "x" * 200 + "\n  → 원문 확인"
    replaced = composer_mod._url_too_long_block(block, 60)
    assert composer_mod.URL_TOO_LONG_NOTICE in replaced
    assert replaced.endswith("  → 원문 확인")


# ─── 8. 미리보기 문구 · 초안 경고의 착지 ──────────────────────────────────
def test_preview_says_discarded_when_the_whole_output_was_thrown_away(
    digest_fixture, tmp_path
):
    markdown_path = digest_fixture["markdown_path"]
    broken = tmp_path / "broken.json"
    broken.write_text("not json", encoding="utf-8")

    class BrokenArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = str(broken)

    assert glm_mod.run(BrokenArgs()) == 0
    warnings_file = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_warnings.json").read_text(
            encoding="utf-8")
    )
    assert warnings_file["discarded"] is True

    check = check_digest(
        db_path=digest_fixture["db_path"], markdown_path=markdown_path,
        output_path=None, skip_network=False,
    )
    assert check["glm_discarded"] is True
    preview = preview_mod.render_preview(
        W13, markdown_path.read_text(encoding="utf-8"), check
    )
    assert "GLM 출력 전체 폐기" in preview
    assert "원문 확인'으로 대체" not in preview


def test_preview_says_replaced_when_only_some_fields_were_gated(
    digest_fixture, tmp_path
):
    markdown_path = digest_fixture["markdown_path"]

    class DryArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = True
        apply_json = None

    glm_mod.run(DryArgs())
    payload = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_input.json").read_text(encoding="utf-8")
    )
    fake = [
        {
            "n": entry["n"], "대상 태그": "전체", "마감": "원문 확인",
            "자격": "원문 확인", "금액": "원문 확인",
            "한 줄 의미": "전액 지원 확정",   # 문법 위반 → 필드만 대체
        }
        for entry in payload["items"]
    ]
    path = tmp_path / "gated.json"
    path.write_text(json.dumps(fake, ensure_ascii=False), encoding="utf-8")

    class ApplyArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = str(path)

    assert glm_mod.run(ApplyArgs()) == 0
    check = check_digest(
        db_path=digest_fixture["db_path"], markdown_path=markdown_path,
        output_path=None, skip_network=False,
    )
    assert check["glm_discarded"] is False
    assert check["glm_warnings"] == 2
    preview = preview_mod.render_preview(
        W13, markdown_path.read_text(encoding="utf-8"), check
    )
    assert "원문 확인'으로 대체" in preview
    assert "GLM 출력 전체 폐기" not in preview


def test_a_rejected_headline_draft_is_recorded_in_the_warnings_file(
    digest_fixture, tmp_path, monkeypatch
):
    """초안 형식 위반이 콘솔에만 남지 않는다 — 미리보기까지 흐른다."""
    fake_ds = tmp_path / "ds"
    fake_ds.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(glm_mod, "DS_BIN", fake_ds)

    class DryArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = True
        apply_json = None

    glm_mod.run(DryArgs())
    payload = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_input.json").read_text(encoding="utf-8")
    )
    summary_output = json.dumps([
        {
            "n": entry["n"], "대상 태그": "전체", "마감": "원문 확인",
            "자격": "원문 확인", "금액": "원문 확인",
            "한 줄 의미": _label(entry),
        }
        for entry in payload["items"]
    ], ensure_ascii=False)
    calls = {"n": 0}

    def fake_ds_call(prompt, timeout=None):
        calls["n"] += 1
        return summary_output if calls["n"] == 1 else "-->허위 문장<!--"

    monkeypatch.setattr(glm_mod, "call_ds_glm", fake_ds_call)

    class Args:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = None

    assert glm_mod.run(Args()) == 0
    text = digest_fixture["markdown_path"].read_text(encoding="utf-8")
    assert glm_mod.GLM_DRAFT_PREFIX not in text
    warnings_file = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_warnings.json").read_text(
            encoding="utf-8")
    )
    assert any("초안" in w for w in warnings_file["warnings"])
    assert warnings_file["discarded"] is False


# ══ V3.1 r3 — ds 프롬프트 상한(16,384바이트) 때문에 배치로 나눈다 ═════════
def _big_item(n, chars, id_=None):
    return {
        "n": n, "id": id_ if id_ is not None else 40 + n,
        "title": f"산림 지원사업 참여기업 모집 공고 {n}", "source_name": "기관",
        "summary": "", "detail_text": "가" * chars,
        "quote_deadline": "", "quote_eligibility": "원문 확인",
        "quote_amount": "원문 확인", "url": f"https://example.com/{n}",
    }


def test_batch_input_items_keeps_every_prompt_under_the_byte_budget():
    today = "2026-09-13"
    items = [_big_item(n, 2500) for n in range(1, 6)]

    batches = glm_mod.batch_input_items(today, items)

    assert len(batches) > 1
    for batch in batches:
        assert glm_mod.prompt_bytes(today, batch) <= glm_mod.PROMPT_BYTE_BUDGET
    # 모든 항목이 정확히 한 번씩, 문서 순서대로
    assert [item["n"] for batch in batches for item in batch] == [1, 2, 3, 4, 5]


def test_batch_input_items_trims_a_single_oversized_item_instead_of_dropping_it():
    today = "2026-09-13"
    huge = _big_item(1, 20000)

    batches = glm_mod.batch_input_items(today, [huge])

    assert len(batches) == 1 and len(batches[0]) == 1
    trimmed = batches[0][0]
    assert trimmed["n"] == 1                      # 항목이 사라지지 않았다
    assert 0 < len(trimmed["detail_text"]) < 20000  # 뒤에서 잘렸다
    assert huge["detail_text"].startswith(trimmed["detail_text"])
    assert glm_mod.prompt_bytes(today, [trimmed]) <= glm_mod.PROMPT_BYTE_BUDGET


def test_trim_item_to_budget_leaves_a_small_item_untouched():
    today = "2026-09-13"
    small = _big_item(1, 10)
    assert glm_mod.trim_item_to_budget(today, small) == small


def _stub_ds_calls(monkeypatch, responder):
    """`call_ds_glm` 을 갈아끼우고 (프롬프트, 바이트수) 를 기록한다."""
    seen = []

    def fake(prompt, timeout=None):
        seen.append((prompt, len(prompt.encode("utf-8"))))
        return responder(prompt, len(seen))

    monkeypatch.setattr(glm_mod, "call_ds_glm", fake)
    return seen


def _ds_present(monkeypatch, tmp_path):
    fake_ds = tmp_path / "ds"
    fake_ds.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(glm_mod, "DS_BIN", fake_ds)


def _batch_ns(prompt):
    payload = json.loads(prompt.split("\n\n입력\n", 1)[1])
    return [item["n"] for item in payload["items"]]


def _extractive_for(prompt):
    payload = json.loads(prompt.split("\n\n입력\n", 1)[1])
    return json.dumps([
        {
            "n": item["n"], "대상 태그": "전체", "마감": "원문 확인",
            "자격": "원문 확인", "금액": "원문 확인",
            "한 줄 의미": "«{}»".format(item["title"]),
        }
        for item in payload["items"]
    ], ensure_ascii=False)


def test_run_splits_into_batches_and_each_prompt_fits_the_cap(
    digest_fixture, tmp_path, monkeypatch
):
    """W37 실패 재현: detail_text 를 실으면 한 프롬프트에 다 들어가지 않는다."""
    monkeypatch.setattr(
        glm_mod, "fetch_detail_text", lambda url, **kwargs: "가" * 3000
    )
    _ds_present(monkeypatch, tmp_path)
    seen = _stub_ds_calls(
        monkeypatch,
        lambda prompt, index: _extractive_for(prompt) if index <= 2 else "한 줄 문장",
    )

    class Args:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = None

    assert glm_mod.run(Args()) == 0

    summary_calls = seen[:-1]          # 마지막은 "이번 주 한 줄" 초안 호출
    assert len(summary_calls) == 2     # 항목 2건이 배치 2개로 갈렸다
    for _prompt, size in seen:
        assert size <= glm_mod.PROMPT_BYTE_BUDGET
    assert sorted(
        n for prompt, _ in summary_calls for n in _batch_ns(prompt)
    ) == [1, 2]
    assert [_batch_ns(prompt) for prompt, _ in summary_calls] == [[1], [2]]

    payload = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_input.json").read_text(encoding="utf-8")
    )
    assert payload["batches"] == [[1], [2]]
    assert _enrich_line_count(digest_fixture["markdown_path"]) == 2


def test_a_failing_batch_does_not_stop_the_other_batches(
    digest_fixture, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        glm_mod, "fetch_detail_text", lambda url, **kwargs: "가" * 3000
    )
    _ds_present(monkeypatch, tmp_path)

    def responder(prompt, index):
        if index == 1:
            return "이것은 JSON 이 아닙니다"   # 첫 배치 파싱 실패
        if index == 2:
            return _extractive_for(prompt)
        return "한 줄 문장"

    _stub_ds_calls(monkeypatch, responder)

    class Args:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = None

    assert glm_mod.run(Args()) == 0

    # 둘째 배치만 적용된다
    assert _enrich_line_count(digest_fixture["markdown_path"]) == 1
    warnings_file = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_warnings.json").read_text(
            encoding="utf-8")
    )
    assert any(
        "배치 1" in warning and "n=[1]" in warning
        for warning in warnings_file["warnings"]
    )
    assert warnings_file["discarded"] is False
    result = check_digest(
        db_path=digest_fixture["db_path"],
        markdown_path=digest_fixture["markdown_path"],
        output_path=None, skip_network=False,
    )
    assert result["pass"], result.get("reason")


def test_headline_prompt_stays_under_the_cap(digest_fixture):
    manifest_items = _manifest_items(digest_fixture["markdown_path"])
    prompt = glm_mod.build_headline_prompt(manifest_items)
    assert len(prompt.encode("utf-8")) <= glm_mod.PROMPT_BYTE_BUDGET


# ══ V3.1 r3b (Codex 실검토 3회차 수렴) ════════════════════════════════════
# 1. 한 구절만 허용하는 문법 — Codex 가 뚫은 4행이 전부 막혀야 한다
# ─────────────────────────────────────────────────────────────────────────
def _summary_verdict(evidence: str, line: str):
    """(값, 통과, 사유) — 근거 문자열 하나로 `한 줄 의미`를 직접 판정한다."""
    return glm_mod._gate_summary(line, glm_mod.summary_normalize(evidence))


def test_connectives_alone_cannot_assert_anything():
    """Codex 1행: 근거 `접수는 9월에만` 인데 출력 `상시 접수` 가 통과했다."""
    value, ok, reason = _summary_verdict("접수는 9월에만 진행합니다.", "상시 접수")
    assert (value, ok) == (glm_mod.FALLBACK, False)
    assert "인용 0개" in reason


def test_two_spans_cannot_be_welded_into_a_new_predicate():
    """Codex 2행: `«신청» «불가»` — 다른 문장의 술어를 결합했다."""
    value, ok, reason = _summary_verdict(
        "신청 가능. 환불 불가.", "«신청 가능합니다» «환불 불가합니다»"
    )
    assert (value, ok) == (glm_mod.FALLBACK, False)
    assert "인용 2개" in reason


def test_a_decimal_point_is_not_a_token_boundary():
    """Codex 3행: 근거 `사업비 1.5억원` 에서 `«5억원»` 을 오릴 수 없다."""
    evidence = "사업비 1.5억원 규모, 접수합니다"
    # 짧은 형태는 길이 하한(8자)에서 먼저 막힌다
    short_value, short_ok, short_reason = _summary_verdict(evidence, "«5억원»")
    assert (short_value, short_ok) == (glm_mod.FALLBACK, False)
    assert "3자" in short_reason
    # 길이를 채워도 소수점 뒤에서 오린 구절은 경계 위반이다
    value, ok, reason = _summary_verdict(evidence, "«5억원 규모, 접수합니다»")
    assert (value, ok) == (glm_mod.FALLBACK, False)
    assert "경계로 없는 인용" in reason
    # 소수점을 포함해 통째로 옮기면 통과한다
    assert _summary_verdict(evidence, "«사업비 1.5억원 규모»") == (
        "«사업비 1.5억원 규모»", True, ""
    )


def test_a_full_width_decimal_is_normalized_the_same_way_on_both_sides():
    """Codex 4행: 전각 소수점도 같은 규칙을 지난다 (NFKC)."""
    evidence = "사업비 １．５억원 규모, 접수합니다"
    value, ok, reason = _summary_verdict(evidence, "«５억원 규모, 접수합니다»")
    assert (value, ok) == (glm_mod.FALLBACK, False)
    assert "경계로 없는 인용" in reason
    # 전각으로 옮겨 와도 정규화되어 통과하고, **저장되는 값도 정규화된 값**이다
    assert _summary_verdict(evidence, "«사업비 １．５억원 규모»") == (
        "«사업비 1.5억원 규모»", True, ""
    )


def test_a_period_followed_by_whitespace_still_counts_as_a_boundary():
    evidence = "접수합니다. 의견 제출 기간: 2026. 10. 19.까지 입니다"
    assert _summary_verdict(evidence, "«의견 제출 기간: 2026. 10. 19.까지»") == (
        "«의견 제출 기간: 2026. 10. 19.까지»", True, ""
    )


def test_summary_normalize_is_applied_identically_to_both_sides():
    assert glm_mod.summary_normalize("１．５억원\n규모") == "1.5억원 규모"
    assert glm_mod.summary_normalize("  가   나  ") == "가 나"


# ─── 2. DOM — 단일 상향 측정 · 노드/깊이/종료태그 상한 ────────────────────
def test_content_selection_is_linear_on_deeply_nested_containers():
    """Codex 재현: 20,000단 중첩 `div.view` 가 후보마다 하위를 다시 읽었다."""
    html_text = '<div class="view">' * 20000 + "본문" + "</div>" * 20000
    started = time.monotonic()
    text = http_fetch.visible_text(html_text)
    assert time.monotonic() - started < 1.0
    assert "본문" in text


def test_unmatched_end_tags_do_not_scan_the_whole_stack():
    html_text = "<div>" * 50000 + "</x>" * 50000 + "<p>본문</p>"
    started = time.monotonic()
    http_fetch.visible_text(html_text)
    assert time.monotonic() - started < 1.0


def test_dom_depth_and_node_caps_are_enforced():
    builder = http_fetch._DomBuilder(max_nodes=10, max_depth=4)
    builder.feed("<div>" * 20 + "본문" + "</div>" * 20)
    builder.close()
    depth = 0
    node = builder.root
    while True:
        children = [c for c in node.children if not isinstance(c, str)]
        if not children:
            break
        node = children[0]
        depth += 1
    assert depth <= 4
    assert builder._nodes <= 10


def test_parsing_aborts_when_the_item_deadline_passes():
    clock = _Clock()

    def ticking():
        clock.now += 100.0
        return clock.now

    assert http_fetch.visible_text(
        "<p>본문</p>" * 20000, deadline=clock.now + 1, clock=ticking
    ) == ""


# ─── 3. 숨김·깨진 HTML ────────────────────────────────────────────────────
@pytest.mark.parametrize("style", [
    "display:\nnone",
    "display:/**/none",
    "  VISIBILITY :  HIDDEN  ",
])
def test_hidden_styles_are_normalized_before_matching(style):
    html_text = f"<body><div style=\"{style}\">숨김</div><p>본문</p></body>"
    text = http_fetch.visible_text(html_text)
    assert "숨김" not in text
    assert "본문" in text


def test_an_implicitly_closed_p_does_not_swallow_the_visible_body():
    """`<p hidden>숨김<p>본문` — 둘째 `<p>` 가 첫째를 닫는다."""
    text = http_fetch.visible_text("<body><p hidden>숨김<p>본문</body>")
    assert "숨김" not in text
    assert "본문" in text


def test_a_nested_form_start_tag_is_ignored_like_a_browser():
    """`<form hidden><form></form>본문` — `</form>` 이 바깥 폼을 닫는다."""
    text = http_fetch.visible_text(
        "<body><form hidden><form></form>본문</body>"
    )
    assert "본문" in text


def test_cdata_content_becomes_text():
    text = http_fetch.visible_text(
        "<body><p><![CDATA[접수기간 9월 22일까지]]></p></body>"
    )
    assert "접수기간 9월 22일까지" in text


# ─── 4. md 쓰기도 원자적 ──────────────────────────────────────────────────
def test_a_crash_during_the_md_write_leaves_the_original_intact(
    digest_fixture, tmp_path, monkeypatch
):
    """`write_text` 는 먼저 truncate 한다 — 그 창에서 죽으면 복구가 안 됐다."""
    markdown_path = digest_fixture["markdown_path"]
    _apply_fake_enrichment(digest_fixture, tmp_path)
    before = markdown_path.read_bytes()
    assert b"  \xe2\x86\x92 " in before

    def exploding(src, dst):
        raise RuntimeError("쓰기 도중 중단")

    monkeypatch.setattr(glm_mod.os, "replace", exploding)

    broken = tmp_path / "any.json"
    broken.write_text("[]", encoding="utf-8")

    class Args:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = str(broken)

    with pytest.raises(RuntimeError):
        glm_mod.run(Args())

    assert markdown_path.read_bytes() == before


def test_write_text_atomic_replaces_through_a_temp_file(tmp_path):
    target = tmp_path / "a.md"
    target.write_text("원본", encoding="utf-8")
    glm_mod.write_text_atomic(target, "새 내용")
    assert target.read_text(encoding="utf-8") == "새 내용"
    assert not target.with_name(target.name + ".tmp").exists()


# ─── 5. 카톡 — 줄 단위 정확 대조 (부분문자열 아님) ────────────────────────
def _set_enrich_lines_directly(markdown_path, texts):
    """md 의 항목 블록에 보강 줄을 직접 박는다 (게이트를 거치지 않는 픽스처)."""
    text = markdown_path.read_text(encoding="utf-8")
    ids = [
        str(block["item_id"]) for block in blocks_mod.item_blocks(text)
    ]
    assert len(ids) == len(texts)
    new_text, changed = blocks_mod.set_enrich_lines(
        text,
        {
            item_id: blocks_mod.enrich_line_text(value)
            for item_id, value in zip(ids, texts)
        },
    )
    assert len(changed) == len(texts)
    markdown_path.write_text(new_text, encoding="utf-8")
    return new_text


def test_kakao_check_catches_a_lost_line_that_is_a_substring_of_another(
    digest_fixture, monkeypatch
):
    """Codex r3: `→ «신청»` 이 `→ «신청» «불가»` 안에서 세어져 유실을 놓쳤다."""
    short = "«접수기간 9월 22일까지»"
    long = "«접수기간 9월 22일까지 신청 가능»"
    text = _set_enrich_lines_directly(
        digest_fixture["markdown_path"], [short, long]
    )
    assert composer_mod.markdown_kakao_problems(text) == []

    original = composer_mod.kakao_blocks_from_markdown

    def drop_short(markdown_text):
        out = []
        for block in original(markdown_text):
            lines = block.split("\n")
            if len(lines) == 3 and lines[2].strip() == f"→ {short}":
                out.append("\n".join(lines[:2]))
                continue
            out.append(block)
        return out

    monkeypatch.setattr(composer_mod, "kakao_blocks_from_markdown", drop_short)
    problems = composer_mod.markdown_kakao_problems(text)
    assert any("보강 줄 누락(1→0)" in problem for problem in problems)


# ─── 6. 미리보기 — 세 상태 ────────────────────────────────────────────────
def test_preview_says_only_the_headline_draft_was_discarded(
    digest_fixture, tmp_path, monkeypatch
):
    """항목은 전부 통과하고 초안만 거절된 실행을 '항목 대체'라고 쓰지 않는다."""
    fake_ds = tmp_path / "ds"
    fake_ds.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(glm_mod, "DS_BIN", fake_ds)

    class DryArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = True
        apply_json = None

    glm_mod.run(DryArgs())
    payload = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_input.json").read_text(encoding="utf-8")
    )
    summary_output = json.dumps([
        {
            "n": entry["n"], "대상 태그": "전체", "마감": "원문 확인",
            "자격": "원문 확인", "금액": "원문 확인",
            "한 줄 의미": _label(entry),
        }
        for entry in payload["items"]
    ], ensure_ascii=False)
    calls = {"n": 0}

    def fake_ds_call(prompt, timeout=None):
        calls["n"] += 1
        return summary_output if calls["n"] == 1 else "-->허위 문장<!--"

    monkeypatch.setattr(glm_mod, "call_ds_glm", fake_ds_call)

    class Args:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = None

    assert glm_mod.run(Args()) == 0
    warnings_file = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_warnings.json").read_text(
            encoding="utf-8")
    )
    assert warnings_file["items_replaced"] is False
    assert warnings_file["discarded"] is False

    check = check_digest(
        db_path=digest_fixture["db_path"],
        markdown_path=digest_fixture["markdown_path"],
        output_path=None, skip_network=False,
    )
    assert check["glm_items_replaced"] is False
    preview = preview_mod.render_preview(
        W13, digest_fixture["markdown_path"].read_text(encoding="utf-8"), check
    )
    assert "이번 주 한 줄 초안만 폐기" in preview
    assert "원문 확인'으로 대체" not in preview
    assert "GLM 출력 전체 폐기" not in preview
