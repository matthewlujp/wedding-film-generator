from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from wedding_film import status as status_module
from wedding_film.cli import main


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("wedding-film")
    return subprocess.run(
        [str(executable), *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_user_can_initialize_an_explicit_workspace_without_materials(tmp_path: Path) -> None:
    workspace = tmp_path / "my-wedding"

    result = run_cli("--project", str(workspace), "project", "init")

    assert result.returncode == 0, result.stderr
    assert (workspace / "project.yaml").is_file()
    assert (workspace / "participants.yaml").is_file()
    assert (workspace / "runs" / "analysis").is_dir()
    assert (workspace / ".work" / "candidates").is_dir()
    assert (workspace / "renders").is_dir()
    assert not (workspace / "materials").exists()
    assert "PROJECT_INITIALIZED" in result.stdout


def test_user_can_initialize_an_existing_empty_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "empty"
    workspace.mkdir()

    before = run_cli("--project", str(workspace), "status", "--json")
    before_payload = json.loads(before.stdout)

    result = run_cli("--project", str(workspace), "project", "init")

    assert before_payload["safe_next_commands"] == [
        f"wedding-film --project {workspace} project init"
    ]
    assert result.returncode == 0, result.stderr
    assert (workspace / "project.yaml").is_file()
    assert not (workspace / "materials").exists()


def test_initialization_generates_a_valid_identity_for_any_directory_name(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "結婚式 Movie!"

    initialized = run_cli("--project", str(workspace), "project", "init")
    status = run_cli("--project", str(workspace), "status", "--json")

    assert initialized.returncode == 0
    assert status.returncode == 0
    configuration = json.loads(status.stdout)["prerequisites"]["project_configuration"]
    assert configuration["state"] == "ready"


def test_initialization_rejects_ambiguous_or_unsafe_destinations(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("untouched", encoding="utf-8")
    symlink = tmp_path / "linked"
    symlink.symlink_to(existing, target_is_directory=True)

    missing_destination = run_cli("project", "init")
    non_empty_destination = run_cli("--project", str(existing), "project", "init")
    linked_destination = run_cli("--project", str(symlink), "project", "init")

    assert missing_destination.returncode == 1
    assert "CLI_INPUT_INVALID" in missing_destination.stderr
    assert non_empty_destination.returncode == 1
    assert "UNSAFE_DESTINATION" in non_empty_destination.stderr
    assert linked_destination.returncode == 1
    assert "UNSAFE_DESTINATION" in linked_destination.stderr
    assert marker.read_text(encoding="utf-8") == "untouched"

    unknown_command = run_cli("--project", str(existing), "not-a-command")
    assert unknown_command.returncode == 1
    assert f"workspace={existing}" in unknown_command.stderr
    assert "CLI_INPUT_INVALID" in unknown_command.stderr


def test_initialization_rejects_a_destination_below_a_symlink_ancestor(
    tmp_path: Path,
) -> None:
    actual_parent = tmp_path / "actual"
    actual_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    destination = linked_parent / "workspace"
    destination.mkdir()

    result = run_cli("--project", str(destination), "project", "init")

    assert result.returncode == 1
    assert "UNSAFE_DESTINATION" in result.stderr
    assert list(destination.iterdir()) == []


def test_status_rejects_unknown_config_without_exposing_secret_values(tmp_path: Path) -> None:
    workspace = tmp_path / "private-wedding"
    initialized = run_cli("--project", str(workspace), "project", "init")
    assert initialized.returncode == 0
    secret = "do-not-print-this-secret"
    with (workspace / "project.yaml").open("a", encoding="utf-8") as project_file:
        project_file.write(f"openai_api_key: {secret}\n")

    result = run_cli("--project", str(workspace), "status", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    configuration = payload["prerequisites"]["project_configuration"]
    assert configuration["state"] == "invalid"
    assert configuration["reasons"][0]["code"] == "CONFIG_UNKNOWN_FIELD"
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_human_and_json_status_render_the_same_workspace_facts(tmp_path: Path) -> None:
    workspace = tmp_path / "equivalent-status"
    initialized = run_cli("--project", str(workspace), "project", "init")
    assert initialized.returncode == 0

    machine = run_cli("--project", str(workspace), "status", "--json")
    human = run_cli("--project", str(workspace), "status")

    assert machine.returncode == human.returncode == 0
    payload = json.loads(machine.stdout)
    assert set(payload["prerequisites"]) == {
        "credentials",
        "ffmpeg",
        "ffprobe",
        "project_configuration",
    }
    assert set(payload["layers"]) == {
        "materials",
        "semantic_catalog",
        "story",
        "script",
        "storyboard",
        "rough_cut",
    }
    for group in ("prerequisites", "layers"):
        for name, fact in payload[group].items():
            assert f"{group[:-1]}.{name} state={fact['state']}" in human.stdout
            for reason in fact["reasons"]:
                assert reason["code"] in human.stdout
            for artifact in fact["artifacts"]:
                assert artifact in human.stdout
            for warning in fact["warnings"]:
                assert warning["code"] in human.stdout
            for command in fact["next_commands"]:
                assert command in human.stdout


def test_status_rejects_missing_and_invalid_config_values(tmp_path: Path) -> None:
    workspace = tmp_path / "invalid-values"
    initialized = run_cli("--project", str(workspace), "project", "init")
    assert initialized.returncode == 0
    project_file = workspace / "project.yaml"
    original = project_file.read_text(encoding="utf-8")

    project_file.write_text(
        original.replace("display_title: Invalid Values\n", ""), encoding="utf-8"
    )
    missing = run_cli("--project", str(workspace), "status", "--json")
    assert missing.returncode == 1
    missing_fact = json.loads(missing.stdout)["prerequisites"]["project_configuration"]
    assert missing_fact["reasons"][0]["code"] == "CONFIG_MISSING_FIELD"

    project_file.write_text(
        original.replace("project_id: invalid-values", "project_id: ../outside"),
        encoding="utf-8",
    )
    invalid = run_cli("--project", str(workspace), "status", "--json")
    assert invalid.returncode == 1
    invalid_fact = json.loads(invalid.stdout)["prerequisites"]["project_configuration"]
    assert invalid_fact["reasons"][0]["code"] == "CONFIG_INVALID_VALUE"

    project_file.write_text(f"{original}project_id: shadowed\n", encoding="utf-8")
    duplicate = run_cli("--project", str(workspace), "status", "--json")
    assert duplicate.returncode == 1
    duplicate_fact = json.loads(duplicate.stdout)["prerequisites"]["project_configuration"]
    assert duplicate_fact["reasons"][0]["code"] == "CONFIG_DUPLICATE_FIELD"


def test_status_reads_adapter_credentials_only_from_process_environment(tmp_path: Path) -> None:
    workspace = tmp_path / "environment-credentials"
    initialized = run_cli("--project", str(workspace), "project", "init")
    assert initialized.returncode == 0
    project_file = workspace / "project.yaml"
    project_file.write_text(
        project_file.read_text(encoding="utf-8").replace("name: none", "name: openai", 1),
        encoding="utf-8",
    )
    without_key = os.environ.copy()
    without_key.pop("OPENAI_API_KEY", None)

    missing = run_cli(
        "--project", str(workspace), "status", "--json", env=without_key
    )
    secret = "environment-only-secret"
    with_key = {**without_key, "OPENAI_API_KEY": secret}
    available = run_cli(
        "--project", str(workspace), "status", "--json", env=with_key
    )

    assert json.loads(missing.stdout)["prerequisites"]["credentials"]["state"] == "missing"
    assert json.loads(available.stdout)["prerequisites"]["credentials"]["state"] == "ready"
    assert secret not in available.stdout
    assert secret not in available.stderr


def test_status_rejects_unknown_adapter_names(tmp_path: Path) -> None:
    workspace = tmp_path / "unknown-adapter"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    project_file = workspace / "project.yaml"
    project_file.write_text(
        project_file.read_text(encoding="utf-8").replace("name: none", "name: opneai", 1),
        encoding="utf-8",
    )

    result = run_cli("--project", str(workspace), "status", "--json")
    configuration = json.loads(result.stdout)["prerequisites"]["project_configuration"]

    assert result.returncode == 1
    assert configuration["reasons"][0]["code"] == "CONFIG_UNKNOWN_ADAPTER"


def test_status_reports_invalid_story_and_missing_tools(
    tmp_path: Path,
) -> None:
    stale_workspace = tmp_path / "stale"
    assert run_cli("--project", str(stale_workspace), "project", "init").returncode == 0
    (stale_workspace / "story.md").write_text("# Story\n", encoding="utf-8")
    stale = json.loads(
        run_cli("--project", str(stale_workspace), "status", "--json").stdout
    )
    assert stale["layers"]["story"]["state"] == "invalid"
    assert stale["layers"]["story"]["reasons"][0]["code"] == (
        "STORY_FRONTMATTER_INVALID"
    )

    complete_workspace = tmp_path / "complete"
    assert run_cli("--project", str(complete_workspace), "project", "init").returncode == 0
    (complete_workspace / "materials").mkdir()
    for filename in ("catalog.jsonl", "story.md", "script.md", "storyboard.yaml"):
        (complete_workspace / filename).write_text("present\n", encoding="utf-8")
    (complete_workspace / "renders" / "rough-cut.mp4").write_text(
        "not-a-real-movie-yet", encoding="utf-8"
    )
    no_tools = {**os.environ, "PATH": ""}
    result = run_cli(
        "--project", str(complete_workspace), "status", "--json", env=no_tools
    )
    payload = json.loads(result.stdout)

    assert result.returncode == 1
    assert payload["prerequisites"]["ffmpeg"]["state"] == "missing"
    assert payload["prerequisites"]["ffprobe"]["state"] == "missing"
    assert payload["layers"]["semantic_catalog"]["state"] == "invalid"
    assert payload["layers"]["semantic_catalog"]["reasons"][0]["code"] == (
        "CATALOG_JSON_INVALID"
    )
    assert "semantic_catalog" in payload["layers"]["storyboard"]["upstream_hashes"]
    assert "catalog" not in payload["layers"]["storyboard"]["upstream_hashes"]
    assert payload["layers"]["rough_cut"]["state"] == "stale"
    assert not (complete_workspace / ".status").exists()


def test_status_never_advertises_commands_missing_from_the_cli(tmp_path: Path) -> None:
    workspace = tmp_path / "current-cli"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    (workspace / "materials").mkdir()

    status = json.loads(run_cli("--project", str(workspace), "status", "--json").stdout)

    assert status["safe_next_commands"] == [
        f"wedding-film --project {workspace} catalog scan"
    ]
    assert status["layers"]["semantic_catalog"]["next_commands"] == [
        f"wedding-film --project {workspace} catalog scan"
    ]


def test_status_json_reports_artifact_io_errors_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "unreadable-artifact"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    (workspace / "materials").mkdir()
    catalog = workspace / "catalog.jsonl"
    catalog.write_text('{"asset_id":"photo-1"}\n', encoding="utf-8")
    original_sha256 = status_module._sha256

    def unreadable_catalog(path: Path) -> str:
        if path == catalog:
            raise PermissionError("catalog cannot be read")
        return original_sha256(path)

    monkeypatch.setattr(status_module, "_sha256", unreadable_catalog)

    returncode = main(["--project", str(workspace), "status", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert returncode == 1
    assert "Traceback" not in captured.err
    assert payload["state"] == "invalid"
    assert payload["layers"]["story"]["state"] == "invalid"
    assert payload["layers"]["story"]["reasons"][0]["code"] == "STORY_IO_ERROR"
    assert payload["layers"]["story"]["artifacts"] == [str(catalog)]
