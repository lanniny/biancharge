#!/usr/bin/env python3
"""Rebuild market_autotrader.py: extract 971-line base from diff, apply modification patch, replay StrReplace."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DIFF = Path(
    r"C:\Users\16643\.cursor\projects\c-Users-16643-Documents-bi-an"
    r"\agent-tools\63ccca43-2039-4678-8d92-33b69359584e.txt"
)
TRANSCRIPT = Path(
    r"C:\Users\16643\.cursor\projects\c-Users-16643-Documents-bi-an"
    r"\agent-transcripts\d20b07f3-c2ce-4650-84a8-9bbe0131014c"
)
TARGET = PROJECT / "market_autotrader.py"


def extract_new_file_block(diff_text: str, filename: str) -> str | None:
    """Extract --- /dev/null new file (+ lines only)."""
    marker = f"diff --git a/{filename} b/{filename}"
    for part in diff_text.split(marker)[1:]:
        if not part.lstrip().startswith("\nnew file mode") and not part.lstrip().startswith("new file mode"):
            continue
        body = part.split("\ndiff --git ", 1)[0]
        lines: list[str] = []
        past_hunk = False
        for ln in body.splitlines():
            if ln.startswith("@@ -0,0 +1,"):
                past_hunk = True
                continue
            if not past_hunk:
                continue
            if ln.startswith("+") and not ln.startswith("+++"):
                lines.append(ln[1:])
        if lines:
            return "\n".join(lines) + "\n"
    return None


def extract_modify_section(diff_text: str, filename: str) -> str | None:
    marker = f"diff --git a/{filename} b/{filename}"
    for part in diff_text.split(marker)[1:]:
        head = part.lstrip()
        if head.startswith("new file mode"):
            continue
        if not re.match(r"index [0-9a-f]+\.\.[0-9a-f]+", head):
            continue
        body = part.split("\ndiff --git ", 1)[0]
        idx = body.find(f"+++ b/{filename}")
        if idx < 0:
            continue
        nl = body.find("\n", idx)
        return body[nl + 1 :] if nl >= 0 else None
    return None


def apply_patch(base: str, section: str) -> str:
    lines = base.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    hunks = list(re.finditer(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\n", section, re.M))
    for i, hm in enumerate(hunks):
        old_start = int(hm.group(1)) - 1
        body_start = hm.end()
        body_end = hunks[i + 1].start() if i + 1 < len(hunks) else len(section)
        body = section[body_start:body_end]
        cursor = old_start
        for raw in body.splitlines(keepends=True):
            if not raw:
                continue
            if raw.startswith("+"):
                lines.insert(cursor, raw[1:])
                cursor += 1
            elif raw.startswith("-"):
                if cursor < len(lines):
                    del lines[cursor]
            elif raw.startswith(" "):
                cursor += 1
    return "".join(lines)


def iter_str_replaces(name: str) -> list[tuple[str, str]]:
    ops: list[tuple[str, str]] = []
    files = [TRANSCRIPT / "d20b07f3-c2ce-4650-84a8-9bbe0131014c.jsonl"]
    sub = TRANSCRIPT / "subagents"
    if sub.is_dir():
        files.extend(sorted(sub.glob("*.jsonl")))
    for fp in files:
        for line in fp.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            for block in (row.get("message") or {}).get("content") or []:
                if block.get("type") != "tool_use" or block.get("name") != "StrReplace":
                    continue
                path = str((block.get("input") or {}).get("path") or "")
                if not path.lower().endswith(name.lower()):
                    continue
                inp = block.get("input") or {}
                old, new = inp.get("old_string"), inp.get("new_string")
                if old is not None and new is not None:
                    ops.append((old, new))
    return ops


def main() -> int:
    text = DIFF.read_text(encoding="utf-8", errors="replace")
    base = extract_new_file_block(text, "market_autotrader.py")
    if not base:
        print("no base block", file=sys.stderr)
        return 1
    mod = extract_modify_section(text, "market_autotrader.py")
    if not mod:
        print("no modify section", file=sys.stderr)
        return 1
    patched = apply_patch(base, mod)
    applied = missed = 0
    for old, new in iter_str_replaces("market_autotrader.py"):
        if old in patched:
            patched = patched.replace(old, new, 1)
            applied += 1
        else:
            missed += 1
    if "from exit_engine import apply_holding_priority_signal" not in patched:
        patched = patched.replace(
            "def run_once(config: dict[str, Any]",
            "from exit_engine import apply_holding_priority_signal\n\n\ndef run_once(config: dict[str, Any]",
            1,
        )
    if "CASH_USDT_FUTURES" not in patched:
        patched = patched.replace(
            'BINANCE_FUTURES_BASE = "https://fapi.binance.com"',
            'CASH_USDT_FUTURES = "USDT"\nBINANCE_FUTURES_BASE = "https://fapi.binance.com"',
            1,
        )
    TARGET.write_text(patched, encoding="utf-8")
    print(f"base={len(base.splitlines())} final={len(patched.splitlines())} strreplace applied={applied} missed={missed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
