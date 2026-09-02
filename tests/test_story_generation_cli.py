from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml
from test_script_cli import write_skipped_interview

from wedding_film.story_generation import _catalog_summary


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("wedding-film")
    return subprocess.run([str(executable), *args], check=False, capture_output=True, text=True)


def configured_workspace(
    tmp_path: Path, *, model: str = "fixture-success", with_asset: bool = False
) -> Path:
    workspace = tmp_path / "story-generation-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    config_path = workspace / "project.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["adapters"]["narrative"] = {"name": "fake", "model": model, "prompt_version": "v1"}
    config_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    materials = workspace / "materials"
    materials.mkdir()
    if with_asset:
        (materials / "asset.bin").write_bytes(b"asset bytes for effective corrections test")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    write_skipped_interview(workspace)
    return workspace


def set_model(workspace: Path, model: str) -> None:
    config_path = workspace / "project.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["adapters"]["narrative"]["model"] = model
    config_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def test_generate_writes_a_reviewable_candidate_when_no_canonical_story_exists(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path)

    result = run_cli("--project", str(workspace), "story", "generate", "--json")

    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["state"] == "candidate-ready"
    candidate_path = Path(payload["candidate"])
    assert candidate_path.is_file()
    assert not (workspace / "story.md").exists()

    validated = run_cli(
        "--project", str(workspace), "story", "validate", "--json"
    )
    assert validated.returncode == 1


def test_generate_never_creates_canonical_output_from_an_invalid_candidate(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path, model="fixture-invalid-empty-moments")

    result = run_cli("--project", str(workspace), "story", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["state"] == "failed"
    assert payload["code"] == "NARRATIVE_CANDIDATE_INVALID"
    assert not (workspace / "story.md").exists()
    assert not (workspace / ".work" / "candidates" / "story.candidate.md").exists()


def test_generate_reports_a_candidate_that_fails_the_deeper_story_validator(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path, model="fixture-invalid-formatting-only-intent")

    result = run_cli("--project", str(workspace), "story", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["state"] == "candidate-invalid"
    assert payload["diagnostics"][0]["code"] == "STORY_SECTION_EMPTY"
    assert Path(payload["candidate"]).is_file()
    assert not (workspace / "story.md").exists()


def test_adopt_creates_canonical_story_from_a_valid_candidate_when_absent(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path)
    assert run_cli("--project", str(workspace), "story", "generate").returncode == 0

    result = run_cli("--project", str(workspace), "story", "adopt", "--json")

    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["state"] == "adopted"
    story_path = workspace / "story.md"
    assert story_path.is_file()

    validated = run_cli("--project", str(workspace), "story", "validate", "--json")
    assert validated.returncode == 0, validated.stdout


def test_adopt_refuses_without_a_generated_candidate(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path)

    result = run_cli("--project", str(workspace), "story", "adopt", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "NARRATIVE_CANDIDATE_MISSING"
    assert not (workspace / "story.md").exists()


def test_generate_refuses_silent_overwrite_and_summarizes_meaningful_differences(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path)
    assert run_cli("--project", str(workspace), "story", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "story", "adopt").returncode == 0
    original_story = (workspace / "story.md").read_text(encoding="utf-8")

    set_model(workspace, "fixture-alternate")
    result = run_cli("--project", str(workspace), "story", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["state"] == "candidate-differs"
    assert any(line.startswith("title:") for line in payload["differences"])
    assert any("moments added" in line for line in payload["differences"])
    assert (workspace / "story.md").read_text(encoding="utf-8") == original_story


def test_adopt_requires_force_to_replace_an_existing_canonical_story(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path)
    assert run_cli("--project", str(workspace), "story", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "story", "adopt").returncode == 0
    original_story = (workspace / "story.md").read_text(encoding="utf-8")

    set_model(workspace, "fixture-alternate")
    assert run_cli("--project", str(workspace), "story", "generate").returncode == 1

    refused = run_cli("--project", str(workspace), "story", "adopt", "--json")
    assert refused.returncode == 1
    assert json.loads(refused.stdout)["code"] == "STORY_ADOPTION_REQUIRES_FORCE"
    assert (workspace / "story.md").read_text(encoding="utf-8") == original_story

    forced = run_cli("--project", str(workspace), "story", "adopt", "--force", "--json")
    assert forced.returncode == 0, forced.stdout
    assert (workspace / "story.md").read_text(encoding="utf-8") != original_story
    validated = run_cli("--project", str(workspace), "story", "validate", "--json")
    assert validated.returncode == 0, validated.stdout


def test_adopt_atomically_validates_before_replacing_and_never_touches_canonical_on_failure(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path)
    assert run_cli("--project", str(workspace), "story", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "story", "adopt").returncode == 0
    original_story = (workspace / "story.md").read_text(encoding="utf-8")

    candidate_path = workspace / ".work" / "candidates" / "story.candidate.md"
    candidate_path.write_text("# not a valid story candidate\n", encoding="utf-8")

    result = run_cli("--project", str(workspace), "story", "adopt", "--force", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["code"].startswith("STORY_")
    assert (workspace / "story.md").read_text(encoding="utf-8") == original_story


def test_generate_classifies_adapter_refusal_without_writing_a_candidate(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path, model="fixture-refusal")

    result = run_cli("--project", str(workspace), "story", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "NARRATIVE_ADAPTER_REFUSAL"
    candidate_path = workspace / ".work" / "candidates" / "story.candidate.md"
    assert not candidate_path.exists()


def test_generate_requires_a_configured_narrative_adapter(tmp_path: Path) -> None:
    workspace = tmp_path / "unconfigured-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    (workspace / "materials").mkdir()
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0

    result = run_cli("--project", str(workspace), "story", "generate", "--json")

    assert result.returncode == 1
    assert json.loads(result.stdout)["code"] == "NARRATIVE_ADAPTER_DISABLED"


def test_generate_succeeds_with_an_empty_participant_roster_and_a_populated_one(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path)

    empty_roster = run_cli("--project", str(workspace), "story", "generate", "--json")
    assert empty_roster.returncode == 0, empty_roster.stdout

    assert run_cli(
        "--project", str(workspace), "participant", "add",
        "--id", "alex-chen", "--display-name", "Alex Chen", "--role", "partner", "--principal",
    ).returncode == 0

    populated_roster = run_cli("--project", str(workspace), "story", "generate", "--json")
    assert populated_roster.returncode == 0, populated_roster.stdout


def test_catalog_summary_carries_locators_and_corrections_without_leaking_asset_ids(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path, with_asset=True)
    record = json.loads((workspace / "catalog.jsonl").read_text(encoding="utf-8"))
    asset_id = record["asset_id"]

    corrected = run_cli(
        "--project", str(workspace), "catalog", "correct", "set",
        "--target", "/inferences/wedding_moment", "--value", '"ceremony"',
        "--asset-id", asset_id, "--actor", "test-actor",
    )
    assert corrected.returncode == 0, corrected.stderr

    summary = _catalog_summary(workspace)

    assert summary == [{"wedding_moment": "ceremony", "locators": ["materials/asset.bin"]}]
    serialized = json.dumps(summary)
    assert asset_id not in serialized

    result = run_cli("--project", str(workspace), "story", "generate", "--json")
    assert result.returncode == 0, result.stdout


def test_status_transitions_from_missing_to_present_after_adoption(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path)

    before = json.loads(run_cli("--project", str(workspace), "status", "--json").stdout)
    assert before["layers"]["story"]["state"] == "missing"

    assert run_cli("--project", str(workspace), "story", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "story", "adopt").returncode == 0

    after = json.loads(run_cli("--project", str(workspace), "status", "--json").stdout)
    assert after["layers"]["story"]["state"] != "missing"
