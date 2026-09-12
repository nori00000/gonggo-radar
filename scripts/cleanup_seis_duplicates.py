#!/usr/bin/env python3
"""수리 이전에 적재된 중복·가짜 마감 행을 정리한다 (계약 v2.1 판정 4·6-①).

크롤러 수리(커밋 6cfed6d·080a38b)는 **앞으로 들어오는** 데이터만 바로잡는다.
이미 저장된 행은 재크롤로 사라지지 않으므로 이 스크립트가 치운다.

파일명은 seis에서 출발한 역사적 이름이고, 실제 정리 범위는 아래 4종이다.

1. **복수 링크 / ID 오매칭 중복 삭제** — ``seis``(회차별 링크가 같은 공고를
   9행까지 만듦), ``socialenterprise``(게시판 구분자 ``bsIdx`` 를 글 ID로
   써서 한 글이 2행), ``smartfarm``(``href="#void"`` 가 해소되지 않아 한 글이
   2행). 세 소스 모두 **대표 1행만** 남긴다.
2. **socialenterprise 가짜 마감** — 게시일이 ``period_end`` 에 들어가 있어
   판정 4의 "마감 경과 → 제외" 에 걸리면 살아있는 공고가 사라진다.
3. **coop 가짜 접수 시작일** — 게시일이 ``period_start`` 에 들어가 있어
   존재하지 않는 접수기간이 브리핑에 표시된다.

기본은 ``--dry-run`` 이다. 실제로 쓰려면 ``--apply`` 를 명시해야 하고,
그때만 DB를 ``announcements.db.bak-<timestamp>`` 로 백업한다.

사용::

    python scripts/cleanup_seis_duplicates.py                 # 미리보기
    python scripts/cleanup_seis_duplicates.py --apply         # 실제 정리
    python scripts/cleanup_seis_duplicates.py --db /tmp/copy.db
"""

import argparse
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_DB = "alert/data/announcements.db"

# 중복 정리 대상 소스 (같은 규칙 B를 공유한다)
DEDUPE_SOURCES = ("seis", "socialenterprise", "smartfarm")

# 소스별 "글 고유번호" 파라미터 - 대표 행 선정의 결정적 근거.
# 수리된 크롤러의 _extract_post_id 와 같은 파라미터를 본다.
POST_ID_PARAMS: Dict[str, Tuple[str, ...]] = {
    "seis": ("fncPbofrSn", "dsgnPbofrSn", "itgrdAplyPbancSn", "epsdNo"),
    "socialenterprise": ("bIdx",),
    "smartfarm": ("searchNttId",),
}

# 중복 판별용 제목 정규화 - 크롤러의 SeisCrawler._normalize_title 과 같은 규칙
_TITLE_NOISE = re.compile(r"[\s·.,()\[\]{}「」『』\-~/]+")


def normalize_title(title: str) -> str:
    """공백·구분기호를 없앤 비교용 제목."""
    return _TITLE_NOISE.sub("", title or "")


def load_raw(raw_data: Optional[str]) -> dict:
    """raw_data JSON을 딕셔너리로 읽는다 (깨져 있으면 빈 딕셔너리)."""
    if not raw_data:
        return {}
    try:
        loaded = json.loads(raw_data)
    except (ValueError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def id_matches_url(source: str, source_id: str, url: str) -> bool:
    """source_id 가 URL의 **글 고유번호 자리**에 실제로 들어 있는지 본다.

    같은 글이 두 행으로 갈린 경우(``bsIdx`` 오매칭, ``#void`` 미해소) 어느
    쪽이 진짜인지는 이것으로 갈린다 - 예를 들어 ``bsIdx=10002&bIdx=252628``
    URL에서 ``10002`` 는 게시판 구분자이므로 ``bIdx`` 자리에 없다.

    Args:
        source: 소스 이름
        source_id: 저장된 source_id
        url: 저장된 상세 URL

    Returns:
        고유번호 자리에서 일치하면 True
    """
    if not source_id or not url:
        return False
    for param in POST_ID_PARAMS.get(source, ()):
        if re.search(rf"[?&]{re.escape(param)}={re.escape(source_id)}(?:&|$)", url, re.I):
            return True
    return False


def canonical_rank(row: sqlite3.Row) -> Tuple[int, str, int, int]:
    """대표 행 선정 순위 (큰 쪽이 이긴다).

    순서대로: ①URL의 고유번호 자리와 일치 ②접수 시작일이 늦음
    ③회차 번호가 큼 ④공고번호가 큼.
    """
    payload = load_raw(row["raw_data"])
    round_match = re.search(r"(\d+)\s*차", str(payload.get("round", "")))
    round_no = int(round_match.group(1)) if round_match else -1
    source_id = row["source_id"] or ""
    post_no = int(source_id) if source_id.isdigit() else -1
    url_ok = 1 if id_matches_url(row["source"], source_id, row["url"] or "") else 0
    return (url_ok, row["period_start"] or "", round_no, post_no)


def find_duplicates(
    conn: sqlite3.Connection, source: str
) -> Tuple[List[sqlite3.Row], List[str]]:
    """한 소스에서 삭제할 중복 행과 판단 근거를 찾는다.

    두 규칙을 함께 쓴다:

    - (규칙 A) 대표 행의 ``raw_data.merged_source_ids`` 에 적힌 source_id.
      수리된 파서가 병합하며 남긴 감사 기록이므로 가장 확실하다.
    - (규칙 B) 정규화 제목이 같고 **같은 주(created_at 기준)** 에 적재된 묶음.
      수리 이전 데이터에는 감사 기록이 없으므로 이 규칙이 필요하다.

    Args:
        conn: 열린 DB 커넥션
        source: 소스 이름

    Returns:
        (삭제 대상 행 리스트, 사람이 읽을 근거 문장 리스트)
    """
    rows = conn.execute(
        "SELECT id, source, source_id, title, url, period_start, period_end,"
        "       raw_data, created_at,"
        "       strftime('%Y-%W', created_at) AS week"
        " FROM announcements WHERE source = ?",
        (source,),
    ).fetchall()

    by_source_id: Dict[str, sqlite3.Row] = {r["source_id"]: r for r in rows}
    doomed: Dict[int, sqlite3.Row] = {}
    reasons: List[str] = []

    # 규칙 A: 대표 행이 남긴 병합 기록
    for row in rows:
        merged = load_raw(row["raw_data"]).get("merged_source_ids") or []
        for merged_id in merged:
            victim = by_source_id.get(str(merged_id))
            if victim is not None and victim["id"] != row["id"]:
                doomed[victim["id"]] = victim
                reasons.append(
                    f"  [A] id={victim['id']} source_id={victim['source_id']} "
                    f"-> 대표 source_id={row['source_id']} 의 merged_source_ids"
                )

    # 규칙 B: 같은 제목 + 같은 주
    groups: Dict[Tuple[str, str], List[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(
            (normalize_title(row["title"]), row["week"] or ""), []
        ).append(row)

    for (_title_key, week), group in groups.items():
        if len(group) < 2:
            continue
        keeper = max(group, key=canonical_rank)
        keeper_note = (
            "URL 고유번호 일치"
            if id_matches_url(source, keeper["source_id"], keeper["url"] or "")
            else "접수시작일/회차/번호 최대"
        )
        for row in group:
            if row["id"] == keeper["id"] or row["id"] in doomed:
                continue
            doomed[row["id"]] = row
            reasons.append(
                f"  [B] id={row['id']} source_id={row['source_id']} "
                f"-> 같은 제목·같은 주({week}) 대표 source_id={keeper['source_id']}"
                f" ({keeper_note})"
            )

    ordered = sorted(doomed.values(), key=lambda r: r["id"])
    return ordered, reasons


def find_fake_deadlines(
    conn: sqlite3.Connection, skip_ids: Optional[set] = None
) -> Tuple[List[sqlite3.Row], List[str]]:
    """socialenterprise 의 게시일=마감 행을 찾는다.

    지시받은 조건은 ``period_end == DATE(created_at)`` 이지만, 실측에서는
    게시일이 적재일보다 하루 이상 앞서기 때문에 그 조건만으로는 한 행도
    잡히지 않았다(예: period_end=2026-09-11, created_at=2026-09-12).
    그래서 실제 지문인 ``period_start == period_end`` (단일 게시일이
    기간으로 해석된 흔적) 도 함께 본다. 상세 인용에서 온 진짜 기간
    (``raw_data.quote_period_end``) 은 건드리지 않는다.

    Args:
        conn: 열린 DB 커넥션
        skip_ids: (b) 단계에서 이미 삭제되는 행 id - 이중 계상을 막는다

    Returns:
        (period_end 를 비울 행 리스트, 근거 문장 리스트)
    """
    rows = conn.execute(
        "SELECT id, source_id, title, period_start, period_end, raw_data,"
        "       date(created_at) AS created_date"
        " FROM announcements"
        " WHERE source = 'socialenterprise'"
        "   AND period_end IS NOT NULL AND period_end != ''"
    ).fetchall()

    skip_ids = skip_ids or set()
    doomed: List[sqlite3.Row] = []
    reasons: List[str] = []
    for row in rows:
        if row["id"] in skip_ids:
            continue  # (b)에서 이미 삭제되는 행
        payload = load_raw(row["raw_data"])
        if payload.get("quote_period_end"):
            continue  # 상세 인용에서 온 진짜 마감
        if row["period_end"] == row["created_date"]:
            doomed.append(row)
            reasons.append(
                f"  [지시조건] id={row['id']} period_end={row['period_end']} "
                f"== date(created_at)"
            )
        elif row["period_end"] == row["period_start"]:
            doomed.append(row)
            reasons.append(
                f"  [게시일지문] id={row['id']} period_start=period_end="
                f"{row['period_end']} (단일 게시일이 기간으로 해석됨)"
            )
    return doomed, reasons


def find_fake_starts(
    conn: sqlite3.Connection, skip_ids: Optional[set] = None
) -> Tuple[List[sqlite3.Row], List[str]]:
    """coop 의 게시일=접수 시작일 행을 찾는다.

    coop 목록은 게시일만 주는데 그것이 ``period_start`` 에 들어가 있었다
    (판정 4에 따라 크롤러는 이제 비워 둔다). ``period_end`` 가 없고 상세
    인용 기록도 없는 행이 그 흔적이다.
    """
    rows = conn.execute(
        "SELECT id, source_id, title, period_start, period_end, raw_data"
        " FROM announcements"
        " WHERE source = 'coop'"
        "   AND period_start IS NOT NULL AND period_start != ''"
        "   AND (period_end IS NULL OR period_end = '')"
    ).fetchall()

    skip_ids = skip_ids or set()
    doomed: List[sqlite3.Row] = []
    reasons: List[str] = []
    for row in rows:
        if row["id"] in skip_ids:
            continue  # (b)에서 이미 삭제되는 행
        if load_raw(row["raw_data"]).get("quote_period_start"):
            continue
        doomed.append(row)
        reasons.append(
            f"  [게시일지문] id={row['id']} period_start={row['period_start']} "
            f"(마감 없음 + 인용 없음)"
        )
    return doomed, reasons


def counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """정리 전/후 비교용 집계 (중복 대상 소스는 소스별로 나눈다)."""
    def one(sql: str, params: tuple = ()) -> int:
        return conn.execute(sql, params).fetchone()[0]

    values: Dict[str, int] = {}
    for source in DEDUPE_SOURCES:
        values[f"{source}_rows"] = one(
            "SELECT COUNT(*) FROM announcements WHERE source = ?", (source,)
        )
        values[f"{source}_distinct_titles"] = one(
            "SELECT COUNT(DISTINCT title) FROM announcements WHERE source = ?",
            (source,),
        )
    values["se_rows_with_deadline"] = one(
        "SELECT COUNT(*) FROM announcements WHERE source='socialenterprise'"
        " AND period_end IS NOT NULL AND period_end != ''"
    )
    values["coop_rows_with_start"] = one(
        "SELECT COUNT(*) FROM announcements WHERE source='coop'"
        " AND period_start IS NOT NULL AND period_start != ''"
    )
    values["total_rows"] = one("SELECT COUNT(*) FROM announcements")
    return values


def print_counts(label: str, values: Dict[str, int]) -> None:
    print(f"\n[{label}]")
    for key, value in values.items():
        print(f"  {key:<30} {value}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="수리 이전 중복·가짜 마감 행 정리 (기본 dry-run)"
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB,
        help=f"announcements.db 경로 (기본: {DEFAULT_DB})",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run", action="store_true", default=True,
        help="무엇을 지울지만 보여준다 (기본값)",
    )
    group.add_argument(
        "--apply", action="store_true",
        help="실제로 정리한다 (백업 후 실행)",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB를 찾을 수 없습니다: {db_path}", file=sys.stderr)
        return 1

    apply_changes = bool(args.apply)
    mode = "APPLY" if apply_changes else "DRY-RUN"
    print(f"=== cleanup_seis_duplicates ({mode}) ===")
    print(f"DB: {db_path}")
    print(f"중복 정리 대상 소스: {', '.join(DEDUPE_SOURCES)}")

    # (a) 백업 - 실제로 쓸 때만
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.bak-{timestamp}")
    if apply_changes:
        shutil.copy2(db_path, backup_path)
        print(f"백업 생성: {backup_path}")
    else:
        print(f"백업 예정 경로(미생성): {backup_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    before = counts(conn)
    print_counts("before", before)

    # (b) 소스별 중복
    duplicates: Dict[str, List[sqlite3.Row]] = {}
    print("\n[b] 중복 삭제 대상")
    for source in DEDUPE_SOURCES:
        rows, reasons = find_duplicates(conn, source)
        duplicates[source] = rows
        print(f"  {source}: {len(rows)} 행")
        for line in reasons:
            print(f"  {line}")
    total_duplicates = sum(len(rows) for rows in duplicates.values())
    print(f"  합계: {total_duplicates} 행")

    doomed_ids = {row["id"] for rows in duplicates.values() for row in rows}

    # (c) socialenterprise 가짜 마감 - (b)에서 지워지는 행은 제외해 이중 계상을 막는다
    fake_deadlines, deadline_reasons = find_fake_deadlines(conn, doomed_ids)
    print(f"\n[c] socialenterprise period_end -> NULL: {len(fake_deadlines)} 행")
    for line in deadline_reasons:
        print(line)

    # (c-확장) coop 가짜 접수 시작일
    fake_starts, start_reasons = find_fake_starts(conn, doomed_ids)
    print(f"\n[c+] coop period_start -> NULL: {len(fake_starts)} 행")
    for line in start_reasons:
        print(line)

    if apply_changes:
        with conn:
            if doomed_ids:
                conn.executemany(
                    "DELETE FROM announcements WHERE id = ?",
                    [(row_id,) for row_id in sorted(doomed_ids)],
                )
            if fake_deadlines:
                conn.executemany(
                    "UPDATE announcements SET period_end = NULL,"
                    " updated_at = ? WHERE id = ?",
                    [(datetime.now().isoformat(), row["id"]) for row in fake_deadlines],
                )
            if fake_starts:
                conn.executemany(
                    "UPDATE announcements SET period_start = NULL,"
                    " updated_at = ? WHERE id = ?",
                    [(datetime.now().isoformat(), row["id"]) for row in fake_starts],
                )
        print_counts("after", counts(conn))
        print(f"\n완료. 되돌리려면: cp {backup_path} {db_path}")
    else:
        projected = dict(before)
        for source, rows in duplicates.items():
            projected[f"{source}_rows"] -= len(rows)
        # 중복 삭제로 함께 사라지는 socialenterprise 마감 행까지 반영한다
        deleted_se_with_deadline = sum(
            1 for row in duplicates.get("socialenterprise", [])
            if row["period_end"]
        )
        projected["se_rows_with_deadline"] -= len(fake_deadlines) + deleted_se_with_deadline
        projected["coop_rows_with_start"] -= len(fake_starts)
        projected["total_rows"] -= total_duplicates
        print_counts("after (예상)", projected)
        print("\n실제로 정리하려면 --apply 를 붙여 다시 실행하세요.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
