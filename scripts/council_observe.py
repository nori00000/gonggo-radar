#!/usr/bin/env python3
"""협의회 적재 프로파일 관찰 리포트 (P0 계약 §A) — **읽기 전용**.

무엇을 세는가 (기간 = 최근 ``days`` 일):

| 열 | 출처 |
|---|---|
| fetched | ``run_history.total_fetched`` 합 (크롤러가 가져온 수) |
| 저장 | ``announcements`` 행 수 |
| council_match | ``council_match = 1`` |
| 미측정 | ``council_match IS NULL`` (= 프로파일이 채점한 적 없는 행) |
| 회사통과 | ``council_only = 0`` (= 회사 프로파일이 고른 행) |
| 둘다 | ``council_match = 1 AND council_only = 0`` |

**미측정과 미매치는 다른 것이다.** 마이그레이션만 적용되고 파이프라인이 아직
돌지 않은 행은 미측정이지 "협의회 어휘 없음" 이 아니다 (Codex 게이트 2R LOW).

저장조차 되지 않은 항목(회사·협의회 **둘 다** 탈락)은 ``announcements`` 에
없으므로 ``council_dropped`` 원장에서 따로 뽑아 "탈락 표본" 으로 보여 준다 —
표본이 오탈락을 셀 수 있는 유일한 경로다.

DB 는 ``mode=ro`` URI 로 연다. 출력 경로가 입력 DB 를 가리키면 실행을 거부한다.
크롤러·발송·notify 는 부르지 않는다.

출력: ``digests/observe/<YYYY-MM-DD>.md`` (+ ``--sample`` 이면 같은 이름 ``.csv``).
"""

import csv
import os
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# sys.path 보정
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alert.config import get_config
from alert.utils.redact import redact
from alert.utils.safe_argparse import (
    RedactingArgumentParser,
    reject_secret_argv,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "alert" / "data" / "announcements.db"
OUTPUT_DIR = REPO_ROOT / "digests" / "observe"

# 표본 CSV 의 열 정본 — 계약 §A "id·제목·소스·점수·태그"
CSV_FIELDS = ("표본군", "id", "source", "title", "council_score", "council_tags",
              "council_match", "council_only", "relevance_score", "created_at")

# 표본군 이름
SAMPLE_MATCHED = "council_match=1"
SAMPLE_SOURCE_ONLY = "council_match!=1(협의회 소스)"

# 카운트 열 정본 (표·합계가 같은 순서를 쓴다)
COUNT_KEYS = ("fetched", "stored", "council", "unmeasured", "company", "both")

# 출력이 덮어써도 되는 확장자
SAFE_OUTPUT_SUFFIXES = {".md", ".csv"}

# 마이그레이션 7 이 심는 측정 컬럼
COUNCIL_COLUMNS = ("council_score", "council_tags", "council_match", "council_only")

# 컬럼이 없는 DB 에서 표본 SELECT 가 쓸 대체 식
_ABSENT_COLUMN_SQL = ("NULL AS council_score, NULL AS council_tags,"
                      " NULL AS council_match, 0 AS council_only")
MIGRATION_PENDING_NOTE = (
    "⚠ 이 DB 에는 council_* 컬럼이 없다(마이그레이션 7 미적용). "
    "저장 행 전량이 **미측정**이며, 회사통과는 저장 행 전량이다."
)
DROP_LEDGER_MISSING_NOTE = (
    "⚠ 이 DB 에는 council_dropped 원장이 없다(마이그레이션 8 미적용) — "
    "양쪽 탈락 항목은 아직 관측할 수 없다."
)


def _err(message) -> None:
    print(redact(str(message)), file=sys.stderr)


def _out(message) -> None:
    print(redact(str(message)))


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """``mode=ro`` URI 로 연결한다 — 쓰기 시도는 SQLite 가 거부한다."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _same_file(a: Path, b: Path) -> bool:
    """같은 파일인가 — 이름이 아니라 **inode** 로 본다.

    ``resolve()`` 비교는 심볼릭 링크만 푼다. 하드 링크는 경로도 다르고
    resolve 결과도 다르지만 같은 파일이라 그대로 통과한다
    (Codex 게이트 3R MEDIUM). ``os.path.samefile`` 은 st_dev·st_ino 를 본다.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def unsafe_output(db_path: Path, targets, out_dir: Path = None) -> str:
    """출력이 입력 DB(또는 남의 파일)를 덮어쓰는가. 안전하면 빈 문자열.

    ``mode=ro`` 는 **SQLite 연결**만 막는다. 리포트를 쓰는 것은 평범한
    ``write_text`` 라, DB 파일명이 마침 ``<오늘>.md`` 이면 그대로 덮어쓴다
    (Codex 게이트 2R MEDIUM). 그래서 쓰기 전에 경로를 직접 본다.

    세 겹이다:
      ① 경로가 resolve 후 같은가 (심볼릭 링크·상대 경로)
      ② ``os.path.samefile`` 로 같은 inode 인가 (하드 링크, 게이트 3R)
      ③ 출력 디렉터리 안의 **기존 파일 전부**가 DB 와 다른 파일인가 —
         DB 가 출력 디렉터리에 다른 이름으로 놓여 있으면 아예 쓰지 않는다
      ④ 덮어쓸 기존 파일의 확장자가 .md/.csv 인가
    """
    db_real = db_path.resolve()
    for target in targets:
        if target.resolve() == db_real or _same_file(target, db_path):
            return f"✗ 출력 경로가 입력 DB 와 같습니다: {target}"
        if target.exists() and target.suffix.lower() not in SAFE_OUTPUT_SUFFIXES:
            return f"✗ 출력 경로에 .md/.csv 가 아닌 파일이 있습니다: {target}"

    if out_dir is not None and out_dir.is_dir():
        for existing in sorted(out_dir.iterdir()):
            if existing.is_file() and _same_file(existing, db_path):
                return (f"✗ 출력 디렉터리에 입력 DB 가 들어 있습니다: {existing}")
    return ""


def _has_columns(conn: sqlite3.Connection, table: str, columns) -> bool:
    try:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.DatabaseError:
        return False
    return bool(have) and set(columns) <= have


def has_council_columns(conn: sqlite3.Connection) -> bool:
    """``announcements`` 에 측정 컬럼이 모두 있는가.

    운영 DB 는 파이프라인이 한 번 돌아야 마이그레이션이 적용된다. 관찰
    스크립트는 **쓰지 않으므로** 스스로 적용할 수 없다 - 없으면 없는 대로
    세고, 리포트 머리에 미적용을 밝힌다.
    """
    return _has_columns(conn, "announcements", COUNCIL_COLUMNS)


def has_drop_ledger(conn: sqlite3.Connection) -> bool:
    """``council_dropped`` 원장이 있는가 (마이그레이션 8)."""
    return _has_columns(conn, "council_dropped", ("source", "source_id", "seen_at"))


def window_start(days: int, now=None) -> str:
    """기간 시작 ISO 문자열 (``created_at``/``started_at`` 과 같은 형식)."""
    now = now or datetime.now()
    return (now - timedelta(days=days)).isoformat()


def collect_counts(conn: sqlite3.Connection, since: str) -> dict:
    """소스별 카운트를 모은다 (SELECT 만)."""
    counts: dict = {}

    def row(source: str) -> dict:
        return counts.setdefault(source, {key: 0 for key in COUNT_KEYS})

    for r in conn.execute(
        "SELECT source, COALESCE(SUM(total_fetched), 0) AS fetched"
        "  FROM run_history WHERE started_at >= ? GROUP BY source",
        (since,),
    ):
        row(r["source"])["fetched"] = int(r["fetched"] or 0)

    if has_council_columns(conn):
        measured_sql = (
            "SUM(CASE WHEN COALESCE(council_match, 0) = 1 THEN 1 ELSE 0 END) AS council,"
            " SUM(CASE WHEN council_match IS NULL THEN 1 ELSE 0 END) AS unmeasured,"
            " SUM(CASE WHEN COALESCE(council_only, 0) = 0 THEN 1 ELSE 0 END) AS company,"
            " SUM(CASE WHEN COALESCE(council_match, 0) = 1"
            "      AND COALESCE(council_only, 0) = 0 THEN 1 ELSE 0 END) AS both"
        )
    else:
        # 컬럼이 없던 시절의 행은 전부 미측정이고, 전부 회사 경로가 고른 것이다.
        measured_sql = (
            "0 AS council, COUNT(*) AS unmeasured, COUNT(*) AS company, 0 AS both"
        )

    for r in conn.execute(
        f"""
        SELECT source, COUNT(*) AS stored, {measured_sql}
          FROM announcements WHERE created_at >= ? GROUP BY source
        """,
        (since,),
    ):
        entry = row(r["source"])
        for key in ("stored", "council", "unmeasured", "company", "both"):
            entry[key] = int(r[key] or 0)

    return counts


def render_table(counts: dict, council_sources) -> str:
    """카운트 표를 마크다운으로 (정렬: 저장 내림차순 → 소스명)."""
    lines = [
        "| source | 협의회풀 | fetched | 저장 | council_match | 미측정 | 회사통과 | 둘다 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1]["stored"], kv[0]))
    total = {key: 0 for key in COUNT_KEYS}
    for source, c in ordered:
        for key in COUNT_KEYS:
            total[key] += c[key]
        lines.append(
            f"| {source} | {'Y' if source in council_sources else '-'} "
            f"| {c['fetched']} | {c['stored']} | {c['council']} | {c['unmeasured']} "
            f"| {c['company']} | {c['both']} |"
        )
    lines.append(
        f"| **합계** | | {total['fetched']} | {total['stored']} | {total['council']} "
        f"| {total['unmeasured']} | {total['company']} | {total['both']} |"
    )
    return "\n".join(lines)


def collect_samples(conn, since, council_sources, n, seed):
    """표본 두 묶음: ``council_match=1`` N건 + 협의회 소스의 비매치 N건."""
    if n <= 0:
        return []

    rng = random.Random(seed)
    picked = []

    def take(sql, params, label):
        rows = conn.execute(sql, params).fetchall()
        chosen = rows if len(rows) <= n else rng.sample(rows, n)
        for r in chosen:
            entry = {"표본군": label}
            entry.update({key: r[key] for key in CSV_FIELDS if key != "표본군"})
            picked.append(entry)

    measured = has_council_columns(conn)
    cols = "id, source, title, relevance_score, created_at, " + (
        "council_score, council_tags, council_match, council_only"
        if measured else _ABSENT_COLUMN_SQL
    )
    if measured:
        take(
            f"SELECT {cols} FROM announcements"
            "  WHERE created_at >= ? AND COALESCE(council_match, 0) = 1",
            (since,),
            SAMPLE_MATCHED,
        )
    sources = sorted(council_sources)
    if sources:
        placeholders = ", ".join("?" * len(sources))
        unmatched = " AND COALESCE(council_match, 0) != 1" if measured else ""
        take(
            f"SELECT {cols} FROM announcements"
            f"  WHERE created_at >= ?{unmatched}"
            f"   AND source IN ({placeholders})",
            (since, *sources),
            SAMPLE_SOURCE_ONLY,
        )
    return picked


def collect_drops(conn, since, n, seed):
    """관찰 원장(``council_dropped``)에서 양쪽 탈락 표본을 뽑는다."""
    if n <= 0 or not has_drop_ledger(conn):
        return []
    rows = conn.execute(
        "SELECT source, source_id, title, url, posted_at, company_score,"
        "       council_score, reason, seen_at"
        "  FROM council_dropped WHERE seen_at >= ? ORDER BY seen_at DESC",
        (since,),
    ).fetchall()
    if len(rows) > n:
        rows = random.Random(seed).sample(rows, n)
    return [dict(r) for r in rows]


def render_drops(drops) -> str:
    """탈락 표본을 마크다운 표로."""
    lines = [
        "| source | 사유 | 회사점수 | 협의회점수 | 제목 |",
        "|---|---|---|---|---|",
    ]
    for d in drops:
        title = str(d["title"]).replace("|", "\\|")[:60]
        lines.append(
            f"| {d['source']} | {d['reason']} | {d['company_score']} "
            f"| {d['council_score']} | {title} |"
        )
    return "\n".join(lines)


def write_csv(path: Path, samples) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_FIELDS))
        writer.writeheader()
        for row in samples:
            writer.writerow(row)


def build_report(days, since, db_path, counts, council_sources, samples, csv_name,
                 measured=True, drops=(), drop_ledger=True):
    """리포트 본문 (마크다운)."""
    head = [
        f"# 협의회 적재 프로파일 관찰 — 최근 {days}일",
        "",
        f"- 생성: {datetime.now().isoformat(timespec='seconds')}",
        f"- DB: `{db_path}` (mode=ro, SELECT 전용)",
        f"- 기간 시작: `{since}`",
        f"- 협의회 프로파일 소스 {len(council_sources)}개: "
        + ", ".join(sorted(council_sources)),
        "",
        "## 소스별 카운트",
        "",
        render_table(counts, council_sources),
        "",
    ]
    notes = []
    if not measured:
        notes.append(MIGRATION_PENDING_NOTE)
    if not drop_ledger:
        notes.append(DROP_LEDGER_MISSING_NOTE)
    if notes:
        head[6:6] = [""] + notes

    if samples:
        matched = sum(1 for s in samples if s["표본군"] == SAMPLE_MATCHED)
        head += [
            "## 표본 (저장된 행)",
            "",
            f"- `{SAMPLE_MATCHED}` {matched}건 / "
            f"`{SAMPLE_SOURCE_ONLY}` {len(samples) - matched}건",
            f"- CSV: `{csv_name}`",
            "",
        ]

    head += [
        "## 탈락 표본 (저장되지 않은 항목)",
        "",
    ]
    if drops:
        head += [
            f"- `council_dropped` 원장에서 {len(drops)}건",
            "",
            render_drops(drops),
            "",
        ]
    elif not drop_ledger:
        head += ["- 원장 없음 (마이그레이션 8 미적용).", ""]
    else:
        head += ["- 기간 내 양쪽 탈락 기록 없음.", ""]
    return "\n".join(head)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if reject_secret_argv(argv, _err):
        return 2

    parser = RedactingArgumentParser(
        prog="council_observe",
        description="협의회 적재 프로파일 관찰 리포트 (읽기 전용)",
    )
    parser.add_argument("days", type=int, help="관찰 기간 (일)")
    parser.add_argument("--sample", type=int, default=0, help="표본군당 표본 수")
    parser.add_argument("--seed", type=int, default=0, help="표본 추출 시드")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 경로")
    parser.add_argument("--out-dir", default=str(OUTPUT_DIR), help="출력 디렉터리")
    args = parser.parse_args(argv)

    if args.days <= 0:
        _err("✗ days 는 1 이상이어야 합니다")
        return 2

    db_path = Path(args.db)
    if not db_path.exists():
        _err(f"✗ DB 가 없습니다: {db_path}")
        return 1

    out_dir = Path(args.out_dir)
    stem = date.today().isoformat()
    md_path = out_dir / f"{stem}.md"
    csv_path = out_dir / f"{stem}.csv"

    # 쓰기 전에 본다 - 쓴 뒤에 알아차리면 이미 DB 가 없다.
    problem = unsafe_output(db_path, (md_path, csv_path), out_dir)
    if problem:
        _err(problem)
        return 2

    council_sources = set(get_config().council_profile.sources)
    since = window_start(args.days)

    conn = open_readonly(db_path)
    try:
        measured = has_council_columns(conn)
        drop_ledger = has_drop_ledger(conn)
        counts = collect_counts(conn, since)
        samples = collect_samples(
            conn, since, council_sources, args.sample, args.seed
        )
        drops = collect_drops(conn, since, args.sample, args.seed)
    finally:
        conn.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    if samples:
        write_csv(csv_path, samples)
    report = build_report(
        args.days, since, db_path, counts, council_sources, samples, csv_path.name,
        measured=measured, drops=drops, drop_ledger=drop_ledger,
    )
    md_path.write_text(report + "\n", encoding="utf-8")

    _out(report)
    _out("")
    _out(f"→ {md_path}")
    if samples:
        _out(f"→ {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
