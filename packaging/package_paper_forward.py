"""Build the BTCUSDT forward-paper archive from this checkout.

The archive carries the paper runners and only the tests those modules can
satisfy. A release build extracts the archive into a clean directory and runs
``python -m unittest discover -s tests`` there.
"""

from __future__ import annotations

import hashlib
import io
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_FILES = (
    "paper_forward.py",
    "paper_feed.py",
    "paper_ws.py",
    "paper_alt_forward.py",
    "research_backtest.py",
    "research_data.py",
    "research_calendar.py",
    "research_alt_validation.py",
    "research_leverage_study.py",
    "research_swing_study.py",
    "leverage_policy.py",
    "strategy_candidates.py",
    "paper_forward.example.json",
    "paper_forward.dynamic.example.json",
    "paper_forward.alts.example.json",
    "research_backtest.example.json",
    "leverage_study.example.json",
    "requirements-paper.txt",
    "packaging/package_paper_forward.py",
    "packaging/README-PAPER.md",
    "fixtures/research/btcusdt_perp_synthetic.csv",
    "fixtures/research/aapl_cash_synthetic.csv",
    "fixtures/research/spy_cash_synthetic.csv",
    "fixtures/research/qqq_cash_synthetic.csv",
)
PACKAGE_TESTS = (
    "tests/test_feed_reconnect.py",
    "tests/test_paper_forward.py",
    "tests/test_paper_alt_forward.py",
    "tests/test_alt_validation.py",
    "tests/test_swing_walkforward.py",
    "tests/test_leverage_policy.py",
    "tests/test_research_backtest.py",
    "tests/test_research_data.py",
)


def build_archive(destination: Path, root: Path | None = None) -> Path:
    source = root or ROOT
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest_lines = []
    with tarfile.open(destination, "w:gz") as archive:
        for relative in PACKAGE_FILES + PACKAGE_TESTS:
            path = source / relative
            data = path.read_bytes()
            manifest_lines.append(f"{hashlib.sha256(data).hexdigest()}  {relative}")
            archive.add(path, arcname=f"paper-forward-btcusdt/{relative}")
        readme = (source / "packaging" / "README-PAPER.md").read_bytes()
        manifest_lines.append(f"{hashlib.sha256(readme).hexdigest()}  README-PAPER.md")
        info = tarfile.TarInfo("paper-forward-btcusdt/README-PAPER.md")
        info.size = len(readme)
        archive.addfile(info, io.BytesIO(readme))
        manifest = ("\n".join(manifest_lines) + "\n").encode("utf-8")
        info = tarfile.TarInfo("paper-forward-btcusdt/MANIFEST.sha256")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
    return destination


def verify_archive(archive: Path) -> int:
    """Extract into an empty directory and run the packaged tests."""
    with tempfile.TemporaryDirectory(prefix="paper-package-") as tmp:
        root = Path(tmp) / "extract"
        with tarfile.open(archive, "r:gz") as packed:
            packed.extractall(root)
        package = root / "paper-forward-btcusdt"
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
            cwd=package,
            capture_output=True,
            text=True,
            check=False,
        )
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    if completed.returncode != 0:
        raise RuntimeError("extracted paper package tests failed")
    match = re.search(r"Ran (\d+) tests", completed.stderr)
    if match is None:
        raise RuntimeError("could not read the extracted test count")
    return int(match.group(1))


def main() -> int:
    destination = ROOT / "artifacts" / "paper-forward" / "paper-forward-btcusdt.tar.gz"
    build_archive(destination)
    count = verify_archive(destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    sums = destination.parent / "SHA256SUMS"
    sums.write_text(f"{digest}  paper-forward-btcusdt.tar.gz\n", encoding="utf-8")
    print(f"extracted_tests {count}")
    print(f"{digest}  {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
