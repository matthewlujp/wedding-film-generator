from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("wedding-film")
    return subprocess.run(
        [str(executable), *args], check=False, capture_output=True, text=True
    )


def test_scan_catalogs_nested_duplicate_materials_without_modifying_them(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "private-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    (materials / "ceremony").mkdir(parents=True)
    (materials / "portraits").mkdir()
    first = materials / "ceremony" / "first.jpg"
    duplicate = materials / "portraits" / "duplicate.jpg"
    second = materials / "portraits" / "second.png"
    first.write_bytes(b"privacy-safe-photo-a")
    duplicate.write_bytes(b"privacy-safe-photo-a")
    second.write_bytes(b"privacy-safe-photo-b")
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (first, duplicate, second)
    }

    result = run_cli("--project", str(workspace), "catalog", "scan")

    assert result.returncode == 0, result.stderr
    assert "CATALOG_SCANNED" in result.stdout
    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before
    } == before
    catalog_bytes = (workspace / "catalog.jsonl").read_bytes()
    assert catalog_bytes == (
        b'{"schema_version":1,"asset_id":"sha256:44a50725737bd1d92a1c5bfa50acada3b17caa709e78118fc11583c2d205187a","byte_size":20,"locators":["materials/ceremony/first.jpg","materials/portraits/duplicate.jpg"]}\n'
        b'{"schema_version":1,"asset_id":"sha256:74a4dd1c0fa80f0b84bbd85635dc774cebdb8caada967632f4d81904126e5058","byte_size":20,"locators":["materials/portraits/second.png"]}\n'
    )

    unchanged = run_cli("--project", str(workspace), "catalog", "scan")
    status = run_cli("--project", str(workspace), "status", "--json")

    assert unchanged.returncode == 0
    assert (workspace / "catalog.jsonl").read_bytes() == catalog_bytes
    catalog_status = json.loads(status.stdout)["layers"]["semantic_catalog"]
    assert catalog_status["state"] == "ready"
    assert catalog_status["reasons"][0]["code"] == "SEMANTIC_CATALOG_VALID"


def test_rescan_tracks_content_and_preserves_current_record_history(tmp_path: Path) -> None:
    workspace = tmp_path / "changing-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    stable = materials / "stable.jpg"
    removed = materials / "removed.jpg"
    changed = materials / "changed.jpg"
    stable.write_bytes(b"privacy-safe-photo-a")
    removed.write_bytes(b"remove-me")
    changed.write_bytes(b"old-content")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    records = [
        json.loads(line)
        for line in (workspace / "catalog.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    stable_record = next(
        record
        for record in records
        if record["locators"] == ["materials/stable.jpg"]
    )
    stable_record["corrections"] = [
        {
            "target": "/inferences/mood",
            "op": "set",
            "value": ["joyful"],
            "at": "2026-08-23T10:00:00+08:00",
            "actor": "editor",
            "reason": "reviewed locally",
        }
    ]
    records.sort(key=lambda record: record["asset_id"])
    (workspace / "catalog.jsonl").write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
    nested = materials / "nested"
    nested.mkdir()
    stable.rename(nested / "moved.jpg")
    removed.unlink()
    changed.write_bytes(b"new-content")
    (materials / "added.jpg").write_bytes(b"added-content")

    result = run_cli("--project", str(workspace), "catalog", "scan")

    assert result.returncode == 0, result.stderr
    rescanned = [
        json.loads(line)
        for line in (workspace / "catalog.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rescanned) == 3
    preserved = next(
        record
        for record in rescanned
        if record["asset_id"]
        == "sha256:44a50725737bd1d92a1c5bfa50acada3b17caa709e78118fc11583c2d205187a"
    )
    assert preserved["locators"] == ["materials/nested/moved.jpg"]
    assert preserved["corrections"] == stable_record["corrections"]
    assert {record["asset_id"] for record in rescanned} == {
        "sha256:42b8cc383b0a1ea4fc9b5ff967d743af7274a52ddfe07cac62487e30f00fa505",
        "sha256:44a50725737bd1d92a1c5bfa50acada3b17caa709e78118fc11583c2d205187a",
        "sha256:7a2fdaefa256f1a593e0303d6b1e3576766d3754be47eff945e59b1e63fdda14",
    }
    assert all(
        "removed.jpg" not in locator
        for record in rescanned
        for locator in record["locators"]
    )


def test_scan_rejects_symlinks_and_preserves_the_prior_catalog(tmp_path: Path) -> None:
    workspace = tmp_path / "unsafe-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    (materials / "safe.jpg").write_bytes(b"privacy-safe-photo-a")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    catalog = workspace / "catalog.jsonl"
    prior = catalog.read_bytes()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"private-outside-material")
    (materials / "escape.jpg").symlink_to(outside)

    result = run_cli("--project", str(workspace), "catalog", "scan")

    assert result.returncode == 1
    assert "MATERIALS_SYMLINK_UNSUPPORTED" in result.stderr
    assert catalog.read_bytes() == prior
    assert outside.read_bytes() == b"private-outside-material"


def test_status_rejects_structural_provenance_and_integrity_catalog_failures(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "invalid-catalog"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    source = materials / "asset.jpg"
    source.write_bytes(b"privacy-safe-photo-a")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    catalog = workspace / "catalog.jsonl"
    valid = json.loads(catalog.read_text(encoding="utf-8"))

    invalid_cases: list[tuple[object, str]] = [
        ({**valid, "unknown": "field"}, "CATALOG_UNKNOWN_FIELD"),
        ({**valid, "schema_version": "1"}, "CATALOG_FIELD_TYPE"),
        ({**valid, "asset_id": "sha256:ABC"}, "CATALOG_ASSET_ID_INVALID"),
        ({**valid, "locators": ["../outside.jpg"]}, "CATALOG_LOCATOR_INVALID"),
        ({**valid, "byte_size": None}, "CATALOG_NULL_FORBIDDEN"),
        (
            {
                **valid,
                "inferences": {
                    "mood": {"value": ["joyful"], "confidence": 0.75, "run_id": "missing"}
                },
            },
            "CATALOG_PROVENANCE_DANGLING",
        ),
    ]
    for invalid, expected_code in invalid_cases:
        catalog.write_text(
            json.dumps(invalid, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        status = run_cli("--project", str(workspace), "status", "--json")
        fact = json.loads(status.stdout)["layers"]["semantic_catalog"]
        assert status.returncode == 1
        assert fact["state"] == "invalid"
        assert fact["reasons"][0]["code"] == expected_code

    catalog.write_text(json.dumps(valid, separators=(",", ":")) + "\n", encoding="utf-8")
    source.write_bytes(b"changed-after-catalog")
    integrity = run_cli("--project", str(workspace), "status", "--json")
    fact = json.loads(integrity.stdout)["layers"]["semantic_catalog"]
    assert fact["state"] == "stale"
    assert fact["reasons"][0]["code"] == "CATALOG_SCAN_REQUIRED"


def test_status_rejects_duplicate_records_and_scan_preserves_invalid_prior(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "duplicate-catalog"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    (materials / "asset.jpg").write_bytes(b"privacy-safe-photo-a")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    catalog = workspace / "catalog.jsonl"
    line = catalog.read_text(encoding="utf-8")
    catalog.write_text(line + line, encoding="utf-8")
    prior = catalog.read_bytes()

    status = run_cli("--project", str(workspace), "status", "--json")
    scan = run_cli("--project", str(workspace), "catalog", "scan")

    assert json.loads(status.stdout)["layers"]["semantic_catalog"]["reasons"][0]["code"] == (
        "CATALOG_ASSET_DUPLICATE"
    )
    assert scan.returncode == 1
    assert "CATALOG_ASSET_DUPLICATE" in scan.stderr
    assert catalog.read_bytes() == prior


def test_status_rejects_non_strict_jsonl_and_locator_duplicates(tmp_path: Path) -> None:
    workspace = tmp_path / "strict-catalog"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    (materials / "asset.jpg").write_bytes(b"privacy-safe-photo-a")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    catalog = workspace / "catalog.jsonl"
    valid = catalog.read_text(encoding="utf-8").strip()
    duplicate_field = valid.replace(
        '"schema_version":1,', '"schema_version":1,"schema_version":1,'
    )
    invalid_cases = [
        (duplicate_field + "\n", "CATALOG_DUPLICATE_FIELD"),
        (valid, "CATALOG_JSONL_INVALID"),
        ("[]\n", "CATALOG_FIELD_TYPE"),
        (
            valid.replace(
                '["materials/asset.jpg"]',
                '["materials/asset.jpg","materials/asset.jpg"]',
            )
            + "\n",
            "CATALOG_LOCATOR_DUPLICATE",
        ),
    ]

    for contents, expected_code in invalid_cases:
        catalog.write_text(contents, encoding="utf-8")
        result = run_cli("--project", str(workspace), "status", "--json")
        fact = json.loads(result.stdout)["layers"]["semantic_catalog"]
        assert result.returncode == 1
        assert fact["state"] == "invalid"
        assert fact["reasons"][0]["code"] == expected_code


def test_rescan_can_publish_a_valid_empty_current_state(tmp_path: Path) -> None:
    workspace = tmp_path / "emptied-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    source = materials / "only.jpg"
    source.write_bytes(b"privacy-safe-photo-a")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    source.unlink()

    scan = run_cli("--project", str(workspace), "catalog", "scan")
    status = run_cli("--project", str(workspace), "status", "--json")

    assert scan.returncode == 0
    assert (workspace / "catalog.jsonl").read_bytes() == b""
    assert json.loads(status.stdout)["layers"]["semantic_catalog"]["state"] == "ready"


def test_scan_requires_a_valid_initialized_project_and_materials(tmp_path: Path) -> None:
    uninitialized = tmp_path / "uninitialized"
    (uninitialized / "materials").mkdir(parents=True)
    (uninitialized / "materials" / "asset.jpg").write_bytes(b"privacy-safe-photo-a")

    no_project = run_cli("--project", str(uninitialized), "catalog", "scan")

    assert no_project.returncode == 1
    assert "CONFIG_MISSING" in no_project.stderr
    assert not (uninitialized / "catalog.jsonl").exists()

    initialized = tmp_path / "missing-materials"
    assert run_cli("--project", str(initialized), "project", "init").returncode == 0

    no_materials = run_cli("--project", str(initialized), "catalog", "scan")

    assert no_materials.returncode == 1
    assert "MATERIALS_MISSING" in no_materials.stderr
    assert not (initialized / "catalog.jsonl").exists()


def test_status_requires_rescan_for_every_complete_materials_manifest_change(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "freshness-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    original = materials / "original.jpg"
    original.write_bytes(b"privacy-safe-photo-a")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    initial = json.loads(run_cli("--project", str(workspace), "status", "--json").stdout)
    initial_hash = initial["layers"]["semantic_catalog"]["upstream_hashes"]["materials"]

    duplicate = materials / "duplicate.jpg"
    duplicate.write_bytes(b"privacy-safe-photo-a")
    added_duplicate = run_cli("--project", str(workspace), "status", "--json")
    duplicate_fact = json.loads(added_duplicate.stdout)["layers"]["semantic_catalog"]
    assert duplicate_fact["state"] == "stale"
    assert duplicate_fact["reasons"][0]["code"] == "CATALOG_SCAN_REQUIRED"
    assert duplicate_fact["next_commands"] == [
        f"wedding-film --project {workspace} catalog scan"
    ]
    assert duplicate_fact["upstream_hashes"]["materials"] != initial_hash

    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    added = materials / "added.jpg"
    added.write_bytes(b"added-content")
    added_fact = json.loads(
        run_cli("--project", str(workspace), "status", "--json").stdout
    )["layers"]["semantic_catalog"]
    assert added_fact["state"] == "stale"

    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    nested = materials / "nested"
    nested.mkdir()
    added.rename(nested / "moved.jpg")
    moved_fact = json.loads(
        run_cli("--project", str(workspace), "status", "--json").stdout
    )["layers"]["semantic_catalog"]
    assert moved_fact["state"] == "stale"

    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    original.write_bytes(b"changed-content")
    changed_fact = json.loads(
        run_cli("--project", str(workspace), "status", "--json").stdout
    )["layers"]["semantic_catalog"]
    assert changed_fact["state"] == "stale"

    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    duplicate.unlink()
    removed_fact = json.loads(
        run_cli("--project", str(workspace), "status", "--json").stdout
    )["layers"]["semantic_catalog"]
    assert removed_fact["state"] == "stale"


def test_rescan_preserves_append_ordered_corrections(tmp_path: Path) -> None:
    workspace = tmp_path / "correction-order"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    (materials / "asset.jpg").write_bytes(b"privacy-safe-photo-a")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    catalog = workspace / "catalog.jsonl"
    record = json.loads(catalog.read_text(encoding="utf-8"))
    authored = [
        {
            "target": "/inferences/mood",
            "op": "set",
            "value": ["joyful"],
            "at": "2026-08-23T11:00:00+08:00",
            "actor": "editor",
        },
        {
            "target": "/inferences/mood",
            "op": "set",
            "value": ["calm"],
            "at": "2026-08-23T10:00:00+08:00",
            "actor": "editor",
        },
    ]
    record["corrections"] = authored
    catalog.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")

    result = run_cli("--project", str(workspace), "catalog", "scan")

    assert result.returncode == 0, result.stderr
    assert json.loads(catalog.read_text(encoding="utf-8"))["corrections"] == authored


def test_status_requires_complete_successful_claim_provenance(tmp_path: Path) -> None:
    workspace = tmp_path / "provenance-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    (materials / "asset.jpg").write_bytes(b"privacy-safe-photo-a")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    catalog = workspace / "catalog.jsonl"
    base = json.loads(catalog.read_text(encoding="utf-8"))
    base["observations"] = {
        "media_type": {"value": "image/jpeg", "run_id": "extract-1"}
    }

    incomplete = {**base, "runs": {"extract-1": {}}}
    catalog.write_text(
        json.dumps(incomplete, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    invalid = run_cli("--project", str(workspace), "status", "--json")
    invalid_fact = json.loads(invalid.stdout)["layers"]["semantic_catalog"]
    assert invalid_fact["state"] == "invalid"
    assert invalid_fact["reasons"][0]["code"] == "CATALOG_PROVENANCE_INVALID"

    extraction = {
        "kind": "extraction",
        "tool": "privacy-safe-extractor",
        "version": "1",
        "executed_at": "2026-08-23T10:00:00+08:00",
    }
    complete_without_outcome = {**base, "runs": {"extract-1": extraction}}
    catalog.write_text(
        json.dumps(complete_without_outcome, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    missing_outcome = run_cli("--project", str(workspace), "status", "--json")
    missing_fact = json.loads(missing_outcome.stdout)["layers"]["semantic_catalog"]
    assert missing_fact["state"] == "invalid"
    assert missing_fact["reasons"][0]["code"] == "CATALOG_PROVENANCE_INVALID"

    for outcome in ("failed", "partial"):
        non_success = {
            **base,
            "runs": {"extract-1": {**extraction, "outcome": outcome}},
        }
        catalog.write_text(
            json.dumps(non_success, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        rejected = run_cli("--project", str(workspace), "status", "--json")
        rejected_fact = json.loads(rejected.stdout)["layers"]["semantic_catalog"]
        assert rejected_fact["state"] == "invalid"
        assert rejected_fact["reasons"][0]["code"] == "CATALOG_PROVENANCE_INVALID"

    base["runs"] = {
        "extract-1": {
            **extraction,
            "outcome": "success",
        },
        "vision-1": {
            "kind": "vision",
            "provider": "deterministic-fake",
            "model": "fixture-v1",
            "prompt_version": "v1",
            "settings": {},
            "executed_at": "2026-08-23T10:01:00+08:00",
            "outcome": "success",
        },
    }
    base["inferences"] = {
        "description": {
            "value": "Two people outdoors",
            "confidence": 0.75,
            "run_id": "vision-1",
        }
    }
    catalog.write_text(json.dumps(base, separators=(",", ":")) + "\n", encoding="utf-8")
    valid = run_cli("--project", str(workspace), "status", "--json")
    assert json.loads(valid.stdout)["layers"]["semantic_catalog"]["state"] == "ready"


def test_status_rejects_mistyped_observation_and_correction_values(tmp_path: Path) -> None:
    workspace = tmp_path / "typed-values"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    (materials / "asset.jpg").write_bytes(b"privacy-safe-photo-a")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    catalog = workspace / "catalog.jsonl"
    base = json.loads(catalog.read_text(encoding="utf-8"))
    base["runs"] = {
        "extract-1": {
            "kind": "extraction",
            "tool": "privacy-safe-extractor",
            "version": "1",
            "executed_at": "2026-08-23T10:00:00+08:00",
            "outcome": "success",
        }
    }
    invalid_observations: list[tuple[str, object]] = [
        ("media_type", 42),
        ("format", []),
        ("capture_time", "not-a-timestamp"),
        ("camera_make", ""),
        ("location", {"latitude": "north", "longitude": 10.0}),
        ("location", {"latitude": 91.0, "longitude": 10.0}),
        ("location", {"latitude": 10**400, "longitude": 10.0}),
    ]
    for name, value in invalid_observations:
        invalid = {
            **base,
            "observations": {name: {"value": value, "run_id": "extract-1"}},
        }
        catalog.write_text(
            json.dumps(invalid, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        result = run_cli("--project", str(workspace), "status", "--json")
        fact = json.loads(result.stdout)["layers"]["semantic_catalog"]
        assert fact["state"] == "invalid"
        assert fact["reasons"][0]["code"] in {
            "CATALOG_FIELD_TYPE",
            "CATALOG_FIELD_VALUE",
        }

    invalid_correction = {
        **base,
        "corrections": [
            {
                "target": "/observations/orientation",
                "op": "set",
                "value": "upright",
                "at": "2026-08-23T10:00:00+08:00",
                "actor": "editor",
            }
        ],
    }
    catalog.write_text(
        json.dumps(invalid_correction, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    correction = run_cli("--project", str(workspace), "status", "--json")
    correction_fact = json.loads(correction.stdout)["layers"]["semantic_catalog"]
    assert correction_fact["state"] == "invalid"
    assert correction_fact["reasons"][0]["code"] == "CATALOG_FIELD_TYPE"

    huge_confidence = {
        **base,
        "runs": {
            **base["runs"],
            "vision-1": {
                "kind": "vision",
                "provider": "deterministic-fake",
                "model": "fixture-v1",
                "prompt_version": "v1",
                "settings": {},
                "executed_at": "2026-08-23T10:01:00+08:00",
                "outcome": "success",
            },
        },
        "inferences": {
            "description": {
                "value": "A privacy-safe fixture",
                "confidence": 10**400,
                "run_id": "vision-1",
            }
        },
    }
    catalog.write_text(
        json.dumps(huge_confidence, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    huge = run_cli("--project", str(workspace), "status", "--json")
    huge_fact = json.loads(huge.stdout)["layers"]["semantic_catalog"]
    assert huge_fact["state"] == "invalid"
    assert huge_fact["reasons"][0]["code"] == "CATALOG_FIELD_VALUE"
    assert "Traceback" not in huge.stderr


def test_status_accepts_supported_observation_and_correction_values(tmp_path: Path) -> None:
    workspace = tmp_path / "supported-values"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    (materials / "asset.jpg").write_bytes(b"privacy-safe-photo-a")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    catalog = workspace / "catalog.jsonl"
    record = json.loads(catalog.read_text(encoding="utf-8"))
    record["runs"] = {
        "extract-1": {
            "kind": "extraction",
            "tool": "privacy-safe-extractor",
            "version": "1",
            "executed_at": "2026-08-23T10:00:00+08:00",
            "outcome": "success",
        }
    }
    observation_values: dict[str, object] = {
        "media_type": "image/jpeg",
        "format": "JPEG",
        "pixel_width": 1920,
        "pixel_height": 1080,
        "orientation": 1,
        "capture_time": "2026-08-23T10:00:00+08:00",
        "camera_make": "Privacy Safe Camera",
        "camera_model": "Fixture 1",
        "location": {"latitude": 1.3521, "longitude": 103.8198, "altitude": 15.0},
    }
    record["observations"] = {
        name: {"value": value, "run_id": "extract-1"}
        for name, value in observation_values.items()
    }
    record["corrections"] = [
        {
            "target": "/observations/location",
            "op": "set",
            "value": {"latitude": 1.3, "longitude": 103.8},
            "at": "2026-08-23T11:00:00+08:00",
            "actor": "editor",
        },
        {
            "target": "/inferences/shot_type",
            "op": "set",
            "value": "wide",
            "at": "2026-08-23T11:01:00+08:00",
            "actor": "editor",
        },
        {
            "target": "/subject_attributions",
            "op": "set",
            "value": ["principal-one", "principal-two"],
            "at": "2026-08-23T11:02:00+08:00",
            "actor": "editor",
        },
    ]
    catalog.write_text(
        json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8"
    )

    status = run_cli("--project", str(workspace), "status", "--json")

    assert json.loads(status.stdout)["layers"]["semantic_catalog"]["state"] == "ready"
