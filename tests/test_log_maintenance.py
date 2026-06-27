"""Tests for bounded runtime log maintenance."""

from __future__ import annotations

import time
from pathlib import Path

from scripts.maintain_logs import DEFAULT_TRUNCATE_LOGS, maintain_pipeline, tail_truncate


def test_tail_truncate_keeps_recent_bytes(tmp_path: Path):
    path = tmp_path / "market-autotrader.stdout.log"
    path.write_bytes(b"a" * 100 + b"recent")

    result = tail_truncate(path, max_bytes=50, keep_bytes=10, dry_run=False)

    assert result and "truncated" in result
    assert path.read_bytes() == b"aaaarecent"


def test_maintain_pipeline_compresses_old_handoffs_but_keeps_latest_pointer(tmp_path: Path):
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir()
    old_file = pipeline / "20260101-010101-abcdef12-poller.json"
    old_file.write_text("{}")
    latest = pipeline / "latest-poller.json"
    latest.write_text("{}")
    old_ts = time.time() - 5 * 86400
    old_file.touch(times=(old_ts, old_ts))
    latest.touch(times=(old_ts, old_ts))

    actions = maintain_pipeline(
        pipeline,
        keep_days=1,
        keep_latest=0,
        compressed_keep_days=7,
        dry_run=False,
    )

    assert any("compressed" in item for item in actions)
    assert not old_file.exists()
    assert old_file.with_suffix(".json.gz").exists()
    assert latest.exists()


def test_maintain_pipeline_ignores_unknown_state_like_files(tmp_path: Path):
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir()
    unknown = pipeline / "latest-risk-state.json"
    unknown.write_text("{}")
    old_ts = time.time() - 20 * 86400
    unknown.touch(times=(old_ts, old_ts))

    actions = maintain_pipeline(
        pipeline,
        keep_days=1,
        keep_latest=0,
        compressed_keep_days=1,
        dry_run=False,
    )

    assert actions == []
    assert unknown.exists()


def test_root_jsonl_growth_logs_are_registered_for_truncation():
    assert "market-autotrader-live-decisions.jsonl" in DEFAULT_TRUNCATE_LOGS
    assert "shadow-blocked-counterfactual.jsonl" in DEFAULT_TRUNCATE_LOGS
