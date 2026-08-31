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
        else:
            asset_ids["b"] = record["asset_id"]
    return asset_ids


def test_participant_add_list_and_update_lifecycle(tmp_path: Path) -> None:
    workspace = tmp_path / "participant-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0

    added = run_cli(
        "--project", str(workspace), "participant", "add",
        "--id", "jane-doe", "--display-name", "Jane Doe", "--role", "Bride", "--principal",
    )
    assert added.returncode == 0
    assert "PARTICIPANT_ADDED" in added.stdout

    listed = json.loads(
        run_cli("--project", str(workspace), "participant", "list", "--json").stdout
    )
    assert listed == [
        {"id": "jane-doe", "display_name": "Jane Doe", "role": "Bride", "principal": True}
    ]

    updated = run_cli(
        "--project", str(workspace), "participant", "update",
        "--id", "jane-doe", "--role", "Officiant", "--no-principal",
    )
    assert updated.returncode == 0
    after_update = json.loads(
        run_cli("--project", str(workspace), "participant", "list", "--json").stdout
    )[0]
    assert after_update["role"] == "Officiant"
    assert after_update["principal"] is False
    assert after_update["display_name"] == "Jane Doe"

    cleared = run_cli(
        "--project", str(workspace), "participant", "update",
        "--id", "jane-doe", "--clear-display-name",
    )
    assert cleared.returncode == 0
    after_clear = json.loads(
        run_cli("--project", str(workspace), "participant", "list", "--json").stdout
    )[0]
    assert after_clear["display_name"] is None

    removed = run_cli("--project", str(workspace), "participant", "remove", "--id", "jane-doe")
    assert removed.returncode == 0
    assert (
        json.loads(run_cli("--project", str(workspace), "participant", "list", "--json").stdout)
        == []
    )


def test_participant_add_rejects_malformed_and_duplicate_ids(tmp_path: Path) -> None:
    workspace = tmp_path / "invalid-ids-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0

    malformed = run_cli("--project", str(workspace), "participant", "add", "--id", "Jane_Doe")
    assert malformed.returncode == 1
    assert "PARTICIPANT_ID_INVALID" in malformed.stderr

    assert (
        run_cli("--project", str(workspace), "participant", "add", "--id", "jane-doe").returncode
        == 0
    )

    duplicate = run_cli("--project", str(workspace), "participant", "add", "--id", "jane-doe")
    assert duplicate.returncode == 1
    assert "PARTICIPANT_DUPLICATE" in duplicate.stderr


def test_participant_update_and_remove_reject_unknown_id(tmp_path: Path) -> None:
    workspace = tmp_path / "unknown-id-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0

    update_missing = run_cli(
        "--project", str(workspace), "participant", "update", "--id", "ghost", "--role", "x"
    )
    assert update_missing.returncode == 1
    assert "PARTICIPANT_NOT_FOUND" in update_missing.stderr

    remove_missing = run_cli(
        "--project", str(workspace), "participant", "remove", "--id", "ghost"
    )
    assert remove_missing.returncode == 1
    assert "PARTICIPANT_NOT_FOUND" in remove_missing.stderr


def test_participants_are_listed_in_deterministic_id_order(tmp_path: Path) -> None:
    workspace = tmp_path / "ordering-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0

    for participant_id in ("zed", "amy", "mid"):
        assert (
            run_cli(
                "--project", str(workspace), "participant", "add", "--id", participant_id
            ).returncode
            == 0
        )

    listed = json.loads(
        run_cli("--project", str(workspace), "participant", "list", "--json").stdout
    )
    assert [entry["id"] for entry in listed] == ["amy", "mid", "zed"]


def test_subject_attribution_set_requires_existing_participants_and_remove_returns_to_unknown(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "attribution-project"
    asset_ids = _seed_workspace(workspace)
    assert (
        run_cli(
            "--project", str(workspace), "participant", "add", "--id", "jane-doe", "--principal"
        ).returncode
        == 0
    )

    unknown_participant = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/subject_attributions", "--value", '["ghost"]',
        "--asset-id", asset_ids["a"], "--actor", "editor",
    )
    assert unknown_participant.returncode == 1
    assert "CATALOG_PARTICIPANT_NOT_FOUND" in unknown_participant.stderr

    set_result = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/subject_attributions", "--value", '["jane-doe"]',
        "--asset-id", asset_ids["a"], "--actor", "editor",
    )
    assert set_result.returncode == 0

    shown = json.loads(
        run_cli(
            "--project", str(workspace), "catalog", "show", "--asset", asset_ids["a"], "--json"
        ).stdout
    )
    assert shown["effective"]["/subject_attributions"]["value"] == ["jane-doe"]
    assert shown["effective"]["/subject_attributions"]["source"] == "correction"

    removed = run_cli(
        "--project", str(workspace), "catalog", "correct", "remove",
        "--target", "/subject_attributions",
        "--asset-id", asset_ids["a"], "--actor", "editor",
    )
    assert removed.returncode == 0
    after_remove = json.loads(
        run_cli(
            "--project", str(workspace), "catalog", "show", "--asset", asset_ids["a"], "--json"
        ).stdout
    )
    assert after_remove["effective"]["/subject_attributions"]["present"] is False
    assert after_remove["effective"]["/subject_attributions"]["source"] == "correction-removed"


def test_participant_removal_is_blocked_while_referenced_by_a_subject_attribution(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "in-use-project"
    asset_ids = _seed_workspace(workspace)
    assert (
        run_cli("--project", str(workspace), "participant", "add", "--id", "jane-doe").returncode
        == 0
    )
    assert (
        run_cli(
            "--project", str(workspace), "catalog", "correct", "set",
            "--target", "/subject_attributions", "--value", '["jane-doe"]',
            "--asset-id", asset_ids["a"], "--actor", "editor",
        ).returncode
        == 0
    )

    blocked = run_cli("--project", str(workspace), "participant", "remove", "--id", "jane-doe")
    assert blocked.returncode == 1
    assert "PARTICIPANT_IN_USE" in blocked.stderr

    assert (
        run_cli(
            "--project", str(workspace), "catalog", "correct", "remove",
            "--target", "/subject_attributions",
            "--asset-id", asset_ids["a"], "--actor", "editor",
        ).returncode
        == 0
    )

    allowed = run_cli("--project", str(workspace), "participant", "remove", "--id", "jane-doe")
    assert allowed.returncode == 0


def test_subject_attribution_set_across_multiple_assets_records_provenance_and_stays_sorted_unique(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "multi-asset-project"
    asset_ids = _seed_workspace(workspace)
    for participant_id in ("amy", "ben"):
        assert (
            run_cli(
                "--project", str(workspace), "participant", "add", "--id", participant_id
            ).returncode
            == 0
        )

    unsorted = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/subject_attributions", "--value", '["ben", "amy"]',
        "--asset-id", asset_ids["a"], "--actor", "editor",
    )
    assert unsorted.returncode == 1

    dry_run = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/subject_attributions", "--value", '["amy", "ben"]',
        "--locator", "materials/*.jpg", "--actor", "editor", "--reason", "identified in review",
        "--dry-run",
    )
    assert dry_run.returncode == 0
    assert f"resolved={len(asset_ids)}" in dry_run.stdout

    applied = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/subject_attributions", "--value", '["amy", "ben"]',
        "--locator", "materials/*.jpg", "--actor", "editor", "--reason", "identified in review",
    )
    assert applied.returncode == 0

    for asset_id in asset_ids.values():
        shown = json.loads(
            run_cli(
                "--project", str(workspace), "catalog", "show", "--asset", asset_id, "--json"
            ).stdout
        )
        correction = shown["corrections"][-1]
        assert correction["value"] == ["amy", "ben"]
        assert correction["actor"] == "editor"
        assert correction["reason"] == "identified in review"
        assert "at" in correction


def test_subject_roles_and_subject_attributions_are_shown_distinctly(tmp_path: Path) -> None:
    workspace = tmp_path / "distinct-display-project"
    asset_ids = _seed_workspace(workspace)
    assert (
        run_cli("--project", str(workspace), "participant", "add", "--id", "jane-doe").returncode
        == 0
    )

    catalog = workspace / "catalog.jsonl"
    records = [json.loads(line) for line in catalog.read_text(encoding="utf-8").splitlines()]
    for record in records:
        if record["asset_id"] == asset_ids["a"]:
            record["runs"] = {
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
            record["inferences"] = {
                "subject_roles": {
                    "value": ["bride", "groom"], "confidence": 0.9, "run_id": "vision-1"
                },
            }
    records.sort(key=lambda record: record["asset_id"])
    catalog.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )

    assert (
        run_cli(
            "--project", str(workspace), "catalog", "correct", "set",
            "--target", "/subject_attributions", "--value", '["jane-doe"]',
            "--asset-id", asset_ids["a"], "--actor", "editor",
        ).returncode
        == 0
    )

    shown = json.loads(
        run_cli(
            "--project", str(workspace), "catalog", "show", "--asset", asset_ids["a"], "--json"
        ).stdout
    )
    assert shown["inferences"]["subject_roles"]["value"] == ["bride", "groom"]
    assert shown["effective"]["/subject_attributions"]["value"] == ["jane-doe"]
    assert "/inferences/subject_roles" in shown["effective"]
    assert shown["effective"]["/inferences/subject_roles"]["source"] == "inference"
    assert shown["effective"]["/subject_attributions"]["source"] == "correction"
