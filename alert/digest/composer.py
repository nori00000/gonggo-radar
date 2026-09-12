"""협의회 주간 정책브리핑 다이제스트 생성기."""

import csv
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Tuple


def get_week_date_range(week_str: str) -> Tuple[str, str]:
    """ISO 주 표기(YYYY-Www)에서 시작/종료 날짜를 반환.

    Args:
        week_str: ISO 주 표기 (예: "2026-W13")

    Returns:
        (시작일, 종료일) 튜플 (YYYY-MM-DD 형식)
    """
    parts = week_str.split("-W")
    year = int(parts[0])
    week = int(parts[1])

    # ISO 8601: 주는 월요일부터 시작
    jan4 = datetime(year, 1, 4)
    week_one_monday = jan4 - timedelta(days=jan4.weekday())
    week_start = week_one_monday + timedelta(weeks=week - 1)
    week_end = week_start + timedelta(days=6)

    return week_start.strftime("%Y-%m-%d"), week_end.strftime("%Y-%m-%d")


def normalize_title(title: str) -> str:
    """제목 정규화: 공백 정규화 (개행·탭 제거).

    Args:
        title: 원본 제목

    Returns:
        정규화된 제목
    """
    return " ".join(title.split())


def categorize_item(source: str, title: str) -> str:
    """소스와 제목으로부터 섹션을 결정.

    Args:
        source: 데이터 소스
        title: 공고 제목

    Returns:
        섹션명 ("산림", "사회연대경제", "지원사업")
    """
    # 산림 정책 소스
    forest_sources = {"forest_service", "fowi", "kofpi"}
    # 사회연대경제 소스
    sse_sources = {"socialenterprise", "seis", "mois_sse", "coop"}

    if source in forest_sources:
        # 공고성 제목 확인 (공고, 모집, 지원사업 등)
        announcement_keywords = ["공고", "모집", "지원", "신청", "공모", "채용"]
        if any(kw in title for kw in announcement_keywords):
            return "지원사업"
        else:
            return "산림"
    elif source in sse_sources:
        return "사회연대경제"
    else:
        return "지원사업"


def load_form_responses(forms_csv_path: Optional[Path] = None) -> Tuple[Dict[str, List[str]], List[str]]:
    """forms/responses.csv에서 회원사 동정과 의견 정보 로드.

    Args:
        forms_csv_path: CSV 파일 경로. None이면 forms/responses.csv 시도

    Returns:
        ({"회원사명": ["동정내용", ...]}, ["의견1", "의견2", ...]) 튜플
    """
    if forms_csv_path is None:
        forms_csv_path = Path("forms/responses.csv")

    responses = {}
    opinions = []

    if not forms_csv_path.exists():
        return responses, opinions

    try:
        with open(forms_csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row:
                    continue

                item_type = row.get("유형", "").strip()
                content = row.get("내용", "").strip()
                company = row.get("회원사", "").strip()

                if not content:
                    continue

                if item_type == "동정" and company:
                    if company not in responses:
                        responses[company] = []
                    responses[company].append(content)
                elif item_type == "의견":
                    opinions.append(content)
    except Exception as e:
        print(f"Warning: 회원사 동정 로드 실패: {e}")

    return responses, opinions


def compose_digest(
    db_path: str,
    week_str: Optional[str] = None,
    limit: int = 5,
    output_path: Optional[Path] = None,
    forms_csv_path: Optional[Path] = None,
) -> str:
    """주간 정책브리핑 다이제스트 마크다운 생성.

    Args:
        db_path: announcements.db 경로
        week_str: ISO 주 표기 (기본: 현재 주, 예: "2026-W13")
        limit: 지원사업 공고 최대 항목 수 (기본: 5)
        output_path: 출력 파일 경로. None이면 반환값만 사용
        forms_csv_path: 폼 CSV 경로

    Returns:
        생성된 마크다운 텍스트
    """
    # 현재 주 결정
    if week_str is None:
        today = datetime.now()
        week_num = today.isocalendar()[1]
        year = today.isocalendar()[0]
        week_str = f"{year}-W{week_num:02d}"

    week_start, week_end = get_week_date_range(week_str)

    # DB 연결
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 주간 창 내의 항목 조회 (양끝 포함)
    cursor.execute(
        """
        SELECT id, source, title, summary, url, author, period_end, relevance_score
        FROM announcements
        WHERE DATE(created_at) >= ? AND DATE(created_at) <= ?
        ORDER BY relevance_score DESC
        LIMIT ?
        """,
        (week_start, week_end, limit * 10)  # 버퍼로 10배 조회
    )

    rows = cursor.fetchall()
    conn.close()

    # 섹션별로 분류
    sections = {
        "산림": [],
        "지원사업": [],
        "사회연대경제": [],
    }

    # 중복 제거를 위해 정규화된 제목 추적
    seen_normalized_titles = set()

    for row in rows:
        item_id, source, title, summary, url, author, period_end, score = row
        category = categorize_item(source, title)

        # 제목 정규화
        normalized_title = normalize_title(title)

        # 중복 확인
        if normalized_title in seen_normalized_titles:
            continue
        seen_normalized_titles.add(normalized_title)

        # 섹션별 항목 수 제한
        section_limit = 5 if category == "지원사업" else 3

        if len(sections[category]) >= section_limit:
            continue

        sections[category].append({
            "id": item_id,
            "source": source,
            "title": normalized_title,
            "summary": summary or "",
            "url": url,
            "author": author or "",
            "period_end": period_end or "",
            "score": score,
        })

    # 회원사 동정 및 의견 로드
    form_responses, opinions = load_form_responses(forms_csv_path)

    # 마크다운 생성
    lines = [
        "<!-- lane: Codex(gpt-5.6) -->",
        "",
        f"# 협의회 주간 정책브리핑 {week_str}",
        "",
        f"**기간:** {week_start} ~ {week_end}",
        "",
    ]

    # 산림 정책 동향
    lines.append("## 산림 정책 동향")
    lines.append("")
    if sections["산림"]:
        for item in sections["산림"]:
            lines.extend(_format_item(item))
            lines.append("")
    else:
        lines.append("*(항목 없음)*")
        lines.append("")

    # 지원사업 공고
    lines.append("## 지원사업 공고")
    lines.append("")
    if sections["지원사업"]:
        for item in sections["지원사업"]:
            lines.extend(_format_item(item))
            lines.append("")
    else:
        lines.append("*(항목 없음)*")
        lines.append("")

    # 사회연대경제 동향
    lines.append("## 사회연대경제 동향")
    lines.append("")
    if sections["사회연대경제"]:
        for item in sections["사회연대경제"]:
            lines.extend(_format_item(item))
            lines.append("")
    else:
        lines.append("*(항목 없음)*")
        lines.append("")

    # 회원사 동정
    lines.append("## 회원사 동정")
    lines.append("")
    if form_responses:
        for company, contents in sorted(form_responses.items()):
            lines.append(f"**{company}**")
            for content in contents:
                lines.append(f"- {content}")
            lines.append("")
    else:
        lines.append("*(항목 없음)*")
        lines.append("")

    # 협의회 의견 (마커만)
    lines.append("## 협의회 의견")
    lines.append("")
    lines.append("<!-- 상민 확정 필요 -->")
    lines.append("")

    markdown = "\n".join(lines)

    # 파일 저장
    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(markdown)

    # 의견 파일 저장 (별도)
    if opinions and output_path:
        opinions_path = output_path.with_name(
            output_path.name.replace(".md", ".opinions.md")
        )
        opinions_lines = [
            "<!-- lane: Codex(gpt-5.6) -->",
            "",
            f"# 협의회 의견 {week_str}",
            "",
        ]
        for opinion in opinions:
            opinions_lines.append(f"- {opinion}")
            opinions_lines.append("")

        with open(opinions_path, "w", encoding="utf-8") as f:
            f.write("\n".join(opinions_lines))

    return markdown


def _format_item(item: Dict) -> List[str]:
    """항목을 마크다운 형식으로 포맷.

    Args:
        item: 공고 항목 딕셔너리

    Returns:
        마크다운 줄 리스트
    """
    lines = []
    lines.append(f"### {item['title']}")
    lines.append("")
    lines.append(f"**기관:** {item['author']}")

    # 마감일 (없으면 명시)
    if item['period_end']:
        lines.append(f"**마감:** {item['period_end']}")
    else:
        lines.append("**마감:** 미정")

    lines.append(f"**원문:** [{item['url']}]({item['url']})")
    lines.append("")

    # 요약 (최대 3줄, 없으면 명시)
    if item['summary']:
        summary = item['summary'].strip()
        # 최대 3줄로 절단
        summary_lines = summary.split("\n")[:3]
        summary = "\n".join(summary_lines)
        lines.append(summary)
    else:
        lines.append("*(요약 없음)*")

    return lines
