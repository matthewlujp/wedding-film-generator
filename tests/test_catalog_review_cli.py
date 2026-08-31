from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("wedding-film")
    return subprocess.run(
        [str(executable), *args], check=False, capture_output=True, text=True
    )


def _seed_workspace(workspace: Path) -> dict[str, str]:
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    (materials / "a.jpg").write_bytes(b"privacy-safe-photo-a")
    (materials / "b.jpg").write_bytes(b"privacy-safe-photo-b")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    catalog = workspace / "catalog.jsonl"
    records = [json.loads(line) for line in catalog.read_text(encoding="utf-8").splitlines()]
    asset_ids: dict[str, str] = {}
    for record in records:
        if record["locators"] == ["materials/a.jpg"]:
            asset_ids["a"] = record["asset_id"]
            record["runs"] = {
                "extract-1": {
                    "kind": "extraction",
                    "tool": "privacy-safe-extractor",
                    "version": "1",
                    "executed_at": "2026-08-23T10:00:00Z",
                    "outcome": "success",
                },
                "vision-1": {
                    "kind": "vision",
                    "provider": "deterministic-fake",
                    "model": "fixture-v1",
                    "prompt_version": "v1",
                    "settings": {},
                    "executed_at": "2026-08-23T10:01:00Z",
                    "outcome": "success",
                },
            }
            record["observations"] = {
                "media_type": {"value": "image/jpeg", "run_id": "extract-1"},
                "camera_make": {"value": "Fixture Camera", "run_id": "extract-1"},
            }
            record["inferences"] = {
                "mood": {"value": ["calm"], "confidence": 0.2, "run_id": "vision-1"},
                "wedding_moment": {"value": "ceremony", "confidence": 0.9, "run_id": "vision-1"},
            }
        else:
            asset_ids["b"] = record["asset_id"]
    records.sort(key=lambda record: record["asset_id"])
    catalog.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
    return asset_ids


def test_list_orders_deterministically_and_filters_by_low_confidence(tmp_path: Path) -> None:
    workspace = tmp_path / "list-project"
    asset_ids = _seed_workspace(workspace)

    result = run_cli("--project", str(workspace), "catalog", "list", "--json")
    payload = json.loads(result.stdout)

    assert result.returncode == 0
    assert [entry["asset_id"] for entry in payload] == sorted(asset_ids.values())

    low_confidence = run_cli(
        "--project", str(workspace), "catalog", "list", "--low-confidence", "0.5", "--json"
    )
    low_payload = json.loads(low_confidence.stdout)
    assert [entry["asset_id"] for entry in low_payload] == [asset_ids["a"]]

    unmatched = run_cli(
        "--project", str(workspace), "catalog", "list", "--low-confidence", "0.1", "--json"
    )
    assert json.loads(unmatched.stdout) == []


def test_list_filters_by_locator_glob_and_storyboard_membership(tmp_path: Path) -> None:
    workspace = tmp_path / "locator-project"
    asset_ids = _seed_workspace(workspace)

    by_locator = run_cli(
        "--project", str(workspace), "catalog", "list", "--locator", "materials/a.*", "--json"
    )
    assert json.loads(by_locator.stdout) == [
        entry
        for entry in json.loads(
            run_cli("--project", str(workspace), "catalog", "list", "--json").stdout
        )
        if entry["asset_id"] == asset_ids["a"]
    ]

    no_storyboard = run_cli(
        "--project", str(workspace), "catalog", "list", "--in-storyboard", "--json"
    )
    assert no_storyboard.returncode == 0
    assert json.loads(no_storyboard.stdout) == []


def test_show_resolves_by_asset_id_and_by_locator_and_separates_layers(tmp_path: Path) -> None:
    workspace = tmp_path / "show-project"
    asset_ids = _seed_workspace(workspace)

    by_id = run_cli(
        "--project", str(workspace), "catalog", "show", "--asset", asset_ids["a"], "--json"
    )
    by_locator = run_cli(
        "--project", str(workspace), "catalog", "show", "--asset", "materials/a.jpg", "--json"
    )
    assert by_id.returncode == 0
    assert json.loads(by_id.stdout) == json.loads(by_locator.stdout)

    payload = json.loads(by_id.stdout)
    assert payload["observations"]["media_type"]["value"] == "image/jpeg"
    assert payload["inferences"]["mood"]["value"] == ["calm"]
    assert payload["inferences"]["mood"]["confidence"] == 0.2
    assert payload["corrections"] == []
    assert payload["effective"]["/observations/media_type"]["source"] == "observation"
    assert payload["effective"]["/inferences/mood"]["source"] == "inference"
    assert "/observations/format" not in payload["effective"]

    not_found = run_cli(
        "--project", str(workspace), "catalog", "show", "--asset", "materials/missing.jpg"
    )
    assert not_found.returncode == 1
    assert "CATALOG_ASSET_NOT_FOUND" in not_found.stderr


def test_correct_set_and_remove_record_history_and_resolve_effective_value(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "correct-project"
    asset_ids = _seed_workspace(workspace)

    first_set = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/inferences/mood", "--value", '["joyful"]',
        "--asset-id", asset_ids["a"], "--actor", "editor", "--reason", "reviewed",
    )
    second_set = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/inferences/mood", "--value", '["excited"]',
        "--asset-id", asset_ids["a"], "--actor", "editor",
    )
    assert first_set.returncode == 0
    assert second_set.returncode == 0

    shown = json.loads(
        run_cli(
            "--project", str(workspace), "catalog", "show", "--asset", asset_ids["a"], "--json"
        ).stdout
    )
    assert [correction["value"] for correction in shown["corrections"]] == [
        ["joyful"],
        ["excited"],
    ]
    assert shown["effective"]["/inferences/mood"]["value"] == ["excited"]
    assert shown["effective"]["/inferences/mood"]["source"] == "correction"
    assert shown["inferences"]["mood"]["value"] == ["calm"]

    removed = run_cli(
        "--project", str(workspace), "catalog", "correct", "remove",
        "--target", "/observations/camera_make",
        "--asset-id", asset_ids["a"], "--actor", "editor",
    )
    assert removed.returncode == 0
    after_remove = json.loads(
        run_cli(
            "--project", str(workspace), "catalog", "show", "--asset", asset_ids["a"], "--json"
        ).stdout
    )
    assert after_remove["observations"]["camera_make"]["value"] == "Fixture Camera"
    assert after_remove["effective"]["/observations/camera_make"]["present"] is False
    assert after_remove["effective"]["/observations/camera_make"]["source"] == "correction-removed"


def test_correct_rejects_forbidden_targets(tmp_path: Path) -> None:
    workspace = tmp_path / "forbidden-project"
    asset_ids = _seed_workspace(workspace)

    for target in ("/asset_id", "/locators", "/runs", "/corrections", "/byte_size"):
        result = run_cli(
            "--project", str(workspace), "catalog", "correct", "set",
            "--target", target, "--value", '"x"',
            "--asset-id", asset_ids["a"], "--actor", "editor",
        )
        assert result.returncode == 1
        assert "CATALOG_CORRECTION_INVALID" in result.stderr

    catalog_after = (workspace / "catalog.jsonl").read_text(encoding="utf-8")
    assert "corrections" not in catalog_after or json.loads(
        catalog_after.splitlines()[0]
    ).get("corrections", []) == []


def test_correct_requires_explicit_selection_and_validates_it_before_mutation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "selection-project"
    asset_ids = _seed_workspace(workspace)
    prior = (workspace / "catalog.jsonl").read_bytes()

    empty_selection = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/inferences/mood", "--value", '["x"]', "--actor", "editor",
    )
    assert empty_selection.returncode == 1
    assert "CATALOG_SELECTION_EMPTY" in empty_selection.stderr

    unknown_asset = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/inferences/mood", "--value", '["x"]',
        "--asset-id", "sha256:" + "0" * 64, "--asset-id", asset_ids["a"],
        "--actor", "editor",
    )
    assert unknown_asset.returncode == 1
    assert "CATALOG_ASSET_NOT_FOUND" in unknown_asset.stderr

    overbroad_glob = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/inferences/mood", "--value", '["x"]',
        "--locator", "materials/does-not-exist-*.jpg", "--actor", "editor",
    )
    assert overbroad_glob.returncode == 1
    assert "CATALOG_LOCATOR_SELECTION_EMPTY" in overbroad_glob.stderr

    assert (workspace / "catalog.jsonl").read_bytes() == prior


def test_correct_dry_run_reports_without_mutating_and_atomically_applies_multi_asset(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "dry-run-project"
    asset_ids = _seed_workspace(workspace)
    prior = (workspace / "catalog.jsonl").read_bytes()

    dry_run = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/inferences/mood", "--value", '["excited"]',
        "--locator", "materials/*.jpg", "--actor", "editor", "--dry-run",
    )
    assert dry_run.returncode == 0
    assert "CATALOG_CORRECTION_PLAN" in dry_run.stdout
    assert f"resolved={len(asset_ids)}" in dry_run.stdout
    assert (workspace / "catalog.jsonl").read_bytes() == prior

    applied = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/inferences/mood", "--value", '["excited"]',
        "--locator", "materials/*.jpg", "--actor", "editor",
    )
    assert applied.returncode == 0
    assert f"applied={len(asset_ids)}" in applied.stdout

    for asset_id in asset_ids.values():
        record = json.loads(
            run_cli(
                "--project", str(workspace), "catalog", "show", "--asset", asset_id, "--json"
            ).stdout
        )
        assert record["corrections"][-1]["value"] == ["excited"]
        assert record["corrections"][-1]["actor"] == "editor"


def test_correct_rejects_invalid_value_type_without_writing(tmp_path: Path) -> None:
    workspace = tmp_path / "invalid-value-project"
    asset_ids = _seed_workspace(workspace)
    prior = (workspace / "catalog.jsonl").read_bytes()

    bad_shot_type = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/inferences/shot_type", "--value", '"panoramic"',
        "--asset-id", asset_ids["a"], "--actor", "editor",
    )
    assert bad_shot_type.returncode == 1
    assert "CATALOG_FIELD_VALUE" in bad_shot_type.stderr
    assert (workspace / "catalog.jsonl").read_bytes() == prior

    malformed_json = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/observations/camera_make", "--value", "not-json",
        "--asset-id", asset_ids["a"], "--actor", "editor",
    )
    assert malformed_json.returncode == 1
    assert "CATALOG_VALUE_INVALID" in malformed_json.stderr
    assert (workspace / "catalog.jsonl").read_bytes() == prior


def test_correct_requires_non_empty_actor(tmp_path: Path) -> None:
    workspace = tmp_path / "actor-project"
    asset_ids = _seed_workspace(workspace)

    result = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/inferences/mood", "--value", '["x"]',
        "--asset-id", asset_ids["a"], "--actor", "   ",
    )
    assert result.returncode == 1
    assert "CATALOG_CORRECTION_INVALID" in result.stderr


def test_correct_serializes_deterministically_and_preserves_key_order(tmp_path: Path) -> None:
    workspace = tmp_path / "serialize-project"
    asset_ids = _seed_workspace(workspace)

    run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/subject_attributions", "--value", '["principal-one"]',
        "--asset-id", asset_ids["b"], "--actor", "editor",
    )

    lines = (workspace / "catalog.jsonl").read_text(encoding="utf-8").splitlines()
    ids = [json.loads(line)["asset_id"] for line in lines]
    assert ids == sorted(ids)
    for line in lines:
        record = json.loads(line)
        assert list(record.keys())[:4] == ["schema_version", "asset_id", "byte_size", "locators"]
        if "corrections" in record:
            for correction in record["corrections"]:
                assert list(correction.keys())[:2] == ["target", "op"]
