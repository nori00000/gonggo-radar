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
cd "${ROOT}" || exit 1
PY="${ROOT}/venv/bin/python"
DB="${GONGGO_DB:-alert/data/announcements.db}"

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

NOTIFY_RC=0
"${PY}" scripts/notify_digest.py "${MD}" || NOTIFY_RC=$?
echo "notify_digest exit=${NOTIFY_RC}"
exit "${NOTIFY_RC}"
