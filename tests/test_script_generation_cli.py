from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml
from test_script_cli import write_skipped_interview


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("wedding-film")
    return subprocess.run([str(executable), *args], check=False, capture_output=True, text=True)


def set_model(workspace: Path, model: str) -> None:
    config_path = workspace / "project.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["adapters"]["narrative"]["model"] = model
    config_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def configured_workspace(
    tmp_path: Path, *, model: str = "fixture-success", with_story: bool = True
) -> Path:
    workspace = tmp_path / "script-generation-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    config_path = workspace / "project.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["adapters"]["narrative"] = {
        "name": "fake",
        "model": "fixture-success",
        "prompt_version": "v1",
    }
    config_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    (workspace / "materials").mkdir()
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    write_skipped_interview(workspace)
    if with_story:
        assert run_cli(
            "--project", str(workspace), "story", "generate", "--json"
        ).returncode == 0
        assert run_cli("--project", str(workspace), "story", "adopt").returncode == 0
    set_model(workspace, model)
    return workspace


def test_generate_writes_a_reviewable_candidate_covering_all_block_types(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path)

    result = run_cli("--project", str(workspace), "script", "generate", "--json")

    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["state"] == "candidate-ready"
    candidate_path = Path(payload["candidate"])
    assert candidate_path.is_file()
    assert not (workspace / "script.md").exists()

    validated = run_cli("--project", str(workspace), "script", "validate", "--json")
    assert validated.returncode == 1

    text = candidate_path.read_text(encoding="utf-8")
    for block_type in ("card", "narration", "caption"):
        assert f"type: {block_type}" in text


def test_generate_refuses_when_no_valid_story_source_exists(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path, with_story=False)

    result = run_cli("--project", str(workspace), "script", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "SCRIPT_SOURCE_STORY_MISSING"
    assert not (workspace / "script.md").exists()
    assert not (workspace / ".work" / "candidates" / "script.candidate.md").exists()


def test_generate_refuses_when_story_is_invalid(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path, with_story=False)
    (workspace / "story.md").write_text("# not a valid story\n", encoding="utf-8")

    result = run_cli("--project", str(workspace), "script", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "SCRIPT_SOURCE_STORY_INVALID"


def test_generate_never_creates_canonical_output_from_an_invalid_candidate(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path, model="fixture-invalid-empty-blocks")

    result = run_cli("--project", str(workspace), "script", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["state"] == "failed"
    assert payload["code"] == "NARRATIVE_CANDIDATE_INVALID"
    assert not (workspace / "script.md").exists()
    assert not (workspace / ".work" / "candidates" / "script.candidate.md").exists()


def test_generate_reports_a_candidate_that_fails_the_deeper_script_validator(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path, model="fixture-invalid-rich-body")

    result = run_cli("--project", str(workspace), "script", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["state"] == "candidate-invalid"
    assert payload["diagnostics"][0]["code"] == "SCRIPT_BLOCK_BODY_RICH_TEXT"
    assert Path(payload["candidate"]).is_file()
    assert not (workspace / "script.md").exists()


def test_generate_rejects_invalid_block_type_and_bad_ids(tmp_path: Path) -> None:
    for model, expected_code in (
        ("fixture-invalid-block-type", "NARRATIVE_CANDIDATE_INVALID"),
        ("fixture-invalid-bad-id", "NARRATIVE_CANDIDATE_INVALID"),
        ("fixture-invalid-duplicate-id", "NARRATIVE_CANDIDATE_INVALID"),
        ("fixture-invalid-empty-body", "NARRATIVE_CANDIDATE_INVALID"),
    ):
        workspace = configured_workspace(tmp_path / model, model=model)
        result = run_cli("--project", str(workspace), "script", "generate", "--json")
        assert result.returncode == 1, model
        assert json.loads(result.stdout)["code"] == expected_code, model


def test_adopt_creates_canonical_script_from_a_valid_candidate_when_absent(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path)
    assert run_cli("--project", str(workspace), "script", "generate").returncode == 0

    result = run_cli("--project", str(workspace), "script", "adopt", "--json")

    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["state"] == "adopted"
    script_path = workspace / "script.md"
    assert script_path.is_file()

    validated = run_cli("--project", str(workspace), "script", "validate", "--json")
    assert validated.returncode == 0, validated.stdout


def test_adopt_refuses_without_a_generated_candidate(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path)

    result = run_cli("--project", str(workspace), "script", "adopt", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "NARRATIVE_CANDIDATE_MISSING"
    assert not (workspace / "script.md").exists()


def test_generate_refuses_silent_overwrite_and_summarizes_meaningful_differences(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path)
    assert run_cli("--project", str(workspace), "script", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "script", "adopt").returncode == 0
    original_script = (workspace / "script.md").read_text(encoding="utf-8")

    set_model(workspace, "fixture-alternate")
    result = run_cli("--project", str(workspace), "script", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["state"] == "candidate-differs"
    assert any(line.startswith("title:") for line in payload["differences"])
    assert any("blocks added" in line for line in payload["differences"])
    assert any("blocks removed" in line for line in payload["differences"])
    assert (workspace / "script.md").read_text(encoding="utf-8") == original_script


def test_adopt_requires_force_to_replace_an_existing_canonical_script(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path)
    assert run_cli("--project", str(workspace), "script", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "script", "adopt").returncode == 0
    original_script = (workspace / "script.md").read_text(encoding="utf-8")

    set_model(workspace, "fixture-alternate")
    assert run_cli("--project", str(workspace), "script", "generate").returncode == 1

    refused = run_cli("--project", str(workspace), "script", "adopt", "--json")
    assert refused.returncode == 1
    assert json.loads(refused.stdout)["code"] == "SCRIPT_ADOPTION_REQUIRES_FORCE"
    assert (workspace / "script.md").read_text(encoding="utf-8") == original_script

    forced = run_cli("--project", str(workspace), "script", "adopt", "--force", "--json")
    assert forced.returncode == 0, forced.stdout
    assert (workspace / "script.md").read_text(encoding="utf-8") != original_script
    validated = run_cli("--project", str(workspace), "script", "validate", "--json")
    assert validated.returncode == 0, validated.stdout


def test_adopt_atomically_validates_before_replacing_and_never_touches_canonical_on_failure(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path)
    assert run_cli("--project", str(workspace), "script", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "script", "adopt").returncode == 0
    original_script = (workspace / "script.md").read_text(encoding="utf-8")

    candidate_path = workspace / ".work" / "candidates" / "script.candidate.md"
    candidate_path.write_text("# not a valid script candidate\n", encoding="utf-8")

    result = run_cli("--project", str(workspace), "script", "adopt", "--force", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["code"].startswith("SCRIPT_")
    assert (workspace / "script.md").read_text(encoding="utf-8") == original_script


def test_adopt_refuses_a_candidate_generated_against_a_stale_story(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path)
    assert run_cli("--project", str(workspace), "script", "generate").returncode == 0

    story_path = workspace / "story.md"
    story_text = story_path.read_text(encoding="utf-8")
    story_path.write_text(
        story_text.replace("Fixture Wedding Story", "Renamed Story"), encoding="utf-8"
    )

    result = run_cli("--project", str(workspace), "script", "adopt", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "SCRIPT_STORY_HASH_STALE"
    assert not (workspace / "script.md").exists()


def test_generate_classifies_adapter_refusal_without_writing_a_candidate(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path, model="fixture-refusal")

    result = run_cli("--project", str(workspace), "script", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "NARRATIVE_ADAPTER_REFUSAL"
    candidate_path = workspace / ".work" / "candidates" / "script.candidate.md"
    assert not candidate_path.exists()


def test_generate_requires_a_configured_narrative_adapter(tmp_path: Path) -> None:
    workspace = tmp_path / "unconfigured-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    (workspace / "materials").mkdir()
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0

    result = run_cli("--project", str(workspace), "script", "generate", "--json")

    assert result.returncode == 1
    assert json.loads(result.stdout)["code"] == "NARRATIVE_ADAPTER_DISABLED"


def test_status_transitions_from_missing_to_present_after_adoption(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path)

    before = json.loads(run_cli("--project", str(workspace), "status", "--json").stdout)
    assert before["layers"]["script"]["state"] == "missing"

    assert run_cli("--project", str(workspace), "script", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "script", "adopt").returncode == 0

    after = json.loads(run_cli("--project", str(workspace), "status", "--json").stdout)
    assert after["layers"]["script"]["state"] == "ready"
