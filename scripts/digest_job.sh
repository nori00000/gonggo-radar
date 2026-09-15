#!/usr/bin/env bash
# digest_job.sh — launchd(com.gonggo-radar.digest)가 실행하는 주간 다이제스트 잡.
#
# 왜 래퍼인가: launchd는 "생성 후 미리보기 전송"이라는 두 단계를 한 번에 돌려야 한다
# (계약 W10).
#
# 생성이 실패하면 미리보기를 **보내지 않는다**(크리틱 #8): 이전 주(또는 이전 판)의
# md 가 디스크에 남아 있으므로, 실패한 잡이 notify 를 돌리면 낡은 "발송 가능"
# 미리보기가 다시 올라간다. 실패는 로그 한 줄로 끝내고 잡의 종료 코드로 알린다.
#
# 발송은 이 잡이 하지 않는다 — 사람이 텔레그램 [발송] 버튼으로만 한다.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 잠금 재실행에서 쓸 자기 경로 (cd 전에 절대경로로 굳힌다).
SELF="${ROOT}/scripts/$(basename "${BASH_SOURCE[0]}")"
cd "${ROOT}" || exit 1
PY="${ROOT}/venv/bin/python"
DB="${GONGGO_DB:-alert/data/announcements.db}"

# ── 잡 전역 뮤텍스 (P2-X H4) ─────────────────────────────────────────────
# 감사 V M-4: 주간(23:00)·월간(23:30) 잡은 **서로 다른 호 잠금**만 잡아 상호배제가
# 없었다. 겹치면 z.ai 레인을 동시에 부르고 미리보기가 경합한다. 두 잡이 **같은**
# digests/.job.lock 을 쥔다. macOS 에 flock(1) 이 없어 scripts/job_lock.py 가
# fcntl.flock 을 쥔 채 이 스크립트를 다시 exec 한다(잠금은 exec 를 넘어 산다).
# 대기 한도 20분, 못 얻으면 로그 한 줄 + exit 75(EX_TEMPFAIL).
# 잠금 파이썬은 venv 가 아니라 시스템 파이썬이다 — venv 가 깨져도 뮤텍스는 선다.
if [ -z "${GONGGO_JOB_LOCK_HELD:-}" ]; then
  export GONGGO_JOB_LOCK_HELD=1
  exec "${GONGGO_LOCK_PYTHON:-/usr/bin/python3}" "${ROOT}/scripts/job_lock.py" \
    --lock "${ROOT}/digests/.job.lock" \
    --timeout "${GONGGO_JOB_LOCK_TIMEOUT:-1200}" \
    -- /bin/bash "${SELF}" "$@"
fi

WEEK="${1:-$("${PY}" -c 'import datetime; y, w, _ = datetime.date.today().isocalendar(); print(f"{y}-W{w:02d}")')}"
MD="digests/${WEEK}.md"

echo "=== digest_job ${WEEK} ($(date "+%Y-%m-%d %H:%M:%S")) ==="

GEN_RC=0
"${PY}" scripts/weekly_digest.py --db "${DB}" --week "${WEEK}" --exclude-state || GEN_RC=$?
echo "weekly_digest exit=${GEN_RC}"

if [ "${GEN_RC}" -ne 0 ]; then
  echo "다이제스트 생성 실패(exit ${GEN_RC}) — 미리보기 전송 생략(이전 판 재전송 방지)" >&2
  exit "${GEN_RC}"
fi

if [ ! -f "${MD}" ]; then
  echo "다이제스트 파일 없음: ${MD} — 미리보기 생략" >&2
  exit 1
fi

# V3(GLM 야간 요약 레인): 항목 보강 줄(→ 한 줄 의미)을 붙인다. **실패해도 잡은
# 계속한다** — ds 부재·타임아웃·게이트 실패는 경고일 뿐, 다이제스트 발송을
# 막을 이유가 아니다(glm_enrich.py 자체가 fail-open, 항상 exit 0에 수렴한다).
GLM_RC=0
"${PY}" scripts/glm_enrich.py "${WEEK}" --db "${DB}" || GLM_RC=$?
echo "glm_enrich exit=${GLM_RC} (실패해도 계속)"

# glm_enrich 가 본문에 보강 줄을 붙였을 수 있으므로, notify 가 보기 전에
# check.json 의 해시·항목 대조를 다시 맞춘다(recheck_digest.py — 죽은 URL도
# 함께 재검사한다). 여기서 pass=false 면 weekly_digest 실패와 같은 이유로
# 미리보기를 보내지 않는다(낡은 "발송 가능" 판정이 남지 않게).
RECHECK_RC=0
"${PY}" scripts/recheck_digest.py "${MD}" --db "${DB}" || RECHECK_RC=$?
echo "recheck_digest exit=${RECHECK_RC}"

if [ "${RECHECK_RC}" -ne 0 ]; then
  echo "재검증 실패(exit ${RECHECK_RC}) — 미리보기 전송 생략" >&2
  exit "${RECHECK_RC}"
fi

NOTIFY_RC=0
# 통합 2·3: notify 도 정본 대조·URL 생존을 본다 — **생성과 같은 DB**를 넘긴다
# (GONGGO_DB 로 경로를 바꿔 쓰는 운영에서 두 단계가 다른 DB 를 보면 안 된다).
"${PY}" scripts/notify_digest.py "${MD}" --db "${DB}" || NOTIFY_RC=$?
echo "notify_digest exit=${NOTIFY_RC}"
exit "${NOTIFY_RC}"
