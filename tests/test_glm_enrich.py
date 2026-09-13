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


def _stub_detail(url, **kwargs) -> str:
    """항목마다 후보 두 문장을 주는 가짜 상세 본문 (URL 은 넣지 않는다)."""
    tag = str(url).rstrip("/").rsplit("/", 1)[-1] or "x"
    return (
        f"{tag} 공고는 사회적기업 대상 지원사업입니다. "
        f"{tag} 접수는 9월 22일까지 진행합니다."
    )


@pytest.fixture(autouse=True)
def stub_detail_fetch(monkeypatch):
    """상세 텍스트 수집은 **절대 실호출하지 않는다** — 기본 스텁은 빈 문자열이다.

    개별 테스트가 근거 텍스트를 공급하려면 같은 이름을 다시 갈아끼운다.
    """
    monkeypatch.setattr(glm_mod, "fetch_detail_text", _stub_detail)


def _label(entry, pick: int = 1) -> str:
    """r5: md 에 들어갈 문자열 = **우리가 뽑은 후보 문장** 그대로."""
    return "«{}»".format(entry["candidates"][pick - 1]["text"])


def _pick_results(payload_items, pick: int = 1):
    return [
        {
            "n": entry["n"], "pick": pick, "대상 태그": "전체",
            "마감": "원문 확인", "자격": "원문 확인", "금액": "원문 확인",
        }
        for entry in payload_items
    ]


def _pick_output(payload_items, pick: int = 1) -> str:
    """ds 가 돌려주는 모양 — 결과 배열 그대로."""
    return json.dumps(_pick_results(payload_items, pick), ensure_ascii=False)


def _apply_file(payload, pick: int = 1, input_sha=None) -> str:
    """`--apply-json` 파일 모양 — 입력 지문을 함께 들고 다닌다 (r9)."""
    return json.dumps(
        {
            "input_sha": payload["input_sha"] if input_sha is None else input_sha,
            "results": _pick_results(payload["items"], pick),
        },
        ensure_ascii=False,
    )


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
        "summary": "9월 22일까지 접수합니다. 지원금 300만원.",
        "detail_text": "이 사업은 9월 22일까지 접수합니다. 지원금 300만원을 지급합니다.",
        "quote_deadline": "2026-09-22",
        "quote_eligibility": "원문 확인",
        "quote_amount": "원문 확인",
        "url": "https://example.com/x",
    }
    item.update(overrides)
    item["candidates"] = glm_mod.candidate_sentences(item)
    return item


def test_gate_output_normal_case_applies_all_fields():
    items = [_one_input_item()]
    raw = json.dumps([{
        "n": 1, "대상 태그": "사회적기업(경기)",
        "마감": "2026-09-22 «9월 22일까지»",
        "자격": "원문 확인", "금액": "300만원 «지원금 300만원»",
        "pick": 1,
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert warnings == []
    assert results[1]["한 줄 의미"] == "«{}»".format(items[0]["candidates"][0])
    assert results[1]["마감"] == "2026-09-22 «9월 22일까지»"
    assert results[1]["금액"] == "300만원 «지원금 300만원»"
    assert results[1]["대상 태그"] == "사회적기업(경기)"


def test_gate_output_quote_not_substring_falls_back_and_warns():
    items = [_one_input_item()]
    raw = json.dumps([{
        "n": 1, "대상 태그": "사회적기업",
        "마감": "2026-09-22 «날조된 인용문»",  # 입력 텍스트에 없는 인용
        "자격": "원문 확인", "금액": "원문 확인", "pick": 1,
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["마감"] == glm_mod.FALLBACK
    assert any("마감" in w and "인용" in w for w in warnings)
    # 인용 실패가 고른 문장까지 통째로 버리지 않는다
    assert results[1]["한 줄 의미"] == "«{}»".format(items[0]["candidates"][0])


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
        "n", "title", "source_name", "url", "quote_deadline", "candidates",
    }
    # 원문 상세는 프롬프트에 **나가지 않는다** — 고를 후보만 나간다
    assert "detail_text" not in payload["items"][0]


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
    fake_output_path = tmp_path / "fake_glm_output.json"
    fake_output_path.write_text(
        _apply_file(input_payload), encoding="utf-8")

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
    fake_output_path = tmp_path / "fake.json"
    fake_output_path.write_text(
        _apply_file(input_payload), encoding="utf-8")

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
        lambda url, **kwargs: "이 사업은 지원금 300만원을 지급합니다.",
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
        texts = [candidate["text"] for candidate in entry["candidates"]]
        assert texts, entry
        assert any("300만원" in text for text in texts)


# ─── B1. 한 줄 의미 근거 검증 (숫자 토큰 · 인용 전부) ──────────────────────
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


# ─── B2. 형식 게이트 (저장 전 · 필드 단위 대체) ───────────────────────────
def test_format_violation_never_reaches_the_markdown(
    digest_fixture, tmp_path, monkeypatch
):
    """r5: 형식 위반 문장은 **후보가 되기 전에** 탈락한다."""
    markdown_path = digest_fixture["markdown_path"]
    monkeypatch.setattr(
        glm_mod, "fetch_detail_text",
        lambda url, **kwargs: (
            "신청은 [여기](https://evil.test) 에서 진행합니다. "
            "접수는 9월 22일까지 진행합니다."
        ),
    )

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
    # 링크가 든 문장은 후보 목록에 아예 없다
    for entry in payload["items"]:
        texts = [candidate["text"] for candidate in entry["candidates"]]
        assert texts == ["접수는 9월 22일까지 진행합니다."]
    output_path = tmp_path / "bad_format.json"
    output_path.write_text(_apply_file(payload), encoding="utf-8")

    class ApplyArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = str(output_path)

    assert glm_mod.run(ApplyArgs()) == 0
    text = markdown_path.read_text(encoding="utf-8")
    assert "evil.test" not in text
    assert "  → «접수는 9월 22일까지 진행합니다.»" in text
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
    output_path = tmp_path / name
    output_path.write_text(_apply_file(payload), encoding="utf-8")

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
    assert any("input_sha" in w for w in warnings_now)


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
    assert any("보강 줄 불일치" in p for p in problems)


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
        return "이 공고는 사회적기업 대상 지원사업으로 진행합니다."

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
        assert [candidate["text"] for candidate in entry["candidates"]] == [
            "이 공고는 사회적기업 대상 지원사업으로 진행합니다."
        ]


# ══ V3.1 r2 (Codex 실검토 v3.1 수렴) ══════════════════════════════════════
# 1. 추출형 `한 줄 의미` — 토큰 경계·문법·정규화
# ─────────────────────────────────────────────────────────────────────────
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
def _apply_identical_enrichment(digest_fixture):
    """두 항목에 **같은** 보강 줄을 박는다 (출현 수 대조용)."""
    return _set_enrich_lines_directly(
        digest_fixture["markdown_path"],
        ["«접수는 9월 22일까지 진행합니다.»"] * 2,
    )


def _unused_apply_fallback(digest_fixture, tmp_path):
    """(사용 안 함) 예전 생성형 경로의 픽스처."""
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
    text = _apply_identical_enrichment(digest_fixture)
    assert text.count("  → «접수는 9월 22일까지 진행합니다.»") == 2
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
    assert any("보강 줄 불일치" in problem for problem in problems)


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
    broken = tmp_path / "broken.json"
    # 지문은 맞지만 결과가 배열이 아니다 → 물어보고 **전부 버린** 실행
    broken.write_text(
        json.dumps({"input_sha": payload["input_sha"], "results": "배열이 아님"},
                   ensure_ascii=False),
        encoding="utf-8",
    )

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
    path = tmp_path / "gated.json"
    path.write_text(_apply_file(payload, pick=99), encoding="utf-8")

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
    summary_output = _pick_output(payload["items"])
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
def _many_sentences(count: int, tag: str = "가") -> str:
    """후보로 뽑힐 만한 완결 문장 `count` 개."""
    return " ".join(
        f"{tag}{index}번 사업은 사회적기업 대상 지원사업으로 진행합니다."
        for index in range(count)
    )


def _big_item(n, sentences, id_=None):
    item = {
        "n": n, "id": id_ if id_ is not None else 40 + n,
        "title": f"산림 지원사업 참여기업 모집 공고 {n}", "source_name": "기관",
        "summary": "", "detail_text": _many_sentences(sentences, tag=f"제{n}"),
        "quote_deadline": "", "quote_eligibility": "원문 확인",
        "quote_amount": "원문 확인", "url": f"https://example.com/{n}",
    }
    item["candidates"] = glm_mod.candidate_sentences(item)
    return item


def test_batch_input_items_keeps_every_prompt_under_the_byte_budget():
    today = "2026-09-13"
    items = [_big_item(n, 40) for n in range(1, 6)]
    budget = 5000   # 후보만 보내므로 프롬프트가 작다 — 쪼개려면 예산을 줄인다

    batches, skipped = glm_mod.batch_input_items(today, items, budget=budget)

    assert skipped == []
    assert len(batches) > 1
    for batch in batches:
        assert glm_mod.prompt_bytes(today, batch) <= budget
    # 모든 항목이 정확히 한 번씩, 문서 순서대로
    assert [item["n"] for batch in batches for item in batch] == [1, 2, 3, 4, 5]


def test_batch_input_items_trims_whole_candidates_instead_of_dropping_the_item():
    """r5: 절단은 **후보 단위**다 — 후보 자체가 원문의 완결 문장이다."""
    today = "2026-09-13"
    item = _big_item(1, 40)
    assert len(item["candidates"]) == glm_mod.MAX_CANDIDATES
    budget = glm_mod.prompt_bytes(today, [item]) - 200

    batches, skipped = glm_mod.batch_input_items(today, [item], budget=budget)

    assert skipped == []
    assert len(batches) == 1 and len(batches[0]) == 1
    trimmed = batches[0][0]
    assert trimmed["n"] == 1                      # 항목이 사라지지 않았다
    assert 0 < len(trimmed["candidates"]) < len(item["candidates"])
    assert glm_mod.prompt_bytes(today, [trimmed]) <= budget
    # 남은 후보는 원래 목록의 앞부분 그대로다
    assert trimmed["candidates"] == item["candidates"][:len(trimmed["candidates"])]


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


def _is_summary_prompt(prompt: str) -> bool:
    """요약(고르기) 프롬프트인가 — 초안 프롬프트와 모양이 다르다."""
    return '"candidates"' in prompt


def _extractive_for(prompt):
    """프롬프트에 실린 후보 중 1번을 고르는 가짜 GLM 출력 (r5)."""
    payload = json.loads(prompt.split("\n\n입력\n", 1)[1])
    return _pick_output(payload["items"])


def _auto_respond(prompt, index):
    """요약 프롬프트면 pick 을 돌려주고, 초안 프롬프트면 한 문장."""
    return _extractive_for(prompt) if _is_summary_prompt(prompt) else "한 줄 문장"


def test_run_splits_into_batches_and_each_prompt_fits_the_cap(
    digest_fixture, tmp_path, monkeypatch
):
    """W37 실패 재현: detail_text 를 실으면 한 프롬프트에 다 들어가지 않는다."""
    monkeypatch.setattr(
        glm_mod, "fetch_detail_text",
        lambda url, **kwargs: _many_sentences(12, tag=url[-1]),
    )
    monkeypatch.setattr(glm_mod, "PROMPT_BYTE_BUDGET", 3900)
    _ds_present(monkeypatch, tmp_path)
    seen = _stub_ds_calls(
        monkeypatch,
        _auto_respond,
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
        glm_mod, "fetch_detail_text",
        lambda url, **kwargs: _many_sentences(12, tag=url[-1]),
    )
    monkeypatch.setattr(glm_mod, "PROMPT_BYTE_BUDGET", 3900)
    _ds_present(monkeypatch, tmp_path)

    state = {"summary_calls": 0}

    def responder(prompt, index):
        if not _is_summary_prompt(prompt):
            return "한 줄 문장"
        state["summary_calls"] += 1
        if state["summary_calls"] == 1:
            return "이것은 JSON 이 아닙니다"   # 첫 배치 파싱 실패
        return _extractive_for(prompt)

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
def test_summary_normalize_collapses_whitespace_and_nothing_else():
    """r4: 정규화는 공백 합침뿐 — 글자를 바꾸지 않는다(NFKC 삭제)."""
    assert glm_mod.summary_normalize("  가   나  ") == "가 나"
    assert glm_mod.summary_normalize("\uff11\uff0e\uff15억원\n규모") == "\uff11\uff0e\uff15억원 규모"
    assert glm_mod.summary_normalize("10\u2074\u33a1") == "10\u2074\u33a1"


# ─── 2. DOM — 단일 상향 측정 · 노드/깊이/종료태그 상한 ────────────────────
def test_content_selection_is_linear_on_deeply_nested_containers():
    """20,000단 중첩은 선형으로 처리되고, 깊이 상한을 넘으므로 수집 실패다."""
    html_text = '<div class="view">' * 20000 + "본문" + "</div>" * 20000
    started = time.monotonic()
    text = http_fetch.visible_text(html_text)
    assert time.monotonic() - started < 1.0
    assert text == ""


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
    assert any("보강 줄 불일치" in problem for problem in problems)


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
    summary_output = _pick_output(payload["items"])
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


# ══ V3.1 r4 (Codex 최종 라운드 수렴) ══════════════════════════════════════
# 문장 분해 자체의 계약
# ─────────────────────────────────────────────────────────────────────────
def test_split_sentences_treats_newlines_as_hard_boundaries():
    assert glm_mod.split_sentences("접수기간\n9월 22일까지") == [
        "접수기간", "9월 22일까지",
    ]


def test_split_sentences_splits_on_terminators_followed_by_space():
    assert glm_mod.split_sentences("첫 문장입니다. 둘째 문장입니다!") == [
        "첫 문장입니다.", "둘째 문장입니다!",
    ]


def test_split_sentences_keeps_numeric_runs_together():
    """`1.5` 도 `2026. 10. 19.` 도 문장으로 쪼개지 않는다."""
    assert glm_mod.split_sentences("사업비 1.5억원 규모") == ["사업비 1.5억원 규모"]
    assert glm_mod.split_sentences("의견 제출 2026. 10. 19.까지") == [
        "의견 제출 2026. 10. 19.까지",
    ]


def test_evidence_sentences_keeps_the_fields_apart():
    item = _one_input_item(
        title="신청 불가 대상: 사회적기업",
        summary="",
        detail_text="신청 가능 기간은 9월 22일까지입니다.",
    )
    sentences = glm_mod.evidence_sentences(item)
    assert "신청 불가 대상: 사회적기업" in sentences
    assert "신청 가능 기간은 9월 22일까지입니다." in sentences
    assert not any(
        "사회적기업 신청 가능" in sentence for sentence in sentences
    )


# ─── 3. 절단·건너뛰기 ─────────────────────────────────────────────────────
def test_an_item_too_big_without_its_detail_text_is_skipped_not_sent():
    """제목만으로 예산을 넘으면 ds 에 보내지 않는다 (r4, Codex MEDIUM)."""
    today = "2026-09-13"
    monster = _big_item(1, 10)
    monster["title"] = "가" * 5000
    small = _big_item(2, 10)

    batches, skipped = glm_mod.batch_input_items(today, [monster, small])

    assert [item["n"] for item in skipped] == [1]
    assert [[item["n"] for item in batch] for batch in batches] == [[2]]


def test_run_warns_about_a_skipped_item_and_never_sends_it(
    digest_fixture, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        glm_mod, "fetch_detail_text", lambda url, **kwargs: _many_sentences(3)
    )
    _ds_present(monkeypatch, tmp_path)
    # 페르소나 지시만으로도 넘는 예산 — 어떤 항목도 보낼 수 없다
    monkeypatch.setattr(glm_mod, "PROMPT_BYTE_BUDGET", 100)
    seen = _stub_ds_calls(monkeypatch, lambda prompt, index: "한 줄 문장")

    class Args:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = None

    assert glm_mod.run(Args()) == 0
    payload = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_input.json").read_text(encoding="utf-8")
    )
    assert payload["skipped"] == [1, 2]
    assert payload["batches"] == []
    # 요약 프롬프트는 **한 번도** 나가지 않았다 (초안 프롬프트도 예산 초과로 생략)
    assert seen == []
    warnings_file = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_warnings.json").read_text(
            encoding="utf-8")
    )
    assert sum("예산 초과" in w for w in warnings_file["warnings"]) == 2


# ─── 6. 타임아웃이 프로세스 종료를 막지 않는다 ────────────────────────────
def test_fetch_detail_text_returns_on_time_and_leaves_only_a_daemon_thread():
    """워커는 데몬이다 — 인터프리터 종료가 워커를 기다리지 않는다."""
    import threading

    release = threading.Event()

    def slow_open(url):
        release.wait(30)
        raise AssertionError("도달하면 안 된다")

    original = http_fetch._open
    http_fetch._open = slow_open
    try:
        before = {t.ident for t in threading.enumerate()}
        started = time.monotonic()
        assert http_fetch.fetch_detail_text(
            "https://example.test/a", timeout=0.2
        ) == ""
        assert time.monotonic() - started < 3.0
        leftover = [
            t for t in threading.enumerate() if t.ident not in before
        ]
        # 남은 스레드가 있다면 반드시 데몬이어야 한다 (종료를 막지 않는다)
        assert all(t.daemon for t in leftover)
    finally:
        http_fetch._open = original
        release.set()


# ─── 7. 연결 정리 순서 (close → release_conn) ─────────────────────────────
class _OrderRecordingResponse(_FakeResponse):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order = []

    def close(self):
        self.order.append("close")

    def release_conn(self):
        self.order.append("release_conn")


def test_unconsumed_response_is_closed_before_the_connection_is_released(monkeypatch):
    """release_conn 을 먼저 부르면 미소비 연결이 풀로 돌아가 close 가 무력해진다."""
    response = _OrderRecordingResponse(
        headers={"content-type": "text/html"},
        chunks=[b"a" * (http_fetch.MAX_DETAIL_BYTES + 10)],
    )
    _use_open(monkeypatch, response)
    http_fetch.fetch_detail_text("https://example.test/a")
    assert response.order == ["close", "release_conn"]


# ─── 8. CSS 주석 제거는 선형 ──────────────────────────────────────────────
def test_css_comment_stripping_is_linear_on_unterminated_comments():
    style = "/*x" * 8000
    started = time.monotonic()
    http_fetch.strip_css_comments(style)
    assert time.monotonic() - started < 0.1


def test_is_hidden_is_fast_on_a_pathological_style_attribute():
    html_text = '<body><div style="{}">본문</div></body>'.format("/*x" * 8000)
    started = time.monotonic()
    http_fetch.visible_text(html_text)
    assert time.monotonic() - started < 0.5


# ─── 9. 깊이 상한 초과 = 수집 실패 (숨김 누출 금지) ───────────────────────
def test_exceeding_the_depth_cap_is_a_collection_failure_not_a_leak():
    """Codex r3: 255단 아래의 `<div hidden>` 문구가 가시 부모로 새어 나왔다."""
    html_text = (
        "<div>" * 255
        + "<div hidden>사회적기업 누구나 신청 가능</div>"
        + "</div>" * 255
    )
    assert http_fetch.visible_text(html_text) == ""


def test_exceeding_the_node_cap_is_also_a_collection_failure():
    builder_html = "<p>x</p>" * 50
    assert http_fetch.visible_text(builder_html) != ""
    parser = http_fetch._DomBuilder(max_nodes=5)
    parser.feed(builder_html)
    parser.close()
    assert parser.overflowed is True


# ─── 10. md→manifest 두 파일 트랜잭션 ─────────────────────────────────────
def test_a_failed_manifest_write_rolls_the_markdown_back(
    digest_fixture, tmp_path, monkeypatch
):
    """정본 쓰기가 터지면 md 를 되돌리고 경고만 남긴다 (exit 0 유지)."""
    markdown_path = digest_fixture["markdown_path"]
    before = markdown_path.read_bytes()

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
    output_path = tmp_path / "ok.json"
    output_path.write_text(_apply_file(payload), encoding="utf-8")

    real = composer_mod.set_manifest_enrich_lines
    calls = {"n": 0}

    def flaky(markdown_path_arg, enrich_by_id, clear_all=False):
        calls["n"] += 1
        if calls["n"] == 1:          # 제거 단계는 통과시킨다
            return real(markdown_path_arg, enrich_by_id, clear_all=clear_all)
        raise RuntimeError("정본 쓰기 실패")

    monkeypatch.setattr(composer_mod, "set_manifest_enrich_lines", flaky)

    class ApplyArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = str(output_path)

    assert glm_mod.run(ApplyArgs()) == 0
    assert markdown_path.read_bytes() == before
    assert not markdown_path.with_name(markdown_path.name + ".bak").exists()
    warnings_file = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_warnings.json").read_text(
            encoding="utf-8")
    )
    assert any("md 롤백" in w for w in warnings_file["warnings"])

    monkeypatch.setattr(composer_mod, "set_manifest_enrich_lines", real)
    result = check_digest(
        db_path=digest_fixture["db_path"], markdown_path=markdown_path,
        output_path=None, skip_network=False,
    )
    assert result["pass"], result.get("reason")
    assert result["manifest_problems"] == []


def test_commit_markdown_and_manifest_removes_the_backup_on_success(tmp_path):
    target = tmp_path / "a.md"
    target.write_text("원본", encoding="utf-8")
    assert glm_mod.commit_markdown_and_manifest(
        target, "새 내용", lambda: None
    ) is None
    assert target.read_text(encoding="utf-8") == "새 내용"
    assert not target.with_name(target.name + ".bak").exists()


# ─── 11. 미리보기 부분 배치 상태 · 카톡 항목별 대조 ───────────────────────
def test_preview_reports_a_partially_applied_run_distinctly(
    digest_fixture, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        glm_mod, "fetch_detail_text",
        lambda url, **kwargs: _many_sentences(12, tag=url[-1]),
    )
    monkeypatch.setattr(glm_mod, "PROMPT_BYTE_BUDGET", 3900)
    _ds_present(monkeypatch, tmp_path)

    state = {"summary_calls": 0}

    def responder(prompt, index):
        if not _is_summary_prompt(prompt):
            return "한 줄 문장"
        state["summary_calls"] += 1
        if state["summary_calls"] == 1:
            return None                      # 배치 1 ds 실패
        return _extractive_for(prompt)       # 배치 2 정상

    _stub_ds_calls(monkeypatch, responder)

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
    assert warnings_file["partial_batches"] == [[1]]
    assert warnings_file["items_replaced"] is False

    check = check_digest(
        db_path=digest_fixture["db_path"],
        markdown_path=digest_fixture["markdown_path"],
        output_path=None, skip_network=False,
    )
    assert check["glm_partial_batches"] == [[1]]
    preview = preview_mod.render_preview(
        W13, digest_fixture["markdown_path"].read_text(encoding="utf-8"), check
    )
    assert "일부 배치 미적용 (n=[1])" in preview
    assert "원문 확인'으로 대체" not in preview
    assert "GLM 출력 전체 폐기" not in preview


def test_kakao_check_catches_two_items_swapping_their_enrich_lines(
    digest_fixture, monkeypatch
):
    """전역 다중집합은 교환을 놓친다 — 항목별 자리 맞춤이라야 잡힌다."""
    first = "«접수기간 9월 22일까지입니다»"
    second = "«지원금 300만원을 지급합니다»"
    text = _set_enrich_lines_directly(
        digest_fixture["markdown_path"], [first, second]
    )
    assert composer_mod.markdown_kakao_problems(text) == []

    original = composer_mod.kakao_blocks_from_markdown

    def swap(markdown_text):
        blocks = original(markdown_text)
        enrich_positions = [
            index for index, block in enumerate(blocks)
            if len(block.split("\n")) == 3
            and block.split("\n")[2].strip().startswith("→")
        ]
        assert len(enrich_positions) == 2
        left, right = enrich_positions
        left_lines = blocks[left].split("\n")
        right_lines = blocks[right].split("\n")
        left_lines[2], right_lines[2] = right_lines[2], left_lines[2]
        blocks[left] = "\n".join(left_lines)
        blocks[right] = "\n".join(right_lines)
        return blocks

    monkeypatch.setattr(composer_mod, "kakao_blocks_from_markdown", swap)
    problems = composer_mod.markdown_kakao_problems(text)
    assert any("보강 줄 불일치" in problem for problem in problems)


# ══ V3.1 r5 — 생성이 아니라 선택 ═══════════════════════════════════════════
# 1. 후보 추출 (결정론)
# ─────────────────────────────────────────────────────────────────────────
def _candidates_from(detail, title="어떤 공고 제목"):
    return glm_mod.candidate_sentences({"title": title, "detail_text": detail})


def test_candidates_come_from_the_detail_text_in_document_order():
    detail = (
        "첫 번째 문장은 사회적기업 지원 내용입니다. "
        "두 번째 문장은 접수 방법을 설명합니다. "
        "세 번째 문장은 문의처를 안내합니다."
    )
    assert _candidates_from(detail) == [
        "첫 번째 문장은 사회적기업 지원 내용입니다.",
        "두 번째 문장은 접수 방법을 설명합니다.",
        "세 번째 문장은 문의처를 안내합니다.",
    ]


def test_candidates_drop_sentences_equal_to_or_inside_the_title():
    title = "2026년 산림형 예비사회적기업 지정 계획 공고 안내입니다"
    detail = "2026년 산림형 예비사회적기업 지정 계획 공고. 접수는 9월 22일까지 진행합니다."
    assert _candidates_from(detail, title=title) == [
        "접수는 9월 22일까지 진행합니다.",
    ]


@pytest.mark.parametrize("meta", [
    "작성자 : 서울인천센터",
    "조회수 338 입니다",
    "등록일 2026/09/11 입니다",
    "첨부파일을 내려받으세요",
    "본문 바로가기 메뉴입니다",
    "로그인 후 이용하세요",
    "회원가입 후 신청하세요",
    "다운로드 링크를 누르세요",
    "이전글 보기입니다",
    "다음글 보기입니다",
    "목록으로 돌아가기",
    "공유 버튼을 누르세요",
    "프린트 하기 메뉴입니다",
])
def test_candidates_drop_page_furniture(meta):
    detail = f"{meta}. 접수는 9월 22일까지 진행합니다."
    assert _candidates_from(detail) == ["접수는 9월 22일까지 진행합니다."]


def test_candidates_drop_short_long_blank_and_duplicate_sentences():
    short = "가" * (glm_mod.MIN_CANDIDATE_CHARS - 5)
    long = "나" * (glm_mod.MAX_CANDIDATE_CHARS + 1)
    good = "접수는 9월 22일까지 진행합니다."
    detail = f"{short}. {long}. {good} {good} ■■■■■■■■■■■■■■."
    assert _candidates_from(detail) == [good]


def test_candidates_are_capped_and_keep_the_first_ones():
    detail = _many_sentences(30)
    candidates = _candidates_from(detail)
    assert len(candidates) == glm_mod.MAX_CANDIDATES
    assert candidates == glm_mod.split_sentences(detail)[:glm_mod.MAX_CANDIDATES]


def test_a_sentence_that_fails_the_format_gate_never_becomes_a_candidate():
    detail = (
        "신청은 https://evil.test 에서 진행합니다. "
        "접수는 9월 22일까지 진행합니다."
    )
    assert _candidates_from(detail) == ["접수는 9월 22일까지 진행합니다."]


# ─── 2. 프롬프트: 원문 상세는 나가지 않는다 ───────────────────────────────
def test_the_prompt_carries_numbered_candidates_and_no_raw_detail():
    today = "2026-09-13"
    item = _big_item(1, 5)
    prompt = glm_mod.build_summary_prompt(
        {"today": today, "items": glm_mod.payload_for_glm([item])}
    )
    assert '"candidates"' in prompt
    assert '"k": 1' in prompt or '"k":1' in prompt
    assert "detail_text" not in prompt
    assert item["candidates"][0] in prompt


def test_persona_prompt_asks_for_a_choice_not_a_sentence():
    prompt = glm_mod.PERSONA_SYSTEM_PROMPT
    assert "쓰는 것이 아니라 고르는 것" in prompt
    assert "가장 먼저 알아야 할 한 문장" in prompt
    assert "적합한 문장이 없으면" in prompt
    assert '"pick"' in prompt or "`pick`" in prompt
    assert "한 줄 의미" not in prompt


# ─── 3. pick 검증 ─────────────────────────────────────────────────────────
def _pick_verdict(pick, candidate_count=3):
    return glm_mod.gate_pick(pick, candidate_count)


def test_gate_pick_accepts_zero_and_valid_indices():
    assert _pick_verdict(0) == (0, "")
    assert _pick_verdict(1) == (1, "")
    assert _pick_verdict(3) == (3, "")


@pytest.mark.parametrize("bad", [True, False, "1", 1.0, None, [1]])
def test_gate_pick_rejects_non_integers(bad):
    picked, reason = _pick_verdict(bad)
    assert picked is None
    assert "정수가 아님" in reason


@pytest.mark.parametrize("bad", [-1, 4, 99])
def test_gate_pick_rejects_out_of_range(bad):
    picked, reason = _pick_verdict(bad)
    assert picked is None
    assert "범위 밖" in reason


def test_pick_zero_means_no_enrich_line():
    items = [_one_input_item()]
    raw = json.dumps([{
        "n": 1, "pick": 0, "대상 태그": "전체", "마감": "원문 확인",
        "자격": "원문 확인", "금액": "원문 확인",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["한 줄 의미"] == ""
    assert warnings == []


def test_an_invalid_pick_leaves_the_item_without_an_enrich_line():
    items = [_one_input_item()]
    raw = json.dumps([{
        "n": 1, "pick": 99, "대상 태그": "전체", "마감": "원문 확인",
        "자격": "원문 확인", "금액": "원문 확인",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert results[1]["한 줄 의미"] == ""
    assert any("범위 밖" in w and "보강 줄 없음" in w for w in warnings)


def test_the_markdown_can_only_contain_a_candidate_sentence(
    digest_fixture, tmp_path
):
    """GLM 출력에서 오는 것은 번호뿐이다 — 문자열은 우리 목록에서만 나온다."""
    markdown_path = digest_fixture["markdown_path"]
    payload = _apply_fake_enrichment(digest_fixture, tmp_path)

    text = markdown_path.read_text(encoding="utf-8")
    enrich_lines = [
        (block.get("enrich_line") or "").strip()
        for block in blocks_mod.item_blocks(text)
        if block.get("enrich_line")
    ]
    assert len(enrich_lines) == 2
    allowed = {
        "→ «{}»".format(candidate["text"])
        for entry in payload["items"]
        for candidate in entry["candidates"]
    }
    assert set(enrich_lines) <= allowed


def test_a_fabricated_sentence_in_the_glm_output_is_ignored_entirely(
    digest_fixture, tmp_path
):
    """출력에 문장을 써 보내도 md 에는 들어가지 않는다 — 번호만 읽는다."""
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
    fake = {
        "input_sha": payload["input_sha"],
        "results": [
            {
                "n": entry["n"], "pick": 1, "대상 태그": "전체",
                "마감": "원문 확인", "자격": "원문 확인", "금액": "원문 확인",
                "한 줄 의미": "전 기업 1억원 지급 확정",   # 무시되어야 한다
            }
            for entry in payload["items"]
        ],
    }
    path = tmp_path / "fabricated.json"
    path.write_text(json.dumps(fake, ensure_ascii=False), encoding="utf-8")

    class ApplyArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = str(path)

    assert glm_mod.run(ApplyArgs()) == 0
    text = digest_fixture["markdown_path"].read_text(encoding="utf-8")
    assert "1억원" not in text
    assert "  → «{}»".format(payload["items"][0]["candidates"][0]["text"]) in text


# ─── 4. 후보 0건 항목은 보내지 않는다 ─────────────────────────────────────
def test_items_without_candidates_are_never_sent_and_recorded_as_info(
    digest_fixture, tmp_path, monkeypatch
):
    monkeypatch.setattr(glm_mod, "fetch_detail_text", lambda url, **kwargs: "")
    _ds_present(monkeypatch, tmp_path)
    seen = _stub_ds_calls(monkeypatch, _auto_respond)

    class Args:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = None

    assert glm_mod.run(Args()) == 0
    payload = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_input.json").read_text(encoding="utf-8")
    )
    assert payload["no_candidates"] == [1, 2]
    assert payload["batches"] == []
    assert not any(_is_summary_prompt(prompt) for prompt, _ in seen)

    warnings_file = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_warnings.json").read_text(
            encoding="utf-8")
    )
    assert warnings_file["info"]["no_candidates"] == [1, 2]
    assert _enrich_line_count(digest_fixture["markdown_path"]) == 0


def test_a_w37_sized_fixture_fits_in_two_batches_or_fewer(monkeypatch):
    """r5 의 프롬프트는 작다 — W37 규모(8건)가 두 배치 안에 들어간다."""
    today = "2026-09-13"
    items = [_big_item(n, 30) for n in range(1, 9)]
    batches, skipped = glm_mod.batch_input_items(today, items)
    assert skipped == []
    assert len(batches) <= 2
    for batch in batches:
        assert glm_mod.prompt_bytes(today, batch) <= glm_mod.PROMPT_BYTE_BUDGET


# ══ V3.1 r6 — 분해기 보정 · 후보 하한 · 품질 필터 ═════════════════════════
# 1. 목록 표지·날짜는 문장 끝이 아니다
# ─────────────────────────────────────────────────────────────────────────
def test_a_list_marker_number_does_not_end_a_sentence():
    """W37 n=6: `… 입법예고 1.` 에서 쪼개져 조각이 후보가 됐다."""
    assert glm_mod.split_sentences(
        "산림재난방지법 시행령 일부개정령안 입법예고 1. 개정이유 법률이 개정되었습니다."
    ) == ["산림재난방지법 시행령 일부개정령안 입법예고 1. 개정이유 법률이 개정되었습니다."]


def test_a_single_letter_list_marker_does_not_end_a_sentence():
    assert glm_mod.split_sentences(
        "가. 지원 대상은 사회적기업입니다. 나. 접수는 9월까지입니다."
    ) == ["가. 지원 대상은 사회적기업입니다.", "나. 접수는 9월까지입니다."]


def test_a_short_year_date_run_does_not_split():
    """W37 n=6: `공포, ’26.11.13.` 이 별도 문장이 됐다."""
    assert glm_mod.split_sentences(
        "「산림재난방지법」이 개정(’26.5.12. 공포, ’26.11.13. 시행)됨에 따라 정합니다."
    ) == ["「산림재난방지법」이 개정(’26.5.12. 공포, ’26.11.13. 시행)됨에 따라 정합니다."]


def test_a_spaced_date_run_does_not_split():
    """W37 n=7: `2026. 5. 12.` 에서 인용이 잘렸다."""
    assert glm_mod.split_sentences(
        "법률 제21620호, 2026. 5. 12. 공포되었습니다."
    ) == ["법률 제21620호, 2026. 5. 12. 공포되었습니다."]


def test_a_date_with_a_weekday_suffix_does_not_split():
    assert glm_mod.split_sentences(
        "설명회는 2026.09.30.(수) 14시에 열립니다."
    ) == ["설명회는 2026.09.30.(수) 14시에 열립니다."]


def test_a_period_before_a_closing_bracket_does_not_end_a_sentence():
    assert glm_mod.split_sentences("근거는 제7조입니다 . ) 라고 적혀 있습니다.") == [
        "근거는 제7조입니다 . ) 라고 적혀 있습니다.",
    ]


def test_the_old_decimal_and_paren_cases_still_behave():
    assert glm_mod.split_sentences("사업비 1.5억원 규모입니다.") == [
        "사업비 1.5억원 규모입니다.",
    ]
    assert glm_mod.split_sentences("접수 9.30)까지 진행합니다.") == [
        "접수 9.30)까지 진행합니다.",
    ]


def test_ordinary_sentences_still_split():
    assert glm_mod.split_sentences(
        "접수는 9월 22일까지 진행합니다. 지원금은 300만원입니다."
    ) == ["접수는 9월 22일까지 진행합니다.", "지원금은 300만원입니다."]


# ─── 2. 후보 하한 ─────────────────────────────────────────────────────────
def test_a_single_candidate_is_still_offered(
    digest_fixture, tmp_path, monkeypatch
):
    """r7: 하한이 1이다 — 후보가 하나여도 묻는다(`pick: 0` 이 거절 수단)."""
    monkeypatch.setattr(
        glm_mod, "fetch_detail_text",
        lambda url, **kwargs: "접수는 9월 22일까지 진행합니다.",
    )
    _ds_present(monkeypatch, tmp_path)
    seen = _stub_ds_calls(monkeypatch, _auto_respond)

    class Args:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = None

    assert glm_mod.run(Args()) == 0
    payload = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_input.json").read_text(encoding="utf-8")
    )
    assert [len(entry["candidates"]) for entry in payload["items"]] == [1, 1]
    assert payload["no_candidates"] == []
    assert payload["batches"] == [[1, 2]]
    assert any(_is_summary_prompt(prompt) for prompt, _ in seen)
    assert _enrich_line_count(digest_fixture["markdown_path"]) == 2


def test_a_single_candidate_item_is_batched():
    today = "2026-09-13"
    item = _big_item(1, 1)
    assert len(item["candidates"]) == glm_mod.MIN_CANDIDATES_TO_ASK
    batches, skipped = glm_mod.batch_input_items(today, [item])
    assert skipped == []
    assert [[entry["n"] for entry in batch] for batch in batches] == [[1]]


# ─── 3. 후보 품질 필터 ────────────────────────────────────────────────────
def test_a_closing_greeting_is_not_a_candidate():
    """W37 n=1 이 고른 줄 — 공고문 맺음말."""
    detail = (
        "혁신적인 기술과 아이디어를 보유한 창업기업의 많은 관심과 참여 바랍니다. "
        "접수는 9월 22일까지 진행합니다."
    )
    assert _candidates_from(detail) == ["접수는 9월 22일까지 진행합니다."]


def test_a_bare_date_is_not_a_candidate():
    """W37 n=2 가 고른 줄 — 날짜 조각."""
    detail = "2026. 9. 11. 접수는 9월 22일까지 진행합니다."
    candidates = _candidates_from(detail)
    assert "2026. 9. 11." not in candidates


def test_a_numeric_only_sentence_is_not_a_candidate():
    assert _candidates_from("2026 - 09 - 11 (12:00). 접수는 9월 22일까지 진행합니다.") == [
        "접수는 9월 22일까지 진행합니다.",
    ]


@pytest.mark.parametrize("admin", [
    "문의처는 산림청 산림정책과입니다",
    "담당자는 홍길동 주무관입니다",
    "※ 첨부파일을 확인해 주세요",
])
def test_contact_and_attachment_lines_are_not_candidates(admin):
    detail = f"{admin}. 접수는 9월 22일까지 진행합니다."
    assert _candidates_from(detail) == ["접수는 9월 22일까지 진행합니다."]


def test_persona_prompt_tells_glm_what_to_prefer():
    prompt = glm_mod.PERSONA_SYSTEM_PROMPT
    assert "대상·요건·마감·금액·바뀌는 내용" in prompt
    assert "인사말·맺음말" in prompt


def test_nothing_asked_is_not_the_same_as_everything_discarded(
    digest_fixture, tmp_path, monkeypatch
):
    """후보가 없어 묻지 않은 실행을 `discarded` 로 표시하지 않는다 (r6)."""
    monkeypatch.setattr(glm_mod, "fetch_detail_text", lambda url, **kwargs: "")
    _ds_present(monkeypatch, tmp_path)
    _stub_ds_calls(monkeypatch, _auto_respond)

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
    assert warnings_file["discarded"] is False
    assert warnings_file["info"]["no_candidates"] == [1, 2]

    check = check_digest(
        db_path=digest_fixture["db_path"],
        markdown_path=digest_fixture["markdown_path"],
        output_path=None, skip_network=False,
    )
    preview = preview_mod.render_preview(
        W13, digest_fixture["markdown_path"].read_text(encoding="utf-8"), check
    )
    assert "GLM 출력 전체 폐기" not in preview


# ══ V3.1 r7 — 단위는 문장이 아니라 절(clause) ═════════════════════════════
_N8_TEXT = (
    "가. 산림복지서비스제공자 등록 취소 기준 완화 - 산림복지서비스제공자 등록 기준에 "
    "해당되는 인력의 사망·실종 또는 퇴직으로 인하여 등록기준에 미달되는 기간이 60일 "
    "이내인 경우에는 등록 취소 제외 나. 산림복지지구 지정·변경 관련 사전 절차 면제 "
    "사항 규정 - 산림복지지구 면적을 100분의 10 범위에서 변경하는 경우에는 사전에 "
    "관계 행정기관의 장과 협의하지 아니할 수 있도록 함 다. 시행일 규정 - 이 영은 "
    "공포 후 3개월이 경과한 날부터 시행한다"
)


def test_an_enumeration_splits_with_the_marker_attached_to_what_follows():
    """r6 이 `… 등록 취소 제외 나.` 로 끝내던 것이 `나. …` 로 시작하는 단위가 된다."""
    units = glm_mod.candidate_units(_N8_TEXT)
    assert any(unit.startswith("가. 산림복지서비스제공자 등록 취소 기준 완화")
               for unit in units)
    assert any(unit.startswith("나. 산림복지지구 지정·변경 관련 사전 절차 면제")
               for unit in units)
    assert len(units) > 1


def test_no_unit_ends_with_a_bare_list_marker():
    """attach-forward 이므로 `… 나.` 로 끝나는 단위는 생길 수 없다."""
    units = glm_mod.candidate_units(_N8_TEXT)
    for unit in units:
        assert not glm_mod._TRAILING_MARKER_RE.search(unit), unit


def test_a_trailing_marker_on_a_short_sentence_is_stripped():
    """상한 안이라 쪼개지지 않는 문장이 표지로 끝나면 그 표지는 뗀다."""
    units = glm_mod.candidate_units("등록 기준에 미달되는 경우에는 취소를 제외한다 나.")
    assert units == ["등록 기준에 미달되는 경우에는 취소를 제외한다"]


def test_an_ordinary_verb_ending_is_not_mistaken_for_a_marker():
    assert glm_mod.candidate_units("접수는 9월 22일까지 진행합니다.") == [
        "접수는 9월 22일까지 진행합니다.",
    ]


def test_a_long_enumeration_yields_several_units_inside_the_window():
    """r6 이 1,882자 한 덩어리로 버리던 길이대."""
    body = " ".join(
        f"{marker}. 제{index}호 지원 대상 요건을 다음과 같이 완화한다 "
        f"기존 기준에 미달되는 기간이 60일 이내인 경우에는 취소 대상에서 제외한다"
        for index, marker in enumerate("가나다라마바사아자차카타파하", start=1)
    )
    assert len(body) > glm_mod.MAX_CANDIDATE_CHARS * 5
    units = glm_mod.candidate_units(body)
    assert len(units) >= 5
    assert all(
        glm_mod.MIN_CANDIDATE_CHARS <= len(unit) <= glm_mod.MAX_CANDIDATE_CHARS
        for unit in units
    )
    # 모든 단위는 원문의 부분문자열이다
    assert all(unit in body for unit in units)


@pytest.mark.parametrize("marker", [
    "가.", "하.", "ㄱ.", "1.", "99.", "1)", "99)", "①", "⑳", "ㅇ ", "○ ",
    "- ", "• ", "▶", "■", "※", "ㅁ ", "□", "▪", "◦", "▷",
])
def test_every_declared_marker_starts_a_new_unit(marker):
    """표지 쪼개기는 **상한을 넘는 문장**에만 적용된다 — 긴 입력으로 확인한다."""
    head = "머리 문장은 이만큼 길게 적어 둔다 " * 8
    tail = "뒤 조각도 충분히 길게 적어 둔다 여기서부터가 새 단위다"
    body = f"{head}{marker}{tail}"
    assert len(body) > glm_mod.MAX_CANDIDATE_CHARS
    units = glm_mod.candidate_units(body)
    assert any(unit.startswith(marker.strip()) for unit in units), units


def test_a_fragment_that_cannot_be_split_further_is_dropped():
    monster = "가" * 400
    assert glm_mod.candidate_units(monster) == []


def test_a_long_fragment_splits_on_secondary_separators():
    piece = "지원 대상 요건을 완화하여 적용한다 " * 3
    body = f"{piece.strip()} / {piece.strip()} / {piece.strip()}"
    assert len(body) > glm_mod.MAX_CANDIDATE_CHARS
    units = glm_mod.candidate_units(body)
    assert len(units) == 3
    assert all(len(unit) <= glm_mod.MAX_CANDIDATE_CHARS for unit in units)


def test_the_first_eight_units_keep_document_order_and_late_keywords_fill_the_rest():
    plain = [
        f"{index}) 이 문단은 아무 핵심어도 담고 있지 않은 설명 문장이다 여기에 꼬리를 붙인다"
        for index in range(1, 13)
    ]
    keyworded = [
        "13) 지원 대상 요건은 다음과 같이 완화한다 여기에 자세한 설명이 붙는다",
        "14) 접수 기간은 다음과 같이 정한다 여기에 자세한 설명이 붙는다",
    ]
    body = " ".join(plain + keyworded)
    assert len(body) > glm_mod.MAX_CANDIDATE_CHARS
    item = {"title": "제목", "detail_text": body}
    candidates = glm_mod.candidate_sentences(item)

    # 앞 8개는 문서 순서 그대로
    assert [c[:2] for c in candidates[:8]] == [f"{i})" for i in range(1, 9)]
    # 남은 자리는 **뒤쪽의 핵심어 단위**가 가져간다
    assert candidates[8].startswith("13)")
    assert candidates[9].startswith("14)")
    assert len(candidates) == 10


def test_document_order_is_kept_when_twelve_or_fewer_units():
    """12개 이하면 핵심어 정렬을 하지 않는다 — 문서 순서 그대로."""
    filler = "여기에 충분히 긴 설명 문장을 적어 둔다 " * 4
    item = {
        "title": "제목",
        "detail_text": (
            f"1) 아무 핵심어 없는 설명이다 {filler}"
            f"2) 지원 대상 요건을 완화하는 내용이다 {filler}"
        ),
    }
    candidates = glm_mod.candidate_sentences(item)
    assert len(candidates) <= glm_mod.MAX_CANDIDATES
    assert [c[:2] for c in candidates] == ["1)", "2)"]


def test_a_number_inside_a_date_run_is_not_a_list_marker():
    """`2026. 5. 12. 공포` 의 `12.` 를 표지로 보면 `12. 공포, 2026.` 이 생긴다."""
    filler = "여기에 충분히 긴 설명 문장을 적어 둔다 " * 5
    body = (
        f"{filler}「산림복지 진흥에 관한 법률」이 개정(법률 제21620호, "
        f"2026. 5. 12. 공포, 2026. 11. 13. 시행)됨에 따라 규정한다"
    )
    assert len(body) > glm_mod.MAX_CANDIDATE_CHARS
    units = glm_mod.candidate_units(body)
    assert not any(unit.startswith("12.") for unit in units), units
    assert not any(unit.startswith("13.") for unit in units), units


# ══ V3.1 r8 — 글머리 기호 보강 · 두 덩어리 날짜 보호 ══════════════════════
def test_the_new_bullet_markers_split_a_long_line():
    """W37 n=1 의 실제 글머리 기호는 `ㅁ` 였다."""
    body = (
        "여기에 충분히 긴 설명 문장을 적어 둔다 " * 6
        + "ㅁ 모집대상 창업 7년 이내 기업이 해당한다 ㅁ 모집분야 산림 기술 전반"
    )
    assert len(body) > glm_mod.MAX_CANDIDATE_CHARS
    units = glm_mod.candidate_units(body)
    assert "ㅁ 모집대상 창업 7년 이내 기업이 해당한다" in units
    assert "ㅁ 모집분야 산림 기술 전반" in units


@pytest.mark.parametrize("cued,expected", [
    ("접수기간 2026. 9. 7. ~ 9. 30.(수) 15:00까지", "~ 로 이어진 두 덩어리 날짜"),
    ("신청은 9. 30. 까지 받는다", "까지 가 뒤따르는 두 덩어리 날짜"),
])
def test_a_two_group_date_with_a_cue_is_protected(cued, expected):
    assert glm_mod._split_at_markers(cued) == [cued], expected


@pytest.mark.parametrize("plain", [
    "붙임 목록은 1. 2. 순서로 정리되어 있다",
    "제출 서류는 1. 신청서 2. 사업계획서 순이다",
])
def test_bare_list_numbering_is_still_a_marker(plain):
    """단서 없는 `1. 2.` 는 날짜가 아니라 목록 번호다 — 그대로 쪼갠다."""
    assert len(glm_mod._split_at_markers(plain)) > 1


def test_the_w37_n1_line_survives_as_one_unit():
    """r7 에서 `30.(수) …` 로 잘리던 줄이 통째로 한 단위가 된다."""
    body = (
        "여기에 충분히 긴 설명 문장을 적어 둔다 " * 5
        + "ㅁ 모집기간 2026. 9. 7.(월) ~ 9. 30.(수) 15:00까지 "
        + "ㅁ 모집대상 창업 7년 이내 기업"
    )
    units = glm_mod.candidate_units(body)
    assert "ㅁ 모집기간 2026. 9. 7.(월) ~ 9. 30.(수) 15:00까지" in units
    assert not any(unit.startswith("30.") for unit in units), units


# ══ V3.1 r9 (Codex 선택 설계 검토 수렴) ═══════════════════════════════════
# 1. HIGH — 창이 잘리면 마지막 단위를 버린다
# ─────────────────────────────────────────────────────────────────────────
_NEGATION = "사회적기업은 신청 가능하지 않습니다."


def test_anchored_window_reports_whether_it_reached_the_end():
    body = "가" * 30 + _NEGATION
    full = http_fetch.anchored_window(body, "", limit=len(body))
    assert full.truncated is False
    cut = http_fetch.anchored_window(body, "", limit=len(body) - 6)
    assert cut.truncated is True


def test_a_truncated_window_drops_its_last_unit():
    """Codex HIGH: `…신청 가능|하지 않습니다.` 가 `… 신청 가능` 으로 남았다."""
    head = "접수는 9월 22일까지 진행합니다. "
    body = head + _NEGATION
    cut_at = body.index("가능") + 2          # 부정 바로 앞에서 자른다
    truncated = http_fetch.anchored_window(body, "", limit=cut_at)
    assert truncated.truncated is True
    assert truncated.endswith("신청 가능")

    item = {
        "title": "제목",
        "detail_text": str(truncated),
        "detail_truncated": True,
    }
    candidates = glm_mod.candidate_sentences(item)
    assert not any("신청 가능" in unit for unit in candidates), candidates
    assert candidates == ["접수는 9월 22일까지 진행합니다."]


def test_an_untruncated_window_keeps_its_last_unit():
    item = {
        "title": "제목",
        "detail_text": "접수는 9월 22일까지 진행합니다. " + _NEGATION,
        "detail_truncated": False,
    }
    assert _NEGATION in glm_mod.candidate_sentences(item)


# ─── 2. HIGH — 지수·취소선은 평탄화하면 뜻이 바뀐다 ───────────────────────
def test_superscript_markup_makes_a_unit_ineligible():
    """Codex HIGH: `10<sup>4</sup>㎡` → `104㎡` 로 통과했다."""
    text = http_fetch.visible_text(
        "<article>지원 면적은 10<sup>4</sup>㎡입니다. 접수는 9월 22일까지 진행합니다.</article>"
    )
    assert http_fetch.MARKED_SENTINEL in text
    candidates = glm_mod.candidate_sentences(
        {"title": "제목", "detail_text": str(text)}
    )
    assert candidates == ["접수는 9월 22일까지 진행합니다."]
    assert glm_mod.marked_unit_count(str(text)) == 1


@pytest.mark.parametrize("tag", ["sup", "sub", "del", "s", "strike"])
def test_struck_and_scripted_text_never_becomes_evidence(tag):
    text = http_fetch.visible_text(
        f"<article>신청은 <{tag}>불가</{tag}> 합니다 여기까지가 한 문장이다."
        "접수는 9월 22일까지 진행합니다.</article>"
    )
    candidates = glm_mod.candidate_sentences(
        {"title": "제목", "detail_text": str(text)}
    )
    assert all(http_fetch.MARKED_SENTINEL not in unit for unit in candidates)
    assert all("불가" not in unit for unit in candidates), candidates


# ─── 3. MEDIUM — 적용 파일은 입력 지문을 들고 와야 한다 ───────────────────
def test_an_apply_file_with_a_stale_input_sha_is_refused(
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
    stale = tmp_path / "stale.json"
    stale.write_text(_apply_file(payload, input_sha="0" * 64), encoding="utf-8")

    class ApplyArgs:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = str(stale)

    assert glm_mod.run(ApplyArgs()) == 0
    assert _enrich_line_count(markdown_path) == 0
    warnings_file = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_warnings.json").read_text(
            encoding="utf-8")
    )
    assert any("input_sha" in w for w in warnings_file["warnings"])


def test_the_input_json_records_exactly_what_was_sent(digest_fixture):
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
    sent = [entry["n"] for entry in payload["items"]]
    assert sent == [n for batch in payload["batches"] for n in batch]
    assert len(payload["input_sha"]) == 64


def test_batches_signature_changes_when_the_candidates_change():
    today = "2026-09-13"
    first = [[_big_item(1, 3)]]
    second = [[_big_item(1, 4)]]
    assert glm_mod.batches_signature(today, first) != glm_mod.batches_signature(
        today, second
    )


# ─── 4. MEDIUM — 중단 창 복구 (.bak) ──────────────────────────────────────
def test_a_leftover_bak_is_restored_before_anything_else(
    digest_fixture, tmp_path, monkeypatch
):
    """md 를 쓴 뒤 정본을 쓰기 전에 죽은 실행을 다음 실행이 되돌린다."""
    markdown_path = digest_fixture["markdown_path"]
    original = markdown_path.read_text(encoding="utf-8")

    backup = markdown_path.with_name(markdown_path.name + ".bak")
    backup.write_text(original, encoding="utf-8")
    markdown_path.write_text(
        original.replace("## ✅", "## ✅ 손상된 본문"), encoding="utf-8"
    )
    assert markdown_path.read_text(encoding="utf-8") != original

    monkeypatch.setattr(glm_mod, "DS_BIN", tmp_path / "no-such-ds")

    class Args:
        week = W13
        db = digest_fixture["db_path"]
        out_dir = str(digest_fixture["out_dir"])
        dry_run = False
        apply_json = None

    assert glm_mod.run(Args()) == 0
    assert markdown_path.read_text(encoding="utf-8") == original
    assert not backup.exists()
    warnings_file = json.loads(
        (digest_fixture["out_dir"] / f"{W13}.glm_warnings.json").read_text(
            encoding="utf-8")
    )
    assert any(".bak" in w for w in warnings_file["warnings"])
    result = check_digest(
        db_path=digest_fixture["db_path"], markdown_path=markdown_path,
        output_path=None, skip_network=False,
    )
    assert result["pass"], result.get("reason")
    assert result["manifest_problems"] == []


# ─── 5. MEDIUM — Codex 가 든 두 덩어리 날짜 문자열 ────────────────────────
def test_the_codex_two_group_date_string_stays_one_unit():
    line = "가. 접수기간: 2026. 9. 1. ~ 9. 30. (수) 18시까지 온라인 신청"
    assert glm_mod._split_at_markers(line) == [line]


def test_the_codex_date_string_inside_a_long_sentence_is_not_chopped():
    body = (
        "여기에 충분히 긴 설명 문장을 적어 둔다 " * 5
        + "가. 접수기간: 2026. 9. 1. ~ 9. 30. (수) 18시까지 온라인 신청 "
        + "나. 제출 서류는 신청서와 사업계획서다"
    )
    units = glm_mod.candidate_units(body)
    assert "가. 접수기간: 2026. 9. 1. ~ 9. 30. (수) 18시까지 온라인 신청" in units
    assert not any(unit.startswith("30.") for unit in units), units


# ─── 6. LOW — 뒤늦은 관련 문장이 살아남는다 ───────────────────────────────
def test_a_late_keyword_unit_survives_the_cap():
    plain = [
        f"{index}) 이 문단은 회의실 위치를 안내하는 문장이며 핵심어가 없다"
        for index in range(1, 13)
    ]
    late = "13) 신청 자격 요건은 다음과 같이 완화한다 여기에 설명이 붙는다"
    item = {"title": "제목", "detail_text": " ".join(plain + [late])}

    candidates = glm_mod.candidate_sentences(item)

    assert candidates[-1].startswith("13)")
    assert [c[:2] for c in candidates[:8]] == [f"{i})" for i in range(1, 9)]


def test_the_codex_thirteen_sentence_case_is_documented():
    """Codex LOW 재현 — 앞 12문장이 모두 핵심어를 담으면 13번째는 여전히 밀린다.

    앞 8개는 문서 순서로 보장되고 남은 4자리는 9~12번이 가져간다. 이 순서는
    결정론이지만 **관련성 보장은 아니다** — 보고서에 그대로 적는다.
    """
    plain = [
        f"{index}) 지원센터 제1회의실 위치를 안내합니다 여기에 설명이 붙는다"
        for index in range(1, 13)
    ]
    late = "13) 사회적기업은 이번 사업에 참여할 수 없습니다 여기에 설명이 붙는다"
    item = {"title": "제목", "detail_text": " ".join(plain + [late])}

    candidates = glm_mod.candidate_sentences(item)

    assert not any(c.startswith("13)") for c in candidates)
    assert len(candidates) == glm_mod.MAX_CANDIDATES
    assert all(
        candidate.startswith(f"{index})")
        for index, candidate in enumerate(candidates, start=1)
    )


# ─── 7. LOW — 초안은 사람만 보는 주석이다 (문서화) ────────────────────────
def test_the_checker_threat_model_states_the_headline_draft_is_comment_only():
    from alert.digest import checker as checker_mod

    doc = checker_mod.__doc__ or ""
    assert "GLM 초안" in doc
    assert "주석" in doc
    assert "apply_commentary.py" in doc
