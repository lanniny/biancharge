#!/usr/bin/env python3
"""Bound runtime log growth without touching trading state."""

from __future__ import annotations

import argparse
import gzip
import os
import re
import time
from pathlib import Path


DEFAULT_TRUNCATE_LOGS = (
    "market-autotrader.stdout.log",
    "market-autotrader.stderr.log",
    "market-autotrader-runner.log",
    "market-autotrader-live-decisions.jsonl",
    "shadow-blocked-counterfactual.jsonl",
)

HANDOFF_RE = re.compile(
    r"^\d{8}-\d{6}-[0-9a-fA-F]+-(poller|decisions|execution)\.json(\.gz)?$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-dir", default="logs")
    parser.add_argument("--pipeline-dir", default="logs/pipeline")
    parser.add_argument("--max-log-mb", type=int, default=64)
    parser.add_argument("--keep-log-mb", type=int, default=16)
    parser.add_argument("--pipeline-keep-days", type=float, default=2)
    parser.add_argument("--pipeline-keep-latest", type=int, default=160)
    parser.add_argument("--compressed-keep-days", type=float, default=7)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def tail_truncate(path: Path, *, max_bytes: int, keep_bytes: int, dry_run: bool) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    size = path.stat().st_size
    if size <= max_bytes:
        return None
    if dry_run:
        return f"truncate {path} {size}->{keep_bytes}"
    with path.open("rb") as fh:
        fh.seek(max(0, size - keep_bytes))
        data = fh.read()
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return f"truncated {path} {size}->{len(data)}"


def compress_file(path: Path, *, dry_run: bool) -> str:
    target = path.with_suffix(path.suffix + ".gz")
    if target.exists():
        return f"skip {path}; archive already exists"
    if dry_run:
        return f"compress {path} -> {target}"
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
    os.replace(tmp, target)
    path.unlink()
    return f"compressed {path} -> {target}"


def maintain_pipeline(
    pipeline_dir: Path,
    *,
    keep_days: float,
    keep_latest: int,
    compressed_keep_days: float,
    dry_run: bool,
) -> list[str]:
    if not pipeline_dir.exists():
        return []
    now = time.time()
    files = [p for p in pipeline_dir.iterdir() if p.is_file() and HANDOFF_RE.match(p.name)]
    uncompressed = sorted(
        [p for p in files if not p.name.endswith(".gz")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    keep_set = set(uncompressed[: max(0, keep_latest)])
    actions: list[str] = []
    old_cutoff = now - keep_days * 86400
    for path in uncompressed:
        if path in keep_set or path.stat().st_mtime >= old_cutoff:
            continue
        actions.append(compress_file(path, dry_run=dry_run))

    gz_cutoff = now - compressed_keep_days * 86400
    for path in files:
        if not path.name.endswith(".gz") or path.stat().st_mtime >= gz_cutoff:
            continue
        if dry_run:
            actions.append(f"delete {path}")
        else:
            path.unlink()
            actions.append(f"deleted {path}")
    return actions


def main() -> int:
    args = parse_args()
    logs_dir = Path(args.logs_dir)
    max_bytes = max(1, args.max_log_mb) * 1024 * 1024
    keep_bytes = max(1, min(args.keep_log_mb, args.max_log_mb)) * 1024 * 1024
    actions: list[str] = []
    for name in DEFAULT_TRUNCATE_LOGS:
        result = tail_truncate(logs_dir / name, max_bytes=max_bytes, keep_bytes=keep_bytes, dry_run=args.dry_run)
        if result:
            actions.append(result)
    actions.extend(
        maintain_pipeline(
            Path(args.pipeline_dir),
            keep_days=args.pipeline_keep_days,
            keep_latest=args.pipeline_keep_latest,
            compressed_keep_days=args.compressed_keep_days,
            dry_run=args.dry_run,
        )
    )
    if actions:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        print(f"[{stamp}] log maintenance: " + "; ".join(actions[:50]))
        if len(actions) > 50:
            print(f"[{stamp}] log maintenance: ... {len(actions) - 50} more actions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
