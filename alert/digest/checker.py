"""다이제스트 항목 검증: URL 생존성·정본 대조·마감일 파싱.

THREAT_MODEL (사이클 9에서 확정 — 이 파일의 모든 게이트가 이 전제를 공유한다)
====================================================================
**신뢰 경계 안**: 로컬 파일시스템(`digests/*.md`, `*.items.json`, `*.state.json`,
`*.check.json`). 이 파일들은 소유자와 우리 스크립트만 쓴다. 두 파일을 **동시에**
위조하는 공격자를 막는 것은 이 게이트의 목적이 **아니다** — 그 능력이 있으면
DB 도 고칠 수 있고, 어떤 파일 대조로도 막지 못한다.

**게이트의 목적** (네 가지, 전부 "조용한 실패"를 막는 것):
  ① 살아 있는 공고가 조용히 사라지지 않게 한다 (오병합·자동 삭제 방지)
  ② 죽은/날조 정보가 나가지 않게 한다 (URL 생존·DB 대조·구조 위조 차단)
  ③ 사람이 제외한 항목이 실제로 빠지게 한다 (상태 파일 반영)
  ④ 승인된 본문과 발송 본문이 같게 한다 (바이트 해시·미리보기 지문)

그래서 실패의 기본값은 **멈춤**이다. 특히 md 와 정본(items.json)이 어긋나면
검증은 고쳐주지 않고 `pass=false` 로 멈춘다 — 자동 교정은 ①을 위반한다
(Codex 5차 HIGH #2: 재검토가 불일치 항목을 지워 통과시키면서 살아 있는 공고가
sections·holds 양쪽에서 사라졌다). 해소는 재조립뿐이다.

**GLM 보강 레인의 외부 입력** (V3.1): `scripts/glm_enrich.py` 는 항목 URL 의 상세
페이지를 직접 받아(`alert/utils/http_fetch.fetch_detail_text` — 이 파일의 생존
프로브와 같은 UA·헤더) 앞 2,000자를 `detail_text` 로 GLM 에 넘긴다. 그 텍스트는
**우리가 통제하지 않는 외부 입력**이고, 그것을 읽은 GLM 의 출력도 마찬가지다.
그래서 GLM 이 낸 문장은 세 관문을 다 지난 뒤에만 md 에 들어간다: ①glm_enrich 의
결정론 게이트(`한 줄 의미`는 **추출형 문법** — 근거에서 토큰 경계로 오려 온
«인용»과 접속어 화이트리스트만 허용, 마크다운·URL·제어/형식문자 금지, n 집합
정확 일치 — 하나라도 어긋나면 그 필드는 `원문 확인`이거나 출력 전체 폐기)
②이 파일의 정본 대조·해시 결속(보강 줄도 items.json 의 `enrich_line` 과 문자열
동일해야 한다) ③사람의 미리보기 승인(보강 줄과 `GLM 보강 경고 N건` 이 미리보기에
그대로 보인다). 상세 텍스트 자체는 어떤 경로로도 발송본에 실리지 않는다.

**예외 하나 — `이번 주 한 줄` GLM 초안** (r9 LOW, 명시해 둔다): 이 초안은
`<!-- GLM 초안: … -->` **HTML 주석**으로만 md 에 들어간다. 메일 렌더는 주석을
출력하지 않고 카톡 렌더는 주석 블록을 통째로 제외하므로(composer.
kakao_blocks_from_markdown 의 `kind == "comment"` 분기), 이 텍스트는 어느
채널로도 발송되지 않는다. 사람이 `apply_commentary.py --headline` 으로
**직접 옮겨 적을 때만** 본문이 된다 — 그 순간부터는 사람이 쓴 문장이다.
따라서 초안에는 사실 검증 게이트가 없고, 형식 게이트(주석 탈출 방지)만 있다.
"""

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests

from alert.digest import blocks as blocks_mod
from alert.digest import prune
from alert.digest import sections as sections_mod
from alert.utils import http_fetch
from alert.digest.composer import (
    HEADING_TO_SECTION,
    ITEMS_JSON_SUFFIX,
    ITEMS_SCHEMA_VERSION,
    VERDICT_APPLY,
    classify_item,
    effective_deadline,
    fit_prose_urls,
    load_items_manifest,
    markdown_kakao_problems,
    parse_deadline,
    sanitize_title,
    source_display_name,
    target_display,
)


def markdown_sha256(markdown_bytes: bytes) -> str:
    """검증 결과를 **원시 파일 바이트**에 결속하는 SHA-256 (계약 W10 · PR #1).

    정본은 파일 바이트다 — CRLF 만 바뀐 본문도 해시가 달라져 재검증을 요구한다.
    check.json 의 키는 `markdown_sha256` 하나이고, 텍스트 정규화 해시는 쓰지 않는다.
    state 의 approval.sha(승인 세대 지문)도 이 값이다 — 승인 카드는 세대 id 를 싣는다.
    """
    return hashlib.sha256(markdown_bytes).hexdigest()


def dead_urls(result: Dict) -> List[str]:
    """검증 결과에서 아직 본문에 남아 있는 죽은 URL 목록."""
    return [
        item["url"]
        for item in (result or {}).get("items") or []
        if not item.get("url_alive")
    ]


def manifest_problems(
    markdown_text: str,
    markdown_bytes: bytes,
    item_sections,
    manifest: Optional[Dict],
    db_path: str,
) -> List[str]:
    """md 의 항목 블록을 **항목 정본 파일과 DB** 에 1:1 대조 (사이클 8 #1).

    마커 id 만으로는 출처를 증명하지 못한다 — `<!-- item id=999 -->` 를 손으로 붙이면
    빈 DB에서도 통과했다. 결속 사슬은 이렇다:

        md 블록의 마커 id  →  items.json 의 (id, url, 제목)  →  DB announcements.id 의 url

    어느 고리가 끊겨도 발송하지 않는다. 문제 목록을 돌려주고, 비어 있으면 정합이다.
    """
    if manifest is None:
        return [f"항목 정본 파일 없음 (<주차>{ITEMS_JSON_SUFFIX}) — 재조립 필요"]

    problems: List[str] = []
    # 사이클 13 #1: 렌더 규칙 버전이 다르면 **필드가 다 있어도** 옛 규칙으로 렌더된
    # 본문이다 — 근거가 정본 밖에 있던 시절의 산출물을 통과시키지 않는다.
    if manifest.get("schema_version") != ITEMS_SCHEMA_VERSION:
        problems.append(
            "정본 구버전 — 재조립 필요 (schema_version "
            f"{manifest.get('schema_version')!r} ≠ {ITEMS_SCHEMA_VERSION})"
        )
    problems.extend(_schema_problems(manifest))
    recorded_hash = manifest.get("markdown_sha256")
    if not recorded_hash:
        problems.append("정본에 markdown_sha256 이 없음 — 재조립 필요")
    elif recorded_hash != markdown_sha256(markdown_bytes):
        problems.append("항목 정본 파일이 이 본문의 것이 아님 — 재검토 필요")

    entries = {}
    for entry in manifest.get("items") or []:
        if isinstance(entry, dict) and entry.get("id") is not None:
            key = str(entry["id"])
            if key in entries:
                problems.append(f"정본에 중복된 항목 id={key}")
                continue
            entries[key] = entry

    block_lines = {
        str(block.get("item_id")): block["lines"]
        for block in blocks_mod.parse_blocks(markdown_text, item_sections)
        if block["kind"] == "item"
    }
    blocks = blocks_mod.item_blocks(markdown_text, item_sections)
    if len(blocks) != len(entries):
        problems.append(
            f"항목 수 불일치: 본문 {len(blocks)}건 ≠ 정본 {len(entries)}건"
        )

    seen = set()
    for block in blocks:
        item_id = str(block.get("item_id"))
        entry = entries.get(item_id)
        if entry is None:
            problems.append(f"정본에 없는 항목 id={item_id}")
            continue
        if item_id in seen:
            problems.append(f"중복된 항목 id={item_id}")
            continue
        seen.add(item_id)
        if block["url"] != entry.get("url"):
            problems.append(
                f"id={item_id} URL 불일치: 본문 {block['url']} ≠ 정본 {entry.get('url')}"
            )
        expected_title = entry.get("title")
        if block["fields"]["title"] != expected_title:
            problems.append(
                f"id={item_id} 제목 불일치: 본문 {block['fields']['title']!r} "
                f"≠ 정본 {expected_title!r}"
            )
        # 사이클 9 #4: 정본의 section 이 본문의 실제 섹션과 같아야 한다.
        block_section = HEADING_TO_SECTION.get(
            block["section"], block["section"]
        )
        if entry.get("section") != block_section:
            problems.append(
                f"id={item_id} 섹션 불일치: 본문 {block_section!r} "
                f"≠ 정본 {entry.get('section')!r}"
            )
        # 사이클 10 #2: **항목 줄은 편집 불가 영역**이다. 정본이 담은 렌더 결과와
        # 문자열이 정확히 같아야 한다 — 마감·대상·기관·라벨을 손으로 고치면
        # (`[D-17] … 마감 9/30` → `[D-109] … 마감 12/31`) 여기서 멈춘다.
        # 편집이 허용되는 곳은 이번 주 한 줄·협의회에서·회원사 소식뿐이다.
        lines = block_lines.get(item_id) or []
        expected_line = entry.get("line")
        if expected_line and len(lines) > 1 and lines[1] != expected_line:
            problems.append(
                f"id={item_id} 항목 줄이 정본과 다름 — 재조립 필요"
            )
        expected_origin = entry.get("origin_line")
        if expected_origin and len(lines) > 2 and lines[2] != expected_origin:
            problems.append(
                f"id={item_id} 원문 줄이 정본과 다름 — 재조립 필요"
            )
        # V3(GLM 보강): 보강 줄은 선택 필드다 — 있으면 정본과 문자열이 정확히
        # 같아야 한다. glm_enrich.py 가 md 를 쓴 직후 정본도 함께 갱신하므로
        # (`composer.set_manifest_enrich_lines`), 어긋남은 "누가 md 만 손으로
        # 고쳤다"는 뜻이다 — 재조립이 아니라 GLM 보강 재적용으로 고친다.
        expected_enrich = entry.get("enrich_line") or ""
        actual_enrich = block.get("enrich_line") or ""
        if expected_enrich != actual_enrich:
            problems.append(
                f"id={item_id} 보강 줄이 정본과 다름 — GLM 보강 재적용 필요"
            )

    missing = sorted(set(entries) - seen)
    if missing:
        problems.append("본문에 없는 정본 항목 id=" + ", ".join(missing))

    # 병합 3회차: **번호 좌표의 근거는 정본 순서**다 (봇의 `제외 2,5` 가 가리키는
    # 그 번호). 순서가 본문과 다르면 사람이 승인한 번호와 실제 항목이 어긋나므로
    # 고치지 않고 멈춘다 — 해소는 재조립이다.
    manifest_order = [
        str(entry.get("id")) for entry in (manifest.get("items") or [])
        if isinstance(entry, dict) and entry.get("id") is not None
    ]
    body_order = [str(block.get("item_id")) for block in blocks]
    if (sorted(manifest_order) == sorted(body_order)
            and manifest_order != body_order):
        problems.append("정본 항목 순서가 본문과 다름 — 재조립 필요")

    problems.extend(_db_problems(entries, db_path))
    return problems


# 정본 항목의 필수 필드 (사이클 11 #2).
# `line`·`origin_line`·`period_start`·`period_end`·`deadline_label` 이 없으면
# **구버전 정본**이다 — 그 정본으로는 항목 줄 편집·DB 기간 변경을 잡을 수 없으므로
# 재조립을 요구한다(예전에는 없는 필드를 조용히 건너뛰어 통과했다).
REQUIRED_ENTRY_FIELDS = (
    "id", "url", "title", "section",
    "line", "origin_line", "period_start", "period_end", "deadline_label",
    "org", "target", "region",
)
LEGACY_MANIFEST_FIELDS = (
    "line", "origin_line", "period_start", "period_end", "deadline_label",
    "org", "target", "region",
)
# 값이 비어 있어도 되는 필드 — **키의 존재**만 요구한다.
# `deadline_label` 은 알아두세요 항목에서 빈 문자열이 정상이고(라벨을 렌더하지
# 않는다), DB 기간은 NULL 인 소스가 실제로 있다(seis·coop: period_start NULL).
OPTIONAL_VALUE_FIELDS = (
    "period_start", "period_end", "deadline_label", "target", "region",
)


def _schema_problems(manifest: Dict) -> List[str]:
    """항목 정본 파일의 필수 필드·형식 검증 (사이클 9 #4).

    해시가 없는 옛 정본, 제목 필드를 뺀 정본, 같은 id 를 두 번 넣은 정본이
    각각 통과했다 — 스키마가 느슨하면 대조가 조용히 건너뛰어진다.
    """
    problems: List[str] = []
    if not str(manifest.get("week") or "").strip():
        problems.append("정본에 week 이 없음")
    items = manifest.get("items")
    if not isinstance(items, list):
        return problems + ["정본의 items 가 목록이 아님"]
    for index, entry in enumerate(items):
        if not isinstance(entry, dict):
            problems.append(f"정본 items[{index}] 가 객체가 아님")
            continue
        missing = [
            field for field in REQUIRED_ENTRY_FIELDS
            if field not in entry
            or (entry.get(field) in (None, "")
                and field not in OPTIONAL_VALUE_FIELDS)
        ]
        if missing:
            if any(field in LEGACY_MANIFEST_FIELDS for field in missing):
                problems.append(
                    f"정본 구버전 — 재조립 필요 (items[{index}] 누락: "
                    f"{', '.join(missing)})"
                )
            else:
                problems.append(
                    f"정본 items[{index}] 필수 필드 누락: {', '.join(missing)}"
                )
    return problems


def today() -> date:
    """오늘 날짜 (테스트가 갈아 끼우는 단 한 곳)."""
    return date.today()


def _now_utc() -> datetime:
    """지금 (UTC). 테스트가 갈아 끼우는 단 한 곳."""
    return datetime.now(timezone.utc)


# 검증 생존 판정의 유효 기간 (통합 2 #1).
CHECK_MAX_AGE_HOURS = 24
CHECK_EXPIRED_REASON = "검증 만료 — 재검토 필요"
DEAD_LINK_REASON = "죽은 링크 — 재검토 필요"
SNAPSHOT_CHANGED_REASON = "생존 검사 뒤 본문이 바뀌었습니다 — 재검토 필요"


def check_expired(check_result: Optional[Dict]) -> bool:
    """검증의 생존 판정이 낡았는가 (checked_at 이 없으면 낡은 것으로 본다).

    fail-closed 다: 시각을 모르는 검증은 "언제 본 것인지 모르는 생존 판정" 이고,
    그것을 믿고 보내면 죽은 링크가 실린 메일이 나간다(위협 모델 ②).
    """
    stamped = (check_result or {}).get("checked_at")
    if not stamped:
        return True
    try:
        when = datetime.fromisoformat(str(stamped))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age = _now_utc() - when
    return age > timedelta(hours=CHECK_MAX_AGE_HOURS)


def liveness_snapshot(markdown_path, timeout: int = 8) -> Dict:
    """**잠금 밖**에서 찍는 URL 생존 스냅샷 (통합 2 #1).

    구조가 요점이다. 네트워크는 느리므로 잠금을 쥔 채 기다리지 않는다. 대신
    ①이 바이트를 봤다는 SHA 와 ②그 바이트의 모든 URL 생존 결과를 함께 담고,
    잠금 안에서 "지금 바이트의 SHA 가 스냅샷과 같은가" 를 확인한다 — 다르면
    스냅샷은 무효다(TOCTOU 를 바이트로 닫는다).

    검사 대상·함수·타임아웃은 checker 와 **같다**(`blocks.body_urls` +
    `check_url_alive`) — 두 판정이 다른 URL 을 보면 "검사한 곳과 다른 곳으로
    보내는" 링크가 다시 생긴다.

    Returns:
        {"sha": str|None, "dead": [url], "checked": int, "error": str|None}
    """
    try:
        markdown_bytes = Path(markdown_path).read_bytes()
        markdown_text = markdown_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"sha": None, "dead": [], "checked": 0, "error": str(exc)}

    dead: List[str] = []
    checked = 0
    for url in blocks_mod.body_urls(markdown_text):
        checked += 1
        if not check_url_alive(url, timeout=timeout):
            dead.append(url)
    return {
        "sha": markdown_sha256(markdown_bytes),
        "dead": dead,
        "checked": checked,
        "error": None,
    }


def liveness_problem(snapshot: Optional[Dict], current_sha: str):
    """스냅샷을 지금 바이트에 대고 판정 (None 이면 문제 없음).

    잠금 안에서 부른다 — 판정 문구는 세 경로가 공유한다.
    """
    if not snapshot or snapshot.get("error"):
        return "생존 검사 실패 — 재검토 필요 ({})".format(
            (snapshot or {}).get("error") or "스냅샷 없음")
    if snapshot.get("sha") != current_sha:
        return SNAPSHOT_CHANGED_REASON
    dead = snapshot.get("dead") or []
    if dead:
        return "{} ({}건, 예: {})".format(DEAD_LINK_REASON, len(dead), dead[0])
    return None


def _expiry_problems(item_id: str, entry: Dict, row) -> List[str]:
    """**오늘 기준** 마감 경과 (통합 사이클 1 #2).

    compose 는 조립 시점에 마감 경과를 제외하지만, 그 뒤 날짜가 지나면 이미 만들어진
    발송본에는 지난 마감이 그대로 남는다 — 9/12 에 조립한 `[D-1] … 마감 9/13` 을
    9/14 에 재검토하면 예전에는 pass 였고 발송도 됐다(Codex 통합 게이트 HIGH #2).
    죽은 정보를 보내지 않는 것은 위협 모델 ②(날조·사망 정보 무발송)의 핵심이다.

    규칙은 compose 와 **같다**: 근거는 DB 기간뿐이고(effective_deadline),
    게시일과 마감일이 같은 행은 마감 없음으로 본다. 판정 대상도 같다 —
    신청하세요 항목만(알아두세요는 마감으로 내리지 않는다).

    D-day 표기를 발송 시점에 다시 계산하지 않는다 — 항목 줄은 편집 불가 영역이고
    (사이클 10 #2), 해소는 언제나 재조립이다.
    """
    if entry.get("section") != VERDICT_APPLY:
        return []
    deadline = effective_deadline(row[2], row[3])
    if deadline is None or deadline >= today():
        return []
    return [
        f"id={item_id} 마감 경과({deadline.isoformat()}) — 재조립 필요"
    ]


def _classification_problems(item_id: str, entry: Dict, row) -> List[str]:
    """대상 태그·지역·기관을 DB 행에서 재계산해 정본과 대조 (사이클 13 #2 · 14 #1).

    정본에 적힌 값을 그대로 믿으면, DB 의 summary 를 고쳐 대상이 바뀌어도 재검토가
    통과한다(Codex 9차 MEDIUM #4). 근거는 언제나 DB 이고 정본은 그 사본일 뿐이다.

    기관 표시명도 같다 (사이클 14 #1): 항목 줄에 실리는 세 값(기관·대상·지역)이
    모두 DB 재계산과 맞아야 한다 — 하나라도 정본만 믿으면 그 값은 검증 밖이다.
    """
    problems: List[str] = []
    expected_org = source_display_name(row[5] or "")
    if (entry.get("org") or "") != expected_org:
        problems.append(
            f"id={item_id} DB 기관 변경: {expected_org!r} "
            f"≠ 정본 {entry.get('org')!r} — 재조립 필요"
        )
    try:
        classification = classify_item(row[1] or "", row[4] or "", row[5] or "")
    except Exception as exc:      # noqa: BLE001 — 분류 실패는 fail-closed
        return [f"id={item_id} 분류 재계산 실패: {exc}"]

    expected_region = classification.region or ""
    if (entry.get("region") or "") != expected_region:
        problems.append(
            f"id={item_id} DB 지역 변경: {expected_region!r} "
            f"≠ 정본 {entry.get('region')!r} — 재조립 필요"
        )
    expected_target = target_display(classification.tags, classification.region)
    if (entry.get("target") or "") != expected_target:
        problems.append(
            f"id={item_id} DB 대상 변경: {expected_target!r} "
            f"≠ 정본 {entry.get('target')!r} — 재조립 필요"
        )
    return problems


def recheck_manifest(
    markdown_path,
    markdown_bytes: bytes,
    markdown_text: str,
    check_result: Optional[Dict],
    db_path: str,
) -> List[str]:
    """정본 재대조 — notify·발송기가 **잠금 안에서** 부르는 단일 판정 (통합 1 #1).

    개수만 비교하면 번호 결속이 닫히지 않는다: items.json 의 순서만 A,B→B,A 로
    바꿔도 개수는 같고, 미리보기의 `1=A` 와 제외 좌표의 `1=B` 가 갈렸다
    (Codex 통합 게이트 HIGH #1). 그래서 checker 가 쓰는 **그 함수**를 그대로
    부른다 — 재구현하면 세 경로의 판정이 또 갈라진다.

    돌려주는 것은 `manifest_problems` 와 같은 문제 목록이다(비면 정합).
    """
    item_sections, _ = sections_mod.resolve(check_result, markdown_text)
    return manifest_problems(
        markdown_text,
        markdown_bytes,
        item_sections,
        load_items_manifest(markdown_path),
        db_path,
    )


def _db_problems(entries: Dict[str, Dict], db_path: str) -> List[str]:
    """정본의 (id, url, 제목) 이 DB 와 맞는지 (사이클 8 #1 + 사이클 9 #3).

    제목까지 대조하는 이유: id·URL 만 보면 md 와 정본의 제목을 **함께** 고쳐 놓은
    본문이 통과했다(Codex 5차 HIGH #1). DB 제목은 우리가 만들지 않은 값이므로
    `sanitize_title(DB 제목) == 정본 제목` 이 제목의 독립 근거가 된다.
    """
    if not entries:
        return []
    problems: List[str] = []
    try:
        conn = sqlite3.connect(db_path)
        try:
            for item_id, entry in entries.items():
                row = conn.execute(
                    "SELECT url, title, period_start, period_end,"
                    " summary, source FROM announcements WHERE id = ? LIMIT 1",
                    (item_id,),
                ).fetchone()
                if row is None:
                    problems.append(f"DB에 없는 항목 id={item_id}")
                    continue
                if row[0] != entry.get("url"):
                    problems.append(
                        f"id={item_id} DB URL 불일치: {row[0]} ≠ {entry.get('url')}"
                    )
                expected = sanitize_title(row[1] or "")
                if expected != entry.get("title"):
                    problems.append(
                        f"id={item_id} DB 제목 불일치: {expected!r} "
                        f"≠ 정본 {entry.get('title')!r}"
                    )
                # 사이클 10 #2 + 11 #2: DB **기간**이 바뀌면 정본이 낡았다 —
                # 렌더된 마감 표기가 더 이상 근거와 맞지 않으므로 재조립을 요구한다.
                # period_start 도 본다: 게시일이 바뀌면 유효 마감(마감 경과 판정)이
                # 달라지는데, period_end 만 보면 그것을 놓친다.
                # 사이클 13 #2: 표시 근거를 **DB 에서 다시 계산**해 대조한다 —
                # summary 만 고쳐 대상 태그를 바꾸던 경로가 여기서 막힌다.
                problems.extend(_classification_problems(item_id, entry, row))
                problems.extend(_expiry_problems(item_id, entry, row))
                for column, field, label in (
                    (2, "period_start", "게시일"),
                    (3, "period_end", "마감"),
                ):
                    if field not in entry:
                        continue
                    if (row[column] or "") != (entry.get(field) or ""):
                        problems.append(
                            f"id={item_id} DB {label} 변경: {row[column]!r} "
                            f"≠ 정본 {entry.get(field)!r} — 재조립 필요"
                        )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        problems.append(f"DB 대조 실패: {exc}")
    return problems


def parse_period_end(period_end_str: Optional[str]) -> bool:
    """기간_종료 날짜 파싱 가능 여부 확인 (계약 v1.2: 정보 필드, 게이트 아님).

    판정 정본은 composer.parse_deadline이다 — 렌더가 D-day를 계산할 수 있는
    형식만 True다. 게이트와 렌더가 서로 다른 날짜 파서를 쓰면 "마감 파싱됨"과
    "D-day 표기됨"이 조용히 갈라진다.

    Args:
        period_end_str: 기간_종료 문자열

    Returns:
        파싱 가능 여부
    """
    return parse_deadline(period_end_str) is not None


# UA·헤더 정본은 alert/utils/http_fetch 다 — 생존 프로브(여기)와 GLM 보강의 상세
# 텍스트 수집(scripts/glm_enrich.py)이 **같은 UA** 로 같은 사이트를 친다.
BROWSER_USER_AGENT = http_fetch.BROWSER_USER_AGENT
PROBE_HEADERS = http_fetch.PROBE_HEADERS
# 생존 확인에는 응답 본문이 필요 없다. 연결만 확인하고 최대 이만큼만 읽는다.
MAX_PROBE_BYTES = 64 * 1024

# V3.1: GLM 보강 레인의 게이트 경고 파일 (`digests/<주차>.glm_warnings.json`).
# 발송을 막지는 않는다 — 미리보기 상단에 건수만 보여 사람이 알고 승인하게 한다.
GLM_WARNINGS_SUFFIX = ".glm_warnings.json"


def glm_warnings_path(markdown_path) -> Path:
    """발송본 마크다운 경로 → GLM 보강 경고 파일 경로."""
    markdown_path = Path(markdown_path)
    name = markdown_path.name
    stem = name[:-3] if name.endswith(".md") else name
    return markdown_path.with_name(stem + GLM_WARNINGS_SUFFIX)


def glm_warning_summary(markdown_path) -> Dict:
    """GLM 보강 경고 요약 — `{count, discarded, items_replaced}`.

    세 상태를 구분한다(Codex r3 LOW): ①항목 필드가 대체됨 ②출력 전체 폐기
    ③초안만 폐기(항목은 멀쩡). 미리보기가 셋을 같은 문구로 말하면 거짓 안내다.
    """
    empty = {"count": 0, "discarded": False, "items_replaced": False,
             "partial_batches": []}
    try:
        loaded = json.loads(
            glm_warnings_path(markdown_path).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(loaded, dict):
        return empty
    warnings = loaded.get("warnings")
    partial = loaded.get("partial_batches")
    return {
        "count": len(warnings) if isinstance(warnings, list) else 0,
        "discarded": bool(loaded.get("discarded")),
        "items_replaced": bool(loaded.get("items_replaced")),
        "partial_batches": partial if isinstance(partial, list) else [],
    }


def glm_warning_count(markdown_path) -> int:
    """GLM 보강 게이트 경고 건수 (표시용, 게이트 아님)."""
    return glm_warning_summary(markdown_path)["count"]

def extract_item_urls(markdown_text: str, item_sections=None) -> List[str]:
    """항목 블록의 원문 URL을 문서 순서대로, 중복 없이 뽑는다.

    사이클 6 #1: 판정은 `alert.digest.blocks` 가 정본이다 — 여기서 따로 세지 않는다.
    반환값의 뜻은 "재조립이 제외할 수 있는 항목 좌표"라는 좁은 것이고, 해설·산문·
    제목에 남은 링크는 check_digest 가 body_links() 로 이어 붙여 함께 검사한다
    (계약 W10 #2).
    """
    urls: List[str] = []
    seen = set()
    for url in blocks_mod.item_urls(markdown_text, item_sections):
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def body_links(markdown_text: str) -> List[str]:
    """본문(주석 제외)의 모든 URL — 마크다운 링크 + 맨몸 URL (사이클 10 #3)."""
    return blocks_mod.body_urls(markdown_text)


def check_url_alive(url: str, timeout: int = 8) -> bool:
    """URL이 접근 가능한지 확인.

    HEAD를 먼저 시도하고, HEAD가 예외·405·403 등으로 실패하면 곧바로 GET으로
    폴백한다(HEAD 실패만으로 dead 판정하지 않는다). GET은 stream으로 열어
    본문을 최대 MAX_PROBE_BYTES만 읽고 닫는다.

    Args:
        url: 확인할 URL
        timeout: 타임아웃 (초)

    Returns:
        URL이 접근 가능하면 True
    """
    if not url:
        return False

    try:
        # 1차: HEAD (성공하면 여기서 끝. 실패는 dead 판정이 아니라 GET 폴백)
        try:
            response = requests.head(
                url, timeout=timeout, allow_redirects=True, headers=PROBE_HEADERS
            )
            if response.status_code < 400:
                return True
        except requests.exceptions.RequestException:
            pass

        # 2차: GET (본문은 최대 64KB만 읽는다)
        response = requests.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers=PROBE_HEADERS,
            stream=True,
        )
        try:
            if response.status_code >= 400:
                return False
            for _ in response.iter_content(chunk_size=MAX_PROBE_BYTES):
                break
            return True
        finally:
            response.close()
    except requests.exceptions.RequestException:
        return False
    except Exception:
        return False


def check_digest(
    db_path: str,
    markdown_path: Path,
    output_path: Optional[Path] = None,
    skip_network: bool = False,
    warnings: Optional[List[str]] = None,
) -> Dict:
    """다이제스트 마크다운의 항목들을 검증.

    마크다운에서 URL을 추출하고, 각 URL의 생존성과 기간_종료 파싱 가능 여부를 확인.

    Args:
        db_path: announcements.db 경로
        markdown_path: 생성된 다이제스트 마크다운 경로
        output_path: 검증 결과 JSON 경로. None이면 반환값만 사용
        skip_network: 네트워크 호출 건너뛰기 (테스트용)
        warnings: 생성 단계 경고(예: 폼 로드 실패). 있으면 pass=False로 강등하고
            reason에 기록한다.

    Returns:
        {"items": [...], "dropped": [...], "pass": bool, "network_checked": bool,
        "reason": str, "item_sections": [...], "commentary_sections": [...],
        "markdown_sha256": str} 형태의 검증 결과.

        계약 v1.2: deadline_parsed는 정보 필드이며 게이트가 아니다. url_alive=False
        항목은 dropped에 기록되고 다이제스트에서 제외된다. pass=False 조건은
        ①제외 후 항목 0건 ②network_checked=False ③폼 로드 실패(warnings)
        ④체크 자체 예외 — 넷뿐이다.
    """
    markdown_path = Path(markdown_path)
    if not markdown_path.exists():
        result = {
            "items": [],
            "dropped": [],
            "pass": False,
            "network_checked": False,
            "item_blocks": 0,
            "item_sections": [],
            "commentary_sections": [],
            "manifest_problems": ["마크다운 파일 없음"],
            "reason": "마크다운 파일 없음",
            "markdown_sha256": "",
        }
        result = _apply_warnings(result, warnings)
        write_check_result(output_path, result)
        return result

    # 본문 텍스트와 결속 해시는 **같은 읽기의 바이트**에서 나와야 한다 (PR #1).
    markdown_bytes = markdown_path.read_bytes()
    markdown_text = markdown_bytes.decode("utf-8")
    content_hash = markdown_sha256(markdown_bytes)

    # 계약 W10 사이클6 #2: 허용되지 않은 제어 문자는 그 자체로 fail-closed —
    # 파서마다(split vs splitlines) 줄 수가 달라져 "같은 본문, 다른 항목 수" 가 된다.
    # 이 검사는 **가장 먼저** 한다: 아래 판정들이 모두 같은 줄 나눔을 전제한다.
    if prune.control_chars(markdown_text):
        result = {
            "items": [],
            "dropped": [],
            "item_blocks": 0,
            "item_sections": [],
            "commentary_sections": [],
            "manifest_problems": [],
            "pass": False,
            "network_checked": False,
            "reason": "제어 문자 포함 ({})".format(
                prune.control_chars_label(markdown_text)),
            "markdown_sha256": content_hash,
        }
        result = _apply_warnings(result, warnings)
        write_check_result(output_path, result)
        return result

    # 사이클3 #7: 섹션 정본을 여기서 확정해 check.json 에 남긴다 — prune·preview·
    # 봇은 이 목록과 **정확 일치**로 판정한다(부분 일치 금지).
    item_sections, commentary_sections = sections_mod.classify(markdown_text)

    # 검사 대상 = 항목의 "원문" 링크(개정 v2.5 #2) + 본문에 남은 나머지 링크
    # (계약 W10 크리틱 #2). 앞쪽은 재조립이 제외할 수 있는 항목 좌표이고, 뒤쪽은
    # 해설·산문에 사람이 써넣은 링크다 — 후자를 안 보면 `/digest 재검토` 경로에서
    # 죽은 링크가 그대로 발송된다(재검토는 재조립을 하지 않는다).
    item_urls = extract_item_urls(markdown_text, item_sections)
    urls = list(item_urls)
    seen = set(item_urls)
    # 사이클 10 #3: 마크다운 링크 + **맨몸 URL** 까지 전부 검사한다 — 해설·회원사
    # 소식에 그냥 붙여넣은 죽은 주소가 카톡·메일에 그대로 실렸다.
    for url in blocks_mod.body_urls(markdown_text):
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    # 항목 0건은 fail-closed (검사한 URL이 0건이므로 network_checked=False)
    if not urls:
        result = {
            "items": [],
            "dropped": [],
            "pass": False,
            "network_checked": False,
            "item_blocks": 0,
            "item_sections": list(item_sections),
            "commentary_sections": list(commentary_sections),
            "reason": "항목 없음",
            "markdown_sha256": content_hash,
        }
        result = _apply_warnings(result, warnings)
        write_check_result(output_path, result)
        return result

    # DB에서 항목 정보 로드
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    url_to_period_end = {}
    url_to_title = {}
    for url in urls:
        cursor.execute(
            "SELECT period_end, title FROM announcements WHERE url = ? LIMIT 1",
            (url,)
        )
        row = cursor.fetchone()
        if row:
            url_to_period_end[url] = row[0]
            url_to_title[url] = row[1]

    conn.close()

    # 각 URL 검증
    items = []
    dropped = []
    network_checked_count = 0

    for url in urls:
        period_end = url_to_period_end.get(url)
        if skip_network:
            url_alive = True
        else:
            url_alive = check_url_alive(url)
            network_checked_count += 1
        # 계약 v1.2: deadline_parsed는 정보 필드(게이트 아님)
        deadline_parsed = parse_period_end(period_end)

        items.append({
            "url": url,
            "url_alive": url_alive,
            "deadline_parsed": deadline_parsed,
            "period_end": period_end,
            "passed": url_alive,
        })

        if not url_alive:
            dropped.append({
                "title": url_to_title.get(url) or url,
                "url": url,
            })

    network_checked = network_checked_count > 0
    alive_count = len(items) - len(dropped)

    # 계약 W10 크리틱 #2: items 는 **본문에 실린 URL 전부**다. 그 안에 죽은 URL이
    # 하나라도 남아 있으면 통과시키지 않는다 — 죽은 링크를 메일로 보내는 것이
    # "살아 있는 항목도 있으니 pass" 보다 나쁘다. 제거는 호출자(재조립·prune)가 한다.
    #
    # 사이클2 #6·#7: 항목 수는 **링크 수가 아니라 항목 블록 수**다(해설의 참고 링크가
    # 항목으로 세어지면 "공고 0건인데 pass" 가 난다). 섹션 상한 초과도 fail 이다.
    item_blocks = blocks_mod.item_block_count(markdown_text, item_sections)
    cap_violations = blocks_mod.cap_violations(markdown_text, item_sections)

    # 사이클 7 (Codex 3차 HIGH #2): 항목 섹션에는 composer 가 만든 항목 블록만
    # 실려야 한다. 마커 없는 줄은 사람이 끼워 넣은 것이고, 그 줄이 항목 모양이면
    # 발송 HTML 에는 링크가 실리면서 항목 수·상한 게이트는 통과해버린다.
    prose_lines = blocks_mod.prose_lines_in_item_sections(
        markdown_text, item_sections
    )

    # 사이클 8 #1: 항목을 **텍스트가 아니라 데이터**에 결속한다.
    manifest = load_items_manifest(markdown_path)
    problems = manifest_problems(
        markdown_text, markdown_bytes, item_sections, manifest, db_path
    )
    allowed_urls = [
        entry.get("url") for entry in (manifest or {}).get("items") or []
        if entry.get("url")
    ]
    # 항목 섹션의 링크는 정본 URL 집합에만 있어야 한다 (초과 1개라도 fail)
    links = blocks_mod.link_audit(
        markdown_text, item_sections,
        allowed_urls if manifest is not None else None,
    )
    link_mismatch = (
        links["stray"] > 0
        or links["total"] != links["items"] + links["commentary"]
    )

    # 사이클 8 #4: 해설의 한도 초과 URL 은 카톡·미리보기에서 안내 문구로 바뀐다.
    # 조용히 바뀌면 사람이 모르므로 경고로 남긴다(pass 는 바꾸지 않는다).
    _, long_prose_urls = fit_prose_urls(markdown_text)
    # 사이클 9 #5: 치환 규칙이 완전한지 **생성 후** 확인한다 — 한도를 넘긴 조각과
    # 분절·유실된 URL 을 둘 다 본다(조각 길이만 보면 "URL 을 잘라 맞춘" 출력이 통과).
    kakao_problems = markdown_kakao_problems(markdown_text)
    glm_summary = glm_warning_summary(markdown_path)

    if not network_checked:
        reason = "네트워크 미검사"
    elif alive_count == 0:
        reason = "생존 항목 없음"
    elif len(dropped) > 0:
        reason = f"본문에 죽은 URL {len(dropped)}건 잔존"
    elif problems:
        reason = "항목 정본 대조 실패(재조립 필요): " + "; ".join(problems[:3])
    elif kakao_problems:
        reason = "카톡 조각 초과: " + "; ".join(kakao_problems[:3])
    elif prose_lines:
        reason = "항목 섹션에 산문 {}건: {}".format(
            len(prose_lines), prose_lines[0][:40]
        )
    elif link_mismatch:
        reason = (
            "링크 수 불일치: 본문 {total}개 ≠ 항목 {items} + 해설 {commentary}"
            .format(**links)
        )
    elif item_blocks == 0:
        reason = "항목 0건"
    elif cap_violations:
        reason = "섹션 상한 초과: " + ", ".join(
            f"{name} {count}>{cap}" for name, count, cap in cap_violations
        )
    else:
        reason = ""

    result = {
        "items": items,
        "dropped": dropped,
        "item_blocks": item_blocks,
        "item_sections": list(item_sections),
        "commentary_sections": list(commentary_sections),
        "prose_in_item_sections": prose_lines,
        "manifest_problems": problems,
        "link_audit": links,
        "long_prose_urls": long_prose_urls,
        "kakao_problems": kakao_problems,
        # V3.1: GLM 보강 게이트가 남긴 경고 건수 — 미리보기 상단에 표시만 한다
        # (발송 차단 아님. 경고 = "GLM 이 낸 문장을 게이트가 버렸다" 이므로 본문은
        # 이미 안전한 쪽으로 대체돼 있다. 사람은 그 사실을 알고 승인해야 한다).
        "glm_warnings": glm_summary["count"],
        "glm_discarded": glm_summary["discarded"],
        "glm_items_replaced": glm_summary["items_replaced"],
        "glm_partial_batches": glm_summary["partial_batches"],
        "pass": (
            network_checked
            and alive_count > 0
            and len(dropped) == 0
            and item_blocks > 0
            and not cap_violations
            and not prose_lines
            and not problems
            and not kakao_problems
            and not link_mismatch
        ),
        # 실제로 네트워크 검사한 URL이 0건이면 False
        "network_checked": network_checked,
        "reason": reason,
        "markdown_sha256": content_hash,
        # 통합 2 #1: 이 검증이 **언제** 네트워크를 봤는가. 생존 판정은 시간이
        # 지나면 낡는다 — 발송기가 이 시각으로 만료를 판정한다.
        "checked_at": _now_utc().isoformat(),
    }
    result = _apply_warnings(result, warnings)

    # 파일 저장
    write_check_result(output_path, result)

    return result


def _apply_warnings(result: Dict, warnings: Optional[List[str]]) -> Dict:
    """생성 단계 경고를 검증 결과에 반영 (pass 강등 + reason 기록).

    Args:
        result: 검증 결과 딕셔너리
        warnings: 경고 문자열 목록

    Returns:
        경고가 반영된 검증 결과
    """
    if not warnings:
        return result

    result["pass"] = False
    reasons = list(warnings)
    if result.get("reason"):
        reasons.append(result["reason"])
    result["reason"] = "; ".join(reasons)
    return result


def write_check_result(output_path: Optional[Path], result: Dict) -> None:
    """검증 결과를 JSON 파일로 저장.

    Args:
        output_path: 출력 파일 경로
        result: 검증 결과 딕셔너리
    """
    if not output_path:
        return

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
