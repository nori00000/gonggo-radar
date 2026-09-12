"""GLM 야간 요약 레인(V3) — 게이트·보강 줄 부착 테스트."""

import json
import sqlite3

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


def _label(n: int) -> str:
    """숫자 없는 보강 문구 — 숫자 토큰 근거 게이트(B1)에 걸리지 않게 한다."""
    return "확인 필요 " + "가나다라마바사아자차"[n - 1]


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
        "한 줄 의미": "조달 컨설팅 신청 가능",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert warnings == []
    assert results[1]["한 줄 의미"] == "조달 컨설팅 신청 가능"
    assert results[1]["마감"] == "2026-09-22 «9월 22일까지»"
    assert results[1]["금액"] == "300만원 «지원금 300만원»"
    assert results[1]["대상 태그"] == "사회적기업(경기)"


def test_gate_output_quote_not_substring_falls_back_and_warns():
    items = [_one_input_item()]
    raw = json.dumps([{
        "n": 1, "대상 태그": "사회적기업",
        "마감": "2026-09-22 «날조된 인용문»",  # 입력 텍스트에 없는 인용
        "자격": "원문 확인", "금액": "원문 확인",
        "한 줄 의미": "신청 가능",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["마감"] == glm_mod.FALLBACK
    assert any("마감" in w and "인용" in w for w in warnings)
    # 인용 실패가 다른 필드(한 줄 의미)까지 통째로 버리지 않는다
    assert results[1]["한 줄 의미"] == "신청 가능"


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
            "한 줄 의미": _label(entry["n"]),
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
    assert "  → " + _label(1) in new_markdown

    # 항목 줄·원문 줄은 정본과 여전히 동일 문자열(편집 불가 영역 불변)
    refreshed_items = _manifest_items(markdown_path)
    for entry in refreshed_items:
        assert entry["line"] == original_lines[entry["id"]]
        assert entry["origin_line"] == original_origins[entry["id"]]
        assert entry["enrich_line"] == blocks_mod.enrich_line_text(
            _label(id_to_n[entry["id"]])
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
            "자격": "원문 확인", "금액": "원문 확인", "한 줄 의미": "확인 가능",
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
    assert once.count("확인 가능") == len(input_payload["items"])


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
    def __init__(self, status_code=200, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.closed = False

    def iter_content(self, chunk_size=1):
        return iter(self._chunks)

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.max_redirects = None
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.response

    def close(self):
        pass


def _use_session(monkeypatch, session):
    monkeypatch.setattr(http_fetch.requests, "Session", lambda: session)
    return session


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


def test_fetch_detail_text_reads_html_with_shared_browser_headers(monkeypatch):
    body = "<html><body><p>공고 본문</p></body></html>".encode("utf-8")
    session = _use_session(monkeypatch, _FakeSession(_FakeResponse(
        headers={"Content-Type": "text/html; charset=utf-8"}, chunks=[body],
    )))
    text = http_fetch.fetch_detail_text("https://example.test/a")
    assert text == "공고 본문"
    assert session.max_redirects == http_fetch.MAX_REDIRECTS
    assert session.calls[0][1]["headers"] is http_fetch.PROBE_HEADERS
    assert session.calls[0][1]["timeout"] == http_fetch.DETAIL_TIMEOUT


def test_fetch_detail_text_is_fail_open_on_error_status_non_html_and_exception(monkeypatch):
    _use_session(monkeypatch, _FakeSession(_FakeResponse(status_code=404)))
    assert http_fetch.fetch_detail_text("https://example.test/a") == ""

    _use_session(monkeypatch, _FakeSession(_FakeResponse(
        headers={"Content-Type": "application/pdf"}, chunks=[b"%PDF-1.4"],
    )))
    assert http_fetch.fetch_detail_text("https://example.test/a") == ""

    _use_session(monkeypatch, _FakeSession(exc=RuntimeError("boom")))
    assert http_fetch.fetch_detail_text("https://example.test/a") == ""

    assert http_fetch.fetch_detail_text("ftp://example.test/a") == ""
    assert http_fetch.fetch_detail_text("") == ""


def test_fetch_detail_text_stops_at_max_bytes(monkeypatch):
    _use_session(monkeypatch, _FakeSession(_FakeResponse(
        headers={"Content-Type": "text/html"},
        chunks=[b"a" * 400000, b"b" * 400000, b"c" * 400000],
    )))
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
def test_gate_summary_rejects_numbers_with_no_evidence():
    items = [_one_input_item()]
    raw = json.dumps([{
        "n": 1, "대상 태그": "전체", "마감": "원문 확인",
        "자격": "원문 확인", "금액": "원문 확인",
        "한 줄 의미": "전 기업 1억원 지급 확정",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["한 줄 의미"] == glm_mod.FALLBACK
    assert any("근거 없는" in w and "1억" in w for w in warnings)


def test_gate_summary_accepts_numbers_grounded_in_detail_text():
    items = [_one_input_item(detail_text="사업비 1억원 규모로 9.30까지 접수")]
    raw = json.dumps([{
        "n": 1, "대상 태그": "전체", "마감": "원문 확인",
        "자격": "원문 확인", "금액": "원문 확인",
        "한 줄 의미": "1억원 규모 9.30까지",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["한 줄 의미"] == "1억원 규모 9.30까지"
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
    assert any("날조" in w for w in warnings)


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
        "한 줄 의미": "확인\n" + "가" * 60,
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
            "한 줄 의미": _label(entry["n"]),
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
    _apply_fake_enrichment(digest_fixture, tmp_path)
    text = markdown_path.read_text(encoding="utf-8")

    blocks = composer_mod.kakao_blocks_from_markdown(text)
    item_chunks = [b for b in blocks if "http" in b and "\n" in b]
    assert item_chunks
    for chunk in item_chunks:
        lines = chunk.split("\n")
        assert len(lines) == 3
        assert lines[2].strip().startswith("→")
    assert _label(1) in composer_mod.kakao_file_text_from_markdown(text)
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
    _apply_fake_enrichment(digest_fixture, tmp_path)
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
    assert _label(1) in preview


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
