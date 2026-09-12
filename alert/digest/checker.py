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
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional
import requests

from alert.digest import blocks as blocks_mod
from alert.digest import sections as sections_mod
from alert.digest.composer import (
    HEADING_TO_SECTION,
    ITEMS_JSON_SUFFIX,
    fit_prose_urls,
    load_items_manifest,
    markdown_kakao_problems,
    parse_deadline,
    sanitize_title,
)


def markdown_sha256(markdown_bytes: bytes) -> str:
    """검증 결과를 **원시 파일 바이트**에 결속하는 SHA-256 (계약 W10 · PR #1).

    정본은 파일 바이트다 — CRLF 만 바뀐 본문도 해시가 달라져 재검증을 요구한다.
    check.json 의 키는 `markdown_sha256` 하나이고, 텍스트 정규화 해시는 쓰지 않는다.
    미리보기 지문(state.preview_sha)·승인 카드 지문·`--approved-sha` 도 모두 이 값이다.
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

    problems: List[str] = _schema_problems(manifest)
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

    missing = sorted(set(entries) - seen)
    if missing:
        problems.append("본문에 없는 정본 항목 id=" + ", ".join(missing))

    problems.extend(_db_problems(entries, db_path))
    return problems


REQUIRED_ENTRY_FIELDS = ("id", "url", "title", "section")


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
            if entry.get(field) in (None, "")
        ]
        if missing:
            problems.append(
                f"정본 items[{index}] 필수 필드 누락: {', '.join(missing)}"
            )
    return problems


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
                    "SELECT url, title FROM announcements WHERE id = ? LIMIT 1",
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


# 공공기관 사이트 다수가 기본 python-requests UA를 차단하거나 HEAD를 지원하지 않는다.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PROBE_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
# 생존 확인에는 응답 본문이 필요 없다. 연결만 확인하고 최대 이만큼만 읽는다.
MAX_PROBE_BYTES = 64 * 1024

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
    """본문(주석 제외)에 남아 있는 모든 링크 URL (균형 괄호·스킴 필수)."""
    return blocks_mod.body_link_urls(markdown_text)


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
    for url in body_links(markdown_text):
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
