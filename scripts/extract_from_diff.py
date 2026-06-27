#!/usr/bin/env python3
"""Extract post-diff file content from unified diff hunks (+ and context lines)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

DIFF = Path(
    r"C:\Users\16643\.cursor\projects\c-Users-16643-Documents-bi-an"
    r"\agent-tools\63ccca43-2039-4678-8d92-33b69359584e.txt"
)
OUT = Path(r"C:\Users\16643\Documents\bi an\_market_autotrader_from_diff.py")


def extract_new_file(diff_text: str, filename: str) -> str:
    pattern = rf"diff --git a/{re.escape(filename)} b/{re.escape(filename)}\n(.*?)(?=\ndiff --git |\Z)"
    match = re.search(pattern, diff_text, re.S)
    if not match:
        raise SystemExit(f"no section: {filename}")
    section = match.group(1)
    out_lines: list[str] = []
    for line in section.splitlines():
        if line.startswith("+++ ") or line.startswith("--- ") or line.startswith("@@"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            out_lines.append(line[1:])
        elif line.startswith(" "):
            out_lines.append(line[1:])
        # skip '-' lines (old only)
    return "\n".join(out_lines) + "\n"


def main() -> int:
    text = DIFF.read_text(encoding="utf-8", errors="replace")
    content = extract_new_file(text, "market_autotrader.py")
    OUT.write_text(content, encoding="utf-8")
    print(f"Wrote {OUT} lines={len(content.splitlines())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
