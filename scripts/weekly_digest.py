#!/usr/bin/env python3
"""주간 정책브리핑 다이제스트 생성 스크립트."""

import argparse
import sys
from datetime import datetime
from pathlib import Path

# sys.path 보정
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alert.digest.composer import compose_digest
from alert.digest.checker import check_digest


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
        "--limit",
        type=int,
        default=5,
        help="지원사업 공고 최대 항목 수 (기본: 5)",
    )
    parser.add_argument(
        "--forms",
        help="폼 CSV 경로 (기본: forms/responses.csv)",
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

    # 다이제스트 생성
    print(f"Composing digest for {week}...")
    try:
        markdown = compose_digest(
            db_path=args.db,
            week_str=week,
            limit=args.limit,
            output_path=markdown_path,
            forms_csv_path=forms_csv_path,
        )
        print(f"✓ 다이제스트 생성: {markdown_path}")
    except Exception as e:
        print(f"✗ 다이제스트 생성 실패: {e}", file=sys.stderr)
        return 1

    # URL 검증
    print("Checking digest items...")
    try:
        result = check_digest(
            db_path=args.db,
            markdown_path=markdown_path,
            output_path=check_path,
            skip_network=False,
        )
        print(f"✓ 검증 완료: {check_path}")
        if result["pass"]:
            print("  모든 항목 통과")
        else:
            failed_count = sum(1 for item in result["items"] if not item["passed"])
            if failed_count > 0:
                print(f"  ⚠️  {failed_count}/{len(result['items'])} 항목 실패")
            else:
                print(f"  ⚠️  {result.get('reason', '검증 실패')}")
    except Exception as e:
        print(f"✗ 검증 실패: {e}", file=sys.stderr)
        return 1

    # pass=false면 exit 1
    if not result["pass"]:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
