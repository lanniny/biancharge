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
export PYTHONUNBUFFERED=1

echo "[$(date -Is)] autotrader loop start config=$CONFIG" >> logs/market-autotrader-runner.log

while [[ ! -f logs/market-autotrader.stop ]]; do
  date -Is > logs/market-autotrader-heartbeat.txt
  $PYTHON -u market_autotrader.py --config "$CONFIG" --once \
    >> logs/market-autotrader.stdout.log 2>> logs/market-autotrader.stderr.log || true
  echo "[$(date -Is)] cycle done" >> logs/market-autotrader-runner.log
  [[ -f logs/market-autotrader.stop ]] && break
  sleep "$LOOP_DELAY"
done

echo "[$(date -Is)] autotrader stopped" >> logs/market-autotrader-runner.log
