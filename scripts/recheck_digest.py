#!/usr/bin/env python3
"""현재 다이제스트 파일 기준으로 팩트 게이트를 재실행 (계약 W10).

weekly_digest.py 와 다르다: **재조립하지 않는다**. 사람이 맥에서 본문을 고치거나
apply_commentary.py 로 협의회 의견을 채운 뒤, 그 파일 기준으로 check.json 만
새로 쓴다(재조립하면 손 수정과 해설이 마커로 되돌아간다).

대신 죽은 URL은 본문에서 직접 걷어낸다(크리틱 #2) — 검증만 갱신하고 본문에 죽은
링크를 남기면 "pass 인데 죽은 링크가 실린 메일"이 나간다. 체크 자체가 실패하면
check.json 을 pass=false 로 덮어쓴다(크리틱 #3) — 과거의 pass 가 재사용되지 않게.
"""

import argparse
import sys
from pathlib import Path

# sys.path 보정
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alert.digest import prune
from alert.digest import sections as sections_mod
from alert.digest.composer import kakao_file_text_from_markdown
from alert.digest.checker import (
    check_digest,
    dead_urls,
    markdown_sha256,
    write_check_result,
)

# 죽은 URL 제거 → 재검증을 반복하는 최대 횟수 (무한 루프 방지)
MAX_PRUNE_ROUNDS = 3


def _merge_dropped(accumulated, from_check):
    """제거 이력 + 마지막 검증이 아직 죽은 것으로 본 항목 (url 기준 합집합)."""
    merged = list(accumulated)
    seen = {item["url"] for item in merged}
    for item in from_check or []:
        if item.get("url") not in seen:
            merged.append(item)
            seen.add(item.get("url"))
    return merged


def recheck(markdown_path: Path, db_path: str, check_path: Path):
    """(검증 결과, 제거된 항목 목록). 본문을 고쳤으면 파일도 갱신되어 있다."""
    dropped = []
    result = None
    for attempt in range(MAX_PRUNE_ROUNDS):
        result = check_digest(
            db_path=db_path,
            markdown_path=markdown_path,
            output_path=None,
            skip_network=False,
        )
        text = markdown_path.read_text(encoding="utf-8")
        # 사이클3 #7: 섹션 판정은 방금 쓴 check.json 의 목록과 정확 일치로 한다.
        item_sections, _ = sections_mod.resolve(result, text)

        # 사이클 7 (Codex 3차 MEDIUM #4): 같은 URL 중복 제거는 **죽은 링크와 무관하게
        # 항상** 돈다. 예전에는 prune 라운드 안에만 있어서, 무관한 dead 산문 링크가
        # 있을 때만 중복이 접히고 없으면 같은 URL 2건이 그대로 pass 했다.
        deduped, duplicates = prune.dedupe_urls(text, item_sections)
        if duplicates:
            markdown_path.write_text(deduped, encoding="utf-8")
            dropped.extend(duplicates)
            for item in duplicates:
                print(f"  중복 URL 제거: {item['title']} ({item['url']})")
            text = deduped
            continue

        dead = dead_urls(result)
        if not dead or attempt == MAX_PRUNE_ROUNDS - 1:
            break

        pruned, removed, unlinked = prune.strip_dead_urls(text, dead, item_sections)
        if pruned == text:
            # 제거 대상을 본문에서 찾지 못했다 → 더 돌려도 같다. 아래에서 pass=false.
            break
        markdown_path.write_text(pruned, encoding="utf-8")
        dropped.extend(removed)
        # 해설·본문에서 링크만 떼어낸 죽은 URL도 check.json 에 남긴다 —
        # 미리보기 헤더의 "죽은 URL 제외 N건" 이 실제 제거 건수와 맞아야 한다.
        dropped.extend({"title": "본문 링크", "url": url} for url in unlinked)
        for url in unlinked:
            print(f"  죽은 링크 제거(문장 보존): {url}")
        for item in removed:
            print(f"  죽은 항목 제거: {item['title']} ({item['url']})")

    result["dropped"] = _merge_dropped(dropped, result.get("dropped"))
    write_check_result(check_path, result)
    # 사이클 6 #5: `.kakao.txt` 는 **항상 md 에서** 재생성한다. 예전에는 재검토가
    # md 의 죽은 링크만 지워서, 카톡 발송본에는 죽은 링크가 그대로 남았다.
    _refresh_kakao(markdown_path)
    return result, dropped


def _refresh_kakao(markdown_path: Path) -> None:
    """`<주차>.kakao.txt` 를 발송본 마크다운에서 다시 렌더한다."""
    kakao_path = markdown_path.with_name(f"{markdown_path.stem}.kakao.txt")
    try:
        kakao_path.write_text(
            kakao_file_text_from_markdown(
                markdown_path.read_text(encoding="utf-8")
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"⚠️  카톡 평문 재생성 실패: {exc}", file=sys.stderr)


def write_failure(check_path: Path, markdown_path: Path, reason: str) -> None:
    """재검증 실패를 check.json 에 착지 (과거 pass 를 남겨두지 않는다)."""
    try:
        md_sha = markdown_sha256(markdown_path.read_bytes())
    except OSError:
        md_sha = ""
    write_check_result(check_path, {
        "items": [],
        "dropped": [],
        "pass": False,
        "network_checked": False,
        "item_blocks": 0,
        "item_sections": [],
        "commentary_sections": [],
        "reason": f"재검증 실패: {reason}",
        "markdown_sha256": md_sha,
    })


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
        result, dropped = recheck(markdown_path, args.db, check_path)
    except Exception as exc:  # noqa: BLE001 — 체크 자체 예외는 fail-closed
        # 예외·타임아웃·DB 오류로 검증을 못 했으면 이전 check.json 을 무효화한다.
        write_failure(check_path, markdown_path, str(exc))
        print(f"✗ 검증 실패: {exc}", file=sys.stderr)
        print(f"  check.json 을 pass=false 로 덮어썼습니다: {check_path}",
              file=sys.stderr)
        return 1

    print(f"✓ 검증 완료: {check_path}")
    if result["pass"]:
        print(
            "  통과 항목 {}건 / 제거 {}건".format(
                len(result["items"]), len(dropped)
            )
        )
        return 0

    print(f"  ⚠️  {result.get('reason', '검증 실패')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
