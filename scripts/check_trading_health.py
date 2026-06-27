#!/usr/bin/env python3
"""Quick trading stack health check for LO."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


def pid_alive(pid: str | None) -> bool:
    if not pid or not pid.isdigit():
        return False
    r = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"],
        capture_output=True,
        text=True,
    )
    return pid in (r.stdout or "")


def tail_jsonl(path: Path, n: int = 1) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[dict] = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    logs = root / "logs"
    issues: list[str] = []
    ok: list[str] = []

    sys.path.insert(0, str(root))
    try:
        import market_autotrader  # noqa: F401

        ok.append("market_autotrader import")
    except Exception as exc:
        issues.append(f"market_autotrader import failed: {exc}")

    stderr = logs / "market-autotrader.stderr.log"
    if stderr.exists():
        err_lines = stderr.read_text(encoding="utf-8", errors="replace").splitlines()
        recent = err_lines[-20:]
        if any("ImportError" in ln for ln in recent):
            # ignore if runner already succeeding (exit=0)
            runner = logs / "market-autotrader-runner.log"
            if runner.exists():
                rt = runner.read_text(encoding="utf-8", errors="replace").splitlines()[-1:]
                if not rt or "exit=0" not in rt[-1]:
                    issues.append("stderr has ImportError and runner not exit=0")

    for name in ("market-autotrader.pid", "realtime-supervisor.pid", "trading-dashboard.pid"):
        pid_path = logs / name
        pid = pid_path.read_text(encoding="utf-8").strip() if pid_path.exists() else None
        label = name.replace(".pid", "")
        if pid_alive(pid):
            ok.append(f"{label} pid={pid} alive")
        else:
            issues.append(f"{label} pid missing or dead ({pid})")

    hb = logs / "market-autotrader-heartbeat.txt"
    runner = logs / "market-autotrader-runner.log"
    cycle_ok = False
    if runner.exists():
        rt = runner.read_text(encoding="utf-8", errors="replace").splitlines()[-1:]
        if rt and "exit=0" in rt[-1]:
            cycle_ok = True
            ok.append("last runner cycle exit=0")
        elif rt and "exit=1" in rt[-1]:
            issues.append(f"last runner cycle exit=1: {rt[-1][-80:]}")
    if hb.exists():
        hb_age = time.time() - hb.stat().st_mtime
        # Full discovery cycles can exceed 3 min; use runner exit=0 as primary signal.
        if hb_age > 600 and not cycle_ok:
            issues.append(f"autotrader heartbeat stale ({int(hb_age)}s)")
        elif hb_age <= 600:
            ok.append(f"autotrader heartbeat ({int(hb_age)}s since last touch)")

    decisions = logs / "market-autotrader-live-decisions.jsonl"
    latest = tail_jsonl(decisions, 1)
    if latest:
        ts = int(latest[0].get("timestamp", 0) or 0)
        age = int(time.time()) - ts if ts else -1
        if age > 600:
            issues.append(f"decision log stale (last {age}s ago, {latest[0].get('symbol')})")
        else:
            ok.append(f"decision log fresh ({age}s, {latest[0].get('symbol')} {latest[0].get('status')})")
    else:
        issues.append("decision log empty")

    if runner.exists() and not cycle_ok:
        tail = runner.read_text(encoding="utf-8", errors="replace").splitlines()[-1:]
        if tail and "exit=1" in tail[-1]:
            issues.append(f"last runner cycle exit=1: {tail[-1][-80:]}")

    print("=== Trading Health ===")
    for line in ok:
        print(f"OK  {line}")
    for line in issues:
        print(f"!!  {line}")
    print(f"\nSummary: {len(ok)} ok, {len(issues)} issues")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
