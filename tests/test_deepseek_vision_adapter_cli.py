from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from wedding_film.deepseek_adapter import (
    API_URL,
    DEFAULT_IMAGE_DETAIL,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
)

SECRET = "sk-test-do-not-leak-0123456789"


def run_cli(
    *args: str, env: dict[str, str | None] | None = None
) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("wedding-film")
    merged = {
        **os.environ,
        "WEDDING_FILM_DEEPSEEK_STUB_TRANSPORT": "1",
        "DEEPSEEK_API_KEY": SECRET,
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
    workspace = tmp_path / "deepseek-vision-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    config = workspace / "project.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "name: none\n    model: none", f"name: deepseek\n    model: {model}", 1
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
    assert DEFAULT_MODEL == "deepseek-v4-flash-vision-exp"
    assert DEFAULT_REASONING_EFFORT == "low"
    assert DEFAULT_MAX_OUTPUT_TOKENS == 800
    assert DEFAULT_IMAGE_DETAIL == "high"
    assert API_URL == "https://api.deepseek.com/responses"


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
    run_id = record["inferences"]["description"]["run_id"]
    run = record["runs"][run_id]
    assert run["kind"] == "vision"
    assert run["adapter"] == "deepseek"
    assert run["provider"] == "deepseek"
    assert run["version"] == "1"
    assert run["model"] == "stub-success"
    assert run["settings"]["parameters"] == {
        "reasoning_effort": "low",
        "max_output_tokens": 800,
        "image_detail": "high",
    }
    run_file_text = (
        next((workspace / "runs" / "analysis").glob("vision-*.jsonl")).read_text(
            encoding="utf-8"
        )
    )
    assert SECRET not in run_file_text

    reused = run_cli("--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id)
    assert reused.returncode == 0
    assert "reused=1" in reused.stdout


def test_missing_api_key_maps_to_authentication_failure(tmp_path: Path) -> None:
    workspace, asset_id = configured_workspace(tmp_path, model="stub-success")

    result = run_cli(
        "--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id,
        env={"DEEPSEEK_API_KEY": None},
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
        ("stub-insufficient-balance", "provider_error", False),
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


@pytest.mark.skipif(
    os.environ.get("WEDDING_FILM_DEEPSEEK_LIVE_PILOT") != "1" or not os.environ.get(
        "DEEPSEEK_API_KEY"
    ),
    reason=(
        "opt-in cost-approved pilot; set WEDDING_FILM_DEEPSEEK_LIVE_PILOT=1 and "
        "DEEPSEEK_API_KEY to run it against the real DeepSeek Responses API"
    ),
)
def test_live_pilot_records_real_compatibility_and_usage_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "deepseek-live-pilot"
    live_env: dict[str, str | None] = {"WEDDING_FILM_DEEPSEEK_STUB_TRANSPORT": None}
    assert run_cli(
        "--project", str(workspace), "project", "init", env=live_env
    ).returncode == 0
    config = workspace / "project.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "name: none\n    model: none", f"name: deepseek\n    model: {DEFAULT_MODEL}", 1
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
    assert run["adapter"] == "deepseek"
    assert run["settings"]["analysis_input"]["sha256"].startswith("sha256:")
