#!/usr/bin/env bash
# monthly_job.sh — 월간 종합호 잡 (P1' 계약 §2).
#
# digest_job.sh 와 **같은 순서**다: 생성 → glm_enrich → recheck → notify.
# 발송은 이 잡이 하지 않는다 — 사람이 텔레그램 [발송] 버튼으로만 한다.
#
# 왜 날짜 가드가 스크립트에 있나: launchd 는 "매월 첫째 목요일"을 표현하지 못한다.
# plist 는 **매주 목요일 23:00** 에 깨우고, 여기서 `date +%d` 가 1~7 일 때만
# 진행한다(그 주의 목요일이 곧 그 달의 첫째 목요일이다). 나머지 주는 로그 한 줄을
# 남기고 exit 0 으로 끝낸다 — 실패가 아니라 "이번 주는 차례가 아님"이다.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}" || exit 1
PY="${ROOT}/venv/bin/python"
DB="${GONGGO_DB:-alert/data/announcements.db}"

# 첫째 목요일 판정. 인자로 호 키를 직접 주면(재실행·수동 조립) 가드를 건너뛴다.
if [ "$#" -eq 0 ]; then
  DAY_OF_MONTH="$(date "+%d")"
  # 10진수 강제 (08·09 를 8진수로 읽지 않게)
  if [ "$((10#${DAY_OF_MONTH}))" -gt 7 ]; then
    echo "=== monthly_job skip ($(date "+%Y-%m-%d")) — 첫째 목요일이 아님(일자 ${DAY_OF_MONTH}) ==="
    exit 0
  fi
  MONTH="$("${PY}" -c 'import datetime,sys; t=datetime.date.today(); y,m=(t.year,t.month-1) if t.month>1 else (t.year-1,12); print(f"{y}-M{m:02d}")')"
else
  MONTH="$1"
fi

MD="digests/${MONTH}.md"

echo "=== monthly_job ${MONTH} ($(date "+%Y-%m-%d %H:%M:%S")) ==="

GEN_RC=0
"${PY}" scripts/monthly_digest.py "${MONTH}" --db "${DB}" --exclude-state || GEN_RC=$?
echo "monthly_digest exit=${GEN_RC}"

if [ "${GEN_RC}" -ne 0 ]; then
  echo "월간호 생성 실패(exit ${GEN_RC}) — 미리보기 전송 생략(이전 판 재전송 방지)" >&2
  exit "${GEN_RC}"
fi

if [ ! -f "${MD}" ]; then
  echo "월간호 파일 없음: ${MD} — 미리보기 생략" >&2
  exit 1
fi

# V3(GLM 야간 요약 레인): 항목 보강 줄(→ 한 줄 의미). 실패해도 잡은 계속한다.
GLM_RC=0
"${PY}" scripts/glm_enrich.py "${MONTH}" --db "${DB}" || GLM_RC=$?
echo "glm_enrich exit=${GLM_RC} (실패해도 계속)"

RECHECK_RC=0
"${PY}" scripts/recheck_digest.py "${MD}" --db "${DB}" || RECHECK_RC=$?
echo "recheck_digest exit=${RECHECK_RC}"

if [ "${RECHECK_RC}" -ne 0 ]; then
  echo "재검증 실패(exit ${RECHECK_RC}) — 미리보기 전송 생략" >&2
  exit "${RECHECK_RC}"
fi

NOTIFY_RC=0
"${PY}" scripts/notify_digest.py "${MD}" --db "${DB}" || NOTIFY_RC=$?
echo "notify_digest exit=${NOTIFY_RC}"
exit "${NOTIFY_RC}"
