"""GLM 야간 요약 레인(V3) — 게이트·보강 줄 부착 테스트."""

import json
import sqlite3

import pytest

from alert.digest import blocks as blocks_mod
from alert.digest.checker import check_digest
from alert.digest.composer import compose_digest, load_items_manifest
from scripts import glm_enrich as glm_mod

W13 = "2026-W13"
W13_CREATED_AT = "2026-03-26T12:00:00"


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


def test_gate_output_missing_n_is_skipped_with_warning():
    items = [_one_input_item(n=1), _one_input_item(n=2, id=47)]
    raw = json.dumps([{
        "n": 1, "대상 태그": "전체", "마감": "원문 확인",
        "자격": "원문 확인", "금액": "원문 확인", "한 줄 의미": "확인 가능",
    }], ensure_ascii=False)

    results, warnings = glm_mod.gate_output(raw, items)

    assert set(results) == {1}
    assert any("n=[2]" in w or "[2]" in w for w in warnings)


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
        "n", "title", "source_name", "summary",
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
            "한 줄 의미": f"확인 필요 {entry['n']}",
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
    assert "  → 확인 필요 1" in new_markdown

    # 항목 줄·원문 줄은 정본과 여전히 동일 문자열(편집 불가 영역 불변)
    refreshed_items = _manifest_items(markdown_path)
    for entry in refreshed_items:
        assert entry["line"] == original_lines[entry["id"]]
        assert entry["origin_line"] == original_origins[entry["id"]]
        assert entry["enrich_line"] == blocks_mod.enrich_line_text(
            f"확인 필요 {id_to_n[entry['id']]}"
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
