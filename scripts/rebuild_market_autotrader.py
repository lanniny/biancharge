#!/usr/bin/env python3
"""Rebuild market_autotrader.py from git diff base + transcript StrReplace replay."""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DIFF_FILE = Path(
    r"C:\Users\16643\.cursor\projects\c-Users-16643-Documents-bi-an"
    r"\agent-tools\63ccca43-2039-4678-8d92-33b69359584e.txt"
)
TRANSCRIPT_DIR = Path(
    r"C:\Users\16643\.cursor\projects\c-Users-16643-Documents-bi-an"
    r"\agent-transcripts\d20b07f3-c2ce-4650-84a8-9bbe0131014c"
)
TARGET = PROJECT / "market_autotrader.py"
ORIGINAL_SIMPLE = PROJECT / "_market_autotrader_simple_backup.py"


def apply_unified_diff(base_text: str, diff_section: str) -> str:
    """Apply unified diff hunks to base_text line list."""
    base_lines = base_text.splitlines(keepends=True)
    if base_lines and not base_lines[-1].endswith("\n"):
        base_lines[-1] += "\n"

    hunks = re.split(r"^@@ .+ @@\n", diff_section, flags=re.M)[1:]
    meta = re.findall(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", diff_section, re.M)

    offset = 0
    for idx, hunk_body in enumerate(hunks):
        if idx >= len(meta):
            break
        old_start = int(meta[idx][0]) - 1 + offset
        cursor = old_start
        for line in hunk_body.splitlines(keepends=True):
            if not line:
                continue
            if line.startswith("+"):
                base_lines.insert(cursor, line[1:])
                cursor += 1
                offset += 1
            elif line.startswith("-"):
                if cursor < len(base_lines):
                    del base_lines[cursor]
                    offset -= 1
            elif line.startswith(" "):
                cursor += 1
    return "".join(base_lines)


def extract_diff_section(text: str, filename: str) -> str:
    pattern = rf"diff --git a/{re.escape(filename)} b/{re.escape(filename)}\n(.*?)(?=\ndiff --git |\Z)"
    match = re.search(pattern, text, re.S)
    if not match:
        raise SystemExit(f"diff section not found for {filename}")
    return match.group(1)


def iter_transcript_str_replaces(target_name: str) -> list[tuple[int, str, str]]:
    ops: list[tuple[int, str, str]] = []
    seq = 0
    files = [TRANSCRIPT_DIR / "d20b07f3-c2ce-4650-84a8-9bbe0131014c.jsonl"]
    sub = TRANSCRIPT_DIR / "subagents"
    if sub.is_dir():
        files.extend(sorted(sub.glob("*.jsonl")))
    for fp in files:
        if not fp.is_file():
            continue
        for line in fp.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = row.get("message") or {}
            for block in msg.get("content") or []:
                if block.get("type") != "tool_use":
                    continue
                if block.get("name") != "StrReplace":
                    continue
                inp = block.get("input") or {}
                path = str(inp.get("path") or "")
                if not path.lower().endswith(target_name.lower()):
                    continue
                old = inp.get("old_string")
                new = inp.get("new_string")
                if old is None or new is None:
                    continue
                seq += 1
                ops.append((seq, old, new))
    return ops


def main() -> int:
    if not DIFF_FILE.is_file():
        print("missing diff file", DIFF_FILE, file=sys.stderr)
        return 1

    diff_text = DIFF_FILE.read_text(encoding="utf-8", errors="replace")
    section = extract_diff_section(diff_text, "market_autotrader.py")

    # Base: recovered simple version (971 lines) if backup exists, else current before corrupt patch attempt
    if ORIGINAL_SIMPLE.is_file():
        base = ORIGINAL_SIMPLE.read_text(encoding="utf-8")
    else:
        # minimal original from diff context - use first version in repo from diff's --- a/
        # The diff modifies 58ab642 version; use embedded recovery from agent-tools /dev/null not available
        # Fall back: reconstruct by applying diff to a copy saved before corruption
        backup = PROJECT / "market_autotrader.py.corrupt.bak"
        if TARGET.is_file():
            shutil.copy2(TARGET, backup)
        # Use the simple scaffold from tests companion - write minimal and patch
        simple = PROJECT / "market_autotrader.py"
        if simple.is_file() and "class MarketBar:" in simple.read_text(encoding="utf-8", errors="replace"):
            # corrupted - try to find clean copy in agent-tools full + file
            plus_only = []
            in_ma = False
            for ln in diff_text.splitlines():
                if ln.startswith("diff --git a/market_autotrader.py"):
                    in_ma = True
                    continue
                if in_ma and ln.startswith("diff --git ") and "market_autotrader.py" not in ln:
                    break
                if not in_ma:
                    continue
                if ln.startswith("+") and not ln.startswith("+++"):
                    plus_only.append(ln[1:])
            if len(plus_only) > 500:
                base = "\n".join(plus_only) + "\n"
            else:
                print("cannot determine base", file=sys.stderr)
                return 1
        else:
            base = simple.read_text(encoding="utf-8")

    patched = apply_unified_diff(base, section)
    ops = iter_transcript_str_replaces("market_autotrader.py")
    applied = 0
    missed = 0
    for seq, old, new in ops:
        if old in patched:
            patched = patched.replace(old, new, 1)
            applied += 1
        else:
            missed += 1
            print(f"WARN miss seq={seq}")

    # Ensure re-exports used by trading_pipeline
    if "apply_holding_priority_signal" not in patched:
        insert = (
            "\nfrom exit_engine import apply_holding_priority_signal  # noqa: E402\n"
        )
        if "from exit_engine import" not in patched:
            # add before run_once
            marker = "def run_once("
            if marker in patched:
                patched = patched.replace(marker, insert + "\n" + marker, 1)

    if "CASH_USDT_FUTURES" not in patched:
        patched = patched.replace(
            "BINANCE_FUTURES_BASE = ",
            'CASH_USDT_FUTURES = "USDT"\nBINANCE_FUTURES_BASE = ',
            1,
        )

    TARGET.write_text(patched, encoding="utf-8")
    print(f"Wrote {TARGET} lines={len(patched.splitlines())} strreplace applied={applied} missed={missed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
