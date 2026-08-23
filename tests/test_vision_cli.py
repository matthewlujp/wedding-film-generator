from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageCms


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("wedding-film")
    return subprocess.run(
        [str(executable), *args],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def configured_workspace(tmp_path: Path, *, model: str = "fixture-v1") -> tuple[Path, str]:
    workspace = tmp_path / "vision-project"
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
    Image.new("RGB", (12, 8), (20, 40, 60)).save(materials / "asset.png")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    record = json.loads((workspace / "catalog.jsonl").read_text(encoding="utf-8"))
    return workspace, record["asset_id"]


def test_analyze_one_asset_merges_complete_fake_snapshot_with_provenance(tmp_path: Path) -> None:
    workspace, asset_id = configured_workspace(tmp_path)

    result = run_cli(
        "--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id
    )

    assert result.returncode == 0, result.stderr
    assert "VISION_ANALYSIS_COMPLETED" in result.stdout
    record = json.loads((workspace / "catalog.jsonl").read_text(encoding="utf-8"))
    assert set(record["inferences"]) == {
        "description", "wedding_moment", "subject_roles", "setting", "mood",
        "shot_type", "quality_flags",
    }
    assert all(0 <= claim["confidence"] <= 1 for claim in record["inferences"].values())
    run_id = record["inferences"]["description"]["run_id"]
    run = record["runs"][run_id]
    assert run["kind"] == "vision"
    assert run["adapter"] == "fake"
    assert run["provider"] == "deterministic-fake"
    assert run["model"] == "fixture-v1"
    assert run["fingerprint"].startswith("sha256:")
    derivative = run["settings"]["analysis_input"]
    assert derivative == {
        "sha256": derivative["sha256"],
        "pixel_width": 12,
        "pixel_height": 8,
        "media_type": "image/jpeg",
        "recipe_version": "analysis-input-jpeg-v1",
    }
    assert derivative["sha256"].startswith("sha256:")
    events = [
        json.loads(line)
        for line in next((workspace / "runs" / "analysis").glob("vision-*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    success = next(event for event in events if event["type"] == "asset_stage")
    assert success["usage"] == {"input_images": 1, "output_fields": 7}
    assert success["derivative_sha256"] == derivative["sha256"]
    assert list((workspace / ".work" / "candidates").iterdir()) == []
    assert hashlib.sha256((workspace / "materials" / "asset.png").read_bytes()).hexdigest() == (
        asset_id.removeprefix("sha256:")
    )


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


def test_analysis_input_is_oriented_srgb_metadata_free_and_deterministic(tmp_path: Path) -> None:
    derivatives: list[dict[str, object]] = []
    for index, metadata in enumerate(("private-a", "private-b")):
        workspace = tmp_path / f"derivative-{index}"
        assert run_cli("--project", str(workspace), "project", "init").returncode == 0
        config = workspace / "project.yaml"
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                "name: none\n    model: none", "name: fake\n    model: fixture-v1", 1
            ), encoding="utf-8"
        )
        materials = workspace / "materials"
        materials.mkdir()
        source = materials / "oriented.png"
        image = Image.new("RGBA", (3000, 1500), (255, 0, 0, 0))
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = metadata
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        image.save(source, exif=exif, icc_profile=profile)
        assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
        asset_id = json.loads((workspace / "catalog.jsonl").read_text())["asset_id"]
        result = run_cli(
            "--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id
        )
        assert result.returncode == 0, result.stderr
        record = json.loads((workspace / "catalog.jsonl").read_text())
        run_id = record["inferences"]["description"]["run_id"]
        derivatives.append(record["runs"][run_id]["settings"]["analysis_input"])
        assert list((workspace / ".work" / "candidates").iterdir()) == []

    assert derivatives[0] == derivatives[1]
    assert derivatives[0]["pixel_width"] == 1024
    assert derivatives[0]["pixel_height"] == 2048


def test_identical_contract_reuses_and_model_change_is_stale(tmp_path: Path) -> None:
    workspace, asset_id = configured_workspace(tmp_path)
    first = run_cli("--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id)
    assert first.returncode == 0
    first_record = json.loads((workspace / "catalog.jsonl").read_text())
    first_run_id = first_record["inferences"]["description"]["run_id"]

    reused = run_cli("--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id)
    assert reused.returncode == 0
    assert "reused=1" in reused.stdout
    assert json.loads((workspace / "catalog.jsonl").read_text()) == first_record
    assert _vision_event(workspace)["outcome"] == "skipped"

    config = workspace / "project.yaml"
    config.write_text(config.read_text().replace("model: fixture-v1", "model: fixture-v2"))
    stale = run_cli("--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id)
    assert stale.returncode == 0, stale.stderr
    changed = json.loads((workspace / "catalog.jsonl").read_text())
    assert changed["inferences"]["description"]["run_id"] != first_run_id
    assert first_run_id not in changed["runs"]


def test_invalid_complete_candidate_and_refusal_merge_nothing(tmp_path: Path) -> None:
    cases = {
        "fixture-refusal": "VISION_ADAPTER_REFUSAL",
        "fixture-incomplete": "VISION_CANDIDATE_INCOMPLETE",
        "fixture-invalid-enum": "VISION_CANDIDATE_INVALID",
        "fixture-invalid-confidence": "VISION_CANDIDATE_CONFIDENCE",
        "fixture-empty": "VISION_CANDIDATE_INCOMPLETE",
        "fixture-malformed-claim": "VISION_CANDIDATE_SCHEMA",
        "fixture-invalid-type": "VISION_CANDIDATE_INVALID",
    }
    for model, expected_code in cases.items():
        workspace, asset_id = configured_workspace(tmp_path / model, model=model)
        before = (workspace / "catalog.jsonl").read_bytes()
        result = run_cli(
            "--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id
        )
        assert result.returncode == 1
        assert expected_code in result.stderr
        assert "Traceback" not in result.stderr
        assert (workspace / "catalog.jsonl").read_bytes() == before
        assert list((workspace / ".work" / "candidates").iterdir()) == []
        failure = _vision_event(workspace)
        assert failure["attempt"] == 1
        assert failure["outcome"] == "permanent_failure"
        assert isinstance(failure["usage"], dict)


def test_decode_failure_is_recorded_after_analysis_run_starts(tmp_path: Path) -> None:
    workspace = tmp_path / "decode-failure"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    config = workspace / "project.yaml"
    config.write_text(
        config.read_text().replace(
            "name: none\n    model: none", "name: fake\n    model: fixture-v1", 1
        )
    )
    materials = workspace / "materials"
    materials.mkdir()
    (materials / "broken.jpg").write_bytes(b"not-an-image")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    catalog = workspace / "catalog.jsonl"
    asset_id = json.loads(catalog.read_text())["asset_id"]
    before = catalog.read_bytes()

    result = run_cli(
        "--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id
    )

    assert result.returncode == 1
    assert "VISION_INPUT_INVALID" in result.stderr
    assert catalog.read_bytes() == before
    failure = _vision_event(workspace)
    assert failure["error_code"] == "VISION_INPUT_INVALID"
    assert failure["outcome"] == "permanent_failure"
    assert list((workspace / ".work" / "candidates").iterdir()) == []


def test_adapter_failure_preserves_category_retryability_usage_and_metadata(
    tmp_path: Path,
) -> None:
    workspace, asset_id = configured_workspace(tmp_path, model="fixture-transient-failure")
    result = run_cli(
        "--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id
    )

    assert result.returncode == 1
    assert "VISION_ADAPTER_FAILURE" in result.stderr
    failure = _vision_event(workspace)
    assert failure["outcome"] == "transient_failure"
    assert failure["failure_category"] == "provider_unavailable"
    assert failure["retryable"] is True
    assert failure["usage"] == {"input_images": 1}
    assert failure["provider_metadata"] == {"fixture": "fixture-transient-failure"}


def test_cleanup_failure_is_recorded_and_prevents_catalog_publish(tmp_path: Path) -> None:
    workspace, asset_id = configured_workspace(tmp_path)
    catalog = workspace / "catalog.jsonl"
    before = catalog.read_bytes()

    result = run_cli(
        "--project",
        str(workspace),
        "catalog",
        "analyze",
        "--asset-id",
        asset_id,
        env={"WEDDING_FILM_TEST_FAIL_DERIVATIVE_CLEANUP": "1"},
    )

    assert result.returncode == 1
    assert "VISION_INPUT_CLEANUP_FAILED" in result.stderr
    assert catalog.read_bytes() == before
    failure = _vision_event(workspace)
    assert failure["error_code"] == "VISION_INPUT_CLEANUP_FAILED"
    assert failure["retryable"] is False
    derivatives = list((workspace / ".work" / "candidates").glob(".analysis-input-*.jpg"))
    assert len(derivatives) == 1
    derivatives[0].unlink()


def test_null_removes_field_empty_lists_complete_and_unrelated_catalog_data_survives(
    tmp_path: Path,
) -> None:
    workspace, asset_id = configured_workspace(tmp_path)
    assert run_cli("--project", str(workspace), "catalog", "extract").returncode == 0
    assert run_cli(
        "--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id
    ).returncode == 0
    catalog = workspace / "catalog.jsonl"
    record = json.loads(catalog.read_text())
    record["corrections"] = [{
        "target": "/inferences/mood", "op": "set", "value": ["reviewed"],
        "at": "2026-08-23T10:00:00+08:00", "actor": "editor",
    }]
    catalog.write_text(json.dumps(record, separators=(",", ":")) + "\n")
    config = workspace / "project.yaml"
    config.write_text(config.read_text().replace("model: fixture-v1", "model: fixture-nulls"))

    result = run_cli(
        "--project", str(workspace), "catalog", "analyze", "--asset-id", asset_id
    )

    assert result.returncode == 0, result.stderr
    updated = json.loads(catalog.read_text())
    assert "setting" not in updated["inferences"]
    assert updated["inferences"]["quality_flags"]["value"] == []
    assert updated["corrections"] == record["corrections"]
    assert updated["observations"] == record["observations"]
    extraction_run_ids = {
        run_id for run_id, run in updated["runs"].items() if run["kind"] == "extraction"
    }
    assert extraction_run_ids == {
        claim["run_id"] for claim in updated["observations"].values()
    }
