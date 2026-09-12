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
from alert.digest import state as state_mod
from alert.digest.checker import (
    check_digest,
    dead_urls,
    markdown_sha256,
    write_check_result,
)
from alert.utils.redact import redact

# 죽은 URL 제거 → 재검증을 반복하는 최대 횟수 (무한 루프 방지)
MAX_PRUNE_ROUNDS = 3


def _err(message) -> None:
    """재검증의 **모든** stderr 출력 (사이클7 #5).

    check_digest 가 올리는 예외 문자열에 토큰·URL 자격이 섞여 launchd 로그로
    새지 않도록 한 곳에서 redact 한다.
    """
    print(redact(message), file=sys.stderr)


def _out(message) -> None:
    print(redact(message))


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
        dead = dead_urls(result)
        if not dead or attempt == MAX_PRUNE_ROUNDS - 1:
            break

        text = markdown_path.read_text(encoding="utf-8")
        # 사이클3 #7: 섹션 판정은 방금 쓴 check.json 의 목록과 정확 일치로 한다.
        item_sections, _ = sections_mod.resolve(result, text)
        pruned, removed, unlinked = prune.strip_dead_urls(text, dead, item_sections)
        if pruned == text:
            # 제거 대상을 본문에서 찾지 못했다 → 더 돌려도 같다. 아래에서 pass=false.
            break
        # 사이클2 #7: 링크를 떼어내면 제목이 겹칠 수 있다 → 중복 재검사.
        pruned, duplicates = prune.dedupe_titles(pruned, item_sections)
        markdown_path.write_text(pruned, encoding="utf-8")
        dropped.extend(removed)
        dropped.extend(duplicates)
        for item in duplicates:
            _out(f"  중복 제목 제거: {item['title']}")
        # 해설·본문에서 링크만 떼어낸 죽은 URL도 check.json 에 남긴다 —
        # 미리보기 헤더의 "죽은 URL 제외 N건" 이 실제 제거 건수와 맞아야 한다.
        dropped.extend({"title": "본문 링크", "url": url} for url in unlinked)
        for url in unlinked:
            _out(f"  죽은 링크 제거(문장 보존): {url}")
        for item in removed:
            _out(f"  죽은 항목 제거: {item['title']} ({item['url']})")

    result["dropped"] = _merge_dropped(dropped, result.get("dropped"))
    write_check_result(check_path, result)
    return result, dropped


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
        # 사이클7 #5: 사유는 미리보기로 흐른다 — 토큰을 남기지 않는다.
        "reason": f"재검증 실패: {redact(reason)}",
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
    week = state_mod.week_from_markdown(markdown_path)
    if not state_mod.valid_week(week):
        _err(f"✗ 주차 파일명이 아닙니다: {markdown_path.name}")
        return 2

    state_path = state_mod.state_path_for_markdown(markdown_path)
    lock_path = state_mod.lock_path_for_markdown(markdown_path)

    # 사이클7 #1: 재검증도 **같은 잠금**을 따른다. 발송기가 쥐고 있으면 기다리고,
    # 한도를 넘기면 본문·검증·표식을 하나도 건드리지 않고 끝낸다.
    try:
        handle = state_mod.acquire_lock(lock_path, blocking=True)
    except (state_mod.LockBusy, OSError) as exc:
        _err(f"✗ 재검증 거부: 잠금 대기 실패({redact(exc)})")
        return 2

    try:
        if not markdown_path.exists():
            _err(f"✗ 파일 없음: {markdown_path}")
            return 2

        # 사이클7 #2: **첫 동작은 승인 세대 폐기**다. 실패하면 즉시 중단한다 —
        # 옛 승인이 살아 있는 채로 본문·검증을 바꾸면 그 카드로 발송이 된다.
        try:
            state_mod.update_state_locked(
                state_path, week, state_mod.clear_approval)
        except (state_mod.StateError, state_mod.TransitionError, OSError) as exc:
            _err(
                f"✗ 재검증 중단: 승인 폐기 실패({redact(exc)}) "
                "— 본문·검증을 건드리지 않았습니다"
            )
            return 2

        check_path = markdown_path.with_suffix(".check.json")
        try:
            result, dropped = recheck(markdown_path, args.db, check_path)
        except Exception as exc:  # noqa: BLE001 — 체크 자체 예외는 fail-closed
            # 예외·타임아웃·DB 오류로 검증을 못 했으면 이전 check.json 을 무효화한다.
            write_failure(check_path, markdown_path, str(exc))
            _err(f"✗ 검증 실패: {exc}")
            _err(f"  check.json 을 pass=false 로 덮어썼습니다: {check_path}")
            return 1

        _out(f"✓ 검증 완료: {check_path}")
        if result["pass"]:
            _out(
                "  통과 항목 {}건 / 제거 {}건".format(
                    len(result["items"]), len(dropped)
                )
            )
            return 0

        _out(f"  ⚠️  {result.get('reason', '검증 실패')}")
        return 1
    finally:
        state_mod.release_lock(handle)


def guarded_main():
    """예외·traceback 까지 redact 해서 내보낸다 (사이클6 #7 · 사이클7 #5)."""
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
