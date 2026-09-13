"""월간 종합호 (P1' 계약) 테스트.

계약 조항별로 묶었다:
  §1 호 식별자 — 키 형식·경로·두 호 동시 운영
  §2 조립      — 창·선별·상한·다양성·렌더·정본 구조
  §3 게이트    — checker/prune/preview 가 그대로 작동하는가
"""

import json
import sqlite3

import pytest

from alert.digest import blocks as blocks_mod
from alert.digest import preview as preview_mod
from alert.digest import prune as prune_mod
from alert.digest import sections as sections_mod
from alert.digest import state as state_mod
from alert.digest.checker import check_digest
from alert.digest.composer import (
    HOLD_REASON_DIVERSITY,
    HOLD_REASON_SECTION_CAP,
    KIND_MONTHLY,
    KIND_WEEKLY,
    MARKER,
    MEDIA_MARKER,
    MONTHLY_FOOTER_LINE,
    MONTHLY_HOLD_ACTIONABLE,
    MONTHLY_HOLD_DEADLINE,
    MONTHLY_HOLD_DEADLINE_UNKNOWN,
    MONTHLY_HOLD_NOT_ELIGIBLE,
    MONTHLY_HOLD_NOT_MONTHLY,
    MONTHLY_HOLD_NO_COUNCIL_MATCH,
    MONTHLY_ITEM_SECTIONS,
    MONTHLY_RESCUE_MEDIA_REASON,
    MONTHLY_RESCUE_PRESS_REASON,
    MONTHLY_RULE_MEDIA,
    MONTHLY_RULE_NOTICE,
    MONTHLY_RULE_PRESS,
    SECTION_HEADINGS,
    SECTION_LIMITS,
    SECTION_NOTICE,
    SOURCE_DIVERSITY_LIMIT,
    VERDICT_MONTHLY,
    VERDICT_NOTICE,
    _sort_monthly,
    compose_digest,
    compose_digest_data,
    get_month_date_range,
    deadline_cue,
    issue_kind,
    item_line,
    monthly_rule,
    load_items_manifest,
    source_display_name,
)
from tests.test_digest import _create_announcements_table

M09 = "2026-M09"
MONTHLY_HEADING = SECTION_HEADINGS[VERDICT_MONTHLY]

# 월간호 후보의 표준형: 협의회 소스 + 산림 관련성 + 제도 신호 + 마감 없음.
# (마감이 있으면 §2 규칙에 따라 주간호 몫으로 내려간다.)
MONTHLY_ROWS = (
    ("forest_press", "산림 사회적기업 성장 정책 방향 발표"),
    ("kofpi", "임업 사회적협동조합 통계 기본계획 발표"),
    ("fowi", "산림복지 사회적기업 제도 개선 계획 발표"),
    ("coop", "산림 협동조합 육성 정책 발표"),
    ("socialenterprise", "사회적기업 산림 분야 시행 제도 발표"),
    ("seis", "사회적협동조합 산촌 정책 기본계획 발표"),
)


def _insert(db_path, source, title, *, source_id=None, url=None,
            period_end=None, period_start=None, created_at="2026-09-10T12:00:00",
            summary="요약", raw_data="", council_only=None, **extra):
    """공고 1건 삽입. `extra` 는 라운드 2의 적재 근거 열(council_match·kind 등)."""
    source_id = source_id or f"{source}-{abs(hash(title)) % 10 ** 8}"
    url = url or f"https://example.com/{source_id}"
    conn = sqlite3.connect(str(db_path))
    columns = ["source", "source_id", "title", "summary", "url", "author",
               "period_start", "period_end", "raw_data", "relevance_score",
               "created_at", "updated_at"]
    values = [source, source_id, title, summary, url, "기관", period_start,
              period_end, raw_data, 0.9, created_at, created_at]
    if council_only is not None:
        columns.append("council_only")
        values.append(council_only)
    for name, value in extra.items():
        columns.append(name)
        values.append(value)
    conn.execute(
        "INSERT INTO announcements ({}) VALUES ({})".format(
            ", ".join(columns), ", ".join("?" * len(columns))),
        values,
    )
    conn.commit()
    conn.close()
    return url


# 라운드 2 의 적재 근거 열 — media 레인이 머지되기 전 DB 에도 붙여 쓸 수 있다.
ROUND2_COLUMNS = (
    ("council_score", "REAL DEFAULT NULL"),
    ("council_match", "INTEGER DEFAULT NULL"),
    ("council_only", "INTEGER DEFAULT 0"),
    ("kind", "TEXT DEFAULT 'gonggo'"),
)


def _round2_db(tmp_path, name="round2.db"):
    """적재 근거 열까지 갖춘 빈 DB (P1-R 레인의 media 행 모양을 재현할 수 있다)."""
    db = tmp_path / name
    _create_announcements_table(db)
    conn = sqlite3.connect(str(db))
    for column, decl in ROUND2_COLUMNS:
        conn.execute(
            f"ALTER TABLE announcements ADD COLUMN {column} {decl}")
    conn.commit()
    conn.close()
    return db


MEDIA_SUMMARY = "피드가 준 기사 요약 본문이며 어떤 렌더에도 실려서는 안 된다."


def _media_raw(posted="2026-09-10", summary=MEDIA_SUMMARY,
               publisher="라이프인", link="https://lifein.news/1"):
    """P1-R 보고서 §7 이 정한 media `raw_data` 모양 (화이트리스트 7키)."""
    return json.dumps({
        "title": "산림 사회적기업 현장 기사",
        "link": link,
        "pubDate": "Wed, 10 Sep 2026 09:00:00 +0900",
        "posted": posted,
        "summary": summary,
        "publisher": publisher,
        "license_note": "headline+link only",
    }, ensure_ascii=False)


def _insert_media(db, title="산림 사회적기업 현장 기사", *, source="lifein",
                  council_match=1, posted="2026-09-10", **kwargs):
    """media 행 1건 — kind='media', council_only=1 (P1-R 레인의 실제 모양)."""
    return _insert(
        db, source, title,
        summary=MEDIA_SUMMARY,
        raw_data=_media_raw(posted=posted),
        council_only=1, council_match=council_match, kind="media",
        **kwargs,
    )


def _monthly_db(tmp_path, rows=MONTHLY_ROWS, **kwargs):
    db = tmp_path / "monthly.db"
    _create_announcements_table(db)
    for source, title in rows:
        _insert(db, source, title, **kwargs)
    return db


def _compose(db, issue=M09, **kwargs):
    return compose_digest_data(db_path=str(db), week_str=issue, **kwargs)


# ══ §1 호 식별자 ══════════════════════════════════════════════════════════
@pytest.mark.parametrize("key,kind", [
    ("2026-M01", state_mod.KIND_MONTHLY),
    ("2026-M12", state_mod.KIND_MONTHLY),
    ("2026-W37", state_mod.KIND_WEEKLY),
    ("2026-M00", None),
    ("2026-M13", None),
    ("2026-m09", None),
    ("2026-M9", None),
    ("2026-M09\n", None),
    ("", None),
    (None, None),
])
def test_issue_kind_is_strict(key, kind):
    assert state_mod.issue_kind(key) is kind
    assert state_mod.valid_issue(key) is (kind is not None)
    assert state_mod.valid_week(key) is (kind is not None)


def test_issue_paths_use_the_key_verbatim(tmp_path):
    """§1: md·lock·state·broken 이 전부 호 키를 그대로 쓴다."""
    assert state_mod.state_path(M09, tmp_path).name == f"{M09}.state.json"
    assert state_mod.lock_path(M09, tmp_path).name == f"{M09}.lock"
    assert state_mod.tombstone_path(M09, tmp_path).name == f"{M09}.broken"
    assert state_mod.week_from_markdown(tmp_path / f"{M09}.md") == M09


def test_require_week_rejects_path_traversal():
    with pytest.raises(ValueError):
        state_mod.state_path("../2026-M09")


@pytest.mark.parametrize("issue,expected", [
    ("2026-M01", ("2026-01-01", "2026-01-31")),
    ("2026-M02", ("2026-02-01", "2026-02-28")),
    ("2024-M02", ("2024-02-01", "2024-02-29")),
    ("2026-M09", ("2026-09-01", "2026-09-30")),
    ("2026-M12", ("2026-12-01", "2026-12-31")),
])
def test_month_date_range(issue, expected):
    assert get_month_date_range(issue) == expected


def test_compose_window_is_the_month(tmp_path):
    """§2: 그 달에 posted 된 것만 후보다 (앞뒤 달은 창 밖)."""
    db = tmp_path / "window.db"
    _create_announcements_table(db)
    inside = _insert(db, "forest_press", "산림 사회적기업 정책 방향 발표",
                     source_id="in", created_at="2026-09-30T23:00:00")
    _insert(db, "forest_press", "산림 사회적기업 제도 개선 발표",
            source_id="before", created_at="2026-08-31T23:00:00")
    _insert(db, "forest_press", "산림 사회적기업 시행 계획 발표",
            source_id="after", created_at="2026-10-01T00:30:00")
    data = _compose(db)
    urls = [item["url"] for item in data["sections"][VERDICT_MONTHLY]]
    assert urls == [inside]


# ══ §2 선별 규칙 ══════════════════════════════════════════════════════════
def test_actionable_items_go_to_the_weekly_issue(tmp_path):
    """§2: 주간호 판정이 `신청하세요` 면 월간호에 싣지 않는다."""
    db = tmp_path / "apply.db"
    _create_announcements_table(db)
    _insert(db, "kofpi", "산림 분야 지원사업 참여기업 모집 공고",
            source_id="apply", period_end=None)
    data = _compose(db)
    assert data["sections"][VERDICT_MONTHLY] == []
    assert [hold["reason"] for hold in data["holds"]] == [
        MONTHLY_HOLD_ACTIONABLE]


def test_items_with_a_deadline_go_to_the_weekly_issue(tmp_path):
    """§2: 마감이 있으면(의견 마감·마감 있는 행사) 주간호 몫이다."""
    db = tmp_path / "deadline.db"
    _create_announcements_table(db)
    _insert(db, "lawmaking", "산림 사회적기업 지원 조례 입법예고",
            source_id="notice", period_end="2026-09-25")
    data = _compose(db)
    assert data["sections"][VERDICT_MONTHLY] == []
    assert [hold["reason"] for hold in data["holds"]] == [
        MONTHLY_HOLD_DEADLINE]


def test_deadline_free_notice_is_monthly(tmp_path):
    """§2: 마감 없는 제도·정책 소식은 월간호가 싣는다."""
    db = tmp_path / "notice.db"
    _create_announcements_table(db)
    url = _insert(db, "lawmaking", "산림 사회적기업 지원 조례 입법예고",
                  source_id="notice", period_end=None)
    data = _compose(db)
    assert [item["url"] for item in data["sections"][VERDICT_MONTHLY]] == [url]


def test_monthly_cap_is_five(tmp_path):
    """§2: 상한 5건 — 초과분은 버리지 않고 보류 주석으로 남는다."""
    rows = MONTHLY_ROWS + (
        ("bizinfo", "사회적기업 산림 분야 지원 정책 발표"),
        ("mois_sse", "마을기업 산림 자원 제도 발표"),
    )
    data = _compose(_monthly_db(tmp_path, rows))
    assert SECTION_LIMITS[VERDICT_MONTHLY] == 5
    assert len(data["sections"][VERDICT_MONTHLY]) == 5
    assert HOLD_REASON_SECTION_CAP in {hold["reason"] for hold in data["holds"]}


def test_monthly_source_diversity_limit(tmp_path):
    """§2: 같은 소스(기관) 최대 2건 — 3번째부터 다양성 사유로 보류."""
    rows = tuple(
        ("forest_press", f"산림 사회적기업 정책 방향 {n}차 발표")
        for n in range(1, 5)
    )
    data = _compose(_monthly_db(tmp_path, rows))
    selected = data["sections"][VERDICT_MONTHLY]
    assert len(selected) == SOURCE_DIVERSITY_LIMIT == 2
    assert HOLD_REASON_DIVERSITY in {hold["reason"] for hold in data["holds"]}


def test_council_only_rows_are_candidates_for_monthly(tmp_path):
    """§2: 월간호는 관찰 모드 가드의 예외다 (주간호는 그대로 제외한다)."""
    db = tmp_path / "council_only.db"
    _create_announcements_table(db)
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE announcements ADD COLUMN council_only INTEGER DEFAULT 0")
    conn.commit()
    conn.close()
    url = _insert(db, "mois_sse", "마을기업 산림 자원 정책 방향 발표",
                  source_id="council", council_only=1,
                  created_at="2026-09-10T12:00:00")

    monthly = _compose(db)
    assert [item["url"] for item in monthly["sections"][VERDICT_MONTHLY]] == [url]

    weekly = compose_digest_data(db_path=str(db), week_str="2026-W37")
    assert weekly["candidate_ids"] == []


def test_pin_restores_an_item_the_monthly_filter_dropped(tmp_path):
    """§2: `핀 n` 은 월간 선별에서 빠진 항목을 되살린다 (섹션은 하나뿐)."""
    db = tmp_path / "pin.db"
    _create_announcements_table(db)
    _insert(db, "kofpi", "산림 분야 지원사업 참여기업 모집 공고",
            source_id="apply", period_end=None)
    dropped = _compose(db)["holds"][0]
    data = _compose(db, pin_ids={dropped["id"]})
    selected = data["sections"][VERDICT_MONTHLY]
    assert [item["id"] for item in selected] == [dropped["id"]]
    assert selected[0]["pinned"] is True


# ══ §2 렌더·정본 구조 ═════════════════════════════════════════════════════
def test_monthly_markdown_header_and_headline(tmp_path):
    """§2: 헤더 `📚 협의회 월간 종합 2026년 9월` · "이번 달 한 줄" 마커는 주간과 동일."""
    markdown = compose_digest(db_path=str(_monthly_db(tmp_path)), week_str=M09)
    assert markdown.splitlines()[2] == "# 📚 협의회 월간 종합 2026년 9월"
    assert f"이번 달 한 줄: {MARKER}" in markdown
    assert "이번 주 한 줄" not in markdown
    assert f"## {MONTHLY_HEADING}" in markdown
    assert "✅ 신청하세요" not in markdown
    assert "👀 알아두세요" not in markdown


def test_monthly_item_block_is_title_and_link(tmp_path):
    """§2: 항목은 제목 줄 + 링크 (마감 표기 없음 — 마감 없는 소식만 싣는다)."""
    markdown = compose_digest(db_path=str(_monthly_db(tmp_path)), week_str=M09)
    parsed = blocks_mod.parse_blocks(markdown)
    items = [block for block in parsed if block["kind"] == "item"]
    assert len(items) == 5
    for block in items:
        assert "마감" not in block["title"]
        assert block["url"].startswith("https://")
        assert block["lines"][2].startswith("  [원문](")


def test_monthly_writes_the_same_canonical_files(tmp_path):
    """§2: items.json·kakao.txt 가 주간호와 같은 정본 구조로 나온다."""
    out = tmp_path / "digests"
    out.mkdir()
    md = out / f"{M09}.md"
    compose_digest(db_path=str(_monthly_db(tmp_path)), week_str=M09,
                   output_path=md)
    manifest = load_items_manifest(md)
    assert manifest["week"] == M09
    assert len(manifest["items"]) == 5
    assert {entry["section"] for entry in manifest["items"]} == {VERDICT_MONTHLY}
    kakao = (out / f"{M09}.kakao.txt").read_text(encoding="utf-8")
    assert "📚 협의회 월간 종합 2026년 9월" in kakao
    assert "이번 달 한 줄: " in kakao


def test_monthly_holds_carry_pin_coordinates(tmp_path):
    """§2: 보류 주석 구조(번호·사유·id)가 주간호와 같다 — `핀 n` 좌표가 산다."""
    rows = MONTHLY_ROWS + (("kofpi", "산림 분야 지원사업 참여기업 모집 공고"),)
    markdown = compose_digest(db_path=str(_monthly_db(tmp_path, rows)),
                              week_str=M09)
    holds = preview_mod.parse_digest(markdown)["holds"]
    assert holds
    assert all(hold["id"] is not None for hold in holds)
    assert [hold["number"] for hold in holds] == list(
        range(1, len(holds) + 1))


# ══ §2·§3 검증 경로 (checker·prune·preview) ═══════════════════════════════
def _composed(tmp_path, rows=MONTHLY_ROWS):
    out = tmp_path / "digests"
    out.mkdir(exist_ok=True)
    md = out / f"{M09}.md"
    compose_digest(db_path=str(_monthly_db(tmp_path, rows)), week_str=M09,
                   output_path=md)
    return md


def test_checker_passes_a_monthly_digest(tmp_path):
    """§3: checker 가 월간호를 항목 섹션으로 알아보고 통과시킨다."""
    db = tmp_path / "monthly.db"
    md = _composed(tmp_path)
    result = check_digest(db_path=str(db), markdown_path=md,
                          output_path=md.with_suffix(".check.json"))
    assert result["pass"] is True, result["reason"]
    assert result["item_sections"] == [MONTHLY_HEADING]
    assert result["item_blocks"] == 5
    assert result["manifest_problems"] == []


def test_monthly_cap_is_known_to_the_gate(tmp_path):
    """§3: 게이트가 월간 섹션의 상한을 안다 (6건짜리 본문은 상한 위반)."""
    text = _composed(tmp_path).read_text(encoding="utf-8")
    assert sections_mod.caps_by_heading([MONTHLY_HEADING]) == {
        MONTHLY_HEADING: 5}
    forged = text.replace(
        f"## {MONTHLY_HEADING}\n\n",
        f"## {MONTHLY_HEADING}\n\n" + "\n".join([
            "<!-- item id=999 -->",
            "위조 항목 — 산림청",
            "  [원문](https://example.com/forged)",
            "",
        ]),
    )
    assert blocks_mod.cap_violations(forged) == [(MONTHLY_HEADING, 6, 5)]


def test_checker_rejects_a_forged_monthly_item(tmp_path):
    """§3: 손으로 끼워 넣은 항목은 정본 대조에서 막힌다 (fail-closed)."""
    md = _composed(tmp_path)
    text = md.read_text(encoding="utf-8")
    block = "\n".join([
        "<!-- item id=999 -->",
        "위조 항목 — 산림청",
        "  [원문](https://example.com/forged)",
        "",
    ])
    md.write_text(text.replace(f"## {MONTHLY_HEADING}\n\n",
                               f"## {MONTHLY_HEADING}\n\n{block}"),
                  encoding="utf-8")
    result = check_digest(db_path=str(tmp_path / "monthly.db"),
                          markdown_path=md, output_path=None)
    assert result["pass"] is False
    assert MONTHLY_HEADING in result["reason"] or "정본" in result["reason"]


def test_prune_counts_monthly_items(tmp_path):
    """§3: prune 의 항목 계수·상한 판정이 월간 섹션에도 걸린다."""
    text = _composed(tmp_path).read_text(encoding="utf-8")
    assert prune_mod.item_block_count(text) == 5
    assert prune_mod.section_block_counts(text) == {MONTHLY_HEADING: 5}
    assert prune_mod.cap_violations(text) == []


def test_preview_renders_a_monthly_issue(tmp_path):
    """§3: 미리보기가 월간호임을 머리글로 알리고 번호 좌표를 만든다."""
    text = _composed(tmp_path).read_text(encoding="utf-8")
    body = preview_mod.render_preview(M09, text, {"pass": True})
    assert body.startswith(f"🏛 협의회 월간 종합 {M09}")
    assert "기간 9/1~9/30" in body
    assert "상태: 해설 대기 (이번 달 한 줄 미확정)" in body
    assert "  상단: <한 줄> — 이번 달 한 줄 확정" in body
    assert preview_mod.item_urls(text) == [
        item for item in blocks_mod.item_urls(text)
    ]


def test_sections_resolution_is_exact_match(tmp_path):
    """§3: 월간 헤딩은 항목 섹션, 주간 본문의 목록은 그대로다 (조용한 오분류 없음)."""
    monthly = _composed(tmp_path).read_text(encoding="utf-8")
    assert sections_mod.classify(monthly)[0] == [MONTHLY_HEADING]

    weekly_db = tmp_path / "weekly.db"
    _create_announcements_table(weekly_db)
    weekly = compose_digest(db_path=str(weekly_db), week_str="2026-W37")
    assert sections_mod.classify(weekly)[0] == list(
        sections_mod.V2_ITEM_SECTIONS)


# ══ §1 두 호 동시 운영 ════════════════════════════════════════════════════
def test_two_open_issues_are_independent(tmp_path):
    """§1: 같은 주에 주간호·월간호가 공존해도 상태·승인·좌표가 서로 독립이다."""
    out = tmp_path / "digests"
    out.mkdir()
    db = _monthly_db(tmp_path)
    # 두 호를 같은 digests/ 에 만든다
    compose_digest(db_path=str(db), week_str=M09, output_path=out / f"{M09}.md")
    compose_digest(db_path=str(db), week_str="2026-W37",
                   output_path=out / "2026-W37.md")
    assert (out / f"{M09}.md").exists() and (out / "2026-W37.md").exists()
    assert (out / f"{M09}.items.json").exists()
    assert (out / "2026-W37.items.json").exists()

    weekly_state = state_mod.state_path("2026-W37", out)
    monthly_state = state_mod.state_path(M09, out)
    assert weekly_state != monthly_state
    assert state_mod.lock_path("2026-W37", out) != state_mod.lock_path(M09, out)

    # 각자 draft 로 시작한다
    for path, issue in ((weekly_state, "2026-W37"), (monthly_state, M09)):
        state_mod.save_state(path, state_mod.default_state(issue))
        assert state_mod.load_state(path, issue)["status"] == "draft"

    # 한쪽에 미리보기·승인 세대를 발급한다
    state_mod.save_state(monthly_state, state_mod.record_preview(
        state_mod.load_state(monthly_state, M09), [111], ["https://a"],
        approval_sha="sha-monthly"))
    # 다른 쪽은 그대로다 (승인 없음·좌표 없음·draft)
    weekly = state_mod.load_state(weekly_state, "2026-W37")
    assert state_mod.approval_of(weekly) == {}
    assert weekly["preview_message_ids"] == []
    assert weekly["status"] == "draft"

    # 한쪽 발송 완료가 다른 쪽 상태를 건드리지 않는다
    monthly = state_mod.load_state(monthly_state, M09)
    monthly = state_mod.mark_sending(monthly, "2026-10-01T00:00:00")
    monthly = state_mod.mark_sent(monthly, "상민", 30, "2026-10-01T00:01:00")
    state_mod.save_state(monthly_state, monthly)
    assert state_mod.load_state(monthly_state, M09)["status"] == "sent"
    assert state_mod.load_state(weekly_state, "2026-W37")["status"] == "draft"
    assert state_mod.can_send(
        state_mod.load_state(weekly_state, "2026-W37"))[0] is True


# ══ 스크립트 (monthly_digest.py) ══════════════════════════════════════════
def test_monthly_digest_dry_run_writes_nothing(tmp_path, capsys):
    """§산출: `--dry-run` 은 파일을 쓰지 않고 선정 표만 찍는다."""
    from scripts import monthly_digest

    out = tmp_path / "digests"
    out.mkdir()
    db = _monthly_db(tmp_path)
    selected, holds, stats = monthly_digest.dry_run_rows(str(db), M09)
    table = monthly_digest.format_dry_run(M09, selected, holds, stats)
    assert "[DRY-RUN] 2026-M09" in table
    assert len(selected) == 5
    assert list(out.iterdir()) == []


def test_monthly_digest_rejects_a_week_key(tmp_path, monkeypatch, capsys):
    from scripts import monthly_digest

    monkeypatch.setattr("sys.argv", ["monthly_digest.py", "2026-W37"])
    assert monthly_digest.main() == 2
    assert "월간호 키 형식이 아닙니다" in capsys.readouterr().err


@pytest.mark.parametrize("today,expected", [
    ((2026, 10, 1), "2026-M09"),
    ((2026, 1, 5), "2025-M12"),
    ((2026, 3, 7), "2026-M02"),
])
def test_previous_month_key(today, expected):
    from datetime import date

    from scripts import monthly_digest

    assert monthly_digest.previous_month_key(date(*today)) == expected


def test_monthly_job_script_shape(tmp_path):
    """§2: 잡의 순서·가드·시간대가 스크립트 안에 있다."""
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "monthly_job.sh"
    source = script.read_text(encoding="utf-8")
    assert 'export TZ="Asia/Seoul"' in source      # 라운드 3: 시간대 고정
    assert '"+%u"' in source and '"+%d"' in source  # 요일 AND 일자
    assert "10#" in source          # 08·09 를 8진수로 읽지 않는다
    assert "monthly_digest.py" in source
    assert "glm_enrich.py" in source
    assert "recheck_digest.py" in source
    assert "notify_digest.py" in source
    # 순서는 digest_job.sh 와 같아야 한다
    order = [source.index(name) for name in (
        "monthly_digest.py", "glm_enrich.py", "recheck_digest.py",
        "notify_digest.py")]
    assert order == sorted(order)
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


def test_monthly_plist_is_a_draft_and_not_loaded():
    """§2: plist 초안은 목요일 23:00 · 로드는 사람이 한다."""
    import plistlib
    from pathlib import Path

    path = (Path(__file__).resolve().parent.parent / "launchd"
            / "com.gonggo-radar.monthly.plist")
    data = plistlib.loads(path.read_bytes())
    assert data["Label"] == "com.gonggo-radar.monthly"
    assert data["RunAtLoad"] is False
    assert data["StartCalendarInterval"] == [
        {"Hour": 23, "Minute": 0, "Weekday": 4}]
    assert data["ProgramArguments"][-1].endswith("scripts/monthly_job.sh")


def test_council_profile_includes_imdo():
    """§0: 레인 A 갭 — `임도` 가 협의회 적재 어휘에 있다."""
    from alert.config import get_config

    profile = get_config(reload=True).council_profile
    assert "임도" in profile.must_match


def test_composer_issue_kind_defaults_to_weekly():
    assert issue_kind("2026-M09") == KIND_MONTHLY
    assert issue_kind("2026-W37") == KIND_WEEKLY
    assert issue_kind(None) == KIND_WEEKLY
    assert tuple(MONTHLY_ITEM_SECTIONS) == (VERDICT_MONTHLY,)


def test_items_manifest_roundtrip_is_json(tmp_path):
    out = tmp_path / "digests"
    out.mkdir()
    md = out / f"{M09}.md"
    compose_digest(db_path=str(_monthly_db(tmp_path)), week_str=M09,
                   output_path=md)
    payload = json.loads((out / f"{M09}.items.json").read_text(encoding="utf-8"))
    assert payload["week"] == M09
    assert all(entry["section"] == VERDICT_MONTHLY
               for entry in payload["items"])


# ══ 라운드 2 §1 — 월간 후보 규칙 ═══════════════════════════════════════════
@pytest.mark.parametrize("title,expected_reason", [
    # 참가자 모집(B2C): 제목에 B2C 신호만 있고 사업자 신호가 없다
    ("산림복지 취업아카데미 참가 모집", "참가자 모집(B2C)"),
    # 노이즈 의심: 노이즈 어휘 + 관련성 + 기회 신호가 함께 있을 때의 주간 판정
    ("산림 사회적기업 수상 기념 지원사업 공모", "노이즈 의심"),
])
def test_noise_and_b2c_holds_are_not_monthly(tmp_path, title, expected_reason):
    """§1: 주간호가 노이즈·B2C 로 내려놓은 것은 월간호 지면에도 올리지 않는다."""
    db = _round2_db(tmp_path)
    _insert(db, "fowi", title, source_id="drop", period_end=None)
    weekly_reason = compose_digest_data(
        db_path=str(db), week_str="2026-W37")["holds"][0]["reason"]
    assert weekly_reason.startswith(expected_reason)

    data = _compose(db)
    assert data["sections"][VERDICT_MONTHLY] == []
    assert [hold["reason"] for hold in data["holds"]] == [
        MONTHLY_HOLD_NOT_MONTHLY]


def test_press_source_row_is_rescued_for_monthly(tmp_path):
    """§1(b): 협의회 소스 풀 밖(mafra)의 보도·정책 행을 월간호가 되살린다."""
    db = _round2_db(tmp_path)
    url = _insert(db, "mafra", "농림축산식품 정책 방향 발표", source_id="mafra1",
                  period_end=None, council_match=1, council_only=1)

    # 주간호는 그대로 배제한다 (협의회 소스 풀 외 + council_only 가드)
    weekly = compose_digest_data(db_path=str(db), week_str="2026-W37")
    assert weekly["candidate_ids"] == []

    data = _compose(db)
    selected = data["sections"][VERDICT_MONTHLY]
    assert [item["url"] for item in selected] == [url]
    assert selected[0]["reason"] == MONTHLY_RESCUE_PRESS_REASON
    assert selected[0]["org"] == "농림축산식품부"


def test_press_rescue_accepts_company_selected_rows(tmp_path):
    """§1(b): `council_match=1` 이 아니어도 회사 선택분(council_only=0)이면 된다."""
    db = _round2_db(tmp_path)
    url = _insert(db, "mafra", "농림축산식품 통계 기본계획 발표", source_id="mafra2",
                  period_end=None, council_match=None, council_only=0)
    data = _compose(db)
    assert [item["url"] for item in data["sections"][VERDICT_MONTHLY]] == [url]


def test_press_rescue_needs_match_or_company_selection(tmp_path):
    """§1(b): 둘 다 아니면 되살리지 않는다 (배제 그대로)."""
    db = _round2_db(tmp_path)
    _insert(db, "mafra", "농림축산식품 통계 기본계획 발표", source_id="mafra3",
            period_end=None, council_match=0, council_only=1)
    data = _compose(db)
    assert data["sections"][VERDICT_MONTHLY] == []
    assert data["holds"] == []
    assert len(data["excluded"]) == 1


def test_press_rescue_requires_no_deadline(tmp_path):
    """§1(b): 마감이 있으면 주간호 몫이므로 되살리지 않는다."""
    db = _round2_db(tmp_path)
    _insert(db, "mafra", "농림축산식품 정책 방향 발표", source_id="mafra4",
            period_end="2026-09-30", council_match=1, council_only=1)
    data = _compose(db)
    assert data["sections"][VERDICT_MONTHLY] == []
    assert data["holds"] == []


def test_press_rescue_does_not_revive_noise(tmp_path):
    """§1(b): 노이즈 제목은 되살리지 않는다.

    `mafra` 는 협의회 소스 풀 밖이라 주간 분류가 **노이즈 판정에 닿기 전에**
    "협의회 소스 풀 외"로 끝낸다 — 배제 사유만 보면 노이즈를 알 수 없으므로
    되살림 함수가 제목에 노이즈 사전을 한 번 더 적용한다. 이 테스트가 그
    이중 관문을 고정한다.
    """
    db = _round2_db(tmp_path)
    _insert(db, "mafra", "농림축산식품부 인사발령 알림", source_id="mafra5",
            period_end=None, council_match=1, council_only=1)
    data = _compose(db)
    assert data["sections"][VERDICT_MONTHLY] == []
    assert data["holds"] == []
    assert data["excluded"][0]["reason"] == "협의회 소스 풀 외"


def test_media_noise_title_is_not_selected(tmp_path):
    """§1(c): 협의회 어휘가 있어도 노이즈 제목의 기사는 싣지 않는다."""
    db = _round2_db(tmp_path)
    _insert_media(db, "사회적기업 채용 공고 안내", source_id="m9")
    data = _compose(db)
    assert data["sections"][VERDICT_MONTHLY] == []


def test_notice_without_deadline_still_qualifies(tmp_path):
    """§1(a): 주간 판정이 `알아두세요` 이고 마감이 없으면 월간호다 (1R 규칙 유지)."""
    db = _round2_db(tmp_path)
    url = _insert(db, "lawmaking", "산림 사회적기업 지원 조례 입법예고",
                  source_id="notice", period_end=None)
    data = _compose(db)
    assert [item["url"] for item in data["sections"][VERDICT_MONTHLY]] == [url]


def test_monthly_order_is_council_score_then_posted(tmp_path):
    """§1: 정렬은 council_score 내림차순 → 게시일 내림차순."""
    items = [
        {"id": 1, "council_score": 0.5, "posted": "2026-09-01"},
        {"id": 2, "council_score": 0.9, "posted": "2026-09-01"},
        {"id": 3, "council_score": 0.5, "posted": "2026-09-20"},
        {"id": 4, "council_score": None, "posted": "2026-09-30"},
    ]
    assert [item["id"] for item in _sort_monthly(items)] == [2, 3, 1, 4]


def test_monthly_order_applies_to_the_real_selection(tmp_path):
    """§1: 같은 게시일이면 협의회 점수가 높은 쪽이 먼저 실린다."""
    db = _round2_db(tmp_path)
    low = _insert(db, "forest_press", "산림 사회적기업 제도 개선 계획 발표",
                  source_id="low", council_score=0.5, council_match=1)
    high = _insert(db, "coop", "산림 협동조합 육성 정책 방향 발표",
                   source_id="high", council_score=0.9, council_match=1)
    data = _compose(db)
    assert [item["url"] for item in data["sections"][VERDICT_MONTHLY]] == [
        high, low]


# ══ 라운드 2 §1(c)·§2 — 2차 미디어 ════════════════════════════════════════
def test_media_row_with_council_match_is_selected(tmp_path):
    """§1(c): `kind='media'` + `council_match=1` 행은 월간호 후보다."""
    db = _round2_db(tmp_path)
    url = _insert_media(db, source_id="m1")
    data = _compose(db)
    selected = data["sections"][VERDICT_MONTHLY]
    assert [item["url"] for item in selected] == [url]
    assert selected[0]["media"] is True
    assert selected[0]["reason"] == MONTHLY_RESCUE_MEDIA_REASON


def test_media_row_without_council_match_is_not_selected(tmp_path):
    """§1(c): 협의회 어휘가 맞지 않은 기사는 싣지 않는다."""
    db = _round2_db(tmp_path)
    _insert_media(db, source_id="m2", council_match=0)
    data = _compose(db)
    assert data["sections"][VERDICT_MONTHLY] == []


def test_media_row_is_not_a_weekly_candidate(tmp_path):
    """§1(c): 주간호는 media 행을 보지 않는다 (council_only 가드 그대로)."""
    db = _round2_db(tmp_path)
    _insert_media(db, source_id="m3")
    weekly = compose_digest_data(db_path=str(db), week_str="2026-W37")
    assert weekly["candidate_ids"] == []


def test_media_item_line_is_title_publisher_date(tmp_path):
    """§2: `제목 — 매체명 · YYYY-MM-DD` — 대상·마감·유사 표기를 붙이지 않는다."""
    db = _round2_db(tmp_path)
    _insert_media(db, source_id="m4", posted="2026-09-10")
    item = _compose(db)["sections"][VERDICT_MONTHLY][0]
    assert item_line(item) == "산림 사회적기업 현장 기사 — 라이프인 · 2026-09-10"
    assert source_display_name("lifein") == "라이프인"
    assert "대상:" not in item_line(item)
    assert "마감" not in item_line(item)


@pytest.mark.parametrize("source,publisher", [
    ("lifein", "라이프인"),
    ("eroun", "이로운넷"),
    ("senews", "사회적경제뉴스"),
    ("kfnews", "주간 한국임업신문"),
])
def test_media_publisher_names(source, publisher):
    """§2: 매체명 정본은 SOURCE_DISPLAY_NAMES 다 (checker 가 여기서 재계산한다)."""
    assert source_display_name(source) == publisher


def test_media_block_carries_the_media_marker(tmp_path):
    """§2: `<!-- media -->` 는 항목 마커 **바로 앞** 독립 줄이다."""
    db = _round2_db(tmp_path)
    _insert_media(db, source_id="m5")
    markdown = compose_digest(db_path=str(db), week_str=M09)
    lines = markdown.split("\n")
    marker_index = lines.index(MEDIA_MARKER)
    assert lines[marker_index + 1].startswith("<!-- item id=")
    assert lines[marker_index + 2].endswith("라이프인 · 2026-09-10")
    assert lines[marker_index + 3].startswith("  [원문](")
    # 표식이 있어도 항목 파서는 항목 1건을 그대로 본다
    assert blocks_mod.item_block_count(markdown) == 1
    assert blocks_mod.prose_lines_in_item_sections(markdown) == []


def test_media_feed_summary_is_never_rendered(tmp_path):
    """§2 저작권: 피드 요약은 md·kakao·미리보기 어디에도 실리지 않는다."""
    out = tmp_path / "digests"
    out.mkdir()
    db = _round2_db(tmp_path)
    _insert_media(db, source_id="m6")
    md = out / f"{M09}.md"
    compose_digest(db_path=str(db), week_str=M09, output_path=md)
    markdown = md.read_text(encoding="utf-8")
    kakao = (out / f"{M09}.kakao.txt").read_text(encoding="utf-8")
    preview = preview_mod.render_preview(M09, markdown, {"pass": True})
    for rendered in (markdown, kakao, preview):
        assert MEDIA_SUMMARY not in rendered
    manifest = load_items_manifest(md)
    assert MEDIA_SUMMARY not in json.dumps(manifest, ensure_ascii=False)


def test_media_enrichment_line_is_the_only_quote(tmp_path):
    """§2: V3 보강이 준 원문 1문장만 인용된다 (있을 때만)."""
    out = tmp_path / "digests"
    out.mkdir()
    db = _round2_db(tmp_path)
    _insert_media(db, source_id="m7")
    md = out / f"{M09}.md"
    compose_digest(db_path=str(db), week_str=M09, output_path=md)
    markdown = md.read_text(encoding="utf-8")
    # 보강 전: 항목은 제목 줄 + 링크 줄 두 줄뿐이다
    block = blocks_mod.item_blocks(markdown)[0]
    assert not (block.get("enrich_line") or "")
    # 보강 후: 같은 덩어리의 셋째 줄로 실린다 (카톡 렌더도 같은 규칙)
    item_id = block["item_id"]
    enriched = blocks_mod.set_enrich_lines(
        markdown, {str(item_id): "  → «원문에서 옮긴 한 문장»"})[0]
    md.write_text(enriched, encoding="utf-8")
    kakao = composer_kakao(enriched)
    assert "→ «원문에서 옮긴 한 문장»" in kakao
    assert MEDIA_SUMMARY not in kakao


def composer_kakao(markdown_text):
    from alert.digest.composer import kakao_file_text_from_markdown

    return kakao_file_text_from_markdown(markdown_text)


def test_media_digest_passes_the_checker(tmp_path):
    """§2: 미디어 항목이 실린 월간호도 정본 대조·게이트를 그대로 통과한다."""
    out = tmp_path / "digests"
    out.mkdir()
    db = _round2_db(tmp_path)
    _insert_media(db, source_id="m8")
    _insert(db, "forest_press", "산림 사회적기업 정책 방향 발표",
            source_id="p8", council_match=1)
    md = out / f"{M09}.md"
    compose_digest(db_path=str(db), week_str=M09, output_path=md)
    result = check_digest(db_path=str(db), markdown_path=md,
                          output_path=md.with_suffix(".check.json"))
    assert result["pass"] is True, result["reason"]
    assert result["item_blocks"] == 2
    assert result["manifest_problems"] == []
    assert result["prose_in_item_sections"] == []


# ══ 라운드 2 §3 — 인용 안내 푸터 ══════════════════════════════════════════
def test_monthly_footer_line(tmp_path):
    """§3: 월간호 md·카톡에 인용 안내 한 줄이 실리고 게이트를 막지 않는다."""
    out = tmp_path / "digests"
    out.mkdir()
    db = _monthly_db(tmp_path)
    md = out / f"{M09}.md"
    compose_digest(db_path=str(db), week_str=M09, output_path=md)
    markdown = md.read_text(encoding="utf-8")
    assert MONTHLY_FOOTER_LINE == "기사 항목은 제목·링크·원문 1문장만 인용합니다."
    assert f"## {SECTION_HEADINGS[SECTION_NOTICE]}" in markdown
    assert MONTHLY_FOOTER_LINE in markdown
    assert MONTHLY_FOOTER_LINE in (
        out / f"{M09}.kakao.txt").read_text(encoding="utf-8")
    assert blocks_mod.prose_lines_in_item_sections(markdown) == []
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is True, result["reason"]
    assert MONTHLY_HEADING in result["item_sections"]
    assert SECTION_HEADINGS[SECTION_NOTICE] in result["commentary_sections"]


def test_weekly_markdown_has_no_footer(tmp_path):
    """§3: 주간호는 그대로다 — 인용 안내도 미디어 표식도 없다."""
    db = tmp_path / "weekly.db"
    _create_announcements_table(db)
    markdown = compose_digest(db_path=str(db), week_str="2026-W37")
    assert MONTHLY_FOOTER_LINE not in markdown
    assert MEDIA_MARKER not in markdown
    assert SECTION_HEADINGS[SECTION_NOTICE] not in markdown


# ══ 라운드 2 — media 레인 병합 후 정합 ═════════════════════════════════════
def test_media_kind_matches_the_media_lane_constant():
    """`composer.MEDIA_KIND` 는 P1-R 레인의 정본(`models.SOURCE_KIND_MEDIA`)과 같다.

    두 레인이 문자열을 따로 들고 있으면 한쪽만 바뀌는 날 월간호가 조용히
    미디어를 못 알아본다 (`kind` 비교는 이 값 하나로만 이뤄진다).
    """
    from alert.models import SOURCE_KIND_MEDIA

    from alert.digest.composer import MEDIA_KIND

    assert MEDIA_KIND == SOURCE_KIND_MEDIA == "media"


def test_media_selected_on_the_real_migrated_schema(tmp_path):
    """§1(c)·§2: **실제 마이그레이션 스키마**(migration 10)에서도 그대로 돈다.

    앞의 테스트들은 ALTER 로 열을 붙인 픽스처를 쓴다. 이 테스트는 레포의
    `run_migrations` 가 만든 스키마에 media 행을 넣어, 컬럼 이름·기본값이
    두 레인 사이에서 어긋나지 않았음을 실행으로 확인한다.
    """
    from alert.migrations import run_migrations

    out = tmp_path / "digests"
    out.mkdir()
    db = tmp_path / "migrated.db"
    _create_announcements_table(db)
    conn = sqlite3.connect(str(db))
    run_migrations(conn, vec_available=False)
    conn.close()

    url = _insert_media(db, source_id="real-1", posted="2026-09-08")
    _insert(db, "forest_press", "산림 사회적기업 정책 방향 발표",
            source_id="real-2", council_match=1, council_score=0.8)

    md = out / f"{M09}.md"
    compose_digest(db_path=str(db), week_str=M09, output_path=md)
    markdown = md.read_text(encoding="utf-8")

    # 선정: 미디어 1 + 기관 보도 1
    data = _compose(db)
    selected = data["sections"][VERDICT_MONTHLY]
    assert url in [item["url"] for item in selected]
    media_item = next(item for item in selected if item["url"] == url)
    assert media_item["media"] is True
    assert media_item["reason"] == MONTHLY_RESCUE_MEDIA_REASON

    # 렌더: 표식 + 제목·매체명·발행일 + 링크, 요약 없음, 푸터 있음
    assert MEDIA_MARKER in markdown
    assert "산림 사회적기업 현장 기사 — 라이프인 · 2026-09-08" in markdown
    assert MEDIA_SUMMARY not in markdown
    assert MONTHLY_FOOTER_LINE in markdown

    # 게이트: 정본 대조·상한·산문 검사 모두 통과
    result = check_digest(db_path=str(db), markdown_path=md, output_path=None)
    assert result["pass"] is True, result["reason"]
    assert result["manifest_problems"] == []

    # 주간호는 여전히 media 를 보지 않는다
    weekly = compose_digest_data(db_path=str(db), week_str="2026-W37")
    assert url not in json.dumps(weekly, ensure_ascii=False, default=str)


# ══ 라운드 3 (Codex 게이트) — 마감 추출 실패 · 허용 목록 ═══════════════════
def test_deadline_cue_detects_extraction_miss(tmp_path):
    """Codex MEDIUM 재현: `알아두세요` + 제목 `(~9.16.)` + period_end NULL.

    라운드 2까지는 제외 사유가 None(= 실림)이었다. 추출 실패를 "마감 없음" 으로
    읽으면 이미 끝난 의견수렴이 월간 종합에 실린다.
    """
    db = _round2_db(tmp_path)
    _insert(db, "lawmaking", "산림 사회적기업 제도 의견수렴 안내 (~9.16.)",
            source_id="cue", period_end=None, summary="요약")
    weekly = compose_digest_data(db_path=str(db), week_str="2026-W37")
    assert weekly["sections"]["알아두세요"], "주간 판정은 알아두세요여야 재현이 된다"

    data = _compose(db)
    assert data["sections"][VERDICT_MONTHLY] == []
    assert [hold["reason"] for hold in data["holds"]] == [
        MONTHLY_HOLD_DEADLINE_UNKNOWN]


@pytest.mark.parametrize("text", [
    "의견수렴 안내 (~9.16.)",
    "접수 ~ 9. 30. 연장",
    "2026-09-16 시행 예정",
    "2026.09.16. 개정",
    "설명회 (9.10.(목) 13시)",
    "9월 16일 개최",
    "신청서 제출 9월 16일까지",
    "마감 임박",
    "접수기간 안내",
    "신청기간 변경",
    "제출기한 연장",
])
def test_deadline_cue_patterns(text):
    """§3: 사람 눈에 보이는 마감 표기는 전부 단서로 잡는다 (fail-closed)."""
    assert deadline_cue(text) is not None


@pytest.mark.parametrize("text", [
    "산림 사회적기업 성장 정책 방향 발표",
    "임업 통계 기본계획 발표",
    "2026년 하반기 제도 개선",
])
def test_deadline_cue_is_silent_without_a_date(text):
    """§3: 마감 표기가 없으면 단서도 없다 — 진짜 마감 없음은 그대로 실린다."""
    assert deadline_cue(text) is None


def test_deadline_cue_reads_the_summary_too(tmp_path):
    """§3: 제목이 깨끗해도 요약에 마감이 보이면 `마감 미확인` 이다."""
    db = _round2_db(tmp_path)
    _insert(db, "lawmaking", "산림 사회적기업 제도 개선 안내", source_id="cue2",
            period_end=None, summary="신청서는 9월 16일까지 제출하세요")
    data = _compose(db)
    assert data["sections"][VERDICT_MONTHLY] == []
    assert data["holds"][0]["reason"] == MONTHLY_HOLD_DEADLINE_UNKNOWN


def test_deadline_cue_does_not_touch_the_weekly_issue(tmp_path):
    """§3: 단서 판정은 월간 경로 전용이다 — 주간호는 그대로 싣는다."""
    db = _round2_db(tmp_path)
    _insert(db, "lawmaking", "산림 사회적기업 제도 의견수렴 안내 (~9.16.)",
            source_id="cue3", period_end=None)
    weekly = compose_digest_data(db_path=str(db), week_str="2026-W37")
    assert len(weekly["sections"]["알아두세요"]) == 1


def test_generic_hold_is_not_eligible(tmp_path):
    """Codex MEDIUM 재현: 비미디어 `보류/섹션 판정 불명` 행은 월간 대상이 아니다.

    허용 목록은 (a) 알아두세요 · (b) 기관 보도·정책 · (c) 2차 미디어뿐이다.
    `kofpi` 는 기관 보도 소스 목록에 없으므로 어느 규칙에도 걸리지 않는다.
    """
    db = _round2_db(tmp_path)
    _insert(db, "kofpi", "산림 사회적기업 협력 사례", source_id="generic",
            period_end=None, summary="요약")
    weekly = compose_digest_data(db_path=str(db), week_str="2026-W37")
    assert weekly["holds"][0]["reason"] == "섹션 판정 불명"

    data = _compose(db)
    assert data["sections"][VERDICT_MONTHLY] == []
    assert [hold["reason"] for hold in data["holds"]] == [
        MONTHLY_HOLD_NOT_ELIGIBLE]


def test_monthly_rule_allowlist_table():
    """§4: 어느 규칙으로 들어오는가 — (a)/(b)/(c) 와 '해당 없음'."""
    from alert.digest.composer import Classification, VERDICT_HOLD

    notice = Classification(VERDICT_NOTICE, "제도: 입법예고", (), ("전체",), None)
    hold = Classification(VERDICT_HOLD, "섹션 판정 불명", (), (), None)
    excluded = Classification("배제", "협의회 소스 풀 외", (), (), None)

    assert monthly_rule("lawmaking", notice, {}) == MONTHLY_RULE_NOTICE
    assert monthly_rule("mafra", excluded, {"council_match": 1}) == \
        MONTHLY_RULE_PRESS
    assert monthly_rule("mafra", excluded, {"council_only": 0}) == \
        MONTHLY_RULE_PRESS
    assert monthly_rule("mafra", excluded,
                        {"council_match": 0, "council_only": 1}) is None
    assert monthly_rule("lifein", excluded,
                        {"kind": "media", "council_match": 1}) == \
        MONTHLY_RULE_MEDIA
    assert monthly_rule("lifein", excluded,
                        {"kind": "media", "council_match": 0}) is None
    # 허용 목록 밖: 기관 보도 소스도 아니고 알아두세요도 아니다
    assert monthly_rule("kofpi", hold, {"council_match": 1}) is None


def test_press_rule_admits_without_a_rescue(tmp_path):
    """§4(b): 기관 보도 소스는 주간 배제가 아니어도 (b)로 들어온다."""
    db = _round2_db(tmp_path)
    from alert.digest.composer import classify_item

    url = _insert(db, "coop", "산림 협동조합 협력 사례", source_id="press-hold",
                  period_end=None, council_match=1, council_only=1)
    # 주간 분류는 `보류/섹션 판정 불명` 이다 (배제가 아니므로 되살림 경로가 아니다)
    assert classify_item("산림 협동조합 협력 사례", "요약", "coop").reason == \
        "섹션 판정 불명"
    data = _compose(db)
    assert [item["url"] for item in data["sections"][VERDICT_MONTHLY]] == [url]
    assert data["sections"][VERDICT_MONTHLY][0]["monthly_rule"] == \
        MONTHLY_RULE_PRESS


def test_deadline_cue_beats_every_admission_rule(tmp_path):
    """§3+§4: 규칙에 맞아도 마감 단서가 있으면 내려간다 (미디어 포함)."""
    db = _round2_db(tmp_path)
    _insert_media(db, "사회적기업 현장 기사 신청기간 안내", source_id="cue-media")
    _insert(db, "mafra", "농림축산식품 정책 발표 (~9.30.)", source_id="cue-press",
            period_end=None, council_match=1, council_only=1)
    data = _compose(db)
    assert data["sections"][VERDICT_MONTHLY] == []
    assert {hold["reason"] for hold in data["holds"]} == {
        MONTHLY_HOLD_DEADLINE_UNKNOWN}


# ══ 라운드 3 §5 — 잡의 날짜 가드 (요일 AND 일자, KST) ═════════════════════
def _run_monthly_job(tmp_path, now, extra_env=None):
    """`monthly_job.sh` 를 스텁 파이썬으로 돌린다 (네트워크·DB 접근 없음).

    스텁은 받은 argv 를 파일에 적고 0 으로 끝난다 — 가드가 열렸는지, 어떤 호 키로
    생성기를 불렀는지를 **실행으로** 본다.
    """
    import os
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    log = tmp_path / "argv.log"
    stub = tmp_path / "py"
    stub.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + f"'{log}'\n" + "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env = dict(os.environ, MONTHLY_JOB_NOW=now, GONGGO_PYTHON=str(stub))
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "monthly_job.sh")],
        capture_output=True, text=True, env=env, cwd=str(root),
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


@pytest.mark.parametrize("now,why", [
    ("2026-10-02", "목요일이 아님"),      # Codex 재현: 금요일
    ("2026-10-08", "첫째 주가 아님"),     # 목요일이지만 일자 8
    ("2026-10-05", "목요일이 아님"),      # 월요일, 일자 5
])
def test_monthly_job_guard_refuses(tmp_path, now, why):
    """§5: 목요일 **그리고** 일자 ≤ 7 이 아니면 파이프라인을 시작하지 않는다."""
    proc, calls = _run_monthly_job(tmp_path, now)
    assert proc.returncode == 0
    assert "skip" in proc.stdout and why in proc.stdout
    assert calls == [], "가드가 열리면 안 되는 날에 생성기를 불렀다"


def test_monthly_job_runs_on_the_first_thursday(tmp_path):
    """§5: 2026-10-01(목, 1일)에는 직전 달 키로 생성기를 부른다."""
    proc, calls = _run_monthly_job(tmp_path, "2026-10-01")
    assert calls and calls[0].startswith("scripts/monthly_digest.py 2026-M09")
    # 스텁이 md 를 만들지 않으므로 그 다음 단계로 넘어가지 않는다 (네트워크 없음)
    assert proc.returncode == 1
    assert "월간호 파일 없음" in proc.stderr
    assert len(calls) == 1


def test_monthly_job_forces_kst(tmp_path):
    """§5: 실행 환경 TZ 가 무엇이든 잡은 KST 달력을 본다."""
    proc, calls = _run_monthly_job(
        tmp_path, "2026-10-01", extra_env={"TZ": "America/New_York"})
    assert calls and calls[0].startswith("scripts/monthly_digest.py 2026-M09")


def test_monthly_job_explicit_key_skips_the_guard(tmp_path):
    """§5: 인자로 호 키를 주면(수동 재실행) 날짜 가드를 지나지 않는다."""
    import os
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    log = tmp_path / "argv.log"
    stub = tmp_path / "py"
    stub.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + f"'{log}'\n" + "exit 0\n",
        encoding="utf-8")
    stub.chmod(0o755)
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "monthly_job.sh"), "2026-M07"],
        capture_output=True, text=True, cwd=str(root),
        env=dict(os.environ, GONGGO_PYTHON=str(stub), MONTHLY_JOB_NOW="2026-10-02"),
    )
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls[0].startswith("scripts/monthly_digest.py 2026-M07")
    assert proc.returncode == 1     # 스텁이 md 를 만들지 않는다
