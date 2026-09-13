"""협의회 적재 프로파일 (P0 계약 §A) — 점수·저장 규칙·불변 조건 1·관찰 표.

네트워크를 타지 않는다: 크롤러·발송·notify 를 부르지 않고, DB 는 전부
임시 파일이다.
"""

import ast
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from alert import council
from alert.config import CouncilProfileConfig, get_config
from alert.analyzer import KeywordAnalyzer
from alert.db import COUNCIL_DROP_RETENTION_DAYS, Database
from alert.digest.composer import compose_digest_data
from alert.main import apply_council_profile, select_for_storage
from alert.migrations import run_migrations
from alert.models import AnalyzedAnnouncement, Keyword, RawAnnouncement

import scripts.council_observe as observe

REPO_ROOT = Path(__file__).resolve().parent.parent

# 다이제스트 테스트가 쓰는 고정 주차 (test_digest.py 와 같은 값)
W13 = "2026-W13"
W13_CREATED_AT = "2026-03-26T12:00:00"
W13_TODAY = date(2026, 3, 26)


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------

@pytest.fixture
def profile():
    """운영 config.yaml 의 협의회 프로파일 (정본 그대로)."""
    return get_config(reload=True).council_profile


@pytest.fixture
def company_analyzer():
    """운영 config.yaml 의 **회사** 키워드로 만든 분석기.

    DB 는 MagicMock 이라 파일을 건드리지 않는다. 키워드는 실제 설정에서
    오므로 불변 조건 1 회귀가 운영 어휘를 그대로 검증한다.
    """
    cfg = get_config(reload=True)
    keywords = (
        [Keyword(keyword=k, category="must_match", weight=2.0) for k in cfg.keywords.must_match]
        + [Keyword(keyword=k, category="boost", weight=1.0) for k in cfg.keywords.boost]
        + [Keyword(keyword=k, category="exclude", weight=1.0) for k in cfg.keywords.exclude]
    )
    db = MagicMock()
    db.get_keywords.return_value = keywords
    return KeywordAnalyzer(db=db)


def raw(source, source_id, title, summary="", target="", category=""):
    return RawAnnouncement(
        source=source,
        source_id=source_id,
        title=title,
        url=f"https://example.com/{source}/{source_id}",
        summary=summary,
        target=target,
        category=category,
    )


# W37 실측 계열 항목 묶음 (A-inventory.md 의 제목 패턴).
# - 앞 2건: 회사 must_match(사회적기업·조경)에 걸려 **회사 경로**가 고른다.
# - 뒤 4건: 회사 어휘에는 없고 협의회 어휘(산림·임업·협동조합·목재)만 있다.
FIXTURE_ITEMS = [
    raw("seis", "s1", "2026년 사회적기업 성장지원센터 입주기업 모집 공고"),
    raw("mafra", "m1", "도시녹화 조경 사업 참여기업 모집"),
    raw("kofpi", "k1", "산림분야 오픈이노베이션 참여기업 모집 공고"),
    raw("coop", "c1", "협동조합 설립 상담 안내 [경기강원센터]"),
    raw("fowi", "f1", "임산물 가공유통 지원사업 신청 안내"),
    raw("forest_press", "p1", "남부지방산림청 호우 안전관리 강화"),
]


def snapshot(items):
    """회사 경로 산출물의 **바이트 단위** 지문."""
    return json.dumps(
        [
            {
                "source_id": a.source_id,
                "relevance_score": a.relevance_score,
                "relevance_reason": a.relevance_reason,
                "matched_keywords": a.matched_keywords,
            }
            for a in items
        ],
        ensure_ascii=False,
        sort_keys=True,
    )


# ---------------------------------------------------------------------------
# 1. 프로파일 점수 계산 (어휘·자격·지역·제외)
# ---------------------------------------------------------------------------

class TestCouncilScoring:
    """``alert.council.score_item`` — 순수 함수 채점."""

    def test_source_outside_pool_never_matches(self, profile):
        """협의회 소스 풀 밖이면 어휘가 맞아도 매치가 아니다."""
        v = council.score_item(profile, "smartfarm", "산림 사회적기업 지원사업 공고")
        assert v.match == 0
        assert v.score == 0.0
        assert v.reason == "협의회 소스 아님"

    def test_must_match_gives_base_score(self, profile):
        """must_match 하나면 기본점 0.5 + 매치."""
        v = council.score_item(profile, "kofpi", "산림분야 오픈이노베이션 공고")
        assert v.match == 1
        assert v.score >= council.SCORE_MUST_MATCH

    def test_no_must_match_means_no_match(self, profile):
        """태그만 붙고 협의회 어휘가 없으면 적재 대상이 아니다."""
        v = council.score_item(profile, "kofpi", "경기도 중소기업 자금 안내")
        assert v.match == 0
        assert v.reason == "협의회 어휘 없음"

    def test_eligibility_and_region_tags(self, profile):
        """자격·지역 태그가 각각 잡히고 점수에 가산된다."""
        v = council.score_item(
            profile, "coop", "협동조합 설립 상담 안내 [경기강원센터]"
        )
        assert v.match == 1
        assert "협동조합" in v.tags["eligibility"]
        assert set(v.tags["region"]) >= {"강원", "경기"}
        assert v.score > council.SCORE_MUST_MATCH

    def test_exclude_keyword_zeroes_score(self, profile):
        """제외어가 있으면 어휘가 맞아도 0점·미매치."""
        v = council.score_item(
            profile, "forest_press", "남부지방산림청 호우 안전관리 강화"
        )
        assert v.match == 0
        assert v.score == 0.0
        assert v.reason.startswith("제외 키워드")

    def test_tag_family_cap(self):
        """태그 계열별 가산은 상한을 넘지 않는다."""
        wide = CouncilProfileConfig(
            sources=["kofpi"],
            must_match=["산림"],
            region={str(i): [f"지역{i}"] for i in range(10)},
        )
        text = "산림 " + " ".join(f"지역{i}" for i in range(10))
        v = council.score_item(wide, "kofpi", text)
        assert len(v.tags["region"]) == 10
        assert v.score == pytest.approx(
            council.SCORE_MUST_MATCH + council.TAG_SCORE_CAP
        )

    def test_normalization_absorbs_parens_and_spaces(self, profile):
        """괄호·공백 표기 변형이 같은 어휘로 잡힌다 (council-vocab.md §1 메모)."""
        v = council.score_item(profile, "seis", "(예비)사회적기업 지정 계획 공고")
        assert v.match == 1
        assert "예비사회적기업" in v.tags["eligibility"]

        spaced = council.score_item(profile, "fowi", "나눔의 숲 캠프 참가자 모집")
        assert spaced.match == 1

    def test_tags_json_is_valid_and_omits_empty_families(self, profile):
        """``council_tags`` 는 JSON 이고, 빈 계열은 키 자체가 없다."""
        v = council.score_item(profile, "kofpi", "산림 목재 이용 공고")
        parsed = json.loads(v.tags_json())
        assert "region" not in parsed
        assert isinstance(parsed, dict)

    def test_empty_profile_is_inert(self):
        """빈 프로파일(=킬 스위치)은 무엇도 매치하지 않는다."""
        v = council.score_item(CouncilProfileConfig(), "kofpi", "산림 사회적기업 공고")
        assert v.match == 0


class TestProfileConfig:
    """``council_profile:`` 블록이 실제로 읽히는가."""

    def test_sources_cover_contract_list(self, profile):
        assert {"mafra", "forest_service", "mois_sse", "socialenterprise", "seis",
                "coop", "fowi", "kofpi", "forest_press", "lawmaking", "bizinfo",
                "g2b", "kstartup", "subsidy24"} <= set(profile.sources)

    def test_vocabulary_present(self, profile):
        assert len(profile.must_match) >= 12
        assert {"사회적기업", "산림", "임업", "협동조합"} <= set(profile.must_match)
        assert profile.eligibility and profile.region and profile.exclude

    def test_tag_tables_map_tag_to_aliases(self, profile):
        for table in (profile.eligibility, profile.region):
            for tag, aliases in table.items():
                assert isinstance(tag, str)
                assert isinstance(aliases, list) and aliases


# ---------------------------------------------------------------------------
# 2. 저장 규칙
# ---------------------------------------------------------------------------

class TestStorageRule:
    """``alert.main.apply_council_profile`` — 무엇이 따로 저장되는가."""

    def test_selected_items_get_measurements_only(self, profile, company_analyzer):
        """회사가 고른 항목에는 측정값만 붙는다 (점수·사유·키워드 불변)."""
        selected, _ = select_for_storage(company_analyzer, FIXTURE_ITEMS, None)
        before = snapshot(selected)

        apply_council_profile(
            profile, "seis", FIXTURE_ITEMS, selected, company_analyzer.analyze
        )

        assert snapshot(selected) == before
        for ann in selected:
            assert ann.council_only == 0

    def test_council_only_items_returned_separately(self, profile, company_analyzer):
        """회사가 버린 협의회 매치는 별도 목록으로 온다."""
        items = [FIXTURE_ITEMS[2]]        # kofpi 산림 공고 - 회사 어휘 없음
        selected, _ = select_for_storage(company_analyzer, items, None)
        assert selected == []

        extras, drops = apply_council_profile(
            profile, "kofpi", items, selected, company_analyzer.analyze
        )
        assert len(extras) == 1
        assert extras[0].council_match == 1
        assert extras[0].council_only == 1
        assert extras[0].source_id == "k1"
        assert drops == []

    def test_council_only_keeps_sub_threshold_company_score(
        self, profile, company_analyzer
    ):
        """협의회 단독 행의 회사 점수는 **계산값 그대로** (임계 미달)."""
        items = [FIXTURE_ITEMS[2]]
        extras, _ = apply_council_profile(
            profile, "kofpi", items, [], company_analyzer.analyze
        )
        threshold = get_config().analyzer.keyword_threshold
        assert extras[0].relevance_score < threshold

    def test_excluded_item_is_dropped_not_stored(self, profile, company_analyzer):
        """제외어 항목은 저장되지 않고 **탈락 원장 레코드**로 온다."""
        items = [FIXTURE_ITEMS[5]]        # forest_press 호우 보도
        extras, drops = apply_council_profile(
            profile, "forest_press", items, [], company_analyzer.analyze
        )
        assert extras == []
        assert len(drops) == 1
        assert drops[0].source_id == "p1"
        assert drops[0].reason.startswith("제외 키워드")

    def test_drop_records_posted_at_from_raw_data(self, profile, company_analyzer):
        """탈락 레코드의 posted_at 은 raw_data 의 ``posted`` 에서 온다."""
        item = raw("kofpi", "d1", "추석 명절 선물 안내")
        item.raw_data = json.dumps({"posted": "2026-09-10"}, ensure_ascii=False)
        _, drops = apply_council_profile(
            profile, "kofpi", [item], [], company_analyzer.analyze
        )
        assert drops[0].posted_at == "2026-09-10"

    def test_no_drops_for_non_council_sources(self, profile, company_analyzer):
        """협의회 소스가 아니면 측정도 탈락 기록도 하지 않는다."""
        extras, drops = apply_council_profile(
            profile, "smartfarm", [raw("smartfarm", "x1", "무관한 공고")],
            [], company_analyzer.analyze
        )
        assert (extras, drops) == ([], [])

    def test_bypass_source_leaves_no_extras(self, profile, company_analyzer):
        """bypass 소스는 전량이 이미 선택되므로 단독 적재분이 없다.

        계약 §A: bypass 소스는 지금처럼 저장하되 council_* 만 채운다.
        """
        from alert.config import SourceConfig

        items = [FIXTURE_ITEMS[2], FIXTURE_ITEMS[4]]
        selected, bypassed = select_for_storage(
            company_analyzer, items, SourceConfig(bypass_threshold=True)
        )
        assert bypassed and len(selected) == len(items)

        extras, drops = apply_council_profile(
            profile, "kofpi", items, selected, company_analyzer.analyze
        )
        assert extras == [] and drops == []
        assert any(a.council_match == 1 for a in selected)

    def test_empty_profile_stores_nothing_extra(self, company_analyzer):
        """프로파일이 비면 배선 전체가 무동작이다."""
        assert apply_council_profile(
            CouncilProfileConfig(), "kofpi", FIXTURE_ITEMS, [], company_analyzer.analyze
        ) == ([], [])


class TestDropLedger:
    """``council_dropped`` — 저장되지 않은 항목의 관찰 원장 (Codex 게이트 2R)."""

    def drop(self, source_id="d1", title="추석 명절 선물 안내"):
        return council.CouncilDrop(
            source="kofpi", source_id=source_id, title=title,
            url=f"https://example.com/{source_id}", posted_at="2026-09-10",
            company_score=0.05, council_score=0.0, reason="협의회 어휘 없음",
        )

    def test_records_and_is_idempotent(self, tmp_path):
        db = Database(tmp_path / "drops.db")
        try:
            db.record_council_drops([self.drop()])
            db.record_council_drops([self.drop(title="제목이 바뀐 같은 공고")])
            rows = db.conn.execute(
                "SELECT source, source_id, title, posted_at, reason"
                "  FROM council_dropped"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["title"] == "제목이 바뀐 같은 공고"
            assert rows[0]["posted_at"] == "2026-09-10"
        finally:
            db.close()

    def test_prunes_beyond_retention(self, tmp_path):
        db = Database(tmp_path / "drops.db")
        try:
            db.record_council_drops([self.drop("old"), self.drop("new")])
            stale = (datetime.now() - timedelta(days=COUNCIL_DROP_RETENTION_DAYS + 1))
            db.conn.execute(
                "UPDATE council_dropped SET seen_at = ? WHERE source_id = 'old'",
                (stale.isoformat(),),
            )
            db.conn.commit()

            assert db.prune_council_drops() == 1
            remaining = [r["source_id"] for r in db.conn.execute(
                "SELECT source_id FROM council_dropped"
            ).fetchall()]
            assert remaining == ["new"]
        finally:
            db.close()

    def test_retention_is_30_days(self):
        assert COUNCIL_DROP_RETENTION_DAYS == 30

    def test_ledger_never_reaches_notification_or_briefing(self):
        """알림·브리핑 코드는 council_dropped 를 읽지 않는다."""
        for rel in ("alert/db.py", "alert/digest/composer.py",
                    "alert/notifiers/telegram_bot.py"):
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            reads = [
                line for line in source.splitlines()
                if "council_dropped" in line and "SELECT" in line.upper()
            ]
            assert reads == [], f"{rel} 가 탈락 원장을 읽는다: {reads}"


# ---------------------------------------------------------------------------
# 3. 불변 조건 1 — 회사용 알림 경로 무변경 (회귀)
# ---------------------------------------------------------------------------

class TestInvariantCompanyAlertPath:
    """계약 불변 조건 1: 회사 알림 경로는 바이트 단위로 같아야 한다."""

    def test_company_selection_identical_with_and_without_profile(
        self, profile, company_analyzer
    ):
        """프로파일 적용 전후로 회사 선택 결과가 완전히 같다.

        같은 픽스처를 두 번 돌린다: ① 협의회 프로파일 없이 ② 운영 프로파일로.
        원소 수·순서·relevance_score·사유·매치 키워드가 모두 같아야 한다.
        """
        before_sel, _ = select_for_storage(company_analyzer, FIXTURE_ITEMS, None)
        before = snapshot(before_sel)

        after_sel, _ = select_for_storage(company_analyzer, FIXTURE_ITEMS, None)
        extras, _drops = apply_council_profile(
            profile, "seis", FIXTURE_ITEMS, after_sel, company_analyzer.analyze
        )

        assert snapshot(after_sel) == before
        assert len(after_sel) == len(before_sel)
        # 협의회 경로는 회사 선택에 원소를 더하지 않는다 - 늘어난 건 별도 목록뿐.
        assert {a.source_id for a in after_sel}.isdisjoint(
            {a.source_id for a in extras}
        )

    def test_unnotified_excludes_council_only_rows(self, tmp_path):
        """알림 쿼리는 council_only 행을 집지 않는다."""
        db = Database(tmp_path / "alert.db")
        try:
            company = AnalyzedAnnouncement(
                source="seis", source_id="ok-1", title="사회적기업 지원사업 공고",
                url="https://example.com/ok-1", relevance_score=0.5,
                council_match=1, council_only=0,
            )
            council_only = AnalyzedAnnouncement(
                source="kofpi", source_id="council-1", title="산림 목재 이용 공고",
                url="https://example.com/council-1", relevance_score=0.05,
                council_score=0.5, council_tags='{"eligibility": []}',
                council_match=1, council_only=1,
            )
            db.insert_announcement(company)
            db.insert_announcement(council_only)

            ids = {a.source_id for a in db.get_unnotified()}
            assert ids == {"ok-1"}
        finally:
            db.close()

    def test_stored_council_only_row_keeps_its_columns(self, tmp_path):
        """협의회 단독 행은 저장은 되고 측정 컬럼이 살아 있다."""
        db = Database(tmp_path / "alert.db")
        try:
            db.insert_announcement(AnalyzedAnnouncement(
                source="kofpi", source_id="council-1", title="산림 목재 이용 공고",
                url="https://example.com/council-1", relevance_score=0.05,
                council_score=0.55, council_tags='{"region": ["경기"]}',
                council_match=1, council_only=1,
            ))
            row = db.conn.execute(
                "SELECT council_score, council_tags, council_match, council_only"
                "  FROM announcements WHERE source_id = 'council-1'"
            ).fetchone()
            assert row["council_score"] == pytest.approx(0.55)
            assert json.loads(row["council_tags"]) == {"region": ["경기"]}
            assert (row["council_match"], row["council_only"]) == (1, 1)
        finally:
            db.close()

    def test_composer_candidates_exclude_council_only_rows(self, tmp_path):
        """브리핑 후보 쿼리도 council_only 행을 건너뛴다 (불변 조건 2)."""
        db_path = tmp_path / "digest.db"
        Database(db_path).close()          # 스키마 + 마이그레이션

        conn = sqlite3.connect(db_path)
        for source_id, council_only in (("keep-1", 0), ("drop-1", 1)):
            conn.execute(
                "INSERT INTO announcements"
                " (source, source_id, title, summary, url, created_at, updated_at,"
                "  council_match, council_only)"
                " VALUES ('kofpi', ?, ?, '', ?, ?, ?, 1, ?)",
                (
                    source_id,
                    f"산림분야 오픈이노베이션 참여기업 모집 공고 {source_id}",
                    f"https://example.com/{source_id}",
                    W13_CREATED_AT, W13_CREATED_AT, council_only,
                ),
            )
        conn.commit()
        kept_id = conn.execute(
            "SELECT id FROM announcements WHERE source_id = 'keep-1'"
        ).fetchone()[0]
        dropped_id = conn.execute(
            "SELECT id FROM announcements WHERE source_id = 'drop-1'"
        ).fetchone()[0]
        conn.close()

        data = compose_digest_data(
            db_path=str(db_path), week_str=W13, today=W13_TODAY
        )
        assert kept_id in data["candidate_ids"]
        assert dropped_id not in data["candidate_ids"]


class TestMigration:
    """마이그레이션 7 — 멱등 ALTER TABLE."""

    def test_adds_council_columns(self, in_memory_conn):
        cols = [r[1] for r in in_memory_conn.execute(
            "PRAGMA table_info(announcements)"
        ).fetchall()]
        for col in ("council_score", "council_tags", "council_match", "council_only"):
            assert col in cols

    def test_idempotent(self, in_memory_conn):
        assert run_migrations(in_memory_conn, vec_available=False) == 0

    def test_measurement_columns_default_to_null(self, in_memory_conn):
        """측정 3열은 NULL(=미측정), 가드 1열만 0 — 둘은 다른 것이다."""
        now = datetime.now().isoformat()
        in_memory_conn.execute(
            "INSERT INTO announcements"
            " (source, source_id, title, url, created_at, updated_at)"
            " VALUES ('seis', 'old-1', '옛 행', 'https://example.com/old-1', ?, ?)",
            (now, now),
        )
        row = in_memory_conn.execute(
            "SELECT council_score, council_tags, council_match, council_only"
            "  FROM announcements WHERE source_id = 'old-1'"
        ).fetchone()
        assert (row[0], row[1], row[2]) == (None, None, None)
        # 가드가 NULL 이면 `council_only = 0` 이 옛 행을 통째로 떨군다.
        assert row[3] == 0

    def test_creates_drop_ledger(self, in_memory_conn):
        tables = {r[0] for r in in_memory_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "council_dropped" in tables


# ---------------------------------------------------------------------------
# 4. 관찰 스크립트
# ---------------------------------------------------------------------------

@pytest.fixture
def observe_db(tmp_path):
    """관찰 스크립트용 임시 DB — 회사 통과 1건 + 협의회 단독 1건 + 미매치 1건."""
    db_path = tmp_path / "observe.db"
    Database(db_path).close()

    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    rows = [
        ("seis", "a1", "사회적기업 성장지원센터 입주기업 모집", 0.5, 0.55, 1, 0),
        ("kofpi", "b1", "산림 목재 이용 공고", 0.05, 0.5, 1, 1),
        ("kofpi", "b2", "기타 안내문", 0.05, 0.0, 0, 1),
    ]
    for source, sid, title, rel, cscore, cmatch, conly in rows:
        conn.execute(
            "INSERT INTO announcements"
            " (source, source_id, title, summary, url, created_at, updated_at,"
            "  relevance_score, council_score, council_tags, council_match, council_only)"
            " VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, '{}', ?, ?)",
            (source, sid, title, f"https://example.com/{sid}", now, now,
             rel, cscore, cmatch, conly),
        )
    conn.execute(
        "INSERT INTO run_history (started_at, source, total_fetched, new_count)"
        " VALUES (?, 'kofpi', 20, 20)",
        (now,),
    )
    conn.commit()
    conn.close()
    return db_path


class TestObserveReport:
    """``scripts/council_observe.py`` — 읽기 전용 관찰 표."""

    def test_open_readonly_rejects_writes(self, observe_db):
        conn = observe.open_readonly(observe_db)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("DELETE FROM announcements")
        finally:
            conn.close()

    def test_counts_split_company_and_council(self, observe_db):
        conn = observe.open_readonly(observe_db)
        try:
            counts = observe.collect_counts(conn, observe.window_start(2))
        finally:
            conn.close()

        assert counts["kofpi"]["fetched"] == 20
        assert counts["kofpi"]["stored"] == 2
        assert counts["kofpi"]["council"] == 1
        assert counts["kofpi"]["company"] == 0
        assert counts["seis"]["company"] == 1
        assert counts["seis"]["both"] == 1

    def test_table_shape(self, observe_db):
        conn = observe.open_readonly(observe_db)
        try:
            counts = observe.collect_counts(conn, observe.window_start(2))
        finally:
            conn.close()

        table = observe.render_table(counts, {"kofpi", "seis"})
        lines = table.splitlines()
        assert lines[0] == (
            "| source | 협의회풀 | fetched | 저장 | council_match | 미측정 "
            "| 회사통과 | 둘다 |"
        )
        assert lines[1] == "|---|---|---|---|---|---|---|---|"
        assert lines[-1].startswith("| **합계** |")
        assert len(lines) == 2 + len(counts) + 1

    def test_samples_cover_both_groups(self, observe_db):
        conn = observe.open_readonly(observe_db)
        try:
            samples = observe.collect_samples(
                conn, observe.window_start(2), {"kofpi", "seis"}, 25, 0
            )
        finally:
            conn.close()

        groups = {s["표본군"] for s in samples}
        assert groups == {observe.SAMPLE_MATCHED, observe.SAMPLE_SOURCE_ONLY}
        assert set(samples[0]) == set(observe.CSV_FIELDS)

    def test_main_writes_md_and_csv(self, observe_db, tmp_path):
        out_dir = tmp_path / "observe"
        rc = observe.main([
            "2", "--sample", "5", "--db", str(observe_db), "--out-dir", str(out_dir)
        ])
        assert rc == 0

        md = out_dir / f"{date.today().isoformat()}.md"
        csv_path = out_dir / f"{date.today().isoformat()}.csv"
        assert md.exists() and csv_path.exists()
        body = md.read_text(encoding="utf-8")
        assert "## 소스별 카운트" in body
        assert "| **합계** |" in body
        assert csv_path.read_text(encoding="utf-8").splitlines()[0].startswith("표본군,id,")

    def test_main_rejects_zero_days(self, observe_db, tmp_path):
        assert observe.main([
            "0", "--db", str(observe_db), "--out-dir", str(tmp_path)
        ]) == 2


class TestObserveOnUnmigratedDb:
    """마이그레이션 7 이 아직 안 닿은 DB — 관찰은 **쓰지 않고** 세고, 밝힌다.

    운영 DB 는 파이프라인이 한 번 돌아야 컬럼이 생긴다. 그 전에 관찰을 돌리면
    0 이 나오는데, 그 0 이 "측정해 보니 0" 으로 읽히면 안 된다.
    """

    @pytest.fixture
    def legacy_db(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        now = datetime.now().isoformat()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE announcements ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,"
            " source_id TEXT NOT NULL, title TEXT NOT NULL, summary TEXT DEFAULT '',"
            " url TEXT NOT NULL, relevance_score REAL DEFAULT 0.0,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE run_history ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,"
            " source TEXT NOT NULL, total_fetched INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO announcements"
            " (source, source_id, title, url, relevance_score, created_at, updated_at)"
            " VALUES ('kofpi', 'old-1', '산림 목재 이용 공고',"
            " 'https://example.com/old-1', 0.5, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO run_history (started_at, source, total_fetched)"
            " VALUES (?, 'kofpi', 20)",
            (now,),
        )
        conn.commit()
        conn.close()
        return db_path

    def test_has_council_columns_is_false(self, legacy_db):
        conn = observe.open_readonly(legacy_db)
        try:
            assert observe.has_council_columns(conn) is False
        finally:
            conn.close()

    def test_counts_fall_back_to_company_only(self, legacy_db):
        conn = observe.open_readonly(legacy_db)
        try:
            counts = observe.collect_counts(conn, observe.window_start(2))
        finally:
            conn.close()
        assert counts["kofpi"] == {
            "fetched": 20, "stored": 1, "council": 0, "unmeasured": 1,
            "company": 1, "both": 0,
        }

    def test_report_states_migration_is_pending(self, legacy_db, tmp_path):
        out_dir = tmp_path / "observe"
        assert observe.main([
            "2", "--sample", "5", "--db", str(legacy_db), "--out-dir", str(out_dir)
        ]) == 0
        body = (out_dir / f"{date.today().isoformat()}.md").read_text(encoding="utf-8")
        assert observe.MIGRATION_PENDING_NOTE in body

    def test_samples_still_list_council_source_rows(self, legacy_db):
        conn = observe.open_readonly(legacy_db)
        try:
            samples = observe.collect_samples(
                conn, observe.window_start(2), {"kofpi"}, 25, 0
            )
        finally:
            conn.close()
        assert [s["표본군"] for s in samples] == [observe.SAMPLE_SOURCE_ONLY]
        # 컬럼이 없는 DB 의 행은 **미측정**이지 미매치가 아니다.
        assert samples[0]["council_match"] is None


# ---------------------------------------------------------------------------
# 5. 라운드 2 — Codex 게이트
# ---------------------------------------------------------------------------

class TestPromotionToCompanyPath:
    """HIGH: 협의회 단독 행이 나중에 회사 임계를 통과하면 **승격**한다."""

    def council_row(self):
        return AnalyzedAnnouncement(
            source="kofpi", source_id="promo-1", title="산림 목재 이용 공고",
            url="https://example.com/promo-1", relevance_score=0.05,
            relevance_reason="추가 키워드 1개", matched_keywords=["산림"],
            council_score=0.5, council_tags='{}', council_match=1, council_only=1,
        )

    def company_row(self, url="https://example.com/promo-1"):
        return AnalyzedAnnouncement(
            source="kofpi", source_id="promo-1", title="산림 사회적기업 지원사업 공고",
            url=url, relevance_score=0.8,
            relevance_reason="필수 키워드 매칭", matched_keywords=["사회적기업"],
            council_score=0.55, council_tags='{}', council_match=1, council_only=0,
        )

    def test_two_runs_promote_to_notification_candidate(self, tmp_path):
        """1회차 협의회 단독 → 2회차 회사 통과 → 알림 후보 1건(0.8)."""
        db = Database(tmp_path / "promo.db")
        try:
            db.insert_announcement(self.council_row())
            assert db.get_unnotified() == []          # 1회차: 알림 후보 아님

            # 2회차: 같은 (source, source_id) 로 회사 점수 재삽입 (URL 변경분 포함)
            db.insert_announcement(self.company_row("https://example.com/promo-1?v=2"))

            candidates = db.get_unnotified()
            assert len(candidates) == 1
            assert candidates[0].relevance_score == pytest.approx(0.8)

            row = db.conn.execute(
                "SELECT council_only, is_notified, relevance_reason"
                "  FROM announcements WHERE source_id = 'promo-1'"
            ).fetchone()
            assert row["council_only"] == 0
            assert row["is_notified"] == 0
            assert row["relevance_reason"] == "필수 키워드 매칭"
        finally:
            db.close()

    def test_company_row_is_never_downgraded(self, tmp_path):
        """회사 행이 협의회 단독으로 강등되면 조용히 알림에서 사라진다 — 금지."""
        db = Database(tmp_path / "promo.db")
        try:
            db.insert_announcement(self.company_row())
            db.insert_announcement(self.council_row())

            row = db.conn.execute(
                "SELECT council_only FROM announcements WHERE source_id = 'promo-1'"
            ).fetchone()
            assert row["council_only"] == 0
            assert len(db.get_unnotified()) == 1
        finally:
            db.close()

    def test_update_keeps_prior_measurement_when_profile_is_off(self, tmp_path):
        """프로파일이 꺼진 실행(측정값 None)이 먼저 잰 측정을 지우지 않는다."""
        db = Database(tmp_path / "promo.db")
        try:
            db.insert_announcement(self.council_row())
            db.insert_announcement(AnalyzedAnnouncement(
                source="kofpi", source_id="promo-1", title="산림 목재 이용 공고",
                url="https://example.com/promo-1", relevance_score=0.8,
            ))
            row = db.conn.execute(
                "SELECT council_score, council_match FROM announcements"
                "  WHERE source_id = 'promo-1'"
            ).fetchone()
            assert row["council_score"] == pytest.approx(0.5)
            assert row["council_match"] == 1
        finally:
            db.close()


class TestExcludeTokenBoundary:
    """MEDIUM (a): 제외어는 토큰 경계로만 맞는다 — Codex 재현 문자열."""

    @pytest.fixture
    def inSA(self):
        return CouncilProfileConfig(
            sources=["kofpi"], must_match=["산림", "지원사업"], exclude=["인사"]
        )

    def test_insight_is_not_excluded(self, inSA):
        v = council.score_item(inSA, "kofpi", "산림 인사이트 지원사업")
        assert v.match == 1, v.reason

    def test_spaced_words_do_not_form_the_term(self, inSA):
        v = council.score_item(
            inSA, "kofpi", "산림 지원사업", summary="법인 사업자 대상 확인 사항"
        )
        assert v.match == 1, v.reason

    def test_standalone_token_is_excluded(self, inSA):
        v = council.score_item(inSA, "kofpi", "산림청 인사 발령 알림 지원사업")
        assert v.match == 0
        assert v.reason == "제외 키워드: 인사"

    def test_multi_token_phrase_matches_in_order(self):
        profile = CouncilProfileConfig(
            sources=["kofpi"], must_match=["공모전"], exclude=["수상작 발표"]
        )
        hit = council.score_item(profile, "kofpi", "「K-포레스트」 공모전 수상작 발표")
        assert hit.reason == "제외 키워드: 수상작 발표"

        miss = council.score_item(profile, "kofpi", "공모전 발표 수상작 안내")
        assert miss.match == 1, miss.reason

    def test_production_exclude_terms_are_token_matched(self, profile):
        """운영 사전으로도 같다: 괄호에 싸인 제외어는 잡고, 접사는 안 잡는다."""
        boxed = council.score_item(
            profile, "kofpi", "[공모결과] 2026년 임산물 가공유통 선정 결과"
        )
        assert boxed.reason == "제외 키워드: 공모결과"


class TestPerFieldEvaluation:
    """MEDIUM (b): 필드를 이어 붙이지 않는다."""

    @pytest.fixture
    def coop_profile(self):
        return CouncilProfileConfig(sources=["coop"], must_match=["협동조합"])

    def test_split_across_title_and_summary_does_not_match(self, coop_profile):
        v = council.score_item(
            coop_profile, "coop", title="지역 협동", summary="조합 지원사업 안내"
        )
        assert v.match == 0, v.reason

    def test_within_one_field_whitespace_is_still_absorbed(self, coop_profile):
        v = council.score_item(coop_profile, "coop", title="지역 협동 조합 안내")
        assert v.match == 1

    def test_summary_alone_can_match(self, coop_profile):
        v = council.score_item(
            coop_profile, "coop", title="안내문", summary="협동조합 설립 상담"
        )
        assert v.match == 1

    def test_tags_are_also_per_field(self):
        profile = CouncilProfileConfig(
            sources=["coop"], must_match=["안내"],
            region={"경기": ["경기도"]},
        )
        split = council.score_item(
            profile, "coop", title="상담 안내 경기", summary="도 지원사업"
        )
        assert "region" not in split.tags


class TestObserveOutputSafety:
    """MEDIUM: 출력이 입력 DB 를 덮어쓰지 못한다."""

    def test_refuses_when_output_resolves_to_input_db(self, tmp_path, observe_db):
        collision = tmp_path / f"{date.today().isoformat()}.md"
        observe_db.rename(collision)
        assert observe.main([
            "2", "--db", str(collision), "--out-dir", str(tmp_path)
        ]) == 2
        # DB 가 살아 있다 — 덮어쓰지 않았다.
        conn = sqlite3.connect(collision)
        try:
            assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 3
        finally:
            conn.close()

    def test_refuses_existing_non_report_file(self, tmp_path, observe_db):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        assert observe.unsafe_output(observe_db, (observe_db,)).startswith("✗")
        assert observe.unsafe_output(observe_db, (out_dir / "x.md",)) == ""

    def test_unsafe_output_flags_foreign_extension(self, tmp_path, observe_db):
        stray = tmp_path / "report.sqlite"
        stray.write_text("not a report", encoding="utf-8")
        assert "md/.csv" in observe.unsafe_output(observe_db, (stray,))


class TestObserveUnmeasuredVsUnmatched:
    """LOW: 미측정(NULL)과 미매치(0)를 따로 센다."""

    @pytest.fixture
    def mixed_db(self, tmp_path):
        db_path = tmp_path / "mixed.db"
        Database(db_path).close()
        now = datetime.now().isoformat()
        conn = sqlite3.connect(db_path)
        rows = [("m1", 1), ("m0", 0), ("mnull", None)]
        for sid, match in rows:
            conn.execute(
                "INSERT INTO announcements"
                " (source, source_id, title, summary, url, created_at, updated_at,"
                "  council_match, council_only)"
                " VALUES ('kofpi', ?, '산림 공고', '', ?, ?, ?, ?, 0)",
                (sid, f"https://example.com/{sid}", now, now, match),
            )
        conn.commit()
        conn.close()
        return db_path

    def test_counts_are_separate(self, mixed_db):
        conn = observe.open_readonly(mixed_db)
        try:
            counts = observe.collect_counts(conn, observe.window_start(2))
        finally:
            conn.close()
        assert counts["kofpi"]["stored"] == 3
        assert counts["kofpi"]["council"] == 1
        assert counts["kofpi"]["unmeasured"] == 1

    def test_unmeasured_row_is_not_reported_as_unmatched_only(self, mixed_db):
        conn = observe.open_readonly(mixed_db)
        try:
            counts = observe.collect_counts(conn, observe.window_start(2))
        finally:
            conn.close()
        c = counts["kofpi"]
        unmatched_only = c["stored"] - c["council"] - c["unmeasured"]
        assert unmatched_only == 1


class TestObserveDropSection:
    """탈락 표본 — 저장되지 않은 항목을 원장에서 읽어 보여 준다."""

    @pytest.fixture
    def dropped_db(self, tmp_path):
        db_path = tmp_path / "dropped.db"
        db = Database(db_path)
        db.record_council_drops([council.CouncilDrop(
            source="kofpi", source_id="d1", title="추석 명절 선물 안내",
            url="https://example.com/d1", posted_at="2026-09-10",
            company_score=0.05, council_score=0.0, reason="협의회 어휘 없음",
        )])
        db.close()
        return db_path

    def test_collect_drops_reads_ledger(self, dropped_db):
        conn = observe.open_readonly(dropped_db)
        try:
            drops = observe.collect_drops(conn, observe.window_start(2), 25, 0)
        finally:
            conn.close()
        assert len(drops) == 1
        assert drops[0]["reason"] == "협의회 어휘 없음"

    def test_report_has_drop_section(self, dropped_db, tmp_path):
        out_dir = tmp_path / "observe"
        assert observe.main([
            "2", "--sample", "5", "--db", str(dropped_db), "--out-dir", str(out_dir)
        ]) == 0
        body = (out_dir / f"{date.today().isoformat()}.md").read_text(encoding="utf-8")
        assert "## 탈락 표본 (저장되지 않은 항목)" in body
        assert "추석 명절 선물 안내" in body

    def test_missing_ledger_is_announced(self, tmp_path):
        db_path = tmp_path / "noledger.db"
        now = datetime.now().isoformat()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE announcements ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,"
            " source_id TEXT NOT NULL, title TEXT NOT NULL, summary TEXT DEFAULT '',"
            " url TEXT NOT NULL, relevance_score REAL DEFAULT 0.0,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE run_history (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " started_at TEXT NOT NULL, source TEXT NOT NULL,"
            " total_fetched INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO announcements"
            " (source, source_id, title, url, created_at, updated_at)"
            " VALUES ('kofpi', 'x', '산림 공고', 'https://example.com/x', ?, ?)",
            (now, now),
        )
        conn.commit()
        conn.close()

        out_dir = tmp_path / "observe"
        assert observe.main(["2", "--db", str(db_path), "--out-dir", str(out_dir)]) == 0
        body = (out_dir / f"{date.today().isoformat()}.md").read_text(encoding="utf-8")
        assert observe.DROP_LEDGER_MISSING_NOTE in body


# db.py 의 announcements SELECT 중 council_only 가드가 **없어도 되는** 자리.
# 새 SELECT 를 추가하면 이 목록에 올리거나 가드를 달아야 테스트가 통과한다.
GUARD_EXEMPT_DB_FUNCTIONS = {
    # 식별·중복 제거·기간 관문 — 모든 행을 봐야 한다. 가드를 달면 협의회 단독
    # 행이 "없는 행" 이 되어 매 실행 중복 저장된다.
    "is_duplicate",
    "get_quoted_source_ids",
    "_find_row",
    "mark_legacy_rows",
    "revalidate_periods",
    "clear_periods_except",
    "overwrite_periods",
    "get_quote_attempts",
    # id 를 가진 쪽이 이미 대상을 골랐다 - 가드를 달면 존재하는 id 가 조용히
    # None 이 된다 (텔레그램 /app, 지식 레이어).
    "get_announcement_by_id",
    "get_announcement_domain",
}


class TestCompanyFacingQueryGuard:
    """sweep: db.py 의 회사향 SELECT 는 전부 council_only 가드를 갖는다."""

    def db_functions_with_announcement_selects(self):
        tree = ast.parse((REPO_ROOT / "alert" / "db.py").read_text(encoding="utf-8"))
        found = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            literals = [
                sub.value for sub in ast.walk(node)
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
            ]
            blob = "\n".join(literals)
            if "FROM announcements" in blob:
                found[node.name] = blob
        return found

    def test_every_select_is_guarded_or_exempt(self):
        offenders = [
            name for name, blob in self.db_functions_with_announcement_selects().items()
            if "council_only = 0" not in blob and name not in GUARD_EXEMPT_DB_FUNCTIONS
        ]
        assert offenders == [], f"council_only 가드 없는 회사향 쿼리: {offenders}"

    def test_guarded_functions_are_the_expected_ones(self):
        guarded = {
            name for name, blob in self.db_functions_with_announcement_selects().items()
            if "council_only = 0" in blob
        }
        assert guarded == {
            "get_unnotified", "search_announcements", "get_stats",
            "get_announcements_by_period", "get_domain_stats",
        }

    def test_telegram_recent_is_guarded(self):
        source = (REPO_ROOT / "alert" / "notifiers" / "telegram_bot.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "cmd_recent":
                blob = "\n".join(
                    sub.value for sub in ast.walk(node)
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                )
                assert "council_only = 0" in blob
                return
        pytest.fail("cmd_recent 을 찾지 못했다")
