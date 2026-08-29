from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("wedding-film")
    return subprocess.run(
        [str(executable), *args],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def configured_workspace(
    tmp_path: Path, *, model: str = "fixture-v1", asset_count: int = 1
) -> tuple[Path, list[str]]:
    workspace = tmp_path / "vision-batch-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    config = workspace / "project.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "name: none\n    model: none", f"name: fake\n    model: {model}", 1
        ),
        encoding="utf-8",
    )
    materials = workspace / "materials"
    materials.mkdir()
    for index in range(asset_count):
        Image.new("RGB", (12, 8), (10 * index, 40, 60)).save(materials / f"asset-{index}.png")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    records = [
        json.loads(line)
        for line in (workspace / "catalog.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    asset_ids = sorted(record["asset_id"] for record in records)
    return workspace, asset_ids


def test_dry_run_selects_pending_assets_without_provider_call_or_mutation(tmp_path: Path) -> None:
    workspace, _ = configured_workspace(tmp_path, asset_count=2)

    result = run_cli("--project", str(workspace), "catalog", "analyze-batch", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "VISION_BATCH_PLAN" in result.stdout
    assert "selected=2" in result.stdout
    assert "max_assets=100" in result.stdout
    assert "max_estimated_usd=1.00" in result.stdout
    assert "concurrency=5" in result.stdout
    assert list((workspace / ".work" / "candidates").iterdir()) == []
    assert list((workspace / "runs" / "analysis").iterdir()) == []
    records = [
        json.loads(line)
        for line in (workspace / "catalog.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert all("inferences" not in record for record in records)


def test_batch_analyzes_and_reuses_on_second_run(tmp_path: Path) -> None:
    workspace, asset_ids = configured_workspace(tmp_path, asset_count=2)

    first = run_cli("--project", str(workspace), "catalog", "analyze-batch")
    assert first.returncode == 0, first.stderr
    assert "succeeded=2 reused=0 failed=0" in first.stdout

    second = run_cli(
        "--project", str(workspace), "catalog", "analyze-batch", "--asset-id", asset_ids[0]
    )
    assert second.returncode == 0, second.stderr
    assert "succeeded=0 reused=1 failed=0" in second.stdout

    records = [
        json.loads(line)
        for line in (workspace / "catalog.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert all(len(record["inferences"]) == 7 for record in records)


def test_permanent_failure_does_not_retry_and_exits_partial(tmp_path: Path) -> None:
    workspace, _ = configured_workspace(tmp_path, model="fixture-refusal", asset_count=1)

    result = run_cli("--project", str(workspace), "catalog", "analyze-batch")

    assert result.returncode == 2, result.stdout
    assert "succeeded=0 reused=0 failed=1" in result.stdout
    events = [
        json.loads(line)
        for line in next((workspace / "runs" / "analysis").glob("vision-batch-*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    failure = next(event for event in events if event["type"] == "asset_stage")
    assert failure["attempt"] == 1
    assert failure["outcome"] == "permanent_failure"


def test_flaky_transient_failure_retries_then_succeeds(tmp_path: Path) -> None:
    workspace, _ = configured_workspace(tmp_path, model="fixture-flaky", asset_count=1)

    result = run_cli("--project", str(workspace), "catalog", "analyze-batch")

    assert result.returncode == 0, result.stdout
    assert "succeeded=1 reused=0 failed=0" in result.stdout
    events = [
        json.loads(line)
        for line in next((workspace / "runs" / "analysis").glob("vision-batch-*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    success = next(event for event in events if event["type"] == "asset_stage")
    assert success["attempt"] == 3


def test_budget_stop_leaves_remaining_assets_unscheduled(tmp_path: Path) -> None:
    workspace, asset_ids = configured_workspace(tmp_path, asset_count=4)

    result = run_cli(
        "--project",
        str(workspace),
        "catalog",
        "analyze-batch",
        "--max-estimated-usd",
        "0.02",
    )

    assert result.returncode == 2, result.stdout
    assert "succeeded=2 reused=0 failed=0" in result.stdout
    assert "budget_stopped=True" in result.stdout
    records = [
        json.loads(line)
        for line in (workspace / "catalog.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    analyzed = sum(1 for record in records if record.get("inferences"))
    assert analyzed == 2


def test_concurrency_limit_bounds_wall_clock_time(tmp_path: Path) -> None:
    workspace, _ = configured_workspace(tmp_path, model="fixture-slow", asset_count=4)

    start = time.monotonic()
    result = run_cli(
        "--project", str(workspace), "catalog", "analyze-batch", "--concurrency", "4"
    )
    parallel_elapsed = time.monotonic() - start

    assert result.returncode == 0, result.stdout
    assert "succeeded=4 reused=0 failed=0" in result.stdout
    assert parallel_elapsed < 0.45


def test_interruption_preserves_prior_completed_work_and_stops_scheduling(tmp_path: Path) -> None:
    workspace, asset_ids = configured_workspace(tmp_path, asset_count=3)
    config = workspace / "project.yaml"

    first = run_cli(
        "--project", str(workspace), "catalog", "analyze-batch", "--asset-id", asset_ids[0]
    )
    assert first.returncode == 0, first.stderr

    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "model: fixture-v1", "model: fixture-interrupt", 1
        ),
        encoding="utf-8",
    )
    second = run_cli(
        "--project",
        str(workspace),
        "catalog",
        "analyze-batch",
        "--asset-id",
        asset_ids[1],
        "--concurrency",
        "1",
    )
    assert second.returncode == 130

    records = [
        json.loads(line)
        for line in (workspace / "catalog.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    by_id = {record["asset_id"]: record for record in records}
    assert by_id[asset_ids[0]]["inferences"]
    assert "inferences" not in by_id[asset_ids[1]]
