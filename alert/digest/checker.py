"""다이제스트 항목 검증: URL 생존성 및 마감일 파싱."""

import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, Optional
import requests


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


def check_url_alive(url: str, timeout: int = 8) -> bool:
    """URL이 접근 가능한지 확인.

    Args:
        url: 확인할 URL
        timeout: 타임아웃 (초)

    Returns:
        URL이 접근 가능하면 True
    """
    if not url:
        return False

    try:
        # HEAD 요청 시도
        try:
            response = requests.head(url, timeout=timeout, allow_redirects=True)
            if response.status_code < 400:
                return True
        except requests.exceptions.RequestException:
            # HEAD 실패하면 GET 시도
            pass

        # GET 요청 시도
        response = requests.get(url, timeout=timeout, allow_redirects=True)
        return response.status_code < 400
    except requests.exceptions.RequestException:
        return False
    except Exception:
        return False


def check_digest(
    db_path: str,
    markdown_path: Path,
    output_path: Optional[Path] = None,
    skip_network: bool = False,
) -> Dict:
    """다이제스트 마크다운의 항목들을 검증.

    마크다운에서 URL을 추출하고, 각 URL의 생존성과 기간_종료 파싱 가능 여부를 확인.

    Args:
        db_path: announcements.db 경로
        markdown_path: 생성된 다이제스트 마크다운 경로
        output_path: 검증 결과 JSON 경로. None이면 반환값만 사용
        skip_network: 네트워크 호출 건너뛰기 (테스트용)

    Returns:
        {"items": [...], "pass": bool, "network_checked": bool, "reason": str} 형태의 검증 결과
    """
    markdown_path = Path(markdown_path)
    if not markdown_path.exists():
        result = {
            "items": [],
            "pass": False,
            "network_checked": False,
            "reason": "마크다운 파일 없음"
        }
        _write_check_result(output_path, result)
        return result

    # 마크다운에서 URL 추출
    with open(markdown_path, "r", encoding="utf-8") as f:
        markdown_text = f.read()

    # [텍스트](URL) 형식에서 URL 추출
    url_pattern = r'\[([^\]]+)\]\(([^)]+)\)'
    url_matches = re.findall(url_pattern, markdown_text)

    # 항목 0건은 fail-closed
    if not url_matches:
        result = {
            "items": [],
            "pass": False,
            "network_checked": not skip_network,
            "reason": "항목 없음"
        }
        _write_check_result(output_path, result)
        return result

    # DB에서 항목 정보 로드
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    url_to_period_end = {}
    for _, url in url_matches:
        cursor.execute(
            "SELECT period_end FROM announcements WHERE url = ? LIMIT 1",
            (url,)
        )
        row = cursor.fetchone()
        if row:
            url_to_period_end[url] = row[0]

    conn.close()

    # 각 URL 검증
    items = []
    all_passed = True

    for _, url in url_matches:
        period_end = url_to_period_end.get(url)
        url_alive = check_url_alive(url) if not skip_network else True
        deadline_parsed = parse_period_end(period_end)

        item_passed = url_alive and deadline_parsed
        all_passed = all_passed and item_passed

        items.append({
            "url": url,
            "url_alive": url_alive,
            "deadline_parsed": deadline_parsed,
            "period_end": period_end,
            "passed": item_passed,
        })

    reason = "" if all_passed else "검증 실패"
    result = {
        "items": items,
        "pass": all_passed,
        "network_checked": not skip_network,
        "reason": reason
    }

    # 파일 저장
    _write_check_result(output_path, result)

    return result


def _write_check_result(output_path: Optional[Path], result: Dict) -> None:
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
