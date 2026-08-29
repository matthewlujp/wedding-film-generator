from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from wedding_film.openai_adapter import (
    API_URL,
    DEFAULT_IMAGE_DETAIL,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_STORE,
)

SECRET = "sk-test-do-not-leak-0123456789"


def run_cli(
    *args: str, env: dict[str, str | None] | None = None
) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("wedding-film")
    merged = {
        **os.environ,
        "WEDDING_FILM_OPENAI_STUB_TRANSPORT": "1",
        "OPENAI_API_KEY": SECRET,
        **(env or {}),
    }
    resolved_env = {key: value for key, value in merged.items() if value is not None}
    return subprocess.run(
        [str(executable), *args],
        check=False,
        capture_output=True,
        text=True,
        env=resolved_env,
    )


def configured_workspace(tmp_path: Path, *, model: str) -> tuple[Path, str]:
    workspace = tmp_path / "openai-vision-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    config = workspace / "project.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "name: none\n    model: none", f"name: openai\n    model: {model}", 1
        ),
        encoding="utf-8",
    )
    materials = workspace / "materials"
    materials.mkdir()
    Image.new("RGB", (12, 8), (20, 40, 60)).save(materials / "asset.png")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    record = json.loads((workspace / "catalog.jsonl").read_text(encoding="utf-8"))
    return workspace, record["asset_id"]


def _vision_event(workspace: Path) -> dict[str, object]:
    run_file = max(
        (workspace / "runs" / "analysis").glob("vision-*.jsonl"),
        key=lambda path: path.stat().st_mtime_ns,
    )
    return next(
        event
        for event in map(json.loads, run_file.read_text(encoding="utf-8").splitlines())
        if event["type"] == "asset_stage"
    )


def test_default_settings_match_the_specified_launch_configuration() -> None:
    assert DEFAULT_MODEL == "gpt-5.6-luna"
    assert DEFAULT_REASONING_EFFORT == "low"
    assert DEFAULT_MAX_OUTPUT_TOKENS == 800
    assert DEFAULT_IMAGE_DETAIL == "high"
    assert DEFAULT_STORE is False
    assert API_URL == "https://api.openai.com/v1/responses"


def test_success_records_provenance_usage_settings_and_no_secrets(tmp_path: Path) -> None:
    workspace, asset_id = configured_workspace(tmp_path, model="stub-success")

    result = run_cli("--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id)

    assert result.returncode == 0, result.stderr
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr
    record = json.loads((workspace / "catalog.jsonl").read_text(encoding="utf-8"))
    assert set(record["inferences"]) == {
        "description", "wedding_moment", "subject_roles", "setting", "mood",
        "shot_type", "quality_flags",
    }
    assert {claim["confidence"] for claim in record["inferences"].values()} <= {
        0.95, 0.75, 0.5, 0.25
    }
    run_id = record["inferences"]["description"]["run_id"]
    run = record["runs"][run_id]
    assert run["kind"] == "vision"
    assert run["adapter"] == "openai"
    assert run["provider"] == "openai"
    assert run["version"] == "1"
    assert run["model"] == "stub-success"
    assert run["settings"]["parameters"] == {
        "reasoning_effort": "low",
        "max_output_tokens": 800,
        "image_detail": "high",
        "store": False,
    }
    run_file_text = (
        next((workspace / "runs" / "analysis").glob("vision-*.jsonl")).read_text(
            encoding="utf-8"
        )
    )
    assert SECRET not in run_file_text
    events = [
        json.loads(line)
        for line in run_file_text.splitlines()
    ]
    success = next(event for event in events if event["type"] == "asset_stage")
    assert success["usage"] == {
        "input_tokens": 912, "output_tokens": 143, "total_tokens": 1055
    }
    assert success["provider_metadata"] == {
        "response_model": "stub-success",
        "response_id": "resp_stub_success",
    }

    reused = run_cli("--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id)
    assert reused.returncode == 0
    assert "reused=1" in reused.stdout


def test_missing_api_key_maps_to_authentication_failure(tmp_path: Path) -> None:
    workspace, asset_id = configured_workspace(tmp_path, model="stub-success")

    result = run_cli(
        "--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id,
        env={"OPENAI_API_KEY": None},
    )

    assert result.returncode == 1
    assert "VISION_ADAPTER_FAILURE" in result.stderr
    failure = _vision_event(workspace)
    assert failure["failure_category"] == "authentication"
    assert failure["retryable"] is False


@pytest.mark.parametrize(
    ("model", "category", "retryable"),
    [
        ("stub-refusal", "refusal", False),
        ("stub-invalid-schema", "unsupported_schema", False),
        ("stub-auth-failure", "authentication", False),
        ("stub-rate-limited", "rate_limited", True),
        ("stub-unavailable", "provider_unavailable", True),
        ("stub-token-limit", "invalid_response", False),
        ("stub-connection-error", "provider_unavailable", True),
        ("stub-malformed", "invalid_response", False),
    ],
)
def test_failure_scenarios_map_to_the_stable_failure_policy(
    tmp_path: Path, model: str, category: str, retryable: bool
) -> None:
    workspace, asset_id = configured_workspace(tmp_path, model=model)
    before = (workspace / "catalog.jsonl").read_bytes()

    result = run_cli("--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id)

    assert result.returncode == 1
    expected_code = "VISION_ADAPTER_REFUSAL" if category == "refusal" else "VISION_ADAPTER_FAILURE"
    assert expected_code in result.stderr
    assert "Traceback" not in result.stderr
    assert (workspace / "catalog.jsonl").read_bytes() == before
    failure = _vision_event(workspace)
    assert failure["failure_category"] == category
    assert failure["retryable"] is retryable
    assert isinstance(failure["usage"], dict)


def test_rate_limit_retry_after_is_captured_for_batch_backoff(tmp_path: Path) -> None:
    workspace, asset_id = configured_workspace(tmp_path, model="stub-rate-limited")

    run_cli("--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id)

    failure = _vision_event(workspace)
    assert failure["provider_metadata"]["retry_after_seconds"] == 1.0


def test_analyze_batch_retries_retryable_openai_failures_then_reports_failed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "openai-batch-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    config = workspace / "project.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "name: none\n    model: none", "name: openai\n    model: stub-unavailable", 1
        ),
        encoding="utf-8",
    )
    materials = workspace / "materials"
    materials.mkdir()
    Image.new("RGB", (12, 8), (5, 5, 5)).save(materials / "asset.png")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0

    result = run_cli(
        "--project", str(workspace), "catalog", "analyze-batch", "--concurrency", "1"
    )

    assert result.returncode == 2, result.stdout
    assert "succeeded=0 reused=0 failed=1" in result.stdout
    events = [
        json.loads(line)
        for line in next((workspace / "runs" / "analysis").glob("vision-batch-*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    failure = next(event for event in events if event["type"] == "asset_stage")
    assert failure["attempt"] == 3


@pytest.mark.skipif(
    os.environ.get("WEDDING_FILM_OPENAI_LIVE_PILOT") != "1" or not os.environ.get(
        "OPENAI_API_KEY"
    ),
    reason=(
        "opt-in cost-approved pilot; set WEDDING_FILM_OPENAI_LIVE_PILOT=1 and "
        "OPENAI_API_KEY to run it against the real OpenAI Responses API"
    ),
)
def test_live_pilot_records_real_compatibility_and_usage_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "openai-live-pilot"
    live_env: dict[str, str | None] = {"WEDDING_FILM_OPENAI_STUB_TRANSPORT": None}
    assert run_cli(
        "--project", str(workspace), "project", "init", env=live_env
    ).returncode == 0
    config = workspace / "project.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "name: none\n    model: none", f"name: openai\n    model: {DEFAULT_MODEL}", 1
        ),
        encoding="utf-8",
    )
    materials = workspace / "materials"
    materials.mkdir()
    Image.new("RGB", (12, 8), (200, 180, 160)).save(materials / "asset.png")
    assert run_cli(
        "--project", str(workspace), "catalog", "scan", env=live_env
    ).returncode == 0
    record = json.loads((workspace / "catalog.jsonl").read_text(encoding="utf-8"))
    asset_id = record["asset_id"]

    result = run_cli(
        "--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id, env=live_env
    )

    assert result.returncode == 0, result.stderr
    updated = json.loads((workspace / "catalog.jsonl").read_text(encoding="utf-8"))
    run_id = updated["inferences"]["description"]["run_id"]
    run = updated["runs"][run_id]
    assert run["adapter"] == "openai"
    assert run["settings"]["analysis_input"]["sha256"].startswith("sha256:")
