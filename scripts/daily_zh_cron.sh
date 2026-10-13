#!/usr/bin/env bash
# daily_zh_cron.sh - Automated daily runner for KC Z/H Spread & ICE Certified Stock Monitor
#
# Recommended schedule: 13:45 NY time (ET) / 17:45 UTC Monday through Friday.
# (ICE releases daily certified stock reports around 13:15 - 13:30 NY time).
#
# To install in crontab:
#   crontab -e
# Add line (adjusting local time):
#   45 13 * * 1-5 /Users/qm/Documents/KCquant/scripts/daily_zh_cron.sh >> /Users/qm/Documents/KCquant/output/monitor/cron.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${ROOT_DIR}"
mkdir -p output/monitor

echo "----------------------------------------------------------------"
echo "[$(date -u +"%Y-%m-%d %H:%M:%S UTC")] Running KC Z/H Spread Daily Monitor..."

# Execute monitor with desktop notifications enabled on trigger
PYTHONPATH="${ROOT_DIR}/src:${ROOT_DIR}/scripts" python3 scripts/monitor_zh_spread.py --notify

EXIT_CODE=$?
echo "[$(date -u +"%Y-%m-%d %H:%M:%S UTC")] Monitor finished with exit code ${EXIT_CODE}"
exit ${EXIT_CODE}
