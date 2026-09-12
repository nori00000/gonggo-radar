#!/usr/bin/env python3
"""주간 정책브리핑 다이제스트 생성 스크립트."""

import argparse
import sys
from datetime import datetime
from pathlib import Path

# sys.path 보정
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alert.digest.composer import compose_digest
from alert.digest.checker import body_links, check_digest, write_check_result
from alert.digest import state as state_mod

# 죽은 URL 제외 → 재조립을 반복하는 최대 횟수 (무한 루프 방지)
MAX_RECOMPOSE_ROUNDS = 3


def main():
    parser = argparse.ArgumentParser(
        description="협의회 주간 정책브리핑 다이제스트 생성"
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
        "--week",
        help='ISO 주 표기 (예: 2026-W13). 기본값: 현재 주',
    )
    parser.add_argument(
        "--forms",
        help="폼 CSV 경로 (기본: forms/responses.csv)",
    )
    parser.add_argument(
        "--exclude-state",
        action="store_true",
        help="상태 파일(YYYY-Www.state.json)의 excluded_urls를 조회 단계에서 제외",
    )

    args = parser.parse_args()

    # 현재 주 결정
    if args.week is None:
        today = datetime.now()
        week_num = today.isocalendar()[1]
        year = today.isocalendar()[0]
        week = f"{year}-W{week_num:02d}"
    else:
        week = args.week

    out_dir = Path(args.out_dir)
    markdown_path = out_dir / f"{week}.md"
    check_path = out_dir / f"{week}.check.json"

    forms_csv_path = Path(args.forms) if args.forms else None

    # 계약 v1.2: compose → check → url_alive=false 제외 → 재조립(섹션 상한 재적용).
    # 재조립이 새 후보를 끌어오므로, 검사되지 않은 URL이 발송물에 실리지 않도록
    # 죽은 URL이 사라질 때까지 (최대 MAX_RECOMPOSE_ROUNDS회) 다시 검사한다.
    warnings: list[str] = []
    dropped: list[dict] = []
    dropped_urls: set[str] = set()
    stats: dict = {}
    result = None

    # 계약 W10: 사람이 텔레그램에서 제외한 항목은 상태 파일이 정본이다.
    # 조회 단계에서 빼므로 섹션 상한이 남은 후보로 다시 채워진다.
    if args.exclude_state:
        state_path = state_mod.state_path(week, out_dir)
        try:
            state = state_mod.load_state(state_path, week)
        except state_mod.StateError as exc:
            print(f"✗ 상태 파일 손상: {exc}", file=sys.stderr)
            return 1
        state_excluded = state_mod.excluded_urls(state)
        if state_excluded:
            dropped_urls.update(state_excluded)
            print(f"제외 상태 반영: {len(state_excluded)}건")

    for attempt in range(1, MAX_RECOMPOSE_ROUNDS + 1):
        label = "다이제스트 생성" if attempt == 1 else "재조립"
        print(f"Composing digest for {week}... (round {attempt})")
        try:
            compose_digest(
                db_path=args.db,
                week_str=week,
                output_path=markdown_path,
                forms_csv_path=forms_csv_path,
                warnings_out=warnings if attempt == 1 else None,
                exclude_urls=dropped_urls or None,
                stats_out=stats,
            )
            print(f"✓ {label}: {markdown_path}")
            kakao_path = markdown_path.with_name(f"{markdown_path.stem}.kakao.txt")
            if kakao_path.exists():
                print(f"✓ 카톡 평문: {kakao_path}")
        except Exception as e:
            print(f"✗ {label} 실패: {e}", file=sys.stderr)
            return 1

        print("Checking digest items...")
        try:
            result = check_digest(
                db_path=args.db,
                markdown_path=markdown_path,
                output_path=None,
                skip_network=False,
                warnings=warnings,
            )
        except Exception as e:
            print(f"✗ 검증 실패: {e}", file=sys.stderr)
            return 1

        round_dropped = [
            item for item in result.get("dropped", [])
            if item["url"] not in dropped_urls
        ]
        if not round_dropped:
            break

        print(f"  ⚠️  죽은 URL {len(round_dropped)}건 제외 후 재조립")
        for item in round_dropped:
            print(f"     - {item['title']} ({item['url']})")
            dropped.append(item)
            dropped_urls.add(item["url"])
    else:
        # 라운드를 소진했는데도 죽은 URL이 남았다 → fail-closed
        result["pass"] = False
        result["reason"] = "; ".join(
            filter(None, ["죽은 URL 반복 검출", result.get("reason", "")])
        )

    # 개정 v2.5 (#2): 제외된 URL이 본문에 남아 있으면 발송을 막는다.
    # 제목에 주입된 링크는 "원문" 링크 구조가 아니어서 검사 루프가 못 보기 때문이다.
    markdown_text = markdown_path.read_text(encoding="utf-8")
    residual = sorted(dropped_urls.intersection(body_links(markdown_text)))
    if residual:
        result["pass"] = False
        result["reason"] = "; ".join(
            filter(
                None,
                [
                    f"제외된 URL이 본문에 남아 있음 {len(residual)}건",
                    result.get("reason", ""),
                ],
            )
        )
        result["residual_urls"] = residual

    # 개정 v2.5 (#8): 조용한 날짜 파싱 실패를 reason에 남긴다 (pass는 바꾸지 않는다)
    failures = stats.get("date_parse_failures") or 0
    if failures:
        result["date_parse_failures"] = failures
        result["reason"] = "; ".join(
            filter(None, [result.get("reason", ""), f"날짜 파싱 실패 {failures}건"])
        )

    # 누적 제외 목록을 기록 (items는 최종 산출물에 실린 항목)
    result["dropped"] = dropped
    write_check_result(check_path, result)
    print(f"✓ 검증 완료: {check_path}")

    if result["pass"]:
        print(f"  통과 항목 {len(result['items'])}건 / 제외 {len(dropped)}건")
        return 0

    print(f"  ⚠️  {result.get('reason', '검증 실패')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
