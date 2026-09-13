#!/usr/bin/env python3
"""협의회 월간 종합호 생성 스크립트 (P1' 계약 §2).

주간호(`weekly_digest.py`)와 **같은 조립·검증 경로**를 탄다 — 다른 것은 호 키
하나뿐이다(`YYYY-Mmm`). 그 키가 digests/ 의 md·lock·state·items.json·
check.json·kakao.txt 이름을 전부 정하므로, 같은 주에 주간호와 월간호가 공존해도
서로의 잠금·승인 세대·미리보기 좌표를 건드리지 않는다.

무엇이 실리는가(결정론, composer.monthly_hold_reason 이 정본):
  · 주간호 섹션 판정이 `신청하세요` 인 항목 → 주간호 몫이라 싣지 않는다
  · 마감이 있는 항목(의견 마감·마감 있는 행사) → 주간호 몫이라 싣지 않는다
  · 나머지(정책 방향·계획·통계·보도자료·마감 없는 행사·2차 미디어) → 월간호
상한 5건, 같은 소스 최대 2건. 빠진 것은 버리지 않고 보류 주석으로 남는다.

`--dry-run` 은 **아무 파일도 쓰지 않는다** — DB 만 읽고 무엇이 뽑히는지 표로
찍는다(잠금·승인 폐기·검증·발송 어느 것도 하지 않는다).
"""

import sys
from datetime import date
from pathlib import Path

# sys.path 보정
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alert.digest import composer as composer_mod
from alert.digest import state as state_mod
from alert.utils.redact import redact
from alert.utils.safe_argparse import (
    RedactingArgumentParser,
    reject_secret_argv,
)
from scripts.weekly_digest import compose_issue

def _err(message) -> None:
    print(redact(message), file=sys.stderr)


def _out(message) -> None:
    print(redact(message))


def previous_month_key(today: date) -> str:
    """그 날짜 기준 **직전 달**의 호 키 (`2026-10-01` → `2026-M09`)."""
    year = today.year
    month = today.month - 1
    if month == 0:
        year, month = year - 1, 12
    return f"{year}-M{month:02d}"


def dry_run_rows(db_path: str, month: str, today=None):
    """조립 없이 "무엇이 뽑히나" 만 계산한다 (파일 쓰기 없음).

    Returns:
        (선정 항목 리스트, 보류 항목 리스트, 통계 dict)
    """
    stats: dict = {}
    data = composer_mod.compose_digest_data(
        db_path=db_path, week_str=month, today=today, stats_out=stats
    )
    selected = list(data["sections"].get(composer_mod.VERDICT_MONTHLY) or [])
    return selected, list(data["holds"]), stats


def format_dry_run(month: str, selected, holds, stats) -> str:
    """드라이런 표 — 사람이 읽는 출력이며 어떤 판정에도 쓰이지 않는다."""
    lines = [
        f"[DRY-RUN] {month} 월간 종합호 (파일을 쓰지 않았습니다)",
        "후보 {}건 · 선정 {}건 · 보류 {}건".format(
            stats.get("candidates", 0), len(selected), len(holds)
        ),
        "",
        "| # | 소스 | 기관 | 제목 | 게시일 | URL |",
        "|---|---|---|---|---|---|",
    ]
    for number, item in enumerate(selected, start=1):
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            number,
            item["source"],
            item["org"],
            composer_mod.sanitize_title(item["title"]).replace("|", "｜"),
            item.get("posted") or "-",
            item["url"],
        ))
    if not selected:
        lines.append("| — | — | — | (선정 0건) | — | — |")
    lines.append("")
    lines.append("보류 사유별:")
    reasons: dict = {}
    for item in holds:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    for reason, count in sorted(reasons.items()):
        lines.append(f"  · {reason}: {count}건")
    if not reasons:
        lines.append("  · (없음)")
    return "\n".join(lines)


def main():
    parser = RedactingArgumentParser(
        description="협의회 월간 종합호 다이제스트 생성"
    )
    parser.add_argument(
        "month",
        nargs="?",
        help="월간호 키 (예: 2026-M09). 기본값: 직전 달",
    )
    parser.add_argument(
        "--db",
        default="alert/data/announcements.db",
        help="announcements.db 경로 (기본: alert/data/announcements.db)",
    )
    parser.add_argument(
        "--out-dir",
        default="digests",
        help="출력 디렉토리 (기본: digests)",
    )
    parser.add_argument(
        "--forms",
        help="폼 CSV 경로 (기본: forms/responses.csv)",
    )
    parser.add_argument(
        "--exclude-state",
        action="store_true",
        help="상태 파일(YYYY-Mmm.state.json)의 excluded_urls를 조회 단계에서 제외",
    )
    parser.add_argument(
        "--pins",
        action="store_true",
        help="상태 파일의 pinned_ids(`핀 n` 승격)를 선정 단계에서 반영",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="파일을 쓰지 않고 어떤 항목이 뽑히는지만 출력",
    )

    if reject_secret_argv(sys.argv[1:], _err):
        return 2
    args = parser.parse_args()

    month = args.month or previous_month_key(date.today())
    if state_mod.issue_kind(month) != state_mod.KIND_MONTHLY:
        _err(f"✗ 월간호 키 형식이 아닙니다: {month!r} (예: 2026-M09)")
        return 2

    if args.dry_run:
        try:
            selected, holds, stats = dry_run_rows(args.db, month)
        except Exception as exc:        # noqa: BLE001 — 드라이런도 조용히 죽지 않는다
            _err(f"✗ 드라이런 조립 실패: {exc}")
            return 1
        _out(format_dry_run(month, selected, holds, stats))
        return 0

    return compose_issue(args, month)


def guarded_main():
    """예외·traceback 까지 redact 해서 내보낸다 (weekly_digest 와 같은 규율)."""
    try:
        return main()
    except SystemExit:
        raise
    except BaseException:       # noqa: BLE001
        import traceback
        _err(traceback.format_exc())
        return 70


if __name__ == "__main__":
    sys.exit(guarded_main())
