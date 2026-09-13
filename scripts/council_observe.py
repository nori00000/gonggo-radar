#!/usr/bin/env python3
"""협의회 적재 프로파일 관찰 리포트 (P0 계약 §A) — **읽기 전용**.

무엇을 세는가 (기간 = 최근 ``days`` 일):

| 열 | 출처 |
|---|---|
| fetched | ``run_history.total_fetched`` 합 (크롤러가 가져온 수) |
| 저장 | ``announcements`` 행 수 |
| council | ``council_match = 1`` |
| 회사통과 | ``council_only = 0`` (= 회사 프로파일이 고른 행) |
| 둘다 | ``council_match = 1 AND council_only = 0`` |

DB 는 ``mode=ro`` URI 로 연다 — 이 스크립트는 어떤 경로로도 쓰지 않는다.
크롤러·발송·notify 는 부르지 않는다.

출력: ``digests/observe/<YYYY-MM-DD>.md`` (+ ``--sample`` 이면 같은 이름 ``.csv``).
"""

import csv
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
SAMPLE_SOURCE_ONLY = "council_match=0(협의회 소스)"

# 마이그레이션 7 이 심는 측정 컬럼
COUNCIL_COLUMNS = ("council_score", "council_tags", "council_match", "council_only")

# 컬럼이 없는 DB 에서 표본 SELECT 가 쓸 대체 식 — 0 을 **측정값처럼 보이지 않게**
# 하려고 리포트 머리에 경고를 함께 찍는다.
_ABSENT_COLUMN_SQL = ("0.0 AS council_score, '{}' AS council_tags,"
                      " 0 AS council_match, 0 AS council_only")
MIGRATION_PENDING_NOTE = (
    "⚠ 이 DB 에는 council_* 컬럼이 없다(마이그레이션 7 미적용). "
    "council_match·둘다 열의 0 은 **측정값이 아니라 미측정**이며, "
    "회사통과는 저장 행 전량이다."
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


def has_council_columns(conn: sqlite3.Connection) -> bool:
    """``announcements`` 에 측정 컬럼이 모두 있는가.

    운영 DB 는 파이프라인이 한 번 돌아야 마이그레이션이 적용된다. 관찰
    스크립트는 **쓰지 않으므로** 스스로 적용할 수 없다 - 없으면 없는 대로
    세고, 리포트 머리에 미적용을 밝힌다.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(announcements)").fetchall()}
    return set(COUNCIL_COLUMNS) <= cols


def window_start(days: int, now=None) -> str:
    """기간 시작 ISO 문자열 (``created_at``/``started_at`` 과 같은 형식)."""
    now = now or datetime.now()
    return (now - timedelta(days=days)).isoformat()


def collect_counts(conn: sqlite3.Connection, since: str) -> dict:
    """소스별 카운트를 모은다 (SELECT 만)."""
    counts: dict = {}

    def row(source: str) -> dict:
        return counts.setdefault(
            source,
            {"fetched": 0, "stored": 0, "council": 0, "company": 0, "both": 0},
        )

    for r in conn.execute(
        "SELECT source, COALESCE(SUM(total_fetched), 0) AS fetched"
        "  FROM run_history WHERE started_at >= ? GROUP BY source",
        (since,),
    ):
        row(r["source"])["fetched"] = int(r["fetched"] or 0)

    if has_council_columns(conn):
        measured = (
            "SUM(CASE WHEN COALESCE(council_match, 0) = 1 THEN 1 ELSE 0 END) AS council,"
            " SUM(CASE WHEN COALESCE(council_only, 0) = 0 THEN 1 ELSE 0 END) AS company,"
            " SUM(CASE WHEN COALESCE(council_match, 0) = 1"
            "      AND COALESCE(council_only, 0) = 0 THEN 1 ELSE 0 END) AS both"
        )
    else:
        # 컬럼이 없던 시절의 행은 전부 회사 경로가 고른 것이다.
        measured = "0 AS council, COUNT(*) AS company, 0 AS both"

    for r in conn.execute(
        f"""
        SELECT source, COUNT(*) AS stored, {measured}
          FROM announcements WHERE created_at >= ? GROUP BY source
        """,
        (since,),
    ):
        entry = row(r["source"])
        entry["stored"] = int(r["stored"] or 0)
        entry["council"] = int(r["council"] or 0)
        entry["company"] = int(r["company"] or 0)
        entry["both"] = int(r["both"] or 0)

    return counts


def render_table(counts: dict, council_sources) -> str:
    """카운트 표를 마크다운으로 (정렬: 저장 내림차순 → 소스명)."""
    lines = [
        "| source | 협의회풀 | fetched | 저장 | council_match | 회사통과 | 둘다 |",
        "|---|---|---|---|---|---|---|",
    ]
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1]["stored"], kv[0]))
    total = {"fetched": 0, "stored": 0, "council": 0, "company": 0, "both": 0}
    for source, c in ordered:
        for key in total:
            total[key] += c[key]
        lines.append(
            f"| {source} | {'Y' if source in council_sources else '-'} "
            f"| {c['fetched']} | {c['stored']} | {c['council']} "
            f"| {c['company']} | {c['both']} |"
        )
    lines.append(
        f"| **합계** | | {total['fetched']} | {total['stored']} "
        f"| {total['council']} | {total['company']} | {total['both']} |"
    )
    return "\n".join(lines)


def collect_samples(conn, since, council_sources, n, seed):
    """표본 두 묶음: ``council_match=1`` N건 + 협의회 소스의 미매치 N건."""
    if n <= 0:
        return []

    rng = random.Random(seed)
    picked = []

    def take(sql, params, label):
        rows = conn.execute(sql, params).fetchall()
        chosen = rows if len(rows) <= n else rng.sample(rows, n)
        for r in chosen:
            picked.append({
                "표본군": label,
                "id": r["id"],
                "source": r["source"],
                "title": r["title"],
                "council_score": r["council_score"],
                "council_tags": r["council_tags"],
                "council_match": r["council_match"],
                "council_only": r["council_only"],
                "relevance_score": r["relevance_score"],
                "created_at": r["created_at"],
            })

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
        unmatched = " AND COALESCE(council_match, 0) = 0" if measured else ""
        take(
            f"SELECT {cols} FROM announcements"
            f"  WHERE created_at >= ?{unmatched}"
            f"   AND source IN ({placeholders})",
            (since, *sources),
            SAMPLE_SOURCE_ONLY,
        )
    return picked


def write_csv(path: Path, samples) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_FIELDS))
        writer.writeheader()
        for row in samples:
            writer.writerow(row)


def build_report(days, since, db_path, counts, council_sources, samples, csv_name,
                 measured=True):
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
    if not measured:
        head[6:6] = ["", MIGRATION_PENDING_NOTE]
    if samples:
        matched = sum(1 for s in samples if s["표본군"] == SAMPLE_MATCHED)
        head += [
            "## 표본",
            "",
            f"- `{SAMPLE_MATCHED}` {matched}건 / "
            f"`{SAMPLE_SOURCE_ONLY}` {len(samples) - matched}건",
            f"- CSV: `{csv_name}`",
            "",
        ]
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

    council_sources = set(get_config().council_profile.sources)
    since = window_start(args.days)

    conn = open_readonly(db_path)
    try:
        measured = has_council_columns(conn)
        counts = collect_counts(conn, since)
        samples = collect_samples(
            conn, since, council_sources, args.sample, args.seed
        )
    finally:
        conn.close()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = date.today().isoformat()
    md_path = out_dir / f"{stem}.md"
    csv_path = out_dir / f"{stem}.csv"

    if samples:
        write_csv(csv_path, samples)
    report = build_report(
        args.days, since, db_path, counts, council_sources, samples, csv_path.name,
        measured=measured,
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
