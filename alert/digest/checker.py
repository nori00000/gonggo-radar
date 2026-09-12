"""다이제스트 항목 검증: URL 생존성 및 마감일 파싱."""

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional
import requests

from alert.digest import prune
from alert.digest.composer import parse_deadline


def markdown_sha256(markdown_text: str) -> str:
    """검증한 본문의 지문 (계약 W10 크리틱 #3).

    check.json 에 이 값을 남기면 "과거 검증 재사용"을 발송 게이트가 잡아낼 수 있다 —
    본문이 한 글자라도 바뀌면 해시가 달라지므로 재검증 없이는 발송되지 않는다.
    """
    return hashlib.sha256((markdown_text or "").encode("utf-8")).hexdigest()


def dead_urls(result: Dict) -> List[str]:
    """검증 결과에서 아직 본문에 남아 있는 죽은 URL 목록."""
    return [
        item["url"]
        for item in (result or {}).get("items") or []
        if not item.get("url_alive")
    ]


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

# 항목 링크는 `  [원문](URL)` 한 줄뿐이다 (형식 v2.1).
_ORIGIN_LINK_RE = re.compile(r"^\[원문\]\((\S+)\)$")
# 어떤 형태든 마크다운 링크 (본문 잔존 검사용)
_ANY_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
# HTML 주석 줄 (형식 v2.1의 보류 목록). 발송본에 실리지 않으므로 게이트 대상도 아니다.
_COMMENT_LINE_RE = re.compile(r"^\s*<!--")


def extract_item_urls(markdown_text: str) -> List[str]:
    """검사 대상 URL을 문서 순서대로, 중복 없이 뽑는다.

    개정 v2.5 (#2): **"원문" 링크 구조에서만** 뽑는다. 제목에 주입된 마크다운 링크를
    검사 대상으로 세면, 재조립이 원문 URL만 제외하는 사이 제목 속 죽은 링크가
    발송본에 남는다. 제목 쪽은 composer.sanitize_title이 링크 문법을 제거하고,
    weekly_digest가 "제외된 URL이 본문에 남아 있지 않은지"를 따로 확인한다.

    여기서 빠진 링크(해설·산문·제목에 남은 링크)는 검사 대상에서 사라지는 것이
    아니다 — check_digest가 body_links()로 이어 붙여 함께 확인한다(계약 W10 #2).
    이 함수의 반환값은 "재조립이 제외할 수 있는 항목 좌표"라는 좁은 뜻이다.
    """
    urls: List[str] = []
    seen = set()
    for line in markdown_text.splitlines():
        if _COMMENT_LINE_RE.match(line):
            continue
        matched = _ORIGIN_LINK_RE.match(line.strip())
        if not matched:
            continue
        url = matched.group(1).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def body_links(markdown_text: str) -> List[str]:
    """본문(주석 제외)에 실제로 남아 있는 모든 마크다운 링크 URL."""
    urls: List[str] = []
    for line in markdown_text.splitlines():
        if _COMMENT_LINE_RE.match(line):
            continue
        for _text, url in _ANY_LINK_RE.findall(line):
            urls.append(url.strip())
    return urls


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
        "reason": str} 형태의 검증 결과.

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
            "reason": "마크다운 파일 없음",
            "md_sha256": "",
        }
        result = _apply_warnings(result, warnings)
        write_check_result(output_path, result)
        return result

    # 마크다운에서 URL 추출
    with open(markdown_path, "r", encoding="utf-8") as f:
        markdown_text = f.read()

    # 검사 대상 = 항목의 "원문" 링크(개정 v2.5 #2) + 본문에 남은 나머지 링크
    # (계약 W10 크리틱 #2). 앞쪽은 재조립이 제외할 수 있는 항목 좌표이고, 뒤쪽은
    # 해설·산문에 사람이 써넣은 링크다 — 후자를 안 보면 `/digest 재검토` 경로에서
    # 죽은 링크가 그대로 발송된다(재검토는 재조립을 하지 않는다).
    item_urls = extract_item_urls(markdown_text)
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
            "reason": "항목 없음",
            "md_sha256": markdown_sha256(markdown_text),
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
    item_blocks = prune.item_block_count(markdown_text)
    cap_violations = prune.cap_violations(markdown_text)

    if not network_checked:
        reason = "네트워크 미검사"
    elif alive_count == 0:
        reason = "생존 항목 없음"
    elif len(dropped) > 0:
        reason = f"본문에 죽은 URL {len(dropped)}건 잔존"
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
        "pass": (
            network_checked
            and alive_count > 0
            and len(dropped) == 0
            and item_blocks > 0
            and not cap_violations
        ),
        # 실제로 네트워크 검사한 URL이 0건이면 False
        "network_checked": network_checked,
        "reason": reason,
        "md_sha256": markdown_sha256(markdown_text),
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
