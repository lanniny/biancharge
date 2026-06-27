# Repository Guidelines

## Project Structure & Module Organization

This repository is a Python trading and monitoring system. The main entry point is `market_autotrader.py`, with pipeline orchestration in `trading_pipeline.py`. Strategy, risk, and support modules live as top-level Python files such as `market_discovery.py`, `quadrant_strategy.py`, `entry_timing.py`, `exit_engine.py`, `shadow_paper.py`, and `proxy_http.py`. Tests are in `tests/` and follow `test_*.py` naming. Operational scripts are in `scripts/`, deployment files are in `deploy/`, and documentation is in `docs/`. Runtime artifacts are written under `logs/` and approval tickets under `approvals/`; do not treat these as source changes. Configuration examples use `*.example.json`, while local or production configs such as `market_autotrader.vps.json` may contain environment-specific settings.

## Build, Test, and Development Commands

- `python -m unittest discover -s tests`: run the full unit test suite.
- `python -m pytest tests/test_trading_pipeline.py -q`: run a focused pytest target when pytest is available.
- `python -m py_compile market_autotrader.py trading_pipeline.py`: check syntax for core entry points.
- `python market_autotrader.py --config market_autotrader.example.json --once`: run one paper-trading decision cycle with the example config.
- `python realtime_supervisor.py --config realtime_supervisor.example.json --once`: run one supervisor cycle.

## Coding Style & Naming Conventions

Use standard Python style: 4-space indentation, `snake_case` for functions and variables, `PascalCase` for classes, and clear module-level constants where needed. Keep modules focused on their current responsibility rather than introducing broad shared abstractions. Prefer structured JSON handling for configs and ledgers. Add comments only for non-obvious trading logic, safety gates, or operational edge cases.

## Testing Guidelines

Add or update tests in `tests/` beside the behavior being changed. Name files `test_<module_or_feature>.py` and test methods/functions with `test_...`. Cover safety-sensitive paths, including blocked trades, live-mode guards, sizing, exit rules, and config fallbacks. Run at least the affected tests before submitting; run the full suite for shared pipeline or risk-control changes.

## Commit & Pull Request Guidelines

This checkout does not expose usable Git history, so no local commit convention can be inferred. Use concise imperative commit messages, for example `Add funding-rate risk guard`. Pull requests should describe the behavior change, list tests run, note config or deployment impacts, and include screenshots only for dashboard UI changes.

## Security & Configuration Tips

Never commit API keys, secrets, `.env`, or live account data. Keep credentials in environment variables such as `BINANCE_API_KEY` and `BINANCE_API_SECRET`. Preserve live-trading safety gates: config opt-in, arm file presence, and kill file absence must remain explicit and reviewable.
