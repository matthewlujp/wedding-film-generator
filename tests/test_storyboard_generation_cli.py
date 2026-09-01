from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml
from PIL import Image
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
    tmp_path: Path,
    *,
    model: str = "fixture-success",
    with_story: bool = True,
    with_script: bool = True,
    photo_count: int = 2,
) -> Path:
    workspace = tmp_path / "storyboard-generation-project"
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
    materials = workspace / "materials"
    materials.mkdir()
    colors = [(200, 100, 50), (40, 160, 200), (90, 200, 90)]
    for index in range(photo_count):
        Image.new("RGB", (400, 300), colors[index % len(colors)]).save(
            materials / f"photo-{index}.jpg"
        )
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    write_skipped_interview(workspace)
    if with_story:
        assert run_cli(
            "--project", str(workspace), "story", "generate", "--json"
        ).returncode == 0
        assert run_cli("--project", str(workspace), "story", "adopt").returncode == 0
    if with_script:
        assert run_cli(
            "--project", str(workspace), "script", "generate", "--json"
        ).returncode == 0
        assert run_cli("--project", str(workspace), "script", "adopt").returncode == 0
    set_model(workspace, model)
    return workspace


def test_generate_writes_a_reviewable_candidate_covering_card_and_photo_items(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path)

    result = run_cli("--project", str(workspace), "storyboard", "generate", "--json")

    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["state"] == "candidate-ready"
    candidate_path = Path(payload["candidate"])
    assert candidate_path.is_file()
    assert not (workspace / "storyboard.yaml").exists()

    document = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    types = {item["type"] for item in document["sequence"]}
    assert types == {"card", "photo"}
    assert document["schema_version"] == 1
    assert document["output"] == {"width": 1920, "height": 1080, "fps": 24}

    warning_codes = {warning["code"] for warning in payload["warnings"]}
    assert "STORYBOARD_MUSIC_UNRESOLVED" in warning_codes
    assert "STORYBOARD_NARRATION_NOT_RENDERED" in warning_codes


def test_generate_refuses_when_no_valid_script_source_exists(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path, with_script=False)

    result = run_cli("--project", str(workspace), "storyboard", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "STORYBOARD_SOURCE_SCRIPT_MISSING"
    assert not (workspace / "storyboard.yaml").exists()
    assert not (workspace / ".work" / "candidates" / "storyboard.candidate.yaml").exists()


def test_generate_refuses_when_story_is_missing(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path, with_story=False, with_script=False)

    result = run_cli("--project", str(workspace), "storyboard", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "STORYBOARD_SOURCE_STORY_MISSING"


def test_generate_never_creates_canonical_output_from_a_structurally_invalid_candidate(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path, model="fixture-invalid-empty-sequence")

    result = run_cli("--project", str(workspace), "storyboard", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["state"] == "failed"
    assert payload["code"] == "NARRATIVE_CANDIDATE_INVALID"
    assert not (workspace / "storyboard.yaml").exists()
    assert not (workspace / ".work" / "candidates" / "storyboard.candidate.yaml").exists()


def test_generate_rejects_duplicate_item_id(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path, model="fixture-invalid-duplicate-item-id")

    result = run_cli("--project", str(workspace), "storyboard", "generate", "--json")

    assert result.returncode == 1
    assert json.loads(result.stdout)["code"] == "NARRATIVE_CANDIDATE_INVALID"


def test_generate_reports_a_candidate_with_an_unknown_asset_reference(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path, model="fixture-invalid-unknown-asset")

    result = run_cli("--project", str(workspace), "storyboard", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["state"] == "candidate-invalid"
    assert payload["diagnostics"][0]["code"] == "STORYBOARD_ASSET_UNKNOWN"
    assert Path(payload["candidate"]).is_file()
    assert not (workspace / "storyboard.yaml").exists()


def test_generate_reports_a_candidate_with_an_unknown_story_moment(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path, model="fixture-invalid-unknown-story-moment")

    result = run_cli("--project", str(workspace), "storyboard", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["state"] == "candidate-invalid"
    assert payload["diagnostics"][0]["code"] == "STORYBOARD_STORY_MOMENT_UNKNOWN"


def test_generate_reports_a_candidate_with_an_invalid_transition(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path, model="fixture-invalid-bad-transition")

    result = run_cli("--project", str(workspace), "storyboard", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["state"] == "candidate-invalid"
    assert payload["diagnostics"][0]["code"] == "STORYBOARD_TRANSITION_INVALID"


def test_adopt_creates_canonical_storyboard_from_a_valid_candidate_when_absent(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path)
    assert run_cli("--project", str(workspace), "storyboard", "generate").returncode == 0

    result = run_cli("--project", str(workspace), "storyboard", "adopt", "--json")

    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["state"] == "adopted"
    storyboard_path = workspace / "storyboard.yaml"
    assert storyboard_path.is_file()

    validated = run_cli("--project", str(workspace), "storyboard", "validate", "--json")
    assert validated.returncode == 0, validated.stdout


def test_adopt_refuses_without_a_generated_candidate(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path)

    result = run_cli("--project", str(workspace), "storyboard", "adopt", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "NARRATIVE_CANDIDATE_MISSING"
    assert not (workspace / "storyboard.yaml").exists()


def test_generate_refuses_silent_overwrite_and_summarizes_meaningful_differences(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path)
    assert run_cli("--project", str(workspace), "storyboard", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "storyboard", "adopt").returncode == 0
    original_storyboard = (workspace / "storyboard.yaml").read_text(encoding="utf-8")

    set_model(workspace, "fixture-alternate")
    result = run_cli("--project", str(workspace), "storyboard", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["state"] == "candidate-differs"
    assert any("duration_frames" in line for line in payload["differences"])
    assert (workspace / "storyboard.yaml").read_text(encoding="utf-8") == original_storyboard


def test_adopt_requires_force_to_replace_an_existing_canonical_storyboard(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path)
    assert run_cli("--project", str(workspace), "storyboard", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "storyboard", "adopt").returncode == 0
    original_storyboard = (workspace / "storyboard.yaml").read_text(encoding="utf-8")

    set_model(workspace, "fixture-alternate")
    assert run_cli("--project", str(workspace), "storyboard", "generate").returncode == 1

    refused = run_cli("--project", str(workspace), "storyboard", "adopt", "--json")
    assert refused.returncode == 1
    assert json.loads(refused.stdout)["code"] == "STORYBOARD_ADOPTION_REQUIRES_FORCE"
    assert (workspace / "storyboard.yaml").read_text(encoding="utf-8") == original_storyboard

    forced = run_cli("--project", str(workspace), "storyboard", "adopt", "--force", "--json")
    assert forced.returncode == 0, forced.stdout
    assert (workspace / "storyboard.yaml").read_text(encoding="utf-8") != original_storyboard
    validated = run_cli("--project", str(workspace), "storyboard", "validate", "--json")
    assert validated.returncode == 0, validated.stdout


def test_adopt_atomically_validates_before_replacing_and_never_touches_canonical_on_failure(
    tmp_path: Path,
) -> None:
    workspace = configured_workspace(tmp_path)
    assert run_cli("--project", str(workspace), "storyboard", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "storyboard", "adopt").returncode == 0
    original_storyboard = (workspace / "storyboard.yaml").read_text(encoding="utf-8")

    candidate_path = workspace / ".work" / "candidates" / "storyboard.candidate.yaml"
    candidate_path.write_text("schema_version: 1\n", encoding="utf-8")

    result = run_cli("--project", str(workspace), "storyboard", "adopt", "--force", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["code"].startswith("STORYBOARD_")
    assert (workspace / "storyboard.yaml").read_text(encoding="utf-8") == original_storyboard


def test_adopt_allows_a_candidate_generated_against_a_stale_script_but_warns_on_validation(
    tmp_path: Path,
) -> None:
    """Hash staleness is warn-by-default per the Storyboard contract, unlike Story/Script
    adoption which requires zero warnings; only diagnostics block Storyboard adoption."""
    workspace = configured_workspace(tmp_path)
    assert run_cli("--project", str(workspace), "storyboard", "generate").returncode == 0

    script_path = workspace / "script.md"
    script_text = script_path.read_text(encoding="utf-8")
    script_path.write_text(
        script_text.replace("Fixture Wedding Script", "Renamed Script"), encoding="utf-8"
    )

    result = run_cli("--project", str(workspace), "storyboard", "adopt", "--json")

    assert result.returncode == 0, result.stdout
    assert (workspace / "storyboard.yaml").exists()

    validated = run_cli("--project", str(workspace), "storyboard", "validate", "--json")
    validated_payload = json.loads(validated.stdout)
    assert validated_payload["state"] == "complete-with-warnings"
    warning_codes = {warning["code"] for warning in validated_payload["warnings"]}
    assert "STORYBOARD_SCRIPT_HASH_STALE" in warning_codes

    strict = run_cli("--project", str(workspace), "storyboard", "validate", "--json", "--strict")
    assert strict.returncode == 1


def test_generate_classifies_adapter_refusal_without_writing_a_candidate(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path, model="fixture-refusal")

    result = run_cli("--project", str(workspace), "storyboard", "generate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "NARRATIVE_ADAPTER_REFUSAL"
    candidate_path = workspace / ".work" / "candidates" / "storyboard.candidate.yaml"
    assert not candidate_path.exists()


def test_generate_requires_a_configured_narrative_adapter(tmp_path: Path) -> None:
    workspace = tmp_path / "unconfigured-project"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    (workspace / "materials").mkdir()
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0

    result = run_cli("--project", str(workspace), "storyboard", "generate", "--json")

    assert result.returncode == 1
    assert json.loads(result.stdout)["code"] == "NARRATIVE_ADAPTER_DISABLED"


def test_status_transitions_from_missing_to_present_after_adoption(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path)

    before = json.loads(run_cli("--project", str(workspace), "status", "--json").stdout)
    assert before["layers"]["storyboard"]["state"] == "missing"

    assert run_cli("--project", str(workspace), "storyboard", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "storyboard", "adopt").returncode == 0

    after = json.loads(run_cli("--project", str(workspace), "status", "--json").stdout)
    assert after["layers"]["storyboard"]["state"] in ("ready", "complete-with-warnings")


def test_frame_arithmetic_matches_sum_of_item_durations(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path, photo_count=2)
    assert run_cli("--project", str(workspace), "storyboard", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "storyboard", "adopt").returncode == 0

    validated = run_cli("--project", str(workspace), "storyboard", "validate", "--json")
    assert validated.returncode == 0, validated.stdout
    document = json.loads(validated.stdout)["document"]

    expected = sum(item["duration_frames"] for item in document["sequence"])
    assert document["total_frames"] == expected
    assert document["total_frames"] == 24 + 48 + 48


def test_render_and_rerender_after_adopting_a_generated_storyboard(tmp_path: Path) -> None:
    workspace = configured_workspace(tmp_path, photo_count=2)
    assert run_cli("--project", str(workspace), "storyboard", "generate").returncode == 0
    assert run_cli("--project", str(workspace), "storyboard", "adopt").returncode == 0

    first = run_cli("--project", str(workspace), "render", "rough-cut")
    assert first.returncode == 0, first.stderr
    artifact = workspace / "renders" / "rough-cut.mp4"
    assert artifact.is_file()
    first_bytes = artifact.read_bytes()

    script_path = workspace / "script.md"
    script_text = script_path.read_text(encoding="utf-8")
    assert "Their story begins here." in script_text
    script_path.write_text(
        script_text.replace("Their story begins here.", "Their story truly begins here now."),
        encoding="utf-8",
    )

    second = run_cli("--project", str(workspace), "render", "rough-cut")
    assert second.returncode == 0, second.stderr
    assert artifact.read_bytes() != first_bytes

    storyboard_after = (workspace / "storyboard.yaml").read_text(encoding="utf-8")
    assert "story_moment: getting-ready" in storyboard_after
