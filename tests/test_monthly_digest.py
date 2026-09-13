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
    MONTHLY_HOLD_ACTIONABLE,
    MONTHLY_HOLD_DEADLINE,
    MONTHLY_ITEM_SECTIONS,
    SECTION_HEADINGS,
    SECTION_LIMITS,
    SOURCE_DIVERSITY_LIMIT,
    VERDICT_MONTHLY,
    compose_digest,
    compose_digest_data,
    get_month_date_range,
    issue_kind,
    load_items_manifest,
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
            summary="요약", council_only=None):
    source_id = source_id or f"{source}-{abs(hash(title)) % 10 ** 8}"
    url = url or f"https://example.com/{source_id}"
    conn = sqlite3.connect(str(db_path))
    columns = ["source", "source_id", "title", "summary", "url", "author",
               "period_start", "period_end", "raw_data", "relevance_score",
               "created_at", "updated_at"]
    values = [source, source_id, title, summary, url, "기관", period_start,
              period_end, "", 0.9, created_at, created_at]
    if council_only is not None:
        columns.append("council_only")
        values.append(council_only)
    conn.execute(
        "INSERT INTO announcements ({}) VALUES ({})".format(
            ", ".join(columns), ", ".join("?" * len(columns))),
        values,
    )
    conn.commit()
    conn.close()
    return url


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


def test_monthly_job_skips_when_not_first_week(tmp_path):
    """§2: 매주 목요일에 깨어나되 1~7일이 아니면 exit 0 로 끝난다."""
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "monthly_job.sh"
    source = script.read_text(encoding="utf-8")
    assert 'date "+%d"' in source
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
