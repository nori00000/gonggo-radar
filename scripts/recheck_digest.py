#!/usr/bin/env python3
"""현재 다이제스트 파일 그대로 팩트 게이트만 재실행 (계약 W10).

weekly_digest.py 와 다르다: **재조립하지 않는다**. 사람이 맥에서 본문을 고치거나
apply_commentary.py 로 협의회 의견을 채운 뒤, 그 파일 기준으로 check.json 만
새로 쓴다(재조립하면 손 수정과 해설이 마커로 되돌아간다).
"""

import argparse
import sys
from pathlib import Path

# sys.path 보정
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alert.digest.checker import check_digest


def main():
    parser = argparse.ArgumentParser(description="다이제스트 팩트 게이트 재검증")
    parser.add_argument("markdown", help="다이제스트 마크다운 경로")
    parser.add_argument(
        "--db",
        default="alert/data/announcements.db",
        help="announcements.db 경로 (기본: alert/data/announcements.db)",
    )
    args = parser.parse_args()

    markdown_path = Path(args.markdown)
    if not markdown_path.exists():
        print(f"✗ 파일 없음: {markdown_path}", file=sys.stderr)
        return 2

    check_path = markdown_path.with_suffix(".check.json")
    try:
        result = check_digest(
            db_path=args.db,
            markdown_path=markdown_path,
            output_path=check_path,
            skip_network=False,
        )
    except Exception as exc:  # noqa: BLE001 — 체크 자체 예외는 fail-closed
        print(f"✗ 검증 실패: {exc}", file=sys.stderr)
        return 1

    print(f"✓ 검증 완료: {check_path}")
    if result["pass"]:
        print(
            "  통과 항목 {}건 / 죽은 URL {}건".format(
                len(result["items"]) - len(result.get("dropped") or []),
                len(result.get("dropped") or []),
            )
        )
        return 0

    print(f"  ⚠️  {result.get('reason', '검증 실패')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
