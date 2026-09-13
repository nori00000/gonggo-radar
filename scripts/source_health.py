#!/usr/bin/env python3
"""소스 상태 점검 (P0-B).

32개 크롤러 소스 각각에 대해 **읽기 전용**으로 다음을 모은다:

1. ``run_history`` 최근 N런(기본 7)의 ``total_fetched`` / ``new_count``
2. ``announcements`` 의 소스별 적재 건수, 최신 게시일(period_start 정본,
   없으면 created_at — ``digest/composer`` 의 ``posted`` 규칙과 같다),
   최신 수집 시각
3. 마지막 성공 런 시각 (``run_history.status='success'``)
4. **라이브 프로브** — 소스당 GET **1회**(기본 10초)로 상태코드·바이트를 재고,
   받은 본문을 그 크롤러의 목록 파서에 **건식(dry)** 으로 넣어 몇 건이 잡히는지 센다.

금지 사항(이 스크립트가 지키는 것):

* DB 는 ``mode=ro`` URI 로만 연다 — 어떤 경로로도 쓰지 않는다.
* ``fetch()`` 를 호출하지 않는다. 상세 페이지·페이징·POST 를 타지 않는다.
* 파싱 결과를 저장하지 않는다. 발송·알림을 호출하지 않는다.

판정 5분류 (위에서부터 먼저 걸리는 것이 이긴다):

* ``접속 실패``      — 요청이 예외로 끝났거나 상태코드가 400 이상
* ``파싱 실패``      — 200 인데 목록 파서가 0건 (파서가 있는 소스에 한한다)
* ``오래된 목록``    — 파싱은 되는데 DB 최신 게시일이 ``STALE_DAYS`` 일 이상 지남
* ``정상-무공고``    — 목록은 잡히는데 DB 적재가 0건 (임계값·중복으로 저장까지 못 감)
* ``정상``           — 그 외
"""
from __future__ import annotations

import argparse
import inspect
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alert.main import _import_crawlers  # noqa: E402

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - 운영 환경엔 설치돼 있다
    BeautifulSoup = None  # type: ignore

DEFAULT_DB = "alert/data/announcements.db"
PROBE_TIMEOUT_SEC = 10.0
RECENT_RUNS = 7
STALE_DAYS = 90

VERDICT_OK = "정상"
VERDICT_EMPTY = "정상-무공고"
VERDICT_UNREACHABLE = "접속 실패"
VERDICT_PARSE_FAIL = "파싱 실패"
VERDICT_STALE = "오래된 목록"
# 판정이 아니다 — ``--no-probe`` 로 **재지 않았다**는 표시.
# 프로브를 돌리면 절대 나오지 않는다 (재지 않은 것을 "접속 실패"로 적으면 거짓말이 된다).
VERDICT_SKIPPED = "미점검"

# GET 1회로는 **1차 수집 전략을 재현할 수 없는** 소스 (크롤러 코드 실측).
# 판정은 바꾸지 않는다 — 비고에 한 줄로 붙여, 프로브의 한계를 판정으로 오독하지
# 않게 한다.
PROBE_CAVEATS: Dict[str, str] = {
    "gafi": "1차 전략이 POST AJAX(`_fetch_ajax_board`) — GET 프로브로는 재현 불가",
    "socialenterprise": (
        "1차 전략이 POST AJAX(`/homepage/bbs/ajax/boardList.do`) — "
        "GET 프로브로는 재현 불가"
    ),
    "smes": "1차 시도가 POST(`_fetch_board_listing`) — GET 프로브로는 재현 불가",
    "kosmes": "Playwright 렌더 후 파싱 — 정적 GET 본문에는 목록이 없다",
    "apfs": "Playwright 렌더 후 파싱 — 정적 GET 본문에는 목록이 없다",
}

# 본문 문자열을 그대로 받는 파서 (soup 를 만들지 않는다)
TEXT_PARSERS: Tuple[str, ...] = (
    "_parse_rss_xml",      # mafra (RSS)
    "_parse_notice_html",  # kosmes
    "_parse_board_html",   # apfs
)

# BeautifulSoup 을 받는 목록 파서. **크롤러 자신의 폴백 순서**대로 둔다
# (소스 전용 파서 → table → list/card/div → 범용 링크).
SOUP_PARSERS: Tuple[str, ...] = (
    "parse_list",
    "_parse_goview_board",
    "_parse_press_list",
    "_parse_notice_list",
    "_parse_main_cards",
    "_parse_table_board",
    "_parse_list_board",
    "_parse_card_board",
    "_parse_div_board",
    "_parse_responsive_board",
    "_parse_generic_board",
    "_parse_generic_links",
)


@dataclass
class Probe:
    """라이브 GET 1회의 결과."""

    url: str
    status: Optional[int] = None
    size: Optional[int] = None
    parsed: Optional[int] = None
    parser: str = ""
    error: str = ""
    skipped: bool = False


@dataclass
class SourceHealth:
    source: str
    probe: Probe
    runs: List[Tuple[str, int, int, str]] = field(default_factory=list)
    fetched_recent: int = 0
    new_recent: int = 0
    last_success: str = ""
    db_count: int = 0
    latest_posted: str = ""
    latest_created: str = ""
    verdict: str = VERDICT_OK
    note: str = ""


# ---------------------------------------------------------------------------
# 프로브 대상 URL
# ---------------------------------------------------------------------------

def probe_url_for(crawler) -> str:
    """이 크롤러가 **실제로 처음 GET 하는** 목록 URL 을 고른다.

    ``config.yaml`` 의 ``base_url`` 은 30/32 소스에서 호스트 루트일 뿐이라
    그것만 때리면 전 소스가 일률적으로 "파싱 실패" 로 나온다. 크롤러가 선언한
    첫 게시판 경로를 base_url 에 붙여 쓴다 — 요청 횟수는 여전히 소스당 1회다.
    """
    base = (crawler.get_base_url() or getattr(crawler, "BASE_URL", "")).rstrip("/")

    template = getattr(crawler, "LIST_PAGE_URL", "")
    boards = getattr(crawler, "BOARDS", None)
    if template and boards:
        first = boards[0]
        return template.format(base=base, boardId=first[0], menuId=first[1])

    for attr in ("BOARD_PATHS", "BOARD_URLS"):
        paths = getattr(crawler, attr, None)
        if paths:
            return _join(base, paths[0])

    if boards and isinstance(boards[0], (tuple, list)):
        first_path = str(boards[0][0])
        if first_path.startswith("/"):
            return _join(base, first_path)

    for attr in ("LIST_PATH", "LIST_URL", "NOTICE_PAGE_URL"):
        path = getattr(crawler, attr, "")
        if path:
            return _join(base, path)

    return base


def _join(base: str, path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    return f"{base}/{path.lstrip('/')}"


# ---------------------------------------------------------------------------
# 건식 파싱
# ---------------------------------------------------------------------------

def dry_parse(crawler, text: str) -> Tuple[Optional[int], str]:
    """받은 본문을 크롤러의 목록 파서에 넣어 본다 (저장 없음).

    Returns:
        (건수, 파서명). 이 크롤러에 목록 파서가 하나도 없으면 ``(None, "")``.
    """
    found_any = False

    for name in TEXT_PARSERS:
        method = getattr(crawler, name, None)
        if method is None:
            continue
        found_any = True
        count = _call_parser(method, text)
        if count:
            return count, name

    soup = None
    if BeautifulSoup is not None:
        try:
            soup = BeautifulSoup(text, "html.parser")
        except Exception:  # pragma: no cover - 파서 자체가 죽는 경우
            soup = None

    for name in SOUP_PARSERS:
        method = getattr(crawler, name, None)
        if method is None:
            continue
        found_any = True
        if soup is None:
            continue
        count = _call_parser(method, soup)
        if count:
            return count, name

    return (0, "") if found_any else (None, "")


def _call_parser(method: Callable, payload) -> int:
    """파서 1개를 건식 호출한다. 남는 필수 인자는 빈 문자열로 채운다."""
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return 0
    extra = [
        ""
        for name, param in list(signature.parameters.items())[1:]
        if param.default is inspect.Parameter.empty
        and param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD)
    ]
    try:
        result = method(payload, *extra)
    except Exception:
        return 0
    try:
        return len(result)
    except TypeError:
        return 0


# ---------------------------------------------------------------------------
# 라이브 프로브 (유일한 네트워크 지점)
# ---------------------------------------------------------------------------

def probe_source(crawler, timeout: float = PROBE_TIMEOUT_SEC) -> Probe:
    """소스당 GET **1회**. 재시도 없음, 저장 없음."""
    url = probe_url_for(crawler)
    probe = Probe(url=url)
    if not url:
        probe.error = "base_url 미설정"
        return probe
    try:
        response = crawler.session.get(url, timeout=timeout, allow_redirects=True)
    except Exception as exc:
        probe.error = f"{type(exc).__name__}: {exc}"
        return probe

    probe.status = response.status_code
    probe.size = len(response.content or b"")
    if response.status_code >= 400:
        return probe

    response.encoding = response.apparent_encoding or response.encoding or "utf-8"
    probe.parsed, probe.parser = dry_parse(crawler, response.text)
    return probe


# ---------------------------------------------------------------------------
# DB (읽기 전용)
# ---------------------------------------------------------------------------

def _connect_ro(db_path: str) -> sqlite3.Connection:
    """``mode=ro`` URI — 이 연결로는 어떤 쓰기도 할 수 없다."""
    resolved = Path(db_path).resolve()
    return sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)


def collect_db_stats(db_path: str, recent_runs: int = RECENT_RUNS) -> Dict[str, Dict]:
    """소스별 run_history·announcements 요약을 읽는다."""
    stats: Dict[str, Dict] = {}
    conn = _connect_ro(db_path)
    try:
        cursor = conn.execute(
            "SELECT source, started_at, total_fetched, new_count, status "
            "FROM run_history ORDER BY started_at DESC"
        )
        for source, started_at, fetched, new_count, status in cursor.fetchall():
            entry = stats.setdefault(source, _blank_stat())
            if len(entry["runs"]) < recent_runs:
                entry["runs"].append(
                    (started_at, int(fetched or 0), int(new_count or 0), status or "")
                )
            if status == "success" and not entry["last_success"]:
                entry["last_success"] = started_at

        cursor = conn.execute(
            "SELECT source, COUNT(*), "
            "       MAX(COALESCE(NULLIF(period_start, ''), created_at)), "
            "       MAX(created_at) "
            "FROM announcements GROUP BY source"
        )
        for source, count, latest_posted, latest_created in cursor.fetchall():
            entry = stats.setdefault(source, _blank_stat())
            entry["db_count"] = int(count or 0)
            entry["latest_posted"] = latest_posted or ""
            entry["latest_created"] = latest_created or ""
    finally:
        conn.close()

    for entry in stats.values():
        entry["fetched_recent"] = sum(run[1] for run in entry["runs"])
        entry["new_recent"] = sum(run[2] for run in entry["runs"])
    return stats


def _blank_stat() -> Dict:
    return {
        "runs": [],
        "last_success": "",
        "db_count": 0,
        "latest_posted": "",
        "latest_created": "",
        "fetched_recent": 0,
        "new_recent": 0,
    }


# ---------------------------------------------------------------------------
# 판정
# ---------------------------------------------------------------------------

def _as_date(value: str) -> Optional[date]:
    text = (value or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def classify(health: SourceHealth, today: Optional[date] = None,
             stale_days: int = STALE_DAYS) -> Tuple[str, str]:
    """판정 5분류 + 비고 한 줄."""
    today = today or date.today()
    probe = health.probe

    if probe.skipped:
        return VERDICT_SKIPPED, probe.error or "프로브 생략"
    if probe.error:
        return VERDICT_UNREACHABLE, probe.error
    if probe.status is None:
        return VERDICT_UNREACHABLE, "응답 없음"
    if probe.status >= 400:
        return VERDICT_UNREACHABLE, f"HTTP {probe.status}"

    if probe.parsed == 0:
        return VERDICT_PARSE_FAIL, "200이지만 목록 0건"

    posted = _as_date(health.latest_posted)
    if posted is not None and (today - posted).days >= stale_days:
        return VERDICT_STALE, f"최신 게시 {(today - posted).days}일 전"

    if health.db_count == 0:
        note = "목록은 잡히나 DB 적재 0건"
        if probe.parsed is None:
            note = "목록 파서 없음(API 소스) · DB 적재 0건"
        return VERDICT_EMPTY, note

    if probe.parsed is None:
        return VERDICT_OK, "목록 파서 없음(API 소스)"
    return VERDICT_OK, ""


def with_caveat(source: str, note: str) -> str:
    """프로브 한계 주석을 비고에 덧붙인다 (판정은 건드리지 않는다)."""
    caveat = PROBE_CAVEATS.get(source)
    if not caveat:
        return note
    return f"{note} · {caveat}" if note else caveat


# ---------------------------------------------------------------------------
# 조립 + 렌더
# ---------------------------------------------------------------------------

def build_report(
    db_path: str,
    crawlers: Optional[Dict] = None,
    prober: Callable = probe_source,
    sources: Optional[Sequence[str]] = None,
    today: Optional[date] = None,
    recent_runs: int = RECENT_RUNS,
) -> List[SourceHealth]:
    """소스별 SourceHealth 목록. ``prober`` 를 갈아끼우면 네트워크를 타지 않는다."""
    crawler_classes = crawlers if crawlers is not None else _import_crawlers()
    stats = collect_db_stats(db_path, recent_runs=recent_runs)

    rows: List[SourceHealth] = []
    for name in sorted(crawler_classes):
        if sources and name not in sources:
            continue
        try:
            crawler = crawler_classes[name]()
        except Exception as exc:
            rows.append(SourceHealth(
                source=name,
                probe=Probe(url="", error=f"크롤러 생성 실패: {exc}"),
                verdict=VERDICT_UNREACHABLE,
                note=f"크롤러 생성 실패: {exc}",
            ))
            continue

        entry = stats.get(name, _blank_stat())
        health = SourceHealth(
            source=name,
            probe=prober(crawler),
            runs=entry["runs"],
            fetched_recent=entry["fetched_recent"],
            new_recent=entry["new_recent"],
            last_success=entry["last_success"],
            db_count=entry["db_count"],
            latest_posted=entry["latest_posted"],
            latest_created=entry["latest_created"],
        )
        health.verdict, health.note = classify(health, today=today)
        health.note = with_caveat(name, health.note)
        rows.append(health)
    return rows


HEADERS = (
    "source", "판정", "HTTP", "bytes", "파싱", "파서",
    f"최근{RECENT_RUNS}런 fetched", f"최근{RECENT_RUNS}런 신규",
    "마지막 성공", "DB 적재", "DB 최신 게시", "DB 최신 수집",
    "프로브 URL", "비고",
)


def _cell(value) -> str:
    if value is None:
        return "-"
    return str(value).replace("|", "\\|")


def render_markdown(rows: Sequence[SourceHealth],
                    today: Optional[date] = None) -> str:
    today = today or date.today()
    lines = [
        f"# 소스 상태 점검 — {today.isoformat()}",
        "",
        f"소스 {len(rows)}개 · 프로브는 소스당 GET 1회({PROBE_TIMEOUT_SEC:.0f}s) · "
        "DB 는 읽기 전용 · 파싱은 건식(저장 없음)",
        "",
        "| " + " | ".join(HEADERS) + " |",
        "|" + "|".join(["---"] * len(HEADERS)) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_cell(value) for value in (
            row.source,
            row.verdict,
            row.probe.status,
            row.probe.size,
            row.probe.parsed,
            row.probe.parser or "-",
            row.fetched_recent,
            row.new_recent,
            row.last_success or "-",
            row.db_count,
            (row.latest_posted or "-")[:10],
            (row.latest_created or "-")[:10],
            row.probe.url or "-",
            row.note or "-",
        )) + " |")

    counts: Dict[str, int] = {}
    for row in rows:
        counts[row.verdict] = counts.get(row.verdict, 0) + 1
    lines += ["", "## 판정 집계", ""]
    for verdict in (VERDICT_OK, VERDICT_EMPTY, VERDICT_UNREACHABLE,
                    VERDICT_PARSE_FAIL, VERDICT_STALE):
        lines.append(f"- {verdict}: {counts.get(verdict, 0)}")
    if counts.get(VERDICT_SKIPPED):
        lines.append(f"- {VERDICT_SKIPPED}(판정 아님): {counts[VERDICT_SKIPPED]}")
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="소스 상태 점검 (읽기 전용)")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"DB 경로 (기본: {DEFAULT_DB})")
    parser.add_argument("--out", help="마크다운 출력 파일 (기본: 표준출력)")
    parser.add_argument("--timeout", type=float, default=PROBE_TIMEOUT_SEC,
                        help=f"프로브 타임아웃 초 (기본: {PROBE_TIMEOUT_SEC:.0f})")
    parser.add_argument("--source", action="append", dest="sources",
                        help="이 소스만 점검 (여러 번 지정 가능)")
    parser.add_argument("--no-probe", action="store_true",
                        help="네트워크를 타지 않고 DB 지표만 본다")
    args = parser.parse_args(argv)

    if args.no_probe:
        def prober(crawler):
            return Probe(url=probe_url_for(crawler), skipped=True,
                         error="프로브 생략(--no-probe)")
    else:
        def prober(crawler):
            return probe_source(crawler, timeout=args.timeout)

    rows = build_report(args.db, prober=prober, sources=args.sources)
    markdown = render_markdown(rows)
    if args.out:
        Path(args.out).write_text(markdown, encoding="utf-8")
        print(f"✓ {args.out}")
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
