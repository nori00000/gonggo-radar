#!/usr/bin/env python3
"""GLM 야간 요약 레인 — 항목 보강 줄(V3) 부착 스크립트.

역할: 항목 정본(`digests/<week>.items.json`) + DB(`summary`/`raw_data`)에서
프롬프트 입력 JSON을 조립해 `~/bin/ds -g`(GLM, z.ai Coding Plan 정액제)에
넘기고, 출력을 결정론 게이트로 검사한 뒤 통과한 `한 줄 의미`만 항목 블록의
3번째 줄(`  → …`)로 붙인다. 프롬프트 정본은
`scratch/2026-09-12-forest-sse-briefing/lanes/glm/persona-summary-prompt.md`
(캘리브레이션: `GLM_MODEL=glm-5-turbo GLM_TIMEOUT=600 ds -g`).

**항목 줄·원문 줄은 건드리지 않는다** — 계약상 편집 불가 영역이다. GLM 이
같이 산출하는 대상 태그·마감·자격·금액은 게이트만 거치고(향후 확장 대비),
지금 본문에 반영하는 것은 `한 줄 의미` 하나뿐이다.

**fail-open 레인이다** — ds 부재·타임아웃·GLM 출력 이상은 전부 경고만 남기고
스킵(exit 0)한다. 이 레인이 실패해도 다이제스트 생성·검증·발송을 막지 않는다
(digest_job.sh: weekly_digest → glm_enrich → recheck → notify).

호출 방식:
  `glm_enrich.py <week>`             — ds 가 있으면 실제로 부른다.
  `glm_enrich.py <week> --dry-run`   — ds 를 부르지 않고 입력 JSON만 남긴다.
  `glm_enrich.py <week> --apply-json out.json`
      — ds 를 부르지 않고, 이미 가진 GLM 출력 JSON 파일을 게이트+반영만 한다
        (판단 티어가 다른 머신에서 실호출한 결과를 받아 적용할 때, 그리고
        테스트가 쓴다).
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# sys.path 보정
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alert.digest import blocks as blocks_mod
from alert.digest import composer as composer_mod
from alert.digest.composer import extract_quotes, load_items_manifest
from alert.utils.redact import redact
from alert.utils.safe_argparse import (
    RedactingArgumentParser,
    reject_secret_argv,
)

DS_BIN = Path(os.environ.get("GLM_ENRICH_DS_BIN") or str(Path.home() / "bin" / "ds"))
# 캘리브레이션 실측(2026-09-13 lanes/glm/persona-summary-prompt.md): 기본
# 타임아웃·glm-5.3 은 빈 응답 — glm-5-turbo + 600초가 검증된 조합이다.
GLM_ENV_DEFAULTS = {"GLM_MODEL": "glm-5-turbo", "GLM_TIMEOUT": "600"}
DS_SUBPROCESS_TIMEOUT = 630  # ds 내부 alarm(GLM_TIMEOUT) + 여유

FALLBACK = "원문 확인"
_TAG_RE = re.compile(
    r"^(?:협동조합|사회적기업|산림사업자|마을기업|전체|보류)(?:\([^()]{1,20}\))?$"
)
_QUOTE_RE = re.compile(r"«([^»]+)»")
_MAX_SUMMARY_CHARS = 40  # "15자 내외" 규칙에 여유를 둔 상한 — 문단급 산출만 걸러낸다

HEADLINE_LINE_RE = re.compile(r"^이번 주 한 줄:.*$")
GLM_DRAFT_PREFIX = "<!-- GLM 초안: "
GLM_DRAFT_SUFFIX = " -->"
_MAX_HEADLINE_CHARS = 80

# 페르소나 시스템 지시 (lanes/glm/persona-summary-prompt.md 의 "시스템 지시"
# 절을 그대로 담는다. 게이트·캘리브레이션 절은 사람이 읽는 부분이라 뺐다).
PERSONA_SYSTEM_PROMPT = """너는 산림형사회연대경제협의회의 편집 보조다. 독자는 "산림형 사회적경제 기업 대표"(예비/인증 사회적기업, 사회적협동조합, 마을기업, 임업·산촌·산림복지·목재·숲체험 사업자)다. 바쁘고 폰으로 읽는다. 알고 싶은 것은 셋뿐: 내가 신청할 수 있는 돈·기회, 내 사업에 영향을 주는 규칙 변화, 협의회가 대신 뭘 하고 있나.

절대 규칙 (위반 = 실패)
1. 아래 필드는 원문(제목·요약·인용 텍스트)에 문자 그대로 있는 내용만 채운다: `마감`, `자격`, `금액`. 원문에 없으면 값 대신 정확히 `원문 확인`이라고 쓴다. 추정·일반 상식·유사 사업의 조건으로 채우지 않는다.
2. 각 채운 필드 옆에 근거 인용을 `«…»`로 20자 이내 붙인다. 인용을 붙일 수 없으면 그 필드는 `원문 확인`.
3. `대상 태그`는 {협동조합, 사회적기업, 산림사업자, 마을기업, 전체} 중에서만 고르고 지역 한정이 원문에 있으면 `(도명)`을 붙인다. 판단이 안 서면 `보류`.
4. `한 줄 의미`는 독자에게 "그래서 나는 무엇을 하면 되나"를 15자 내외로. 과장·권유 금지("꼭 신청하세요" 금지). 사실만.
5. 마감이 오늘 이전이면 `한 줄 의미`에 `마감 경과`라고만 쓴다.
6. 출력은 아래 JSON 배열만. 설명·머리말 금지.

입력 형식
{"today":"YYYY-MM-DD","items":[{"n":1,"title":"…","source_name":"…","summary":"…","quote_deadline":"…","quote_eligibility":"…","quote_amount":"…","url":"…"}]}

출력 형식
[{"n":1,"대상 태그":"사회적기업(경기)","마감":"2026-09-22 «~9.22까지»","자격":"원문 확인","금액":"원문 확인","한 줄 의미":"조달 컨설팅 신청 가능"}]"""

HEADLINE_SYSTEM_PROMPT = """너는 산림형사회연대경제협의회의 편집 보조다. 입력은 이번 주 다이제스트에 확정된 항목 목록(JSON 배열, 각 {"title":..., "deadline_label":..., "period_end":...})이다.
출력: 독자에게 이번 주 브리핑을 한 문장(40자 내외)으로. 사실만, 항목 수와 가장 임박한 마감을 포함한다. 과장·권유 금지. 출력은 문장 하나뿐 — 따옴표·설명·머리말 금지.
예: 신청 2건(가장 빠른 마감 9/22), 산림재난방지법 시행령 입법예고 의견 10/19까지."""


def _err(message) -> None:
    print(redact(message), file=sys.stderr)


def _out(message) -> None:
    print(redact(message))


# ─── 입력 JSON 조립 ──────────────────────────────────────────────────────
def fetch_summary_raw(db_path: str, ids: Sequence[int]) -> Dict[int, Tuple[str, str]]:
    """announcements.id → (summary, raw_data). DB 가 없으면 빈 dict(degraded)."""
    if not ids or not Path(db_path).exists():
        return {}
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        placeholders = ", ".join("?" * len(ids))
        cursor.execute(
            f"SELECT id, summary, raw_data FROM announcements "
            f"WHERE id IN ({placeholders})",
            list(ids),
        )
        return {row[0]: (row[1] or "", row[2] or "") for row in cursor.fetchall()}
    finally:
        conn.close()


def build_input_items(manifest_items: List[Dict], db_path: str) -> List[Dict]:
    """정본 항목 + DB raw → 프롬프트 입력 항목 목록 (내부용 `id` 포함).

    `id` 는 GLM 에 보내는 JSON(`payload_for_glm`)에서는 뺀다 — 결과를 다시
    항목 id 로 묶기 위한 내부 부기일 뿐이다.
    """
    ids = [entry["id"] for entry in manifest_items if entry.get("id") is not None]
    db_rows = fetch_summary_raw(db_path, ids)
    items: List[Dict] = []
    for index, entry in enumerate(manifest_items, start=1):
        summary, raw_data = db_rows.get(entry.get("id"), ("", ""))
        quotes = extract_quotes(summary, raw_data)
        items.append({
            "n": index,
            "id": entry.get("id"),
            "title": entry.get("title") or "",
            "source_name": entry.get("org") or "",
            "summary": summary,
            "quote_deadline": entry.get("period_end") or "",
            "quote_eligibility": quotes["quote_eligibility"],
            "quote_amount": quotes["quote_amount"],
            "url": entry.get("url") or "",
        })
    return items


def payload_for_glm(items: List[Dict]) -> List[Dict]:
    """내부 부기 필드(`id`)를 뺀, GLM 에 실제로 보내는 항목 목록."""
    return [{k: v for k, v in item.items() if k != "id"} for item in items]


# ─── ds 호출 ─────────────────────────────────────────────────────────────
def call_ds_glm(prompt_text: str, timeout: int = DS_SUBPROCESS_TIMEOUT) -> Optional[str]:
    """`~/bin/ds -g` 호출(stdin 파이프 — 인자 전달 금지 규칙). 실패하면 None."""
    if not DS_BIN.exists():
        return None
    env = os.environ.copy()
    for key, value in GLM_ENV_DEFAULTS.items():
        env.setdefault(key, value)
    try:
        proc = subprocess.run(
            [str(DS_BIN), "-g"],
            input=prompt_text,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _parse_json_array(text: Optional[str]):
    """코드펜스를 허용하는 JSON 파싱. 실패하면 None."""
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except (TypeError, ValueError):
        return None


# ─── 결정론 게이트 ────────────────────────────────────────────────────────
def _gate_tag(value) -> Tuple[str, bool]:
    if isinstance(value, str) and _TAG_RE.match(value.strip()):
        return value.strip(), True
    return "보류", False


def _gate_quoted_field(value, search_text: str) -> Tuple[str, bool]:
    """`원문 확인` 또는 `«…» 인용이 입력 텍스트의 부분문자열`인 값만 통과."""
    if not isinstance(value, str) or not value.strip():
        return FALLBACK, False
    text = value.strip()
    if text == FALLBACK:
        return FALLBACK, True
    match = _QUOTE_RE.search(text)
    if not match:
        return FALLBACK, False
    quoted = match.group(1).strip()
    if not quoted or quoted not in search_text:
        return FALLBACK, False
    return text, True


def _gate_summary(value) -> Tuple[str, bool]:
    if not isinstance(value, str):
        return FALLBACK, False
    text = " ".join(value.strip().split())
    if not text or len(text) > _MAX_SUMMARY_CHARS:
        return FALLBACK, False
    return text, True


def gate_output(
    raw_text: Optional[str], input_items: List[Dict]
) -> Tuple[Dict[int, Dict], List[str]]:
    """GLM 출력 → (n → 검증된 필드 dict, 경고 목록).

    실패한 필드는 개별적으로 `원문 확인`으로 강제 치환한다(항목 전체를 버리지
    않는다) — 파싱 자체가 실패하면 결과는 비고 경고 하나만 남는다.
    """
    parsed = _parse_json_array(raw_text)
    if parsed is None:
        return {}, ["GLM 출력 JSON 파싱 실패"]
    if not isinstance(parsed, list):
        return {}, ["GLM 출력이 JSON 배열이 아님"]

    input_by_n = {item["n"]: item for item in input_items}
    expected_ns = set(input_by_n)

    output_by_n: Dict[int, Dict] = {}
    for entry in parsed:
        if not isinstance(entry, dict) or not isinstance(entry.get("n"), int):
            continue
        output_by_n[entry["n"]] = entry

    warnings: List[str] = []
    got_ns = set(output_by_n)
    if got_ns != expected_ns:
        missing = sorted(expected_ns - got_ns)
        extra = sorted(got_ns - expected_ns)
        if missing:
            warnings.append(f"GLM 출력에 없는 n={missing}")
        if extra:
            warnings.append(f"GLM 출력의 알 수 없는 n={extra}")

    results: Dict[int, Dict] = {}
    for n in sorted(expected_ns & got_ns):
        entry = output_by_n[n]
        item = input_by_n[n]
        search_text = "\n".join(
            str(item.get(key) or "")
            for key in ("title", "summary", "quote_deadline",
                        "quote_eligibility", "quote_amount")
        )
        fields: Dict[str, str] = {}
        tag, tag_ok = _gate_tag(entry.get("대상 태그"))
        fields["대상 태그"] = tag
        if not tag_ok:
            warnings.append(f"n={n} 대상 태그 허용값 위반 — 보류로 대체")
        for key in ("마감", "자격", "금액"):
            value, ok = _gate_quoted_field(entry.get(key), search_text)
            fields[key] = value
            if not ok:
                warnings.append(f"n={n} {key} 인용 검증 실패 — 원문 확인으로 대체")
        summary_line, summary_ok = _gate_summary(entry.get("한 줄 의미"))
        fields["한 줄 의미"] = summary_line
        if not summary_ok:
            warnings.append(f"n={n} 한 줄 의미 형식 오류 — 원문 확인으로 대체")
        results[n] = fields
    return results, warnings


# ─── "이번 주 한 줄" 초안 ─────────────────────────────────────────────────
def build_headline_prompt(manifest_items: List[Dict]) -> str:
    payload = [
        {
            "title": entry.get("title") or "",
            "deadline_label": entry.get("deadline_label") or "",
            "period_end": entry.get("period_end") or "",
        }
        for entry in manifest_items
    ]
    return (
        HEADLINE_SYSTEM_PROMPT
        + "\n\n입력\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def apply_headline_draft(markdown_text: str, draft: str) -> Tuple[str, bool]:
    """이번 주 한 줄 확정 마커 **뒤**에 `<!-- GLM 초안: … -->` 를 붙이거나 갱신한다.

    확정 마커 자체는 손대지 않는다 — 채택은 사람이 `apply_commentary.py
    --headline`으로 한다.
    """
    lines = markdown_text.split("\n")
    new_comment = f"{GLM_DRAFT_PREFIX}{draft}{GLM_DRAFT_SUFFIX}"
    out: List[str] = []
    changed = False
    inserted = False
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        out.append(line)
        if not inserted and HEADLINE_LINE_RE.match(line.strip()):
            inserted = True
            next_index = index + 1
            if (next_index < total
                    and lines[next_index].strip().startswith(GLM_DRAFT_PREFIX)):
                if lines[next_index] != new_comment:
                    changed = True
                out.append(new_comment)
                index = next_index  # 옛 주석 줄은 건너뛴다(교체)
            else:
                out.append(new_comment)
                changed = True
        index += 1
    return "\n".join(out), changed


# ─── 메인 ────────────────────────────────────────────────────────────────
def _load_json(path: Path) -> Optional[Dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def run(args) -> int:
    out_dir = Path(args.out_dir)
    markdown_path = out_dir / f"{args.week}.md"
    check_path = out_dir / f"{args.week}.check.json"

    if not markdown_path.exists():
        _err(f"다이제스트 없음: {markdown_path} — GLM 보강 생략")
        return 0

    manifest = load_items_manifest(markdown_path)
    if manifest is None:
        _err("항목 정본 없음(<week>.items.json) — GLM 보강 생략")
        return 0

    manifest_items = [
        entry for entry in (manifest.get("items") or [])
        if isinstance(entry, dict)
    ]
    if not manifest_items:
        _out("항목 0건 — GLM 보강 생략")
        return 0

    input_items = build_input_items(manifest_items, args.db)
    input_payload = {"today": date.today().isoformat(), "items": payload_for_glm(input_items)}

    input_json_path = out_dir / f"{args.week}.glm_input.json"
    input_json_path.write_text(
        json.dumps(input_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _out(f"입력 JSON: {input_json_path}")

    if args.dry_run:
        _out("--dry-run: ds 호출 생략")
        return 0

    if args.apply_json:
        raw_output = Path(args.apply_json).read_text(encoding="utf-8")
        _out(f"GLM 출력(파일): {args.apply_json}")
    else:
        if not DS_BIN.exists():
            _out(f"⚠️  {DS_BIN} 없음 — GLM 보강 생략")
            return 0
        prompt = (
            PERSONA_SYSTEM_PROMPT
            + "\n\n입력\n"
            + json.dumps(input_payload, ensure_ascii=False)
        )
        raw_output = call_ds_glm(prompt)
        if raw_output is None:
            _out("⚠️  ds -g 호출 실패/타임아웃 — GLM 보강 생략")
            return 0

    results, warnings = gate_output(raw_output, input_items)
    for warning in warnings:
        _out(f"  ⚠️  {warning}")

    id_by_n = {item["n"]: item["id"] for item in input_items}
    enrich_by_id = {
        str(id_by_n[n]): blocks_mod.enrich_line_text(fields["한 줄 의미"])
        for n, fields in results.items()
        if id_by_n.get(n) is not None
    }

    applied = 0
    if enrich_by_id:
        markdown_text = markdown_path.read_text(encoding="utf-8")
        check_result = _load_json(check_path) or {}
        item_sections = check_result.get("item_sections")

        new_text, changed_ids = blocks_mod.set_enrich_lines(
            markdown_text, enrich_by_id, item_sections
        )
        if changed_ids:
            markdown_path.write_text(new_text, encoding="utf-8")
            composer_mod.set_manifest_enrich_lines(
                markdown_path,
                {item_id: enrich_by_id[item_id] for item_id in changed_ids},
            )
            applied = len(changed_ids)

    _out(f"✓ 보강 줄 적용: {applied}건 (게이트 경고 {len(warnings)}건)")

    if warnings:
        warn_path = out_dir / f"{args.week}.glm_warnings.json"
        warn_path.write_text(
            json.dumps({"warnings": warnings}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # "이번 주 한 줄" 초안 — 별도 호출, 실호출 경로에서만 시도(실패해도 무시).
    if not args.apply_json:
        headline_raw = call_ds_glm(build_headline_prompt(manifest_items))
        if headline_raw:
            draft = " ".join(headline_raw.strip().strip('"').split())
            if draft and len(draft) <= _MAX_HEADLINE_CHARS:
                text_now = markdown_path.read_text(encoding="utf-8")
                new_text, headline_changed = apply_headline_draft(text_now, draft)
                if headline_changed:
                    markdown_path.write_text(new_text, encoding="utf-8")
                    _out("✓ 이번 주 한 줄 GLM 초안 갱신")

    return 0


def main() -> int:
    parser = RedactingArgumentParser(
        description="GLM 야간 요약 레인 — 항목 보강 줄(V3) 부착"
    )
    parser.add_argument("week", help='ISO 주 표기 (예: 2026-W37)')
    parser.add_argument(
        "--db", default="alert/data/announcements.db",
        help="announcements.db 경로 (기본: alert/data/announcements.db)",
    )
    parser.add_argument(
        "--out-dir", default="digests", help="다이제스트 디렉토리 (기본: digests)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="ds 를 부르지 않고 GLM 입력 JSON만 생성",
    )
    parser.add_argument(
        "--apply-json",
        help="ds 를 부르지 않고, 이 경로의 GLM 출력 JSON을 게이트+반영만 함"
             "(m4-air 등 다른 머신의 실호출 결과를 적용할 때, 테스트용)",
    )

    if reject_secret_argv(sys.argv[1:], _err):
        return 2
    args = parser.parse_args()

    try:
        return run(args)
    except Exception as exc:  # noqa: BLE001 — 이 레인은 fail-open 이다
        _err(f"⚠️  GLM 보강 실패(무시하고 계속): {exc}")
        return 0


def guarded_main() -> int:
    try:
        return main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        import traceback
        _err(traceback.format_exc())
        return 0  # fail-open — 이 레인의 예외가 잡을 막지 않는다


if __name__ == "__main__":
    sys.exit(guarded_main())
