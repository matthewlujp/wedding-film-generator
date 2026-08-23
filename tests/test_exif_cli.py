from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("wedding-film")
    return subprocess.run([str(executable), *args], check=False, capture_output=True, text=True)


def write_jpeg(path: Path, tags: dict[int, object]) -> None:
    exif = Image.Exif()
    for tag, value in tags.items():
        exif[tag] = value
    Image.new("RGB", (3, 2), (12, 34, 56)).save(path, format="JPEG", exif=exif)


def test_extract_records_basic_image_observations_and_preserves_source(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "private-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    source = materials / "pixel.png"
    source.write_bytes(PNG_1X1)
    os.utime(source, (946684800, 946684800))
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0

    result = run_cli("--project", str(workspace), "catalog", "extract")

    assert result.returncode == 0, result.stderr
    assert "EXIF_EXTRACTION_COMPLETED" in result.stdout
    record = json.loads((workspace / "catalog.jsonl").read_text(encoding="utf-8"))
    run_id = next(iter(record["runs"]))
    assert record["observations"] == {
        "format": {"value": "PNG", "run_id": run_id},
        "media_type": {"value": "image/png", "run_id": run_id},
        "pixel_height": {"value": 1, "run_id": run_id},
        "pixel_width": {"value": 1, "run_id": run_id},
    }
    assert record["runs"][run_id]["kind"] == "extraction"
    assert record["runs"][run_id]["tool"] == "pillow-exif"
    assert record["runs"][run_id]["outcome"] == "success"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    run_files = list((workspace / "runs" / "analysis").glob("*.jsonl"))
    assert len(run_files) == 1
    events = [json.loads(line) for line in run_files[0].read_text().splitlines()]
    assert events[0]["type"] == "command"
    assert events[1]["outcome"] == "succeeded"
    assert events[1]["run_id"] == run_id
    assert events[1]["source_tags"] == {}


def test_extract_normalizes_allowed_exif_with_time_precedence_and_gps_pair(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "metadata-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    source = materials / "metadata.jpg"
    write_jpeg(
        source,
        {
            271: "  Camera Corp  ",
            272: "  Model One  ",
            274: 6,
            306: "2022:03:04 05:06:07",
            36880: "+01:00",
            36867: "2024:05:06 07:08:09",
            36881: "+08:00",
            36868: "2023:04:05 06:07:08",
            36882: "+09:00",
            34853: {
                1: "N",
                2: (1.0, 30.0, 0.0),
                3: "E",
                4: (103.0, 45.0, 0.0),
                5: 0,
                6: 12.5,
            },
        },
    )
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0

    result = run_cli("--project", str(workspace), "catalog", "extract")

    assert result.returncode == 0, result.stderr
    record = json.loads((workspace / "catalog.jsonl").read_text(encoding="utf-8"))
    values = {name: claim["value"] for name, claim in record["observations"].items()}
    assert values == {
        "media_type": "image/jpeg",
        "format": "JPEG",
        "pixel_width": 3,
        "pixel_height": 2,
        "orientation": 6,
        "capture_time": "2024-05-06T07:08:09+08:00",
        "camera_make": "Camera Corp",
        "camera_model": "Model One",
        "location": {"latitude": 1.5, "longitude": 103.75, "altitude": 12.5},
    }
    event = json.loads(
        (next((workspace / "runs" / "analysis").glob("*.jsonl")))
        .read_text(encoding="utf-8")
        .splitlines()[1]
    )
    assert event["source_tags"]["DateTimeOriginal"] == "2024:05:06 07:08:09"
    assert event["source_tags"]["OffsetTimeOriginal"] == "+08:00"
    assert event["source_tags"]["DateTimeDigitized"] == "2023:04:05 06:07:08"
    assert event["source_tags"]["DateTime"] == "2022:03:04 05:06:07"
    assert event["warnings"] == []
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_extract_discards_malformed_fields_independently(tmp_path: Path) -> None:
    workspace = tmp_path / "malformed-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    source = materials / "malformed.jpg"
    write_jpeg(
        source,
        {
            271: " Valid Make ",
            274: 9,
            36867: "not-a-date",
            36881: "+08:00",
            34853: {1: "N", 2: (1.0, 2.0, 3.0)},
        },
    )
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0

    result = run_cli("--project", str(workspace), "catalog", "extract")

    assert result.returncode == 0, result.stderr
    record = json.loads((workspace / "catalog.jsonl").read_text(encoding="utf-8"))
    values = {name: claim["value"] for name, claim in record["observations"].items()}
    assert values["camera_make"] == "Valid Make"
    assert "orientation" not in values
    assert "capture_time" not in values
    assert "location" not in values
    event = json.loads(
        (next((workspace / "runs" / "analysis").glob("*.jsonl")))
        .read_text(encoding="utf-8")
        .splitlines()[1]
    )
    assert [warning["field"] for warning in event["warnings"]] == [
        "orientation",
        "capture_time",
        "location",
    ]
    assert event["source_tags"]["Orientation"] == 9
    assert event["source_tags"]["DateTimeOriginal"] == "not-a-date"


def test_capture_time_uses_only_a_corresponding_offset(tmp_path: Path) -> None:
    workspace = tmp_path / "offset-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    write_jpeg(
        materials / "offset.jpg",
        {
            36867: "2024:05:06 07:08:09",
            36868: "2023:04:05 06:07:08",
            36882: "-04:30",
            36880: "+11:00",
        },
    )
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0

    result = run_cli("--project", str(workspace), "catalog", "extract")

    assert result.returncode == 0, result.stderr
    record = json.loads((workspace / "catalog.jsonl").read_text(encoding="utf-8"))
    assert record["observations"]["capture_time"]["value"] == ("2023-04-05T06:07:08-04:30")


def test_decode_failure_is_partial_and_successful_checkpoints_resume_deterministically(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "partial-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    valid = materials / "valid.png"
    invalid = materials / "invalid.jpg"
    valid.write_bytes(PNG_1X1)
    invalid.write_bytes(b"not an image")
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (valid, invalid)}
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0

    first = run_cli("--project", str(workspace), "catalog", "extract")

    assert first.returncode == 2, first.stderr
    assert "failed=1" in first.stdout
    catalog_after_first = (workspace / "catalog.jsonl").read_bytes()
    records = [json.loads(line) for line in catalog_after_first.splitlines()]
    valid_record = next(
        record for record in records if record["locators"] == ["materials/valid.png"]
    )
    invalid_record = next(
        record for record in records if record["locators"] == ["materials/invalid.jpg"]
    )
    assert valid_record["observations"]["media_type"]["value"] == "image/png"
    assert "observations" not in invalid_record
    first_run = next((workspace / "runs" / "analysis").glob("*.jsonl"))
    first_run_bytes = first_run.read_bytes()
    attempts = [
        event
        for event in map(json.loads, first_run.read_text(encoding="utf-8").splitlines())
        if event["type"] == "asset_stage"
    ]
    failure = next(event for event in attempts if event["outcome"] == "permanent_failure")
    assert failure["error_code"] == "image_decode_failed"
    assert failure["retryable"] is False
    assert failure["started_at"].endswith("Z")
    assert failure["ended_at"].endswith("Z")

    second = run_cli("--project", str(workspace), "catalog", "extract")

    assert second.returncode == 2
    assert "reused=1" in second.stdout
    assert (workspace / "catalog.jsonl").read_bytes() == catalog_after_first
    assert first_run.read_bytes() == first_run_bytes
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before} == before


def test_extract_rejects_an_analysis_run_directory_outside_the_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "unsafe-run-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    (materials / "pixel.png").write_bytes(PNG_1X1)
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    catalog_before = (workspace / "catalog.jsonl").read_bytes()
    run_directory = workspace / "runs" / "analysis"
    run_directory.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    run_directory.symlink_to(outside, target_is_directory=True)

    result = run_cli("--project", str(workspace), "catalog", "extract")

    assert result.returncode == 1
    assert "ANALYSIS_RUN_INVALID_ARTIFACT" in result.stderr
    assert list(outside.iterdir()) == []
    assert (workspace / "catalog.jsonl").read_bytes() == catalog_before
