"""Build the BTCUSDT forward-paper archive from this checkout."""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_FILES = (
    "paper_forward.py",
    "paper_ws.py",
    "research_backtest.py",
    "research_data.py",
    "research_calendar.py",
    "paper_forward.example.json",
    "paper_forward.dynamic.example.json",
    "leverage_policy.py",
    "strategy_candidates.py",
    "research_leverage_study.py",
    "research_swing_study.py",
    "leverage_study.example.json",
    "requirements-paper.txt",
    "packaging/README-PAPER.md",
)


def build_archive(destination: Path, root: Path | None = None) -> Path:
    source = root or ROOT
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest_lines = []
    with tarfile.open(destination, "w:gz") as archive:
        for relative in PACKAGE_FILES:
            path = source / relative
            data = path.read_bytes()
            manifest_lines.append(f"{hashlib.sha256(data).hexdigest()}  {path.name}")
            archive.add(path, arcname=f"paper-forward-btcusdt/{path.name}")
        manifest = ("\n".join(manifest_lines) + "\n").encode("utf-8")
        info = tarfile.TarInfo("paper-forward-btcusdt/MANIFEST.sha256")
        info.size = len(manifest)
        import io

        archive.addfile(info, io.BytesIO(manifest))
    return destination
