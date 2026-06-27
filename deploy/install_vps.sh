#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${1:-/home/deploy/market-autotrader}"
cd "$APP_DIR"
if python3 -m venv .venv 2>/dev/null; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install -U pip
  pip install -r requirements.txt
else
  echo "venv unavailable; using system python3 + --user pip"
  python3 -m pip install --user -r requirements.txt || pip3 install --user -r requirements.txt
fi
mkdir -p logs config approvals
chmod +x deploy/run_autotrader.sh
if [[ ! -f .env ]]; then
  cat > .env.example <<'EOF'
# Copy to .env and fill (never commit .env)
BINANCE_API_KEY=
BINANCE_API_SECRET=
EOF
  echo "Create $APP_DIR/.env from .env.example with API keys before live trading."
fi
python3 -m pytest tests/ -q --tb=no 2>/dev/null || echo "WARN: pytest skipped or partial failures"
echo "Install done."
