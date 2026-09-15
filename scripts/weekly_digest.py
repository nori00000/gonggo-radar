#!/usr/bin/env python3
"""주간 정책브리핑 다이제스트 생성 스크립트."""

import sys
from datetime import datetime
from pathlib import Path

# sys.path 보정
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alert.digest.composer import compose_digest
from alert.digest.checker import body_links, check_digest, write_check_result
from alert.digest import state as state_mod
from alert.utils.redact import redact
from alert.utils.safe_argparse import (
    RedactingArgumentParser,
    reject_secret_argv,
)

# 죽은 URL 제외 → 재조립을 반복하는 최대 횟수 (무한 루프 방지)
MAX_RECOMPOSE_ROUNDS = 3


def _err(message) -> None:
    """생성기의 **모든** stderr 출력 (사이클7 #5)."""
    print(redact(message), file=sys.stderr)


def _out(message) -> None:
    print(redact(message))


def main():
    parser = RedactingArgumentParser(
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
    parser.add_argument(
        "--pins",
        action="store_true",
        help="상태 파일의 pinned_ids(`핀 n` 승격)를 선정 단계에서 반영",
    )

    # 사이클8 #3: argparse 는 guarded_main 보다 먼저 말한다 — argv 에 토큰 형태가
    # 있으면 **내용을 출력하지 않고** 일반 오류로 끝낸다.
    if reject_secret_argv(sys.argv[1:], _err):
        return 2
    args = parser.parse_args()

    # 현재 주 결정
    if args.week is None:
        today = datetime.now()
        week_num = today.isocalendar()[1]
        year = today.isocalendar()[0]
        week = f"{year}-W{week_num:02d}"
    else:
        week = args.week

    if not state_mod.valid_week(week):
        _err(f"✗ 주차 형식이 아닙니다: {week!r}")
        return 2

    return compose_issue(args, week)


def compose_issue(args, week):
    """호 하나를 조립·검증한다 (잠금 → 승인 폐기 → 생성 → 검증).

    P1' 계약 §1: 주간호·월간호가 **같은 코드**를 탄다 — 잠금·승인 세대 폐기·재조립
    규율이 호 종류에 따라 갈라지면 한쪽만 고쳐지는 길이 생긴다. 호를 가르는 것은
    키 형식 하나이며, 그 키가 digests/ 의 모든 파일 이름이다.
    """
    out_dir = Path(args.out_dir)
    state_path = state_mod.state_path(week, out_dir)
    lock_path = state_mod.lock_path(week, out_dir)

    # 사이클7 #1: 생성·재조립도 **같은 잠금**을 따른다 (발송기가 쥐고 있으면 대기).
    try:
        handle = state_mod.acquire_lock(lock_path, blocking=True)
    except (state_mod.LockBusy, OSError) as exc:
        _err(f"✗ 생성 거부: 잠금 대기 실패({redact(exc)})")
        return 2

    try:
        # 사이클7 #2: **첫 동작은 승인 세대 폐기**다. 실패하면 즉시 중단한다 —
        # 생성이 실패(rc=1)해도 옛 승인이 살아 있으면 그 카드로 발송이 된다.
        try:
            state_mod.update_state_locked(
                state_path, week, state_mod.clear_approval)
        except (state_mod.StateError, state_mod.TransitionError, OSError) as exc:
            _err(
                f"✗ 생성 중단: 승인 폐기 실패({redact(exc)}) "
                "— 본문·검증을 건드리지 않았습니다"
            )
            return 2
        rc = _run(args, week, out_dir)
    finally:
        state_mod.release_lock(handle)

    # H5 (라운드 2, Codex MEDIUM): **여기서 만료시키지 않는다.**
    # 라운드 1 은 조립 성공 직후 옛 호를 만료시켰다. 그러면 조립은 됐는데 검증
    # 또는 텔레그램 전송이 실패한 회차에서 **옛 호는 이미 무효**가 되고 새 미리보기는
    # 오지 않는다 — 편집자에게 아무것도 남지 않는다. 만료·회수는 새 미리보기가
    # **실제로 게시된 것을 확인한 뒤**(`scripts/notify_digest.py` 의
    # `expire_stale_issues`) 일어난다.
    return rc


def _run(args, week, out_dir):
    """잠금·승인 폐기를 끝낸 뒤의 생성 본체."""
    markdown_path = out_dir / f"{week}.md"
    check_path = out_dir / f"{week}.check.json"

    forms_csv_path = Path(args.forms) if args.forms else None

    # 계약 v1.2: compose → check → url_alive=false 제외 → 재조립(섹션 상한 재적용).
    # 재조립이 새 후보를 끌어오므로, 검사되지 않은 URL이 발송물에 실리지 않도록
    # 죽은 URL이 사라질 때까지 (최대 MAX_RECOMPOSE_ROUNDS회) 다시 검사한다.
    warnings: list[str] = []
    dropped: list[dict] = []
    dropped_urls: set[str] = set()
    pin_ids: set[int] = set()
    stats: dict = {}
    result = None

    # 계약 W10: 사람이 텔레그램에서 제외한 항목은 상태 파일이 정본이다.
    # 조회 단계에서 빼므로 섹션 상한이 남은 후보로 다시 채워진다.
    # V4 계약 ③: 승격(`핀 n`)도 같은 정본에서 읽는다 — 상태 파일 한 번만 읽는다.
    if args.exclude_state or args.pins:
        state_path = state_mod.state_path(week, out_dir)
        try:
            state = state_mod.load_state(state_path, week)
        except state_mod.StateError as exc:
            _err(f"✗ 상태 파일 손상: {exc}")
            return 1
        if args.exclude_state:
            state_excluded = state_mod.excluded_urls(state)
            if state_excluded:
                dropped_urls.update(state_excluded)
                _out(f"제외 상태 반영: {len(state_excluded)}건")
        if args.pins:
            pin_ids = set(state_mod.pinned_ids(state))
            if pin_ids:
                _out(f"승격 상태 반영: {len(pin_ids)}건")

    for attempt in range(1, MAX_RECOMPOSE_ROUNDS + 1):
        label = "다이제스트 생성" if attempt == 1 else "재조립"
        _out(f"Composing digest for {week}... (round {attempt})")
        try:
            compose_digest(
                db_path=args.db,
                week_str=week,
                output_path=markdown_path,
                forms_csv_path=forms_csv_path,
                warnings_out=warnings if attempt == 1 else None,
                exclude_urls=dropped_urls or None,
                stats_out=stats,
                pin_ids=pin_ids or None,
            )
            # 출력 경로는 main 의 `_out`(종료 코드·리다이렉트 통일)을 따른다.
            _out(f"✓ {label}: {markdown_path}")
            kakao_path = markdown_path.with_name(f"{markdown_path.stem}.kakao.txt")
            if kakao_path.exists():
                _out(f"✓ 카톡 평문: {kakao_path}")
        except Exception as e:
            _err(f"✗ {label} 실패: {e}")
            return 1

        _out("Checking digest items...")
        try:
            result = check_digest(
                db_path=args.db,
                markdown_path=markdown_path,
                output_path=None,
                skip_network=False,
                warnings=warnings,
            )
        except Exception as e:
            _err(f"✗ 검증 실패: {e}")
            return 1

        round_dropped = [
            item for item in result.get("dropped", [])
            if item["url"] not in dropped_urls
        ]
        if not round_dropped:
            break

        _out(f"  ⚠️  죽은 URL {len(round_dropped)}건 제외 후 재조립")
        for item in round_dropped:
            _out(f"     - {item['title']} ({item['url']})")
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
    _out(f"✓ 검증 완료: {check_path}")

    if result["pass"]:
        _out(f"  통과 항목 {len(result['items'])}건 / 제외 {len(dropped)}건")
        return 0

    _out(f"  ⚠️  {result.get('reason', '검증 실패')}")
    return 1


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
