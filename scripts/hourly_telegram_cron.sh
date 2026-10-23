#!/usr/bin/env bash
# hourly_telegram_cron.sh - Hourly runner for Telegram KC Z/H Spread Updates
#
# Market open hours for ICE Coffee "C": 04:15 - 13:30 NY Time (ET), Monday - Friday.
#
# Recommended crontab schedule:
#   crontab -e
# Add line (runs at :15 past each hour between 04:00 and 13:00 ET):
#   15 4-13 * * 1-5 /Users/qm/Documents/KCquant/scripts/hourly_telegram_cron.sh >> /Users/qm/Documents/KCquant/output/monitor/telegram_cron.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${ROOT_DIR}"
mkdir -p output/monitor

echo "----------------------------------------------------------------"
echo "[$(date -u +"%Y-%m-%d %H:%M:%S UTC")] Running hourly Telegram monitor check..."

PYTHONPATH="${ROOT_DIR}/src:${ROOT_DIR}/scripts" python3 scripts/telegram_kc_bot.py

EXIT_CODE=$?
echo "[$(date -u +"%Y-%m-%d %H:%M:%S UTC")] Finished with exit code ${EXIT_CODE}"
exit ${EXIT_CODE}
