#!/usr/bin/env python3
"""수리 이전에 적재된 중복·가짜 마감 행을 정리한다 (계약 v2.1 판정 4·6-①).

크롤러 수리(커밋 6cfed6d·080a38b)는 **앞으로 들어오는** 데이터만 바로잡는다.
이미 저장된 행은 재크롤로 사라지지 않으므로 이 스크립트가 치운다.
파일명은 seis에서 출발한 역사적 이름이고, 실제 범위는 아래 4종이다.

규칙은 **적용 순서가 있다** (Codex 크리틱 #1):

- **규칙 A** (모든 소스): 대표 행의 ``raw_data.merged_source_ids`` 에 적힌
  source_id를 삭제한다. 이때 대표 행은 *canonical* 로 잠기고 이후 어떤
  규칙도 건드리지 못한다. A를 B보다 먼저 적용하지 않으면 A가 지목한
  대표를 B가 지워 **묶음 전체가 사라진다**.
- **규칙 B** (``seis`` 전용, 크리틱 #2): 정규화 제목 · 주체(span.sub) ·
  접수 종료일이 **모두** 같은 묶음만 병합한다. 지역·회차·접수기간이 다르면
  별개 공고다. 다른 소스에는 적용하지 않는다.
- **규칙 C** (seis 외): "source_id가 자기 URL의 글번호 자리에 없다" 는
  **증명 가능한 결함**만 지운다 (``bsIdx`` 를 글 ID로 쓴 행, ``#void`` 로
  남은 행). 제목이 같아도 양쪽 모두 제 글번호를 갖고 있으면 손대지 않는다.
- **불변식**: 어떤 묶음도 0행이 되지 않고 canonical은 반드시 살아남는다.
  위반하면 아무것도 지우지 않고 종료한다.

그 밖에 정리하는 것:

- **가짜 마감 (전 소스)**: 게시일이 ``period_end`` 에 들어간 행.
  단, 원문이 **당일 접수**(``date`` 가 범위 표기)라고 말하는 행은 남긴다
  (크리틱 #9).
- **coop 가짜 접수 시작일**: 게시일이 ``period_start`` 에 들어간 행.

기본은 ``--dry-run`` 이다. ``--apply`` 를 명시해야 쓰며, 그때만 SQLite
backup API로 백업한다(WAL 포함 - 크리틱 #5).

사용::

    python scripts/cleanup_seis_duplicates.py                 # 미리보기
    python scripts/cleanup_seis_duplicates.py --apply         # 실제 정리
    python scripts/cleanup_seis_duplicates.py --db /tmp/copy.db
"""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# sys.path 보정 - 크롤러와 **같은** 중복 판별 키를 쓰기 위해 공유 모듈을 읽는다
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alert.crawlers.dedupe_keys import (  # noqa: E402
    extract_region,
    group_key,
    keys_compatible,
    normalize_title,
)
from alert.crawlers.date_labels import POSTED_LABELS  # noqa: E402
from alert.crawlers.detail_quotes import period_from_quote  # noqa: E402

DEFAULT_DB = "alert/data/announcements.db"

# 마감 교정 대상 = **협의회 8소스** (7차 게이트 #1).
# 전 소스로 넓혔더니 g2b 처럼 목록이 실제 마감(``bidClseDt``)을 주는 소스에서
# 적재일과 마감일이 같은 날이면 **정상 마감을 지웠다**. 브리핑이 쓰는 소스만
# 손댄다.
DEADLINE_FIX_SOURCES = (
    "seis", "socialenterprise", "fowi", "forest_service", "forest_press",
    "kofpi", "coop", "lawmaking",
)

# 규칙 B(제목·주체·종료일 병합)는 회차별 링크를 뿌리는 seis 에만 쓴다
RULE_B_SOURCE = "seis"
# 규칙 C(글번호 오매칭·미해소 URL)를 적용할 소스
RULE_C_SOURCES = ("socialenterprise", "smartfarm")
CLEANUP_SOURCES = (RULE_B_SOURCE,) + RULE_C_SOURCES

# 소스별 "글 고유번호" 파라미터 - 수리된 _extract_post_id 와 같은 자리를 본다
POST_ID_PARAMS: Dict[str, Tuple[str, ...]] = {
    "seis": ("fncPbofrSn", "dsgnPbofrSn", "itgrdAplyPbancSn", "epsdNo"),
    "socialenterprise": ("bIdx",),
    "smartfarm": ("searchNttId",),
}

# 기간 문자열에 범위 표기가 있으면 게시일이 아니라 원문이 말한 접수기간이다
_RANGE_MARK = re.compile(r"[~∼〜]|부터")
_DATE_TOKEN = re.compile(r"\d{4}\s*[.\-/년]\s*\d{1,2}\s*[.\-/월]\s*\d{1,2}")
# 원문이 접수를 이야기하고 있다는 신호 (날짜와 함께 있을 때만 의미가 있다)
_RECEPTION_WORDS = re.compile(r"당일|접수|신청|모집|공모|마감")
# 시작만 말하는 표현 - 종료 근거로 쓰면 안 된다 (4차 게이트 #4)
_START_ONLY = re.compile(r"부터|이후|개시|시작")
# 기간을 말하는 필드는 게시일 후보가 아니다 (4차 게이트 #4).
# "2026-09-30까지" 를 게시일로 넣은 탓에 정상 마감이 교정 대상이 됐다.
_PERIOD_WORDS = re.compile(r"까지|부터|기간|마감|접수|신청|모집|공모")


def load_raw(raw_data: Optional[str]) -> dict:
    """raw_data JSON을 딕셔너리로 읽는다 (깨져 있으면 빈 딕셔너리)."""
    if not raw_data:
        return {}
    try:
        loaded = json.loads(raw_data)
    except (ValueError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def region_candidates(row: sqlite3.Row) -> List[str]:
    """지역 후보 문자열 - ``sub`` 와 ``info`` 를 **함께** 본다.

    한쪽만 보면 사업명이 같고 지역만 다른 공고가 합쳐진다
    (Codex 재검토 #4). 구 파서가 남긴 ``region`` 키도 후보에 넣는다.
    """
    payload = load_raw(row["raw_data"])
    candidates = [str(payload.get("sub") or ""), str(payload.get("region") or "")]
    info = payload.get("info")
    if isinstance(info, list):
        candidates.extend(str(value) for value in info)
    return [value for value in candidates if value]


def row_region(row: sqlite3.Row) -> str:
    """이 행이 말하는 지역 (없으면 빈 문자열)."""
    return extract_region(region_candidates(row))


def row_key(
    row: sqlite3.Row, corrected: Optional[Dict[int, Optional[str]]] = None
) -> Tuple[str, Tuple[str, ...], str, str]:
    """행의 중복 판별 키 - 크롤러와 같은 ``group_key`` 를 쓴다.

    접수 종료일이 없으면 **적재 월**로 갈라 회차가 다른 공고가 합쳐지지
    않게 한다 (Codex 재검토 #4).
    """
    created = str(row["created_at"] or "")
    payload = load_raw(row["raw_data"])
    rounds = [str(payload.get("round") or "")] + region_candidates(row)
    # **교정된 마감**을 쓴다 (7차 게이트 #2). 교정될 가짜 마감을 확정 마감처럼
    # 쓰면 회차 키가 비워져 별도 회차(1차/2차)가 삭제 대상이 된다.
    period_end = row["period_end"] or None
    if corrected is not None and row["id"] in corrected:
        period_end = corrected[row["id"]]
    return group_key(
        row["title"],
        region_candidates(row),
        period_end,
        ingested_month=created[:7],
        round_candidates=rounds,
    )


def id_matches_url(source: str, source_id: str, url: str) -> bool:
    """source_id 가 URL의 **글번호 자리**에 실제로 들어 있는지 본다.

    같은 글이 두 행으로 갈린 경우 어느 쪽이 진짜인지는 이것으로 갈린다 -
    ``bsIdx=10002&bIdx=252628`` 에서 ``10002`` 는 게시판 구분자이므로
    ``bIdx`` 자리에 없다.
    """
    if not source_id or not url:
        return False
    for param in POST_ID_PARAMS.get(source, ()):
        if re.search(rf"[?&]{re.escape(param)}={re.escape(source_id)}(?:&|$)", url, re.I):
            return True
    return False


def url_is_unresolved(source: str, url: str) -> bool:
    """상세 URL이 해소되지 않은 행인지 본다 (``#void``, 글번호 없음)."""
    if not url:
        return True
    if url.endswith("#void") or "#void" in url:
        return True
    return not any(
        re.search(rf"[?&]{re.escape(param)}=\d+", url, re.I)
        for param in POST_ID_PARAMS.get(source, ())
    )


def canonical_rank(row: sqlite3.Row) -> Tuple[int, str, int, int]:
    """대표 행 선정 순위 (큰 쪽이 이긴다).

    ①URL의 글번호 자리와 일치 ②접수 시작일이 늦음 ③회차가 큼 ④번호가 큼.
    """
    payload = load_raw(row["raw_data"])
    round_match = re.search(r"(\d+)\s*차", str(payload.get("round", "")))
    round_no = int(round_match.group(1)) if round_match else -1
    source_id = row["source_id"] or ""
    post_no = int(source_id) if source_id.isdigit() else -1
    url_ok = 1 if id_matches_url(row["source"], source_id, row["url"] or "") else 0
    return (url_ok, row["period_start"] or "", round_no, post_no)


def _fetch_rows(conn: sqlite3.Connection, source: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT id, source, source_id, title, url, period_start, period_end,"
        "       raw_data, created_at,"
        "       strftime('%Y-%W', created_at) AS week"
        " FROM announcements WHERE source = ?",
        (source,),
    ).fetchall()


def find_duplicates(
    conn: sqlite3.Connection,
    source: str,
    corrected: Optional[Dict[int, Optional[str]]] = None,
) -> Tuple[List[sqlite3.Row], List[str], Set[int]]:
    """한 소스에서 삭제할 중복 행과 근거, canonical 집합을 낸다.

    Args:
        conn: 열린 DB 커넥션
        source: 소스 이름

    Returns:
        ``(삭제 대상 행, 근거 문장, canonical 행 id 집합)``

    Raises:
        RuntimeError: 어떤 키 묶음이든 0행이 되는 계획이 나오면 (불변식 위반)
    """
    rows = _fetch_rows(conn, source)
    by_source_id: Dict[str, sqlite3.Row] = {r["source_id"]: r for r in rows}
    doomed: Dict[int, sqlite3.Row] = {}
    absorbed: Dict[int, int] = {}      # 삭제되는 행 -> 대표 행 (불변식 검사용)
    reasons: List[str] = []

    # ---------- 규칙 A (먼저, canonical 잠금) ----------
    # 병합 기록을 **검증한 뒤에만** 믿는다: 대표와 제목·지역·기간이 같아야
    # 한다. 예전 오병합 기록("서울 100 이 부산 101 을 병합")을 그대로
    # 확정하면 별개 공고가 사라진다 (Codex 재검토 #5).
    canonical: Set[int] = set()
    for row in rows:
        merged = load_raw(row["raw_data"]).get("merged_source_ids") or []
        if not merged:
            continue
        keeper_key = row_key(row, corrected)
        honoured = False
        for merged_id in merged:
            victim = by_source_id.get(str(merged_id))
            if victim is None or victim["id"] == row["id"]:
                continue
            victim_key = row_key(victim, corrected)
            same_url = (victim["url"] or "") == (row["url"] or "") and bool(row["url"])
            if not keys_compatible(victim_key, keeper_key, ignore_deadline=same_url):
                reasons.append(
                    f"  [A?] id={victim['id']} source_id={victim['source_id']} "
                    f"병합 기록 무시 - 대표 source_id={row['source_id']} 와 "
                    f"제목·지역·기간이 다르다 "
                    f"(대표 {keeper_key[1] or '지역없음'}/{keeper_key[2] or '기간없음'} "
                    f"vs {victim_key[1] or '지역없음'}/{victim_key[2] or '기간없음'})"
                )
                continue
            doomed[victim["id"]] = victim
            absorbed[victim["id"]] = row["id"]
            honoured = True
            reasons.append(
                f"  [A] id={victim['id']} source_id={victim['source_id']} "
                f"-> 대표 source_id={row['source_id']} 의 merged_source_ids "
                f"(키 일치 확인)"
            )
        if honoured:
            canonical.add(row["id"])

    # canonical 은 A가 지목했어도 지우지 않는다 (기록 충돌 시 대표 보존)
    for row_id in list(canonical):
        if doomed.pop(row_id, None) is not None:
            reasons.append(
                f"  [A!] id={row_id} 은 canonical 이므로 삭제 목록에서 제외 (기록 충돌)"
            )

    survivors = [r for r in rows if r["id"] not in doomed]

    # ---------- 규칙 B (seis 전용) 또는 규칙 C ----------
    if source == RULE_B_SOURCE:
        groups: Dict[Tuple[str, Tuple[str, ...], str, str], List[sqlite3.Row]] = {}
        for row in survivors:
            groups.setdefault(row_key(row, corrected), []).append(row)

        for (_title, region, deadline, round_key), group in groups.items():
            if len(group) < 2:
                continue
            locked = [r for r in group if r["id"] in canonical]
            keeper = locked[0] if locked else max(group, key=canonical_rank)
            for row in group:
                if row["id"] == keeper["id"] or row["id"] in canonical:
                    continue
                doomed[row["id"]] = row
                absorbed[row["id"]] = keeper["id"]
                reasons.append(
                    f"  [B] id={row['id']} source_id={row['source_id']} -> 대표 "
                    f"source_id={keeper['source_id']} (제목·지역"
                    f"{region or '(없음)'}·마감키{deadline or '(없음)'} 동일)"
                )
    else:
        # 규칙 C는 제목으로 모으고, 키 비교는 행 대 행으로 한다
        groups_by_title: Dict[str, List[sqlite3.Row]] = {}
        for row in survivors:
            groups_by_title.setdefault(normalize_title(row["title"]), []).append(row)

        for group in groups_by_title.values():
            if len(group) < 2:
                continue
            # 규칙 C는 "글번호가 자기 URL에 없다" 는 증명 가능한 결함만 지운다.
            # 게다가 같은 키(제목·지역·기간) 묶음 안에서만 본다 - URL 미해소는
            # 그 자체로 중복 증거가 아니다 (Codex 재검토 #6).
            provable = [
                r for r in group
                if id_matches_url(source, r["source_id"], r["url"] or "")
            ]
            if not provable:
                continue  # 근거가 없으면 손대지 않는다
            for row in group:
                if row["id"] in canonical:
                    continue
                # 같은 URL이면 글번호가 맞든 틀리든 같은 글이다 (4차 게이트 #8).
                # 예전에는 "제 글번호를 가진 행" 이라는 이유로 같은 URL의
                # 중복을 하나도 지우지 못했다.
                same_url_keeper = next(
                    (
                        keeper for keeper in provable
                        if keeper["id"] != row["id"]
                        and (row["url"] or "")
                        and (row["url"] or "") == (keeper["url"] or "")
                        and keys_compatible(
                            row_key(row, corrected), row_key(keeper, corrected), ignore_deadline=True
                        )
                    ),
                    None,
                )
                if same_url_keeper is not None:
                    doomed[row["id"]] = row
                    absorbed[row["id"]] = same_url_keeper["id"]
                    reasons.append(
                        f"  [C] id={row['id']} source_id={row['source_id']} -> 대표 "
                        f"source_id={same_url_keeper['source_id']} (같은 URL)"
                    )
                    continue
                if id_matches_url(source, row["source_id"], row["url"] or ""):
                    continue  # 제 글번호를 가진 행은 별개 공고로 본다
                # **모든** 정상 대표와 대조한다 - 최대 순위 하나만 보면 다른
                # 대표의 중복을 놓친다 (최종 게이트 #7).
                matched = None
                cause = ""
                for keeper in provable:
                    if keeper["id"] == row["id"]:
                        continue
                    same_url = (row["url"] or "") == (keeper["url"] or "")
                    if same_url:
                        # 같은 URL은 적재월이 달라도 같은 글이다
                        if keys_compatible(
                            row_key(row, corrected), row_key(keeper, corrected), ignore_deadline=True
                        ):
                            matched, cause = keeper, "같은 URL + 글번호 자리 불일치"
                            break
                        continue
                    if url_is_unresolved(source, row["url"] or "") and keys_compatible(
                        row_key(row, corrected), row_key(keeper, corrected)
                    ):
                        matched, cause = keeper, "URL 미해소(#void)"
                        break
                if matched is None:
                    continue
                doomed[row["id"]] = row
                absorbed[row["id"]] = matched["id"]
                reasons.append(
                    f"  [C] id={row['id']} source_id={row['source_id']} -> 대표 "
                    f"source_id={matched['source_id']} ({cause}, 키 일치)"
                )

    # ---------- 불변식 ----------
    _assert_invariants(source, doomed, absorbed, canonical)

    ordered = sorted(doomed.values(), key=lambda r: r["id"])
    return ordered, reasons, canonical


def _assert_invariants(
    source: str,
    doomed: Dict[int, sqlite3.Row],
    absorbed: Dict[int, int],
    canonical: Set[int],
) -> None:
    """**삭제되는 행마다 살아남는 대표가 있는지** 확인한다.

    이것이 진짜 불변식이다 (최종 게이트 #7 수정): 키 묶음 단위로 "1행 이상
    생존" 을 보면, 대표에 흡수된 행이 자기 키 묶음을 비우는 정상 병합까지
    전멸로 오판한다(적재월·메타데이터가 대표와 다를 수 있다). 흡수 관계를
    직접 검사하면 "대표 없이 사라지는 공고" 만 정확히 막는다.

    Raises:
        RuntimeError: canonical이 삭제되거나, 대표 없이 삭제되는 행이 있으면
    """
    killed_canonical = canonical & set(doomed)
    if killed_canonical:
        raise RuntimeError(
            f"{source}: canonical 행이 삭제 목록에 있다 (id={sorted(killed_canonical)})"
        )

    orphans = [row_id for row_id in doomed if row_id not in absorbed]
    if orphans:
        raise RuntimeError(
            f"{source}: 대표 없이 삭제되는 행이 있다 (id={sorted(orphans)})"
        )

    cannibals = {
        row_id: keeper
        for row_id, keeper in absorbed.items()
        if keeper in doomed
    }
    if cannibals:
        raise RuntimeError(
            f"{source}: 대표까지 삭제된다 (행->대표 {cannibals})"
        )


def _raw_date_fields(payload: dict) -> List[str]:
    """원문이 날짜를 말한 필드 값들."""
    values = []
    for key in ("date", "period", "WRITE_DATE", "quote_deadline"):
        value = str(payload.get(key) or "")
        if value:
            values.append(value)
    return values


def states_reception_period(payload: dict) -> bool:
    """원문이 **접수 기간/당일 접수를 스스로 말했는지** 본다.

    Codex 재검토 #10: ``date="2026-09-15 당일 접수"`` 같은 정상 당일 접수를
    지우면 진짜 마감을 잃는다. 반대로 ``quote_deadline="접수기간 별도 공지"``
    처럼 **날짜가 없는** 인용은 근거가 아니다.

    근거로 인정하는 경우:

    - 범위 표기(``~``/부터)가 있다
    - 날짜가 둘 이상이다
    - 날짜가 있고 그 옆에 접수/신청/모집/당일 같은 말이 있다
    """
    for value in _raw_date_fields(payload):
        if _RANGE_MARK.search(value):
            return True
        dates = _DATE_TOKEN.findall(value)
        if len(dates) >= 2:
            return True
        if dates and _RECEPTION_WORDS.search(value):
            return True
    return False


def posting_dates(payload: dict, created_date: str) -> Set[str]:
    """이 행의 **게시일** 후보 - 기간을 말하지 않는 단일 날짜와 적재일.

    범위 표기가 있거나 "까지/부터/기간/마감" 처럼 접수 이야기를 하는 필드는
    게시일이 아니다. 그런 필드의 날짜를 게시일로 취급하면 정상 마감이
    "게시일과 같다" 는 이유로 교정 대상이 된다 (4차 게이트 #4).

    Args:
        payload: raw_data 딕셔너리
        created_date: 적재일 (``YYYY-MM-DD``)

    Returns:
        게시일로 볼 수 있는 날짜 집합
    """
    found: Set[str] = set()
    # **적재일은 게시일이 아니다** (8차 게이트 #1). 수집 시각을 게시일 후보로
    # 넣으면 "적재일과 같은 날 마감" 인 정상 공고가 가짜 마감으로 판정된다 -
    # 실측: 실제 마감 09-20/09-30 두 행이 각각 그 날 적재되어 둘 다 NULL이
    # 되고, 그 뒤 규칙 B가 한 행을 삭제했다.

    posted = str(payload.get("posted") or "")
    for year, month, day in re.findall(
        r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})", posted
    ):
        found.add(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")

    for key in ("date", "WRITE_DATE"):
        value = str(payload.get(key) or "")
        if not value or _RANGE_MARK.search(value) or _PERIOD_WORDS.search(value):
            continue
        matches = re.findall(
            r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})", value
        )
        if len(matches) != 1:
            continue
        year, month, day = matches[0]
        found.add(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")
    return found


def deadline_evidence(payload: dict) -> Optional[str]:
    """raw 필드 **전체**에서 접수 종료일 근거를 찾는다 (최종 게이트 #8).

    강한 근거부터 본다: ``quote_deadline`` → ``period`` → ``date``.
    범위·"까지" 같은 표현은 인용 파서에 맡기고, 날짜 하나 + 접수 관련 말
    ("2026-09-15 당일 접수")은 그 날짜를 종료일 근거로 인정한다.

    Args:
        payload: raw_data 딕셔너리

    Returns:
        근거가 되는 종료일 (ISO). 없으면 None
    """
    for key in ("quote_deadline", "period", "date", "WRITE_DATE"):
        value = str(payload.get(key) or "")
        if not value:
            continue
        _start, end = period_from_quote(value)
        if end:
            return end
        # **시작일은 종료 근거가 아니다** (4차 게이트 #4). "…2026.09.01부터"
        # 만 있는 인용을 종료일로 승격해 정상 마감 09-30을 09-01로 바꿨다.
        if _START_ONLY.search(value):
            continue
        dates = _DATE_TOKEN.findall(value)
        if len(dates) == 1 and _RECEPTION_WORDS.search(value):
            match = re.search(
                r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})", value
            )
            if match:
                year, month, day = (int(g) for g in match.groups())
                return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def find_fake_deadlines(
    conn: sqlite3.Connection, skip_ids: Optional[Set[int]] = None
) -> Tuple[List[Tuple[sqlite3.Row, Optional[str]]], List[str]]:
    """**협의회 8소스**의 마감을 근거와 대조해 고친다.

    라벨이 접수 일정(접수/신청/모집/마감/기한)을 말하면 건드리지 않는다
    (7차 게이트 #1) - 게시일과 같은 날이어도 진짜 마감이다. g2b 처럼 목록이
    실제 마감을 주는 소스는 대상에서 아예 빼 두었다.

    판정 (최종 게이트 #8) - 저장된 마감이 **게시일과 같을 때만** 손댄다:

    - raw 필드의 근거가 **다른 종료일**을 말한다 → 그 값으로 **교체**
    - 근거가 아예 없다 → **NULL**
    - 근거가 저장값과 같다 → **그대로**

    저장된 마감이 게시일과 다르면 근거 없이 건드리지 않는다.

    Args:
        conn: 열린 DB 커넥션
        skip_ids: (b)에서 이미 삭제되는 행 id

    Returns:
        ``([(행, 새 마감 또는 None)], 근거 문장)``
    """
    # **전 소스**를 본다 (6차 게이트 #1). socialenterprise 만 보던 동안
    # SEIS의 게시일=마감 행이 그대로 남았다.
    rows = conn.execute(
        "SELECT id, source, source_id, title, period_start, period_end, raw_data,"
        "       date(created_at) AS created_date"
        " FROM announcements"
        f" WHERE source IN ({', '.join('?' for _ in DEADLINE_FIX_SOURCES)})"
        "   AND period_end IS NOT NULL AND period_end != ''",
        DEADLINE_FIX_SOURCES,
    ).fetchall()

    skip_ids = skip_ids or set()
    changes: List[Tuple[sqlite3.Row, Optional[str]]] = []
    reasons: List[str] = []
    for row in rows:
        if row["id"] in skip_ids:
            continue
        payload = load_raw(row["raw_data"])

        # **게시 라벨 허용목록** (8차 게이트 #1). 예전에는 "접수/마감 라벨을
        # 제외" 하는 차단목록이어서 ``제출일`` 처럼 목록에 없는 라벨이
        # 그대로 교정 대상이 됐다. 이제 라벨이 **없거나** 게시 라벨로
        # 확인될 때만 교정한다 - 모르는 라벨은 손대지 않는다.
        label = str(payload.get("date_label") or "")
        if label and not POSTED_LABELS.search(label):
            continue

        if payload.get("quote_period_end"):
            continue  # 상세 인용에서 온 진짜 마감
        if row["period_end"] not in posting_dates(payload, row["created_date"]):
            continue  # 게시일과 다른 마감 - 근거 없이 건드리지 않는다

        evidence = deadline_evidence(payload)
        if evidence == row["period_end"]:
            continue  # 근거가 저장값을 뒷받침한다
        if evidence:
            changes.append((row, evidence))
            reasons.append(
                f"  [교체] {row['source']} id={row['id']} "
                f"period_end={row['period_end']} "
                f"(게시일) -> {evidence} (raw 근거)"
            )
            continue
        changes.append((row, None))
        quote = str(payload.get("quote_deadline") or "")
        note = f"인용에 날짜 없음({quote[:24]})" if quote else "목록 날짜가 단일 게시일"
        reasons.append(
            f"  [게시일] {row['source']} id={row['id']} "
            f"period_end={row['period_end']} "
            f"== 게시일, {note} -> NULL"
        )
    return changes, reasons


def find_fake_starts(
    conn: sqlite3.Connection, skip_ids: Optional[Set[int]] = None
) -> Tuple[List[sqlite3.Row], List[str]]:
    """coop 의 게시일=접수 시작일 행을 찾는다.

    coop 목록은 게시일만 주는데 그것이 ``period_start`` 에 들어가 있었다
    (판정 4에 따라 크롤러는 이제 비워 둔다). 마감이 없고 인용 근거도 없으며
    원문이 기간을 말하지 않은 행이 그 흔적이다.
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
            continue
        payload = load_raw(row["raw_data"])
        if payload.get("quote_period_start") or states_reception_period(payload):
            continue
        doomed.append(row)
        reasons.append(
            f"  [게시일지문] id={row['id']} period_start={row['period_start']} "
            f"(마감 없음 + 인용 없음 + 원문 기간 표기 없음)"
        )
    return doomed, reasons


def counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """정리 전/후 비교용 집계 (소스별)."""
    def one(sql: str, params: tuple = ()) -> int:
        return conn.execute(sql, params).fetchone()[0]

    values: Dict[str, int] = {}
    for source in CLEANUP_SOURCES:
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
        print(f"  {key:<34} {value}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="수리 이전 중복·가짜 마감 행 정리 (기본 dry-run)"
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB,
        help=f"announcements.db 경로 (기본: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="무엇을 지울지만 보여준다 (유일한 동작)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help=argparse.SUPPRESS,   # 비활성 - 아래에서 거부한다
    )
    args = parser.parse_args()

    # **쓰기는 비활성이다** (2026-09-13 판정). 여덟 차례 게이트에서 이 판정
    # 논리가 정상 공고를 삭제 대상으로 잡는 결함을 반복해 냈다. 미리보기는
    # 사람이 읽을 자료로 남기고, DB 잔존 행은 composer 병합에서 다룬다.
    if args.apply:
        print(
            "--apply 는 비활성입니다: experimental: 판정 티어 승인 전 비활성\n"
            "  이 스크립트는 미리보기(dry-run)만 수행합니다. DB 잔존 행은\n"
            "  composer 병합에서 다룹니다.",
            file=sys.stderr,
        )
        return 2

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB를 찾을 수 없습니다: {db_path}", file=sys.stderr)
        return 1

    print("=== cleanup_seis_duplicates (DRY-RUN, 쓰기 비활성) ===")
    print(f"DB: {db_path}")
    print(f"규칙 B(제목·지역·마감키): {RULE_B_SOURCE}")
    print(f"규칙 C(글번호 오매칭·URL 미해소): {', '.join(RULE_C_SOURCES)}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    before = counts(conn)
    print_counts("before", before)

    # **순서가 중요하다** (7차 게이트 #2): 마감 교정 -> 키 재계산 -> 규칙 A/B/C.
    # 교정될 가짜 마감을 확정 마감처럼 쓰면 회차 키가 비워져 별도 회차가
    # 삭제 대상이 된다.
    deadline_changes, deadline_reasons = find_fake_deadlines(conn)
    corrected: Dict[int, Optional[str]] = {
        row["id"]: new_end for row, new_end in deadline_changes
    }
    print(
        f"\n[a] 마감 교정 (협의회 {len(DEADLINE_FIX_SOURCES)}소스, 키 계산 전): "
        f"{len(deadline_changes)} 행"
    )
    for line in deadline_reasons:
        print(line)

    duplicates: Dict[str, List[sqlite3.Row]] = {}
    print("\n[b] 중복 삭제 대상 (교정된 마감으로 키 계산)")
    try:
        for source in CLEANUP_SOURCES:
            rows, reasons, canonical = find_duplicates(conn, source, corrected)
            duplicates[source] = rows
            locked = f", canonical {len(canonical)}건 잠금" if canonical else ""
            print(f"  {source}: {len(rows)} 행{locked}")
            for line in reasons:
                print(f"  {line}")
    except RuntimeError as exc:
        print(f"\n불변식 위반 - 아무것도 변경하지 않고 중단합니다:\n  {exc}", file=sys.stderr)
        conn.close()
        return 2
    total_duplicates = sum(len(rows) for rows in duplicates.values())
    print(f"  합계: {total_duplicates} 행")

    doomed_ids = {row["id"] for rows in duplicates.values() for row in rows}

    # 중복으로 삭제되는 행의 교정은 무의미하므로 제외한다
    deadline_changes = [
        (row, new_end) for row, new_end in deadline_changes
        if row["id"] not in doomed_ids
    ]
    replaced = [c for c in deadline_changes if c[1]]
    nulled = [c for c in deadline_changes if not c[1]]
    print(
        f"\n[c] 실제 적용할 마감 교정: 교체 {len(replaced)} 행 / "
        f"NULL {len(nulled)} 행 (중복 삭제분 제외)"
    )

    fake_starts, start_reasons = find_fake_starts(conn, doomed_ids)
    print(f"\n[c+] coop period_start -> NULL: {len(fake_starts)} 행")
    for line in start_reasons:
        print(line)

    projected = dict(before)
    for source, rows in duplicates.items():
        projected[f"{source}_rows"] -= len(rows)
    deleted_se_with_deadline = sum(
        1 for row in duplicates.get("socialenterprise", []) if row["period_end"]
    )
    projected["se_rows_with_deadline"] -= len(nulled) + deleted_se_with_deadline
    projected["coop_rows_with_start"] -= len(fake_starts)
    projected["total_rows"] -= total_duplicates
    print_counts("after (예상)", projected)
    print(
        "\n이 스크립트는 미리보기만 합니다 - DB를 바꾸지 않습니다.\n"
        "  잔존 행 처리는 composer 병합 판정에 맡깁니다."
    )

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
