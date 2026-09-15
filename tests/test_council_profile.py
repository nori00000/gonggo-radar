"""협의회 적재 프로파일 (P0 계약 §A) — 점수·저장 규칙·불변 조건 1·관찰 표.

네트워크를 타지 않는다: 크롤러·발송·notify 를 부르지 않고, DB 는 전부
임시 파일이다.
"""

import ast
import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from alert import council
from alert.config import CouncilProfileConfig, get_config
from alert.analyzer import ClaudeAnalyzer, KeywordAnalyzer
from alert import main as main_mod
from alert.db import (
    COUNCIL_DROP_RETENTION_DAYS,
    COUNCIL_RECHECK_MIN_HOURS,
    Database,
    announcement_content_hash,
)
from alert.migrations import MIGRATIONS
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

        extras, drops, unmatched = apply_council_profile(
            profile, "kofpi", items, selected, company_analyzer.analyze
        )
        assert unmatched == []
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
        extras, _drops, _unmatched = apply_council_profile(
            profile, "kofpi", items, [], company_analyzer.analyze
        )
        threshold = get_config().analyzer.keyword_threshold
        assert extras[0].relevance_score < threshold

    def test_excluded_item_is_dropped_not_stored(self, profile, company_analyzer):
        """제외어 항목은 저장되지 않고 **탈락 원장 레코드**로 온다."""
        items = [FIXTURE_ITEMS[5]]        # forest_press 호우 보도
        extras, drops, unmatched = apply_council_profile(
            profile, "forest_press", items, [], company_analyzer.analyze
        )
        assert extras == []
        assert len(drops) == 1
        assert drops[0].source_id == "p1"
        assert drops[0].reason.startswith("제외 키워드")
        # 같은 항목이 분석 객체로도 온다 - 재평가분의 측정 갱신에 쓰인다.
        assert [a.source_id for a in unmatched] == ["p1"]
        assert unmatched[0].council_match == 0

    def test_drop_records_posted_at_from_raw_data(self, profile, company_analyzer):
        """탈락 레코드의 posted_at 은 raw_data 의 ``posted`` 에서 온다."""
        item = raw("kofpi", "d1", "추석 명절 선물 안내")
        item.raw_data = json.dumps({"posted": "2026-09-10"}, ensure_ascii=False)
        _extras, drops, _unmatched = apply_council_profile(
            profile, "kofpi", [item], [], company_analyzer.analyze
        )
        assert drops[0].posted_at == "2026-09-10"

    def test_no_drops_for_non_council_sources(self, profile, company_analyzer):
        """협의회 소스가 아니면 측정도 탈락 기록도 하지 않는다."""
        assert apply_council_profile(
            profile, "smartfarm", [raw("smartfarm", "x1", "무관한 공고")],
            [], company_analyzer.analyze
        ) == ([], [], [])

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

        extras, drops, unmatched = apply_council_profile(
            profile, "kofpi", items, selected, company_analyzer.analyze
        )
        assert (extras, drops, unmatched) == ([], [], [])
        assert any(a.council_match == 1 for a in selected)

    def test_empty_profile_stores_nothing_extra(self, company_analyzer):
        """프로파일이 비면 배선 전체가 무동작이다."""
        assert apply_council_profile(
            CouncilProfileConfig(), "kofpi", FIXTURE_ITEMS, [], company_analyzer.analyze
        ) == ([], [], [])


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
        extras, _drops, _unmatched = apply_council_profile(
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
    # 상세 근거 보존·회전 — 협의회 단독 행에도 마감이 있다. 가드를 달면 그
    # 행들의 근거가 "없는 것" 이 되어 재수집 때 마감이 지워진다 (P2-D 라운드 2).
    "get_detail_evidence",
    # id 를 가진 쪽이 이미 대상을 골랐다 - 가드를 달면 존재하는 id 가 조용히
    # None 이 된다 (텔레그램 /app, 지식 레이어).
    "get_announcement_by_id",
    "get_announcement_domain",
}


def _sql_texts(node):
    """함수 안의 SQL 문자열들. f-string 은 치환부를 ``?`` 로 근사한다."""
    texts = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            texts.append(sub.value)
        elif isinstance(sub, ast.JoinedStr):
            texts.append("".join(
                part.value if isinstance(part, ast.Constant)
                and isinstance(part.value, str) else "?"
                for part in sub.values
            ))
    return texts


def _where_clause(sql):
    """``FROM announcements`` 를 가진 문장의 WHERE 절. 없으면 빈 문자열."""
    norm = " ".join(sql.split())
    upper = norm.upper()
    idx = upper.find("FROM ANNOUNCEMENTS")
    if idx < 0:
        return None
    where = upper.find("WHERE", idx)
    if where < 0:
        return ""
    end = len(norm)
    for keyword in ("ORDER BY", "GROUP BY", "LIMIT", "RETURNING"):
        pos = upper.find(keyword, where)
        if pos >= 0:
            end = min(end, pos)
    return norm[where:end]


def _announcement_selects_by_function(rel):
    """``rel`` 파일의 함수별 ``FROM announcements`` SELECT 문장들."""
    tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        statements = []
        for text in _sql_texts(node):
            flat = " ".join(text.split()).upper()
            if "FROM ANNOUNCEMENTS" not in flat or "SELECT" not in flat:
                continue
            statements.append(text)
        if statements:
            found[node.name] = statements
    return found


class TestCompanyFacingQueryGuard:
    """sweep: 회사향 SELECT 는 **그 문장의 WHERE 안에** 가드를 갖는다.

    라운드 2 는 함수 단위로 문자열을 합쳐 봤다 - 한 함수에 가드가 있는 쿼리와
    없는 쿼리가 섞여 있으면 통과해 버린다. 라운드 3 은 문장별로 WHERE 절을
    떼어 본다 (Codex 게이트 3R).
    """

    def test_every_select_is_guarded_in_its_own_where(self):
        offenders = []
        for name, statements in _announcement_selects_by_function("alert/db.py").items():
            if name in GUARD_EXEMPT_DB_FUNCTIONS:
                continue
            for sql in statements:
                where = _where_clause(sql)
                if not where or "COUNCIL_ONLY = 0" not in where.upper():
                    offenders.append((name, " ".join(sql.split())[:80]))
        assert offenders == [], f"WHERE 에 가드가 없는 회사향 쿼리: {offenders}"

    def test_guarded_functions_are_the_expected_ones(self):
        guarded = {
            name for name, statements
            in _announcement_selects_by_function("alert/db.py").items()
            if any("council_only = 0" in sql for sql in statements)
        }
        assert guarded == {
            "get_unnotified", "search_announcements", "get_stats",
            "get_announcements_by_period", "get_domain_stats",
        }

    def test_where_clause_helper_rejects_a_guardless_statement(self):
        """헬퍼 자체가 무르지 않은지 - 가드 없는 문장은 잡혀야 한다."""
        assert "COUNCIL_ONLY" not in _where_clause(
            "SELECT * FROM announcements WHERE is_notified = 0 ORDER BY id"
        ).upper()
        assert _where_clause("SELECT * FROM announcements") == ""

    def test_guard_outside_the_where_does_not_count(self):
        """ORDER BY 뒤에 문자열만 있는 것은 가드가 아니다."""
        sql = "SELECT * FROM announcements WHERE is_notified = 0 ORDER BY council_only = 0"
        assert "COUNCIL_ONLY = 0" not in _where_clause(sql).upper()

    def test_telegram_recent_is_guarded(self):
        selects = _announcement_selects_by_function(
            "alert/notifiers/telegram_bot.py"
        )
        assert "cmd_recent" in selects
        where = _where_clause(selects["cmd_recent"][0])
        assert "COUNCIL_ONLY = 0" in where.upper()


class TestPipelinePromotion:
    """HIGH (재검토): 같은 URL 로 다시 온 협의회 단독 행이 승격되는가.

    DB 계층이 아니라 **파이프라인 전체**를 돌린다. 네트워크는 타지 않는다 -
    크롤러는 스텁이고, 알림 두 채널은 config 에서 꺼져 있다.
    """

    SOURCE = "mois_sse"          # 협의회 소스이면서 bypass_threshold 가 없다
    SOURCE_ID = "promo-1"
    URL = "https://example.com/mois_sse/promo-1"

    COUNCIL_ONLY_TITLE = "산림 목재 이용 안내"          # 협의회만 매치
    COMPANY_TITLE = "사회적기업 지원사업 공고"           # 회사 must_match 매치

    def make_crawler(self, titles):
        source, source_id, url = self.SOURCE, self.SOURCE_ID, self.URL

        class FakeCrawler:
            """네트워크를 타지 않는 크롤러 스텁."""

            def is_enabled(self):
                return True

            def set_quoted_source_ids(self, ids):
                return None

            def safe_fetch(self):
                return [RawAnnouncement(
                    source=source, source_id=source_id,
                    title=titles[0], url=url, summary="",
                )]

        return FakeCrawler

    def run_once(self, monkeypatch, db_path, title, captured):
        titles = [title]
        monkeypatch.setattr(main_mod, "Database", lambda *a, **k: Database(db_path))
        monkeypatch.setattr(
            main_mod, "setup_logger",
            lambda *a, **k: logging.getLogger("test-pipeline"),
        )
        monkeypatch.setattr(
            main_mod, "_import_crawlers",
            lambda: {self.SOURCE: self.make_crawler(titles)},
        )
        original = Database.get_unnotified

        def spy(db_self):
            rows = original(db_self)
            captured.append([(a.source_id, a.relevance_score) for a in rows])
            return rows

        monkeypatch.setattr(Database, "get_unnotified", spy)
        main_mod.run_pipeline()

    def row(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            return dict(conn.execute(
                "SELECT relevance_score, council_score, council_match, council_only,"
                "       is_notified FROM announcements WHERE source_id = ?",
                (self.SOURCE_ID,),
            ).fetchone())
        finally:
            conn.close()

    def test_same_url_second_run_promotes(self, tmp_path, monkeypatch):
        db_path = tmp_path / "pipeline.db"
        captured = []

        # 1회차: 협의회 어휘만 -> 협의회 단독 저장, 알림 후보 0건
        with monkeypatch.context() as m:
            self.run_once(m, db_path, self.COUNCIL_ONLY_TITLE, captured)
        first = self.row(db_path)
        assert first["council_only"] == 1
        assert first["council_match"] == 1
        assert captured[0] == []

        # 2회차: **같은 URL·같은 source_id**, 회사 어휘가 맞는 제목
        with monkeypatch.context() as m:
            self.run_once(m, db_path, self.COMPANY_TITLE, captured)

        second = self.row(db_path)
        assert second["council_only"] == 0, "승격되지 않았다"
        assert second["relevance_score"] >= get_config().analyzer.keyword_threshold
        assert captured[1] == [(self.SOURCE_ID, second["relevance_score"])]
        assert len(captured[1]) == 1

    def test_still_council_only_when_company_keeps_failing(self, tmp_path, monkeypatch):
        """회사가 계속 탈락하면 플래그는 그대로고 측정만 새로 고쳐진다."""
        db_path = tmp_path / "pipeline.db"
        captured = []
        with monkeypatch.context() as m:
            self.run_once(m, db_path, self.COUNCIL_ONLY_TITLE, captured)
        with monkeypatch.context() as m:
            self.run_once(m, db_path, "임업 산촌자원 개발 안내", captured)

        row = self.row(db_path)
        assert row["council_only"] == 1
        assert row["council_match"] == 1
        assert captured[1] == []

    def test_row_is_not_duplicated_across_runs(self, tmp_path, monkeypatch):
        db_path = tmp_path / "pipeline.db"
        captured = []
        for title in (self.COUNCIL_ONLY_TITLE, self.COMPANY_TITLE):
            with monkeypatch.context() as m:
                self.run_once(m, db_path, title, captured)
        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM announcements"
            ).fetchone()[0] == 1
        finally:
            conn.close()


class TestExcludePunctuation:
    """MEDIUM (재검토): 구두점도 토큰 분리자다."""

    @pytest.fixture
    def punct_profile(self):
        return CouncilProfileConfig(
            sources=["kofpi"], must_match=["사회적기업"], exclude=["인사"]
        )

    def test_middot_separates(self, punct_profile):
        v = council.score_item(punct_profile, "kofpi", "인사·발령 사회적기업")
        assert v.match == 0
        assert v.reason == "제외 키워드: 인사"

    def test_comma_separates(self, punct_profile):
        v = council.score_item(punct_profile, "kofpi", "인사, 사회적기업")
        assert v.match == 0
        assert v.reason == "제외 키워드: 인사"

    @pytest.mark.parametrize("text", [
        "인사.사회적기업", "인사/사회적기업", "인사:사회적기업",
        "[인사] 사회적기업", "(인사) 사회적기업",
    ])
    def test_other_separators(self, punct_profile, text):
        assert council.score_item(punct_profile, "kofpi", text).match == 0

    def test_word_interior_still_survives(self, punct_profile):
        """구두점이 없으면 여전히 한 토큰이라 제외되지 않는다."""
        assert council.score_item(
            punct_profile, "kofpi", "인사이트 사회적기업"
        ).match == 1

    def test_must_match_is_unaffected_by_punctuation(self):
        """must_match 는 종전 규칙 그대로 (필드 내 공백 흡수, 구두점 유지)."""
        profile = CouncilProfileConfig(
            sources=["coop"], must_match=["협동조합", "사회적기업"]
        )
        assert council.score_item(profile, "coop", "협동 조합 안내").match == 1
        assert council.score_item(profile, "coop", "사회적기업·협동조합").match == 1

    def test_tokens_helper_splits_on_punctuation(self):
        assert council.tokens("인사·발령, 사회적기업") == [
            "인사", "발령", "사회적기업"
        ]


class TestObserveHardlinkSafety:
    """MEDIUM (재검토): 하드 링크로 경로 검사를 우회할 수 없다."""

    def test_hardlinked_target_is_refused(self, tmp_path, observe_db):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        link = out_dir / f"{date.today().isoformat()}.md"
        os.link(observe_db, link)              # 하드 링크: resolve() 는 다르다
        assert link.resolve() != observe_db.resolve()

        assert observe.main([
            "2", "--db", str(observe_db), "--out-dir", str(out_dir)
        ]) == 2

        conn = sqlite3.connect(observe_db)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM announcements"
            ).fetchone()[0] == 3
        finally:
            conn.close()

    def test_db_hardlinked_under_another_name_in_out_dir(self, tmp_path, observe_db):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        os.link(observe_db, out_dir / "backup.md")
        assert observe.main([
            "2", "--db", str(observe_db), "--out-dir", str(out_dir)
        ]) == 2

    def test_samefile_catches_hardlink_without_the_dir_scan(self, tmp_path, observe_db):
        """디렉터리 스캔에 기대지 않고 타깃 자체가 같은 inode 인지 본다.

        하드 링크는 경로도 resolve() 결과도 다르므로 경로 비교만으로는 통과한다.
        """
        link = tmp_path / "link.md"
        os.link(observe_db, link)
        assert link.resolve() != observe_db.resolve()
        assert observe.unsafe_output(observe_db, (link,)).startswith("✗")

    def test_unrelated_out_dir_is_fine(self, tmp_path, observe_db):
        out_dir = tmp_path / "clean"
        assert observe.unsafe_output(
            observe_db, (out_dir / "a.md", out_dir / "a.csv"), out_dir
        ) == ""


class TestDropLedgerPrivacy:
    """MEDIUM (재검토): 원장에 연락처를 남기지 않는다."""

    def test_masks_email_and_phone(self, tmp_path):
        db = Database(tmp_path / "mask.db")
        try:
            db.record_council_drops([council.CouncilDrop(
                source="kofpi", source_id="m1",
                title="문의 hong.gil@example.co.kr 또는 010-1234-5678 로 연락",
                url="https://example.com/apply?email=hong.gil@example.co.kr",
                posted_at="2026-09-10", company_score=0.0, council_score=0.0,
                reason="협의회 어휘 없음",
            )])
            row = db.conn.execute(
                "SELECT title, url FROM council_dropped WHERE source_id = 'm1'"
            ).fetchone()
        finally:
            db.close()

        assert "@" not in row["title"] and "@" not in row["url"]
        assert "010-1234-5678" not in row["title"]
        assert "[이메일]" in row["title"] and "[이메일]" in row["url"]
        assert "[전화]" in row["title"]

    def test_dates_are_not_mistaken_for_phone_numbers(self, tmp_path):
        db = Database(tmp_path / "mask.db")
        try:
            db.record_council_drops([council.CouncilDrop(
                source="kofpi", source_id="m2",
                title="2026-09-13 접수 마감 (제1차, 2025.10.01 공고)",
                url="https://example.com/x", reason="협의회 어휘 없음",
            )])
            row = db.conn.execute(
                "SELECT title FROM council_dropped WHERE source_id = 'm2'"
            ).fetchone()
        finally:
            db.close()
        assert row["title"] == "2026-09-13 접수 마감 (제1차, 2025.10.01 공고)"

    def test_mask_helper_is_pure(self):
        from alert.db import mask_contacts
        assert mask_contacts("") == ""
        assert mask_contacts(None) == ""
        assert mask_contacts("010-1234-5678", phones=False) == "010-1234-5678"


class TestMigrationSevenDefaults:
    """LOW (재검토): 마이그레이션 7 의 NULL 기본값이 되돌아가지 않게 못을 박는다."""

    def migration_sql(self, version):
        for ver, _desc, statements in MIGRATIONS:
            if ver == version:
                return " ".join(statements)
        raise AssertionError(f"migration {version} 을 찾지 못했다")

    def test_measurement_columns_declare_null_default(self):
        sql = self.migration_sql(7)
        for column in ("council_score REAL", "council_tags TEXT",
                       "council_match INTEGER"):
            assert f"ADD COLUMN {column} DEFAULT NULL" in sql

    def test_guard_column_keeps_zero_default(self):
        assert "ADD COLUMN council_only INTEGER DEFAULT 0" in self.migration_sql(7)

    def test_no_backfill_migration_rewrites_measurements(self):
        """측정값을 **소급해 고치는** 마이그레이션은 없다 (보고서 §R3-5).

        7 을 적용한 DB 가 존재한 적이 없어 되돌릴 대상이 없고, 구 기본값
        DB 에서는 미측정과 측정된 미매치를 SQL 로 구분할 수 없다. 그래서
        어떤 마이그레이션도 council_* 값을 UPDATE 하지 않는다 - 라운드 4 의
        마이그레이션 9 도 컬럼만 더할 뿐이다.
        """
        for version, _desc, statements in MIGRATIONS:
            for sql in statements:
                flat = " ".join(sql.split()).upper()
                if "UPDATE ANNOUNCEMENTS" in flat and "COUNCIL_" in flat:
                    raise AssertionError(f"migration {version} 이 측정값을 고친다: {sql}")

    def test_recheck_bookkeeping_columns_are_nullable(self):
        sql = self.migration_sql(9)
        assert "ADD COLUMN council_rechecked_at TEXT DEFAULT NULL" in sql
        assert "ADD COLUMN council_content_hash TEXT DEFAULT NULL" in sql


# ---------------------------------------------------------------------------
# 6. 라운드 4 — Codex 재재검토
# ---------------------------------------------------------------------------

class _StubLLM:
    """LLM 백엔드 스텁 — 주어진 점수로 전량을 다시 매긴다."""

    def __init__(self, score):
        self.score = score
        self.client = object()          # llm_available 을 True 로
        self.backend = "claude"
        self.seen = []

    def analyze_batch(self, announcements):
        """상한이 없는 백엔드 — 받은 항목을 전부 본다."""
        self.seen.append([a.source_id for a in announcements])
        for ann in announcements:
            ann.relevance_score = self.score
            ann.llm_evaluated = True
        return list(announcements)


class TestRecheckGoesThroughTheSameGates(TestPipelinePromotion):
    """HIGH: 재평가분도 신규와 **같은** 선택 경로(키워드 + LLM)를 지난다."""

    def run_with_llm(self, monkeypatch, db_path, title, captured, llm):
        titles = [title]
        monkeypatch.setattr(main_mod, "Database", lambda *a, **k: Database(db_path))
        monkeypatch.setattr(
            main_mod, "setup_logger",
            lambda *a, **k: logging.getLogger("test-pipeline"),
        )
        monkeypatch.setattr(
            main_mod, "_import_crawlers",
            lambda: {self.SOURCE: self.make_crawler(titles)},
        )
        if llm is not None:
            monkeypatch.setattr(main_mod, "ClaudeAnalyzer", lambda: llm)
        original = Database.get_unnotified

        def spy(db_self):
            rows = original(db_self)
            captured.append([a.source_id for a in rows])
            return rows

        monkeypatch.setattr(Database, "get_unnotified", spy)
        main_mod.run_pipeline()

    def seed_council_only(self, monkeypatch, db_path, captured):
        with monkeypatch.context() as m:
            self.run_once(m, db_path, self.COUNCIL_ONLY_TITLE, captured)
        assert self.row(db_path)["council_only"] == 1

    def test_llm_rejection_blocks_promotion(self, tmp_path, monkeypatch):
        """키워드는 통과해도 LLM 이 거절하면 승격되지 않는다."""
        db_path = tmp_path / "llm.db"
        captured = []
        self.seed_council_only(monkeypatch, db_path, captured)

        llm = _StubLLM(0.0)          # claude_threshold(0.3) 미만 -> 전량 탈락
        with monkeypatch.context() as m:
            self.run_with_llm(m, db_path, self.COMPANY_TITLE, captured, llm)

        row = self.row(db_path)
        assert row["council_only"] == 1, "LLM 관문을 건너뛰고 승격됐다"
        assert captured[1] == []
        # 재평가 항목이 실제로 LLM 배치에 들어갔다는 증거
        assert llm.seen and self.SOURCE_ID in llm.seen[0]

    def test_llm_acceptance_promotes(self, tmp_path, monkeypatch):
        db_path = tmp_path / "llm.db"
        captured = []
        self.seed_council_only(monkeypatch, db_path, captured)

        llm = _StubLLM(0.9)
        with monkeypatch.context() as m:
            self.run_with_llm(m, db_path, self.COMPANY_TITLE, captured, llm)

        row = self.row(db_path)
        assert row["council_only"] == 0
        assert row["relevance_score"] == pytest.approx(0.9)
        assert captured[1] == [self.SOURCE_ID]

    def test_single_selection_path_in_source(self):
        """소스에 선택 함수 호출이 하나뿐인지 — 두 번째 경로가 다시 생기면 실패."""
        source = (REPO_ROOT / "alert" / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "select_for_storage"
        ]
        assert len(calls) == 1, "select_for_storage 호출이 하나여야 한다"


class TestRecheckMeasurementRefresh:
    """MEDIUM: 양쪽 탈락한 재평가 행도 측정값이 새 판정으로 갱신된다."""

    SOURCE = "mois_sse"
    SOURCE_ID = "refresh-1"

    def test_unmatched_recheck_refreshes_measurement(self, tmp_path, monkeypatch):
        db_path = tmp_path / "refresh.db"
        captured = []
        promo = TestPipelinePromotion()
        promo.SOURCE, promo.SOURCE_ID = self.SOURCE, self.SOURCE_ID
        promo.URL = f"https://example.com/{self.SOURCE}/{self.SOURCE_ID}"

        # 1회차: 협의회 매치 -> council_only=1, council_match=1
        with monkeypatch.context() as m:
            promo.run_once(m, db_path, "산림 목재 이용 안내", captured)
        first = promo.row(db_path)
        assert (first["council_only"], first["council_match"]) == (1, 1)

        # 2회차: 같은 행, 어느 프로파일에도 걸리지 않는 제목
        with monkeypatch.context() as m:
            promo.run_once(m, db_path, "추석 명절 청탁금지법 선물 바로알기", captured)

        second = promo.row(db_path)
        assert second["council_match"] == 0, "측정값이 갱신되지 않았다"
        assert second["council_only"] == 1, "회사에 고른 적이 없으므로 플래그는 그대로"

        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM announcements"
            ).fetchone()[0] == 1, "행이 사라지거나 늘었다"
            assert conn.execute(
                "SELECT COUNT(*) FROM council_dropped"
            ).fetchone()[0] == 0, "이미 저장된 행이 탈락 원장에 들어갔다"
        finally:
            conn.close()


class TestMarkNotifiedScope:
    """MEDIUM: 보낸 행만 알림 완료로 표시한다."""

    def rows(self, db):
        return {
            (r["source"], r["source_id"]): r["is_notified"]
            for r in db.conn.execute(
                "SELECT source, source_id, is_notified FROM announcements"
            ).fetchall()
        }

    def test_same_source_id_in_another_source_is_untouched(self, tmp_path):
        """(A,x) 발송이 (B,x) 를 건드리지 않는다."""
        db = Database(tmp_path / "notify.db")
        try:
            company = AnalyzedAnnouncement(
                source="seis", source_id="x", title="사회적기업 지원사업 공고",
                url="https://example.com/a/x", relevance_score=0.5,
                council_match=1, council_only=0,
            )
            council_only = AnalyzedAnnouncement(
                source="kofpi", source_id="x", title="산림 목재 이용 공고",
                url="https://example.com/b/x", relevance_score=0.05,
                council_score=0.5, council_tags="{}", council_match=1, council_only=1,
            )
            db.insert_announcement(company)
            db.insert_announcement(council_only)

            sent = db.get_unnotified()
            assert [a.source for a in sent] == ["seis"]
            for ann in sent:
                db.mark_notified(ann)

            marks = self.rows(db)
            assert marks[("seis", "x")] == 1
            assert marks[("kofpi", "x")] == 0, "보내지 않은 행이 알림 완료가 됐다"
        finally:
            db.close()

    def test_falls_back_to_storage_key_without_row_id(self, tmp_path):
        db = Database(tmp_path / "notify.db")
        try:
            db.insert_announcement(AnalyzedAnnouncement(
                source="seis", source_id="x", title="사회적기업 공고",
                url="https://example.com/a/x", relevance_score=0.5,
            ))
            db.insert_announcement(AnalyzedAnnouncement(
                source="kofpi", source_id="x", title="산림 공고",
                url="https://example.com/b/x", relevance_score=0.5,
            ))
            db.mark_notified(AnalyzedAnnouncement(
                source="seis", source_id="x", title="", url="",
            ))
            marks = self.rows(db)
            assert marks[("seis", "x")] == 1
            assert marks[("kofpi", "x")] == 0
        finally:
            db.close()

    def test_only_one_production_caller(self):
        """호출자 감사 — 파이프라인 한 곳만 부른다."""
        hits = []
        for rel in ("alert/main.py", "alert/db.py",
                    "alert/notifiers/telegram_bot.py", "alert/knowledge.py"):
            for num, line in enumerate(
                (REPO_ROOT / rel).read_text(encoding="utf-8").splitlines(), 1
            ):
                if "mark_notified(" in line and "def mark_notified" not in line:
                    hits.append((rel, num, line.strip()))
        # 줄 번호는 고정하지 않는다 - 호출 **수와 자리**만 고정한다.
        assert len(hits) == 1, hits
        rel, _num, text = hits[0]
        assert rel == "alert/main.py"
        assert text == "db.mark_notified(ann)", text


class TestRecheckThrottle:
    """LOW: 같은 행을 24시간 안에 두 번 채점하지 않는다 (본문이 그대로면)."""

    def stored(self, tmp_path, title="산림 목재 이용 공고"):
        db = Database(tmp_path / "throttle.db")
        db.insert_announcement(AnalyzedAnnouncement(
            source="mois_sse", source_id="t1", title=title,
            url="https://example.com/t1", relevance_score=0.05,
            council_score=0.5, council_tags="{}", council_match=1, council_only=1,
        ))
        return db

    def fresh(self, title="산림 목재 이용 공고"):
        return RawAnnouncement(
            source="mois_sse", source_id="t1", title=title,
            url="https://example.com/t1",
        )

    def test_first_sighting_is_always_rechecked(self, tmp_path):
        db = self.stored(tmp_path)
        try:
            assert db.needs_council_recheck(self.fresh()) is True
        finally:
            db.close()

    def test_recent_recheck_with_same_content_is_skipped(self, tmp_path):
        db = self.stored(tmp_path)
        try:
            db.mark_council_rechecked(self.fresh())
            assert db.needs_council_recheck(self.fresh()) is False
        finally:
            db.close()

    def test_changed_content_forces_a_recheck(self, tmp_path):
        db = self.stored(tmp_path)
        try:
            db.mark_council_rechecked(self.fresh())
            assert db.needs_council_recheck(
                self.fresh("사회적기업 지원사업 공고")
            ) is True
        finally:
            db.close()

    def test_stale_bookkeeping_forces_a_recheck(self, tmp_path):
        db = self.stored(tmp_path)
        try:
            db.mark_council_rechecked(self.fresh())
            stale = datetime.now() - timedelta(
                hours=COUNCIL_RECHECK_MIN_HOURS + 1
            )
            db.conn.execute(
                "UPDATE announcements SET council_rechecked_at = ?"
                " WHERE source_id = 't1'",
                (stale.isoformat(),),
            )
            db.conn.commit()
            assert db.needs_council_recheck(self.fresh()) is True
        finally:
            db.close()

    def test_company_rows_are_never_rechecked(self, tmp_path):
        db = Database(tmp_path / "throttle.db")
        try:
            db.insert_announcement(AnalyzedAnnouncement(
                source="mois_sse", source_id="c1", title="사회적기업 공고",
                url="https://example.com/c1", relevance_score=0.5, council_only=0,
            ))
            assert db.needs_council_recheck(RawAnnouncement(
                source="mois_sse", source_id="c1", title="사회적기업 공고",
                url="https://example.com/c1",
            )) is False
        finally:
            db.close()

    def test_unreadable_timestamp_is_treated_as_unknown(self, tmp_path):
        db = self.stored(tmp_path)
        try:
            db.mark_council_rechecked(self.fresh())
            db.conn.execute(
                "UPDATE announcements SET council_rechecked_at = 'not-a-date'"
                " WHERE source_id = 't1'"
            )
            db.conn.commit()
            assert db.needs_council_recheck(self.fresh()) is True
        finally:
            db.close()

    def test_hash_covers_every_scored_field(self):
        """해시가 score_item 이 보는 네 필드를 전부 덮는가."""
        base = RawAnnouncement(source="s", source_id="i", title="t", url="u")
        baseline = announcement_content_hash(base)
        for field_name in ("title", "summary", "target", "category"):
            other = RawAnnouncement(source="s", source_id="i", title="t", url="u")
            setattr(other, field_name, "바뀐값")
            assert announcement_content_hash(other) != baseline, field_name

    def _run_counting_selections(self, monkeypatch, promo, db_path, title, sizes):
        """파이프라인 1회 실행 + 선택 함수에 들어간 항목 수 기록."""
        original = main_mod.select_for_storage

        def spy(analyzer, raw_items, *args, **kwargs):
            # *args 로 받는 이유: select_for_storage 는 source_kind 가 붙으며
            # 인자가 늘었다(P1-R 라운드 2). 이 스파이는 **몇 건이 들어갔는가**
            # 만 세므로 나머지는 그대로 넘긴다.
            sizes.append(len(raw_items))
            return original(analyzer, raw_items, *args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(main_mod, "select_for_storage", spy)
            promo.run_once(m, db_path, title, [])

    def test_pipeline_skips_a_fresh_recheck(self, tmp_path, monkeypatch):
        """3회차는 **선택 함수를 아예 부르지 않는다** - 절약이 실제로 걸렸다.

        절약 장부를 무시하면(중복 필터가 council_only 만 보면) 3회차에도
        재평가 1건이 선택 함수로 들어가므로 이 단언이 깨진다.
        """
        db_path = tmp_path / "throttle-pipeline.db"
        promo = TestPipelinePromotion()
        title = promo.COUNCIL_ONLY_TITLE
        sizes = []

        # 1회차: 신규 1건
        self._run_counting_selections(monkeypatch, promo, db_path, title, sizes)
        assert sizes == [1]
        assert self.bookkeeping(db_path)["council_rechecked_at"] is None

        # 2회차: 장부가 비어 있으므로 재평가 1건
        self._run_counting_selections(monkeypatch, promo, db_path, title, sizes)
        assert sizes == [1, 1]
        book = self.bookkeeping(db_path)
        assert book["council_rechecked_at"] is not None
        assert book["council_content_hash"]

        # 3회차: 24시간 안 + 본문 동일 -> 호출 없음
        self._run_counting_selections(monkeypatch, promo, db_path, title, sizes)
        assert sizes == [1, 1], "절약 장부가 걸리지 않았다"

    def test_pipeline_rechecks_when_content_changes(self, tmp_path, monkeypatch):
        db_path = tmp_path / "throttle-pipeline.db"
        promo = TestPipelinePromotion()
        sizes = []
        self._run_counting_selections(
            monkeypatch, promo, db_path, promo.COUNCIL_ONLY_TITLE, sizes)
        self._run_counting_selections(
            monkeypatch, promo, db_path, promo.COUNCIL_ONLY_TITLE, sizes)
        assert sizes == [1, 1]

        # 본문이 바뀌면 24시간 안이어도 다시 본다
        self._run_counting_selections(
            monkeypatch, promo, db_path, "임업 산촌자원 개발 안내", sizes)
        assert sizes == [1, 1, 1]

    @staticmethod
    def bookkeeping(db_path):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            return dict(conn.execute(
                "SELECT council_rechecked_at, council_content_hash FROM announcements"
            ).fetchone())
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 7. 라운드 5 — LLM 호출 상한을 넘긴 재평가분
# ---------------------------------------------------------------------------

class _CapConfig:
    """`ClaudeAnalyzer.analyze_batch` 가 읽는 최소 설정."""

    def __init__(self, max_calls, threshold=0.3):
        self.analyzer = type(
            "_A", (), {
                "claude_threshold": threshold,
                "max_claude_calls_per_run": max_calls,
            },
        )()


def make_capped_llm(max_calls, verdict_score):
    """**진짜** `analyze_batch`(상한 로직 포함) + 스텁 `analyze`.

    네트워크는 타지 않는다. 상한 안에 든 항목만 ``llm_evaluated`` 가 선다 —
    상한 밖 항목을 키워드 점수 그대로 돌려주는 것은 기존 동작이다.
    """
    llm = ClaudeAnalyzer.__new__(ClaudeAnalyzer)
    llm.config = _CapConfig(max_calls)
    llm.backend = "claude"
    llm.client = object()
    llm.model = "stub"
    llm.evaluated = []

    def fake_analyze(announcement, user_context=None):
        llm.evaluated.append(announcement.source_id)
        announcement.relevance_score = verdict_score
        announcement.relevance_reason = "스텁 판정"
        announcement.llm_evaluated = True
        return announcement

    llm.analyze = fake_analyze
    return llm


class TestLlmCapDoesNotPromoteUnseenRechecks:
    """HIGH: 상한을 넘겨 LLM 이 못 본 재평가분은 이번 실행에 승격되지 않는다."""

    SOURCE = "mois_sse"
    RECHECK_ID = "cap-recheck"
    NEW_ID = "cap-new"

    # 키워드 점수를 갈라 놓는다: 신규 0.8 > 재평가 0.55.
    # 상한 1건은 정렬 상위(신규)가 가져가고 재평가는 상한 밖으로 밀린다.
    NEW_TITLE = "사회적기업 농업 경기도 고양시 지원사업 공모 보조금 안내"
    RECHECK_COUNCIL_TITLE = "산림 목재 이용 안내"          # 협의회만 매치
    RECHECK_COMPANY_TITLE = "사회적기업 지원사업 공고"       # 회사 must_match 매치

    def items(self, specs):
        return [
            RawAnnouncement(
                source=self.SOURCE, source_id=sid, title=title,
                url=f"https://example.com/{self.SOURCE}/{sid}", summary="",
            )
            for sid, title in specs
        ]

    def crawler_for(self, specs):
        payload = self.items(specs)

        class FakeCrawler:
            def is_enabled(self):
                return True

            def set_quoted_source_ids(self, ids):
                return None

            def safe_fetch(self):
                return list(payload)

        return FakeCrawler

    def run(self, monkeypatch, db_path, specs, llm=None):
        monkeypatch.setattr(main_mod, "Database", lambda *a, **k: Database(db_path))
        monkeypatch.setattr(
            main_mod, "setup_logger",
            lambda *a, **k: logging.getLogger("test-cap"),
        )
        monkeypatch.setattr(
            main_mod, "_import_crawlers",
            lambda: {self.SOURCE: self.crawler_for(specs)},
        )
        if llm is not None:
            monkeypatch.setattr(main_mod, "ClaudeAnalyzer", lambda: llm)
        main_mod.run_pipeline()

    def row(self, db_path, source_id):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            found = conn.execute(
                "SELECT relevance_score, council_match, council_only,"
                "       council_rechecked_at, is_notified"
                "  FROM announcements WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            return dict(found) if found else None
        finally:
            conn.close()

    def seed(self, monkeypatch, db_path):
        """1회차: LLM 없이 협의회 단독 행을 만든다."""
        with monkeypatch.context() as m:
            self.run(m, db_path, [(self.RECHECK_ID, self.RECHECK_COUNCIL_TITLE)])
        assert self.row(db_path, self.RECHECK_ID)["council_only"] == 1

    def test_keyword_scores_are_ordered_as_the_test_assumes(self, company_analyzer):
        """전제 고정: 신규가 재평가보다 점수가 높아야 상한 밖으로 밀린다."""
        new_score = company_analyzer.analyze(
            self.items([(self.NEW_ID, self.NEW_TITLE)])[0]
        ).relevance_score
        recheck_score = company_analyzer.analyze(
            self.items([(self.RECHECK_ID, self.RECHECK_COMPANY_TITLE)])[0]
        ).relevance_score
        threshold = get_config().analyzer.keyword_threshold
        assert new_score > recheck_score >= threshold

    def test_over_cap_recheck_is_not_promoted(self, tmp_path, monkeypatch):
        """max_calls=1, LLM 거절 → 상한 밖 재평가분은 council_only=1 로 남는다."""
        db_path = tmp_path / "cap.db"
        self.seed(monkeypatch, db_path)

        llm = make_capped_llm(max_calls=1, verdict_score=0.0)
        with monkeypatch.context() as m:
            self.run(m, db_path, [
                (self.NEW_ID, self.NEW_TITLE),
                (self.RECHECK_ID, self.RECHECK_COMPANY_TITLE),
            ], llm)

        # 상한 1건은 신규가 가져갔다
        assert llm.evaluated == [self.NEW_ID]

        recheck = self.row(db_path, self.RECHECK_ID)
        assert recheck["council_only"] == 1, "LLM 이 못 본 항목이 승격됐다"
        assert recheck["is_notified"] == 0
        # 장부를 건드리지 않아 다음 실행에서 다시 본다
        assert recheck["council_rechecked_at"] is None
        # 측정값은 새 판정으로 갱신됐다 (회사 제목이라 협의회 어휘도 맞는다)
        assert recheck["council_match"] == 1

    def test_next_run_within_the_cap_promotes(self, tmp_path, monkeypatch):
        """상한이 허락하는 다음 실행에서는 평가되고 승격된다."""
        db_path = tmp_path / "cap.db"
        self.seed(monkeypatch, db_path)

        with monkeypatch.context() as m:
            self.run(m, db_path, [
                (self.NEW_ID, self.NEW_TITLE),
                (self.RECHECK_ID, self.RECHECK_COMPANY_TITLE),
            ], make_capped_llm(max_calls=1, verdict_score=0.0))
        assert self.row(db_path, self.RECHECK_ID)["council_only"] == 1

        accepting = make_capped_llm(max_calls=5, verdict_score=0.9)
        with monkeypatch.context() as m:
            self.run(m, db_path, [
                (self.RECHECK_ID, self.RECHECK_COMPANY_TITLE),
            ], accepting)

        assert self.RECHECK_ID in accepting.evaluated
        promoted = self.row(db_path, self.RECHECK_ID)
        assert promoted["council_only"] == 0
        assert promoted["relevance_score"] == pytest.approx(0.9)
        assert promoted["council_rechecked_at"] is not None

    def test_next_run_within_the_cap_can_also_reject(self, tmp_path, monkeypatch):
        """평가된 결과가 거절이면 승격되지 않는다 (상한과 무관)."""
        db_path = tmp_path / "cap.db"
        self.seed(monkeypatch, db_path)

        rejecting = make_capped_llm(max_calls=5, verdict_score=0.0)
        with monkeypatch.context() as m:
            self.run(m, db_path, [
                (self.RECHECK_ID, self.RECHECK_COMPANY_TITLE),
            ], rejecting)

        assert self.RECHECK_ID in rejecting.evaluated
        row = self.row(db_path, self.RECHECK_ID)
        assert row["council_only"] == 1
        # 평가는 받았으므로 장부는 선다 - 24시간 동안 다시 보지 않는다
        assert row["council_rechecked_at"] is not None

    def test_new_items_keep_their_over_cap_behaviour(self, tmp_path, monkeypatch):
        """회사 경로 불변: 상한 밖 **신규** 항목은 종전처럼 키워드 점수로 저장된다."""
        db_path = tmp_path / "cap-new.db"
        llm = make_capped_llm(max_calls=1, verdict_score=0.9)
        with monkeypatch.context() as m:
            self.run(m, db_path, [
                (self.NEW_ID, self.NEW_TITLE),
                ("cap-new-2", self.RECHECK_COMPANY_TITLE),
            ], llm)

        assert llm.evaluated == [self.NEW_ID]
        second = self.row(db_path, "cap-new-2")
        assert second is not None, "상한 밖 신규 항목이 저장되지 않았다"
        assert second["council_only"] == 0
        # LLM 이 못 봤으므로 키워드 점수 그대로 (0.9 가 아니다)
        assert second["relevance_score"] < 0.9


class TestLlmEvaluatedFlag:
    """플래그가 **성공한 호출에만** 선다."""

    def announcement(self):
        return AnalyzedAnnouncement(
            source="mois_sse", source_id="f1", title="사회적기업 공고",
            url="https://example.com/f1", relevance_score=0.5,
        )

    def analyzer(self, monkeypatch, raises=None, payload=None):
        llm = ClaudeAnalyzer.__new__(ClaudeAnalyzer)
        llm.config = _CapConfig(5)
        llm.backend = "claude"
        llm.client = object()
        llm.model = "stub"

        def call(system_prompt, user_prompt):
            if raises is not None:
                raise raises
            return payload

        monkeypatch.setattr(llm, "_call_claude", call, raising=False)
        monkeypatch.setattr(
            llm, "_build_prompts", lambda ann, ctx: ("s", "u"), raising=False
        )
        return llm

    def test_default_is_false(self):
        assert self.announcement().llm_evaluated is False

    def test_successful_call_sets_the_flag(self, monkeypatch):
        llm = self.analyzer(monkeypatch, payload=json.dumps(
            {"score": 0.9, "reason": "맞다"}, ensure_ascii=False
        ))
        result = llm.analyze(self.announcement())
        assert result.llm_evaluated is True
        assert result.relevance_score == pytest.approx(0.9)

    def test_backend_error_leaves_the_flag_down(self, monkeypatch):
        llm = self.analyzer(monkeypatch, raises=RuntimeError("timeout"))
        result = llm.analyze(self.announcement())
        assert result.llm_evaluated is False
        assert result.relevance_score == pytest.approx(0.5)   # 키워드 점수 그대로

    def test_unparseable_response_leaves_the_flag_down(self, monkeypatch):
        llm = self.analyzer(monkeypatch, payload="not json")
        assert llm.analyze(self.announcement()).llm_evaluated is False

    def test_over_cap_items_are_unflagged(self, monkeypatch):
        """`analyze_batch` 의 상한 밖 항목에는 플래그가 서지 않는다."""
        llm = make_capped_llm(max_calls=1, verdict_score=0.9)
        first = self.announcement()
        second = self.announcement()
        second.source_id = "f2"
        second.relevance_score = 0.4          # 임계 위, 정렬 아래

        results = {a.source_id: a for a in llm.analyze_batch([first, second])}
        assert results["f1"].llm_evaluated is True
        assert results["f2"].llm_evaluated is False
        assert results["f2"].relevance_score == pytest.approx(0.4)
