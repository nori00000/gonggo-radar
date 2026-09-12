"""다이제스트 항목 검증: URL 생존성 및 마감일 파싱."""

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional
import requests


def markdown_sha256(markdown_text: str) -> str:
    """Return the SHA-256 digest that binds a check result to its source text."""
    return hashlib.sha256(markdown_text.encode("utf-8")).hexdigest()


def parse_period_end(period_end_str: Optional[str]) -> bool:
    """기간_종료 날짜 파싱 가능 여부 확인.

    Args:
        period_end_str: 기간_종료 문자열

    Returns:
        파싱 가능 여부
    """
    if not period_end_str or not isinstance(period_end_str, str):
        return False

    # 일반적인 날짜 형식 패턴들
    patterns = [
        r'^\d{4}-\d{2}-\d{2}',  # YYYY-MM-DD
        r'^\d{2}/\d{2}/\d{4}',  # MM/DD/YYYY
        r'^\d{4}/\d{2}/\d{2}',  # YYYY/MM/DD
        r'^\d{4}년\s*\d{1,2}월\s*\d{1,2}일',  # YYYY년 M월 D일
    ]

    for pattern in patterns:
        if re.match(pattern, period_end_str.strip()):
            return True

    return False


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
        "reason": str, "markdown_sha256": str} 형태의 검증 결과.

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
            "reason": "마크다운 파일 없음"
        }
        result = _apply_warnings(result, warnings)
        write_check_result(output_path, result)
        return result

    # 마크다운에서 URL 추출
    with open(markdown_path, "r", encoding="utf-8") as f:
        markdown_text = f.read()
    content_hash = markdown_sha256(markdown_text)

    # [텍스트](URL) 형식에서 URL 추출
    url_pattern = r'\[([^\]]+)\]\(([^)]+)\)'
    url_matches = re.findall(url_pattern, markdown_text)

    # 항목 0건은 fail-closed (검사한 URL이 0건이므로 network_checked=False)
    if not url_matches:
        result = {
            "items": [],
            "dropped": [],
            "pass": False,
            "network_checked": False,
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
    for _, url in url_matches:
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

    for _, url in url_matches:
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

    if not network_checked:
        reason = "네트워크 미검사"
    elif alive_count == 0:
        reason = "생존 항목 없음"
    else:
        reason = ""

    result = {
        "items": items,
        "dropped": dropped,
        "pass": network_checked and alive_count > 0,
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
