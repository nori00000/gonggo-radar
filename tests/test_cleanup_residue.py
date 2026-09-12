"""정리 스크립트 회귀 테스트 (scripts/cleanup_seis_duplicates.py).

2026-09-13 Codex 크리틱 #1·#2·#5·#9 의 재현 입력을 그대로 쓴다. 모든
테스트는 임시 파일 DB에서 돌고 워크트리 DB를 건드리지 않는다.
"""

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cleanup_seis_duplicates.py"


def load_script():
    """scripts/ 는 패키지가 아니므로 경로로 직접 읽어들인다."""
    spec = importlib.util.spec_from_file_location("cleanup_residue", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cleanup = load_script()

SCHEMA = """
CREATE TABLE announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT    NOT NULL,
    source_id       TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    summary         TEXT    DEFAULT '',
    url             TEXT    NOT NULL,
    author          TEXT    DEFAULT '',
    category        TEXT    DEFAULT '',
    target          TEXT    DEFAULT '',
    period_start    TEXT,
    period_end      TEXT,
    relevance_score REAL    DEFAULT 0.0,
    relevance_reason TEXT   DEFAULT '',
    matched_keywords TEXT   DEFAULT '[]',
    is_notified     INTEGER DEFAULT 0,
    raw_data        TEXT    DEFAULT '',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE(source, source_id)
)
"""

SEIS_URL = "https://www.seis.or.kr/subPage.do?menuId=30200&tabId=pbancMainView&fncPbofrSn={sid}"


@pytest.fixture
def db(tmp_path):
    """WAL 모드 임시 DB - 저장소 실제 설정과 같게 둔다."""
    path = tmp_path / "announcements.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(SCHEMA)
    conn.commit()
    yield conn, path
    conn.close()


def insert(conn, **kwargs):
    """행 하나를 넣는다. raw_data 는 dict 로 주면 JSON 으로 직렬화한다."""
    raw = kwargs.pop("raw_data", {})
    if isinstance(raw, dict):
        raw = json.dumps(raw, ensure_ascii=False)
    row = {
        "source": "seis",
        "source_id": "1",
        "title": "공고",
        "url": "https://example.com/1",
        "period_start": None,
        "period_end": None,
        "created_at": "2026-09-12T00:00:00",
        "updated_at": "2026-09-12T00:00:00",
        "raw_data": raw,
    }
    row.update(kwargs)
    conn.execute(
        "INSERT INTO announcements"
        " (source, source_id, title, url, period_start, period_end,"
        "  raw_data, created_at, updated_at)"
        " VALUES (:source, :source_id, :title, :url, :period_start, :period_end,"
        "         :raw_data, :created_at, :updated_at)",
        row,
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


class TestCritiqueRuleOrder:
    """크리틱 #1: A/B 규칙 충돌로 대표까지 전멸하던 결함."""

    @pytest.fixture
    def conflict(self, db):
        """A는 8370을, B는(예전 순위로는) 8371을 지목하는 입력."""
        conn, _ = db
        keeper = insert(
            conn, source_id="8371", title="같은 공고", period_start="2026-09-01",
            url=SEIS_URL.format(sid="8371"),
            raw_data={"title": "같은 공고", "merged_source_ids": ["8370"]},
        )
        victim = insert(
            conn, source_id="8370", title="같은 공고", period_start="2026-09-11",
            url=SEIS_URL.format(sid="8370"),
            raw_data={"title": "같은 공고"},
        )
        return conn, keeper, victim

    def test_canonical_survives(self, conflict):
        """대표(merged_source_ids 를 가진 행)는 절대 삭제되지 않는다."""
        conn, keeper, victim = conflict
        doomed, _reasons, canonical = cleanup.find_duplicates(conn, "seis")

        assert keeper in canonical
        assert [row["id"] for row in doomed] == [victim]

    def test_group_is_never_wiped_out(self, conflict):
        """묶음이 0행이 되지 않는다 - 예전에는 잔존 0행이었다."""
        conn, keeper, _victim = conflict
        doomed, _reasons, _canonical = cleanup.find_duplicates(conn, "seis")

        remaining = 2 - len(doomed)
        assert remaining == 1
        survivors = {
            row["id"] for row in conn.execute("SELECT id FROM announcements")
        } - {row["id"] for row in doomed}
        assert survivors == {keeper}

    def test_canonical_detail_url_is_kept(self, conflict):
        """대표의 상세 URL이 살아남는다."""
        conn, keeper, _victim = conflict
        doomed, _reasons, _canonical = cleanup.find_duplicates(conn, "seis")
        doomed_ids = {row["id"] for row in doomed}

        kept = conn.execute(
            "SELECT url FROM announcements WHERE id = ?", (keeper,)
        ).fetchone()
        assert keeper not in doomed_ids
        assert kept["url"] == SEIS_URL.format(sid="8371")

    def test_rule_a_wins_when_records_conflict(self, db):
        """두 행이 서로를 대표로 기록했으면 둘 다 살린다 (전멸 금지)."""
        conn, _ = db
        first = insert(
            conn, source_id="100", title="서로 지목", url=SEIS_URL.format(sid="100"),
            raw_data={"merged_source_ids": ["101"]},
        )
        second = insert(
            conn, source_id="101", title="서로 지목", url=SEIS_URL.format(sid="101"),
            raw_data={"merged_source_ids": ["100"]},
        )
        doomed, _reasons, canonical = cleanup.find_duplicates(conn, "seis")

        assert canonical == {first, second}
        assert doomed == []


class TestCritiqueRuleBScope:
    """크리틱 #2: 규칙 B가 지역·회차가 다른 정상 공고까지 지우던 결함."""

    def test_different_subject_is_not_merged(self, db):
        """제목이 같아도 주체(서울/부산)가 다르면 별개 공고다."""
        conn, _ = db
        insert(conn, source_id="100", title="상주기업 모집 공고",
               url=SEIS_URL.format(sid="100"), period_end="2026-09-30",
               raw_data={"sub": "서울센터"})
        insert(conn, source_id="101", title="상주기업 모집 공고",
               url=SEIS_URL.format(sid="101"), period_end="2026-09-30",
               raw_data={"sub": "부산센터"})

        doomed, _reasons, _canonical = cleanup.find_duplicates(conn, "seis")
        assert doomed == []

    def test_different_period_end_is_not_merged(self, db):
        """1차·2차처럼 접수 종료일이 다르면 병합하지 않는다."""
        conn, _ = db
        insert(conn, source_id="200", title="같은 제목",
               url=SEIS_URL.format(sid="200"), period_end="2026-08-31",
               raw_data={"sub": "서울센터"})
        insert(conn, source_id="201", title="같은 제목",
               url=SEIS_URL.format(sid="201"), period_end="2026-09-30",
               raw_data={"sub": "서울센터"})

        doomed, _reasons, _canonical = cleanup.find_duplicates(conn, "seis")
        assert doomed == []

    def test_same_subject_and_period_end_merges(self, db):
        """제목·주체·종료일이 모두 같을 때만 합친다."""
        conn, _ = db
        insert(conn, source_id="8371", title="사회보험료 지원사업 모집",
               url=SEIS_URL.format(sid="8371"), period_start="2026-09-01",
               period_end="2026-12-31", raw_data={"sub": "사회보험료 지원 사업"})
        older = insert(conn, source_id="8370", title="사회보험료 지원사업 모집",
                       url=SEIS_URL.format(sid="8370"), period_start="2026-08-01",
                       period_end="2026-12-31", raw_data={"sub": "사회보험료 지원 사업"})

        doomed, _reasons, _canonical = cleanup.find_duplicates(conn, "seis")
        assert [row["id"] for row in doomed] == [older]

    def test_rule_b_is_not_applied_to_smartfarm(self, db):
        """규칙 B는 seis 전용 - 다른 소스의 같은 제목 행을 지우지 않는다."""
        conn, _ = db
        base = "https://www.smartfarmkorea.net/board/view.do?menuId=M1&searchNttId={sid}"
        insert(conn, source="smartfarm", source_id="4529", title="실증단지 입주대상 모집",
               url=base.format(sid="4529"))
        insert(conn, source="smartfarm", source_id="4530", title="실증단지 입주대상 모집",
               url=base.format(sid="4530"))

        doomed, _reasons, _canonical = cleanup.find_duplicates(conn, "smartfarm")
        assert doomed == []

    def test_rule_b_is_not_applied_to_socialenterprise(self, db):
        """두 행 모두 제 글번호를 가지면 손대지 않는다."""
        conn, _ = db
        base = "https://www.socialenterprise.or.kr/homepage/bbs/boardView.do?bsIdx=10002&bIdx={sid}"
        insert(conn, source="socialenterprise", source_id="252628", title="인증 공고",
               url=base.format(sid="252628"))
        insert(conn, source="socialenterprise", source_id="252629", title="인증 공고",
               url=base.format(sid="252629"))

        doomed, _reasons, _canonical = cleanup.find_duplicates(conn, "socialenterprise")
        assert doomed == []


class TestRuleCProvableDefects:
    """규칙 C: 증명 가능한 글번호 결함만 지운다."""

    def test_board_discriminator_as_id_is_deleted(self, db):
        """bsIdx 를 글 ID로 쓴 행은 같은 URL의 진짜 행에 흡수된다."""
        conn, _ = db
        url = ("https://www.socialenterprise.or.kr/homepage/bbs/boardView.do"
               "?bsIdx=10002&bIdx=252628&menuId=822")
        bogus = insert(conn, source="socialenterprise", source_id="10002",
                       title="인증 공고", url=url)
        insert(conn, source="socialenterprise", source_id="252628",
               title="인증 공고", url=url)

        doomed, reasons, _canonical = cleanup.find_duplicates(conn, "socialenterprise")
        assert [row["id"] for row in doomed] == [bogus]
        # 같은 URL 규칙이 먼저 걸린다 (4차 게이트 #8) - 삭제 대상은 그대로다
        assert "같은 URL" in " ".join(reasons)

    def test_unresolved_void_url_is_deleted(self, db):
        """#void 로 남은 행은 해소된 행에 흡수된다."""
        conn, _ = db
        void_row = insert(conn, source="smartfarm", source_id="30a490b686668023",
                          title="실증단지 입주대상 모집",
                          url="https://www.smartfarmkorea.net/#void")
        insert(conn, source="smartfarm", source_id="4529",
               title="실증단지 입주대상 모집",
               url="https://www.smartfarmkorea.net/board/view.do?searchNttId=4529")

        doomed, reasons, _canonical = cleanup.find_duplicates(conn, "smartfarm")
        assert [row["id"] for row in doomed] == [void_row]
        assert "URL 미해소" in " ".join(reasons)

    def test_no_provable_keeper_means_no_deletion(self, db):
        """근거가 되는 행이 없으면 아무것도 지우지 않는다."""
        conn, _ = db
        insert(conn, source="smartfarm", source_id="hash1", title="같은 제목",
               url="https://www.smartfarmkorea.net/#void")
        insert(conn, source="smartfarm", source_id="hash2", title="같은 제목",
               url="https://www.smartfarmkorea.net/#void")

        doomed, _reasons, _canonical = cleanup.find_duplicates(conn, "smartfarm")
        assert doomed == []


class TestCritiqueBackupIncludesWal:
    """크리틱 #5: shutil.copy2 가 WAL을 빼먹어 최근 커밋이 누락되던 결함."""

    def test_uncheckpointed_commit_is_in_the_backup(self, db, tmp_path):
        """체크포인트 전 커밋이 백업에 들어 있다."""
        conn, path = db
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

        insert(conn, source_id="9999", title="체크포인트 전에 커밋된 공고",
               url=SEIS_URL.format(sid="9999"))

        backup = tmp_path / "backup.db"
        cleanup.backup_database(path, backup)

        restored = sqlite3.connect(str(backup))
        try:
            titles = [
                row[0] for row in restored.execute("SELECT title FROM announcements")
            ]
        finally:
            restored.close()
        assert "체크포인트 전에 커밋된 공고" in titles

    def test_backup_is_a_usable_database(self, db, tmp_path):
        conn, path = db
        insert(conn, source_id="1000", title="공고", url=SEIS_URL.format(sid="1000"))
        backup = tmp_path / "b.db"
        cleanup.backup_database(path, backup)

        restored = sqlite3.connect(str(backup))
        try:
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            restored.close()


class TestCritiqueSameDayReception:
    """크리틱 #9: 정상 당일 접수까지 마감을 지우던 결함."""

    def test_explicit_same_day_range_is_kept(self, db):
        """raw_data.date 가 "09-15 ~ 09-15" 면 원문이 말한 기간이므로 남긴다."""
        conn, _ = db
        insert(conn, source="socialenterprise", source_id="300",
               title="당일 접수 공고",
               url="https://www.socialenterprise.or.kr/x?bIdx=300",
               period_start="2026-09-15", period_end="2026-09-15",
               created_at="2026-09-12T09:00:00",
               raw_data={"date": "2026-09-15 ~ 2026-09-15"})

        changes, _reasons = cleanup.find_fake_deadlines(conn)
        assert changes == []

    def test_two_dates_in_raw_data_is_kept(self, db):
        """날짜가 둘 있으면 원문이 기간을 말한 것이다."""
        conn, _ = db
        insert(conn, source="socialenterprise", source_id="301", title="공고",
               url="https://www.socialenterprise.or.kr/x?bIdx=301",
               period_start="2026-09-15", period_end="2026-09-15",
               created_at="2026-09-12T09:00:00",
               raw_data={"date": "2026.09.15 2026.09.15"})

        changes, _reasons = cleanup.find_fake_deadlines(conn)
        assert changes == []

    def test_single_posting_date_is_nulled(self, db):
        """게시일 하나가 기간으로 해석된 행은 마감을 비운다."""
        conn, _ = db
        row_id = insert(conn, source="socialenterprise", source_id="302", title="공고",
                        url="https://www.socialenterprise.or.kr/x?bIdx=302",
                        period_start="2026-09-11", period_end="2026-09-11",
                        created_at="2026-09-12T09:00:00",
                        raw_data={"date": "2026/09/11"})

        changes, reasons = cleanup.find_fake_deadlines(conn)
        assert [(row["id"], new_end) for row, new_end in changes] == [(row_id, None)]
        assert "단일 게시일" in " ".join(reasons)

    def test_quote_backed_deadline_is_kept(self, db):
        """상세 인용에서 온 마감은 건드리지 않는다."""
        conn, _ = db
        insert(conn, source="socialenterprise", source_id="303", title="공고",
               url="https://www.socialenterprise.or.kr/x?bIdx=303",
               period_start="2026-09-11", period_end="2026-09-11",
               created_at="2026-09-11T09:00:00",
               raw_data={"date": "2026/09/11",
                         "quote_deadline": "접수기간 2026.09.11까지"})

        changes, _reasons = cleanup.find_fake_deadlines(conn)
        assert changes == []

    def test_coop_same_day_range_is_kept(self, db):
        """coop 도 원문이 기간을 말하면 period_start 를 남긴다."""
        conn, _ = db
        insert(conn, source="coop", source_id="400", title="공고",
               url="https://www.coop.go.kr/x?brd_no=400",
               period_start="2026-09-15",
               raw_data={"date": "2026-09-15 ~ 2026-09-20"})

        doomed, _reasons = cleanup.find_fake_starts(conn)
        assert doomed == []


class TestInvariantGuard:
    """불변식: **삭제되는 행마다 살아남는 대표가 있어야 한다**.

    키 묶음 단위 생존 검사는 정상 병합(적재월·메타데이터가 대표와 다른
    구행)을 전멸로 오판했다. 흡수 관계를 직접 검사한다.
    """

    @staticmethod
    def _rows(conn):
        return {r["id"]: r for r in cleanup._fetch_rows(conn, "seis")}

    @pytest.fixture
    def rows(self, db):
        conn, _ = db
        first = insert(conn, source_id="700", title="A", url=SEIS_URL.format(sid="700"))
        second = insert(conn, source_id="701", title="A", url=SEIS_URL.format(sid="701"))
        return conn, first, second

    def test_valid_plan_passes(self, rows):
        conn, keeper, victim = rows
        table = self._rows(conn)
        cleanup._assert_invariants(
            "seis", {victim: table[victim]}, {victim: keeper}, {keeper}
        )

    def test_deleting_a_canonical_row_raises(self, rows):
        conn, keeper, victim = rows
        table = self._rows(conn)
        with pytest.raises(RuntimeError, match="canonical"):
            cleanup._assert_invariants(
                "seis", {keeper: table[keeper]}, {keeper: victim}, {keeper}
            )

    def test_deleting_without_a_keeper_raises(self, rows):
        conn, _keeper, victim = rows
        table = self._rows(conn)
        with pytest.raises(RuntimeError, match="대표 없이"):
            cleanup._assert_invariants("seis", {victim: table[victim]}, {}, set())

    def test_keeper_also_deleted_raises(self, rows):
        """대표까지 삭제되는 계획은 막는다 (묶음 전멸)."""
        conn, keeper, victim = rows
        table = self._rows(conn)
        plan = {victim: table[victim], keeper: table[keeper]}
        absorbed = {victim: keeper, keeper: victim}
        with pytest.raises(RuntimeError, match="대표까지"):
            cleanup._assert_invariants("seis", plan, absorbed, set())


class TestFinalGateRuleAAndC:
    """최종 게이트 #7: A/C 정리의 누락."""

    def test_same_url_merges_across_ingestion_months(self, db):
        """같은 URL·마감 NULL은 적재월이 달라도 같은 글이다."""
        conn, _ = db
        url = SEIS_URL.format(sid="900")
        keeper = insert(conn, source_id="900", title="같은 공고", url=url,
                        created_at="2026-09-10T00:00:00",
                        raw_data={"merged_source_ids": ["901"]})
        victim = insert(conn, source_id="901", title="같은 공고", url=url,
                        created_at="2026-08-10T00:00:00")

        doomed, _reasons, canonical = cleanup.find_duplicates(conn, "seis")
        assert [row["id"] for row in doomed] == [victim]
        assert canonical == {keeper}

    def test_merge_record_honoured_when_victim_lacks_metadata(self, db):
        """구행에 지역 메타데이터가 없고 대표에만 있어도 병합을 인정한다."""
        conn, _ = db
        keeper = insert(conn, source_id="910", title="같은 공고",
                        url=SEIS_URL.format(sid="910"), period_end="2026-12-31",
                        raw_data={"sub": "사회보험료 지원 사업", "info": ["경기도"],
                                  "merged_source_ids": ["911"]})
        victim = insert(conn, source_id="911", title="같은 공고",
                        url=SEIS_URL.format(sid="911"), period_end="2026-12-31",
                        raw_data={})

        doomed, _reasons, canonical = cleanup.find_duplicates(conn, "seis")
        assert [row["id"] for row in doomed] == [victim]
        assert canonical == {keeper}

    def test_rule_c_compares_every_canonical_row(self, db):
        """정상 대표가 여러 개면 **모두** 와 대조한다."""
        conn, _ = db
        base = "https://www.smartfarmkorea.net/board/view.do?searchNttId={sid}"
        # 대표 둘: 4529(경기), 4600(강원). 미해소 행은 강원 쪽 중복이다.
        insert(conn, source="smartfarm", source_id="4529", title="실증단지 모집",
               url=base.format(sid="4529"), period_end="2026-09-22",
               raw_data={"info": ["경기도"]})
        insert(conn, source="smartfarm", source_id="4600", title="실증단지 모집",
               url=base.format(sid="4600"), period_end="2026-10-31",
               raw_data={"info": ["강원도"]})
        void_row = insert(conn, source="smartfarm", source_id="hash-gw",
                          title="실증단지 모집",
                          url="https://www.smartfarmkorea.net/#void",
                          period_end="2026-10-31", raw_data={"info": ["강원도"]})

        doomed, reasons, _canonical = cleanup.find_duplicates(conn, "smartfarm")
        assert [row["id"] for row in doomed] == [void_row]
        assert "4600" in " ".join(reasons)      # 최대 순위가 아닌 대표와 짝지어야 한다


class TestFinalGateDeadlineEvidence:
    """최종 게이트 #8: 혼합 raw 필드의 근거 판정."""

    def test_evidence_in_another_field_keeps_the_deadline(self, db):
        """``date=09-01`` 이고 ``period="2026-09-15까지"`` 면 09-15는 정상이다."""
        conn, _ = db
        insert(conn, source="socialenterprise", source_id="800", title="공고",
               url="https://www.socialenterprise.or.kr/x?bIdx=800",
               period_start="2026-09-01", period_end="2026-09-15",
               created_at="2026-09-01T09:00:00",
               raw_data={"date": "2026-09-01", "period": "2026-09-15까지"})

        changes, _reasons = cleanup.find_fake_deadlines(conn)
        assert changes == []

    def test_evidence_replaces_a_posting_date_deadline(self, db):
        """게시일 마감인데 근거가 다른 기간을 말하면 **교체**한다."""
        conn, _ = db
        row_id = insert(
            conn, source="socialenterprise", source_id="801", title="공고",
            url="https://www.socialenterprise.or.kr/x?bIdx=801",
            period_start="2026-09-11", period_end="2026-09-11",
            created_at="2026-09-11T09:00:00",
            raw_data={"date": "2026/09/11", "period": "2026-10-01~2026-10-31"},
        )
        changes, reasons = cleanup.find_fake_deadlines(conn)
        assert [(row["id"], new_end) for row, new_end in changes] == [
            (row_id, "2026-10-31")
        ]
        assert "교체" in " ".join(reasons)

    def test_matching_evidence_is_left_alone(self, db):
        """근거가 저장값과 같으면 그대로 둔다 (당일 접수)."""
        conn, _ = db
        insert(conn, source="socialenterprise", source_id="802", title="공고",
               url="https://www.socialenterprise.or.kr/x?bIdx=802",
               period_start="2026-09-15", period_end="2026-09-15",
               created_at="2026-09-12T09:00:00",
               raw_data={"date": "2026-09-15 당일 접수"})

        changes, _reasons = cleanup.find_fake_deadlines(conn)
        assert changes == []

    def test_deadline_evidence_helper(self):
        assert cleanup.deadline_evidence({"period": "2026-09-15까지"}) == "2026-09-15"
        assert cleanup.deadline_evidence(
            {"period": "2026-10-01~2026-10-31"}
        ) == "2026-10-31"
        assert cleanup.deadline_evidence({"date": "2026-09-15 당일 접수"}) == "2026-09-15"
        assert cleanup.deadline_evidence({"date": "2026/09/11"}) is None
        assert cleanup.deadline_evidence({"quote_deadline": "접수기간 별도 공지"}) is None
        # 강한 근거(인용)가 약한 근거(목록 날짜)를 이긴다
        assert cleanup.deadline_evidence({
            "quote_deadline": "접수기간 2026.11.01 ~ 2026.11.30",
            "date": "2026-09-11 접수",
        }) == "2026-11-30"


class TestGate4DeadlineEvidence:
    """4차 게이트 #4: 정상 마감을 **시작일로** 교체하던 결함."""

    def test_start_only_quote_never_replaces_a_deadline(self, db):
        """"…2026.09.01부터" 는 종료 근거가 아니다."""
        conn, _ = db
        insert(conn, source="socialenterprise", source_id="950", title="공고",
               url="https://www.socialenterprise.or.kr/x?bIdx=950",
               period_start="2026-09-01", period_end="2026-09-30",
               created_at="2026-09-01T09:00:00",
               raw_data={"period": "2026-09-30까지",
                         "quote_deadline": "접수기간 2026.09.01부터"})

        changes, _reasons = cleanup.find_fake_deadlines(conn)
        assert changes == []

    def test_period_wording_is_not_a_posting_date(self, db):
        """"2026-09-30까지" 를 게시일 후보로 넣지 않는다."""
        payload = {"date": "2026-09-30까지"}
        assert cleanup.posting_dates(payload, "") == set()

    def test_plain_single_date_is_still_a_posting_date(self, db):
        payload = {"date": "2026/09/11"}
        assert cleanup.posting_dates(payload, "") == {"2026-09-11"}

    def test_evidence_ignores_start_only_values(self):
        assert cleanup.deadline_evidence(
            {"quote_deadline": "접수기간 2026.09.01부터"}
        ) is None
        assert cleanup.deadline_evidence(
            {"quote_deadline": "접수기간 2026.09.01부터", "period": "2026-09-30까지"}
        ) == "2026-09-30"


class TestGate4RegionAndSameUrl:
    """4차 게이트 #8: 전국 와일드카드와 같은 URL 규칙."""

    def test_nationwide_is_not_a_wildcard(self):
        """"전국" 와일드카드는 철회됐다 (5차 게이트 #3 REGRESSED).

        지역 하나를 넘기려던 완화가 주체 서명 전체를 무효화해, 대표가
        ``info=[전국]`` 이면 다른 지역의 **정상 공고까지 삭제 승인**됐다.
        지역은 엄격 비교한다 - 병합을 놓치는 비용이 정상 행을 지우는
        비용보다 싸다.
        """
        from alert.crawlers.dedupe_keys import group_key, keys_compatible

        seoul = group_key("공고", ["서울"], "2026-09-30")
        nationwide = group_key("공고", ["전국"], "2026-09-30")
        assert seoul != nationwide
        assert keys_compatible(seoul, nationwide) is False

    def test_nationwide_canonical_does_not_delete_another_region(self, db):
        """대표가 [전국]이어도 다른 지역 행을 지우지 않는다 (게이트 #3 재현).

        재현 입력: 같은 제목·같은 마감, 대표 ``sub=서울센터 info=[전국]`` 이
        ``merged_source_ids=[2]`` 를 들고 있고 상대는 ``sub=부산센터
        info=[부산]``. 와일드카드가 있던 동안 규칙 A가 부산 행을 삭제
        대상으로 승인했다.
        """
        conn, _ = db
        insert(conn, source_id="1", title="상주기업 모집 공고",
               url=SEIS_URL.format(sid="1"), period_end="2026-09-30",
               raw_data={"sub": "서울센터", "info": ["전국"],
                         "merged_source_ids": ["2"]})
        busan = insert(conn, source_id="2", title="상주기업 모집 공고",
                       url=SEIS_URL.format(sid="2"), period_end="2026-09-30",
                       raw_data={"sub": "부산센터", "info": ["부산"]})

        doomed, reasons, canonical = cleanup.find_duplicates(conn, "seis")
        assert doomed == []
        assert canonical == set()
        assert "병합 기록 무시" in " ".join(reasons)
        survivors = {r["id"] for r in conn.execute("SELECT id FROM announcements")}
        assert busan in survivors

    def test_different_real_regions_stay_incompatible(self):
        from alert.crawlers.dedupe_keys import group_key, keys_compatible

        seoul = group_key("공고", ["서울"], "2026-09-30")
        busan = group_key("공고", ["부산"], "2026-09-30")
        assert keys_compatible(seoul, busan) is False

    def test_same_url_duplicate_is_deleted_even_with_a_valid_id(self, db):
        """같은 URL이면 양쪽 글번호가 맞아도 하나는 중복이다."""
        conn, _ = db
        url = ("https://www.socialenterprise.or.kr/homepage/bbs/boardView.do"
               "?bsIdx=10002&bIdx=252628")
        first = insert(conn, source="socialenterprise", source_id="252628",
                       title="인증 공고", url=url, period_end="2026-09-30")
        second = insert(conn, source="socialenterprise", source_id="10002",
                        title="인증 공고", url=url, period_end="2026-09-30")

        doomed, reasons, _canonical = cleanup.find_duplicates(conn, "socialenterprise")
        assert len(doomed) == 1
        assert doomed[0]["id"] in {first, second}
        assert "같은 URL" in " ".join(reasons)

    def test_same_url_with_different_deadlines_is_kept(self, db):
        """같은 URL이라도 실제 종료일이 다르면 건드리지 않는다."""
        conn, _ = db
        url = ("https://www.socialenterprise.or.kr/homepage/bbs/boardView.do"
               "?bsIdx=10002&bIdx=252628")
        insert(conn, source="socialenterprise", source_id="252628",
               title="인증 공고", url=url, period_end="2026-09-30")
        insert(conn, source="socialenterprise", source_id="10002",
               title="인증 공고", url=url, period_end="2026-10-31")

        doomed, _reasons, _canonical = cleanup.find_duplicates(conn, "socialenterprise")
        assert doomed == []
