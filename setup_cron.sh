#!/bin/bash
# cron 설정 스크립트
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/venv/bin/python"
LOG="$SCRIPT_DIR/alert/data/logs/cron.log"

# Cron entries for 09:00 and 15:00
CRON_ENTRY_09="0 9 * * * cd $SCRIPT_DIR && $PYTHON -m alert.main >> $LOG 2>&1"
CRON_ENTRY_15="0 15 * * * cd $SCRIPT_DIR && $PYTHON -m alert.main >> $LOG 2>&1"

echo "다음 cron 항목을 추가합니다:"
echo "$CRON_ENTRY_09"
echo "$CRON_ENTRY_15"
echo ""

# Add to crontab (preserve existing entries)
(crontab -l 2>/dev/null | grep -v "alert.main"; echo "$CRON_ENTRY_09"; echo "$CRON_ENTRY_15") | crontab -

echo "✅ Cron 설정 완료"
echo "확인: crontab -l"
