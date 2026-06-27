#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
PYTHON="${PYTHON:-python3}"
mkdir -p logs
CONFIG="${CONFIG:-market_autotrader.vps.json}"
LOOP_DELAY="${LOOP_DELAY:-90}"
LOG_MAINTENANCE_INTERVAL="${LOG_MAINTENANCE_INTERVAL:-1800}"
LOG_MAINTENANCE_LAST=0
export PYTHONUNBUFFERED=1

echo "[$(date -Is)] autotrader loop start config=$CONFIG" >> logs/market-autotrader-runner.log

while [[ ! -f logs/market-autotrader.stop ]]; do
  now_epoch="$(date +%s)"
  if (( now_epoch - LOG_MAINTENANCE_LAST >= LOG_MAINTENANCE_INTERVAL )); then
    "$PYTHON" scripts/maintain_logs.py >> logs/market-autotrader-runner.log 2>> logs/market-autotrader.stderr.log || true
    LOG_MAINTENANCE_LAST="$now_epoch"
  fi
  date -Is > logs/market-autotrader-heartbeat.txt
  $PYTHON -u market_autotrader.py --config "$CONFIG" --once \
    >> logs/market-autotrader.stdout.log 2>> logs/market-autotrader.stderr.log || true
  echo "[$(date -Is)] cycle done" >> logs/market-autotrader-runner.log
  [[ -f logs/market-autotrader.stop ]] && break
  sleep "$LOOP_DELAY"
done

echo "[$(date -Is)] autotrader stopped" >> logs/market-autotrader-runner.log
