"""협의회 주간 정책브리핑 다이제스트 생성기."""

import csv
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Set, Tuple

# 섹션 분류 기준 (categorize_item과 섹션별 SQL 쿼리가 공유하는 단일 정본)
FOREST_SOURCES = ("forest_service", "forest_press", "fowi", "kofpi", "lawmaking")
SSE_SOURCES = ("socialenterprise", "seis", "mois_sse", "coop")
ANNOUNCEMENT_KEYWORDS = ("공고", "모집", "지원", "신청", "공모", "채용")

# 섹션별 상한 (계약 v1.1: 지원사업 5, 산림 3, 사회연대경제 3)
SECTION_LIMITS = {"산림": 3, "지원사업": 5, "사회연대경제": 3}

# 렌더링 순서 = 중복 제거 우선순위
SECTION_ORDER = ("산림", "지원사업", "사회연대경제")


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


def strip_controls(text: str) -> str:
    """허용되지 않은 제어 문자를 제거 (계약 W10 사이클6 #2).

    DB·폼에서 들어온 제목·요약·의견에 `\v`·`\r` 같은 문자가 섞이면 파서마다
    줄 수가 달라져 "같은 본문, 다른 항목 수" 가 된다. 저장 시점에 없앤다.
    (검증 단계에도 fail-closed 게이트가 있다 — 여기는 애초에 만들지 않는 쪽.)
    """
    from alert.digest.prune import CONTROL_CHARS_RE

    return CONTROL_CHARS_RE.sub("", text or "")


def normalize_title(title: str) -> str:
    """제목 정규화: 공백 정규화 (개행·탭 제거).

    Args:
        title: 원본 제목

    Returns:
        정규화된 제목
    """
    return " ".join(strip_controls(title).split())


def categorize_item(source: str, title: str) -> str:
    """소스와 제목으로부터 섹션을 결정.

    Args:
        source: 데이터 소스
        title: 공고 제목

    Returns:
        섹션명 ("산림", "사회연대경제", "지원사업")
    """
    if source in FOREST_SOURCES:
        # 공고성 제목 확인 (공고, 모집, 지원사업 등)
        if any(kw in title for kw in ANNOUNCEMENT_KEYWORDS):
            return "지원사업"
        else:
            return "산림"
    elif source in SSE_SOURCES:
        return "사회연대경제"
    else:
        return "지원사업"


def _section_where(section: str) -> Tuple[str, List]:
    """섹션별 WHERE 절과 바인딩 파라미터를 생성.

    categorize_item과 동일한 기준을 SQL로 표현한다.

    Args:
        section: "산림" | "지원사업" | "사회연대경제"

    Returns:
        (WHERE 절 SQL, 파라미터 리스트)
    """
    forest_ph = ", ".join("?" * len(FOREST_SOURCES))
    sse_ph = ", ".join("?" * len(SSE_SOURCES))
    kw_clause = " OR ".join(["title LIKE ?"] * len(ANNOUNCEMENT_KEYWORDS))
    kw_params = [f"%{kw}%" for kw in ANNOUNCEMENT_KEYWORDS]

    if section == "산림":
        sql = f"source IN ({forest_ph}) AND NOT ({kw_clause})"
        params = list(FOREST_SOURCES) + kw_params
    elif section == "사회연대경제":
        sql = f"source IN ({sse_ph})"
        params = list(SSE_SOURCES)
    elif section == "지원사업":
        sql = (
            f"((source IN ({forest_ph}) AND ({kw_clause}))"
            f" OR source NOT IN ({forest_ph}, {sse_ph}))"
        )
        params = (
            list(FOREST_SOURCES)
            + kw_params
            + list(FOREST_SOURCES)
            + list(SSE_SOURCES)
        )
    else:
        raise ValueError(f"알 수 없는 섹션: {section}")

    return sql, params


def load_form_responses(
    forms_csv_path: Optional[Path] = None,
) -> Tuple[Dict[str, List[str]], List[str], List[str]]:
    """forms/responses.csv에서 회원사 동정과 의견 정보 로드.

    Args:
        forms_csv_path: CSV 파일 경로. None이면 forms/responses.csv 시도

    Returns:
        ({"회원사명": ["동정내용", ...]}, ["의견1", ...], ["경고문", ...]) 튜플.
        세 번째 원소는 로드 실패 경고 목록(stderr에도 출력됨).
    """
    if forms_csv_path is None:
        forms_csv_path = Path("forms/responses.csv")

    responses: Dict[str, List[str]] = {}
    opinions: List[str] = []
    warnings: List[str] = []

    if not forms_csv_path.exists():
        return responses, opinions, warnings

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
        warning = f"폼 로드 실패: {e}"
        warnings.append(warning)
        print(f"Warning: {warning}", file=sys.stderr)

    return responses, opinions, warnings


def compose_digest(
    db_path: str,
    week_str: Optional[str] = None,
    output_path: Optional[Path] = None,
    forms_csv_path: Optional[Path] = None,
    warnings_out: Optional[List[str]] = None,
    exclude_urls: Optional[Set[str]] = None,
) -> str:
    """주간 정책브리핑 다이제스트 마크다운 생성.

    섹션 상한은 계약 v1.1 고정값(SECTION_LIMITS)이며 호출자가 조정할 수 없다.

    Args:
        db_path: announcements.db 경로
        week_str: ISO 주 표기 (기본: 현재 주, 예: "2026-W13")
        output_path: 출력 파일 경로. None이면 반환값만 사용
        forms_csv_path: 폼 CSV 경로
        warnings_out: 경고 수집용 리스트. 주어지면 폼 로드 실패 등이 append됨
        exclude_urls: 제외할 원문 URL 집합 (계약 v1.2: url_alive=false 항목 자동 제외).
            제외는 조회 단계에서 적용되므로 섹션 상한이 남은 후보로 다시 채워진다.

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
    # 중복 제거 기준을 SQL과 파이썬이 공유하도록 정규화 함수를 등록
    conn.create_function("normalize_title", 1, normalize_title)
    cursor = conn.cursor()

    # 계약 v1.2: 제외 URL은 조회 단계에서 뺀다. 그래야 LIMIT(섹션 상한)이
    # 남은 후보로 다시 채워진다 = "제외 후 섹션 상한 재적용".
    exclude_list = sorted(exclude_urls) if exclude_urls else []
    if exclude_list:
        exclude_sql = " AND url NOT IN (%s)" % ", ".join("?" * len(exclude_list))
    else:
        exclude_sql = ""

    # 섹션별로 독립 조회 (주간 창 양끝 포함, 중복 제거 후 섹션 상한만큼만)
    section_rows: Dict[str, List] = {}
    for section in SECTION_ORDER:
        where_sql, where_params = _section_where(section)
        cursor.execute(
            f"""
            SELECT id, source, title, summary, url, author, period_end,
                   MAX(relevance_score) AS relevance_score
            FROM announcements
            WHERE {where_sql}{exclude_sql}
              AND DATE(created_at) BETWEEN ? AND ?
            GROUP BY normalize_title(title)
            ORDER BY relevance_score DESC
            LIMIT ?
            """,
            (
                *where_params,
                *exclude_list,
                week_start,
                week_end,
                SECTION_LIMITS[section],
            ),
        )
        section_rows[section] = cursor.fetchall()

    conn.close()

    # 섹션 조립 후 섹션 간 중복 제거 (렌더 순서 우선, 제목 정규화 기준)
    sections: Dict[str, List[Dict]] = {section: [] for section in SECTION_ORDER}
    seen_normalized_titles = set()

    for section in SECTION_ORDER:
        for row in section_rows[section]:
            item_id, source, title, summary, url, author, period_end, score = row
            normalized_title = normalize_title(title)

            # 조회 단계에서 이미 걸렀지만, 호출자가 SQL을 우회해도 안전하도록 방어한다.
            if exclude_urls and url in exclude_urls:
                continue

            if normalized_title in seen_normalized_titles:
                continue
            seen_normalized_titles.add(normalized_title)

            # 제외 후 섹션 상한 재적용
            if len(sections[section]) >= SECTION_LIMITS[section]:
                continue

            sections[section].append({
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
    form_responses, opinions, form_warnings = load_form_responses(forms_csv_path)
    if warnings_out is not None:
        warnings_out.extend(form_warnings)

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
        for opinion in (strip_controls(text) for text in opinions):
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
    lines.append(f"### {strip_controls(item['title'])}")
    lines.append("")
    lines.append(f"**기관:** {strip_controls(item['author'])}")

    # 마감일 (없으면 명시)
    if item['period_end']:
        lines.append(f"**마감:** {item['period_end']}")
    else:
        lines.append("**마감:** 미정")

    lines.append(f"**원문:** [{item['url']}]({item['url']})")
    lines.append("")

    # 요약 (최대 3줄, 없으면 명시)
    if item['summary']:
        summary = strip_controls(item['summary']).strip()
        # 최대 3줄로 절단
        summary_lines = summary.split("\n")[:3]
        summary = "\n".join(summary_lines)
        lines.append(summary)
    else:
        lines.append("*(요약 없음)*")

    return lines
