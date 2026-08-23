from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_script_cli import valid_script, valid_story


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("wedding-film")
    return subprocess.run([str(executable), *args], check=False, capture_output=True, text=True)


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def valid_storyboard(story: str, script: str, catalog: str, asset_id: str) -> str:
    return f"""schema_version: 1
output:
  width: 1920
  height: 1080
  fps: 24
inputs:
  story: {digest(story)}
  script: {digest(script)}
  catalog: {digest(catalog)}
sequence:
  - item_id: opening
    type: card
    story_moment: preparation
    duration_frames: 72
    script_block: opening-card
    transition:
      type: crossfade
      duration_frames: 12
  - item_id: portrait
    type: photo
    story_moment: ceremony
    duration_frames: 120
    asset_id: {asset_id}
    motion: slow-zoom-in
    caption: ceremony-caption
narration_cues:
  - block_id: ceremony-narration
    start_frame: 60
    duration_frames: 100
music_cues:
  - cue_id: gentle-score
    start_frame: 0
    duration_frames: 180
    intent: gentle and warm
"""


def authored_workspace(tmp_path: Path) -> tuple[Path, str]:
    workspace = tmp_path / "movie"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    photo = materials / "photo.jpg"
    photo.write_bytes(b"photo")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    catalog = (workspace / "catalog.jsonl").read_text()
    asset_id = json.loads(catalog)["asset_id"]
    story = valid_story()
    script = valid_script(story)
    (workspace / "story.md").write_text(story)
    (workspace / "script.md").write_text(script)
    (workspace / "storyboard.yaml").write_text(valid_storyboard(story, script, catalog, asset_id))
    return workspace, asset_id


def test_storyboard_validate_accepts_strict_v1_and_computes_exact_frames(tmp_path: Path) -> None:
    workspace, _ = authored_workspace(tmp_path)
    result = run_cli("--project", str(workspace), "storyboard", "validate", "--json")
    payload = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert payload["state"] == "complete-with-warnings"
    assert payload["document"]["total_frames"] == 180
    assert payload["warnings"][0]["code"] == "STORYBOARD_RUNTIME_DEVIATION"


def test_top_level_validate_checks_storyboard_references_and_strict_warnings(
    tmp_path: Path,
) -> None:
    workspace, _ = authored_workspace(tmp_path)
    storyboard = workspace / "storyboard.yaml"

    valid = run_cli("--project", str(workspace), "validate", "--json")
    assert valid.returncode == 0
    assert json.loads(valid.stdout)["artifact"] == str(storyboard)

    storyboard.write_text(storyboard.read_text().replace("opening-card", "missing-card", 1))
    invalid = run_cli("--project", str(workspace), "validate", "--json")
    assert invalid.returncode == 1
    assert json.loads(invalid.stdout)["diagnostics"][0]["code"] == (
        "STORYBOARD_SCRIPT_BLOCK_UNKNOWN"
    )

    workspace, _ = authored_workspace(tmp_path / "strict")
    strict = run_cli("--project", str(workspace), "validate", "--strict", "--json")
    assert strict.returncode == 1
    assert json.loads(strict.stdout)["diagnostics"][0]["code"] == ("STORYBOARD_RUNTIME_DEVIATION")


def test_storyboard_rejects_duplicate_yaml_and_invalid_crossfade(tmp_path: Path) -> None:
    workspace, _ = authored_workspace(tmp_path)
    storyboard = workspace / "storyboard.yaml"
    storyboard.write_text(
        storyboard.read_text().replace("  width: 1920", "  width: 1920\n  width: 1280")
    )
    duplicate = run_cli("--project", str(workspace), "storyboard", "validate", "--json")
    assert duplicate.returncode == 1
    assert json.loads(duplicate.stdout)["diagnostics"][0]["code"] == ("STORYBOARD_DUPLICATE_FIELD")

    workspace, _ = authored_workspace(tmp_path / "transition")
    storyboard = workspace / "storyboard.yaml"
    storyboard.write_text(
        storyboard.read_text().replace("duration_frames: 12", "duration_frames: 72", 1)
    )
    transition = run_cli("--project", str(workspace), "storyboard", "validate", "--json")
    assert transition.returncode == 1
    assert json.loads(transition.stdout)["diagnostics"][0]["code"] == (
        "STORYBOARD_TRANSITION_INVALID"
    )


@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        (("schema_version: 1", "schema_version: 2"), "STORYBOARD_VERSION_UNSUPPORTED"),
        (("  fps: 24", "  fps: null"), "STORYBOARD_NULL_FORBIDDEN"),
        (("  fps: 24", "  fps: 24\n  codec: h264"), "STORYBOARD_UNKNOWN_FIELD"),
        (("item_id: portrait", "item_id: opening"), "STORYBOARD_ITEM_ID_DUPLICATE"),
        (("motion: slow-zoom-in", "motion: pan-left"), "STORYBOARD_MOTION_UNSUPPORTED"),
        (("type: card", "type: card\n    mystery: value"), "STORYBOARD_UNKNOWN_FIELD"),
        (("type: crossfade", "type: cut"), "STORYBOARD_UNKNOWN_FIELD"),
        (("start_frame: 60", "start_frame: 100"), "STORYBOARD_CUE_BOUNDS_INVALID"),
    ],
)
def test_storyboard_rejects_strict_structure(
    tmp_path: Path, replacement: tuple[str, str], code: str
) -> None:
    workspace, _ = authored_workspace(tmp_path)
    storyboard = workspace / "storyboard.yaml"
    storyboard.write_text(storyboard.read_text().replace(*replacement, 1))
    result = run_cli("--project", str(workspace), "storyboard", "validate", "--json")
    assert result.returncode == 1
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == code


def test_storyboard_rejects_same_kind_cue_overlap_and_allows_asset_reuse(tmp_path: Path) -> None:
    workspace, _ = authored_workspace(tmp_path)
    storyboard = workspace / "storyboard.yaml"
    source = storyboard.read_text()
    source = source.replace(
        "music_cues:",
        "  - block_id: opening-card\n    start_frame: 80\n    duration_frames: 20\nmusic_cues:",
    )
    storyboard.write_text(source)
    overlap = run_cli("--project", str(workspace), "storyboard", "validate", "--json")
    assert overlap.returncode == 1
    assert json.loads(overlap.stdout)["diagnostics"][0]["code"] == "STORYBOARD_CUE_OVERLAP"

    workspace, _ = authored_workspace(tmp_path / "reuse")
    storyboard = workspace / "storyboard.yaml"
    source = storyboard.read_text()
    photo = source[source.index("  - item_id: portrait") : source.index("narration_cues:")]
    reused = photo.replace("item_id: portrait", "item_id: portrait-again").replace(
        "duration_frames: 120", "duration_frames: 60"
    )
    storyboard.write_text(source.replace("narration_cues:", reused + "narration_cues:"))
    result = run_cli("--project", str(workspace), "storyboard", "validate", "--json")
    assert result.returncode == 0, result.stdout


def test_unresolved_narration_is_warning_and_stale_hash_is_strict_error(tmp_path: Path) -> None:
    workspace, _ = authored_workspace(tmp_path)
    storyboard = workspace / "storyboard.yaml"
    storyboard.write_text(storyboard.read_text().replace("ceremony-narration", "missing-voice"))
    warning = run_cli("--project", str(workspace), "storyboard", "validate", "--json")
    payload = json.loads(warning.stdout)
    assert warning.returncode == 0
    assert "STORYBOARD_CUE_UNRESOLVED" in {item["code"] for item in payload["warnings"]}
    assert payload["document"]["narration_cues"][0]["block_id"] == "missing-voice"

    storyboard.write_text(
        storyboard.read_text().replace("sha256:", "sha256:" + "0" * 64 + " # ", 1)
    )
    stale = run_cli("--project", str(workspace), "storyboard", "validate", "--strict", "--json")
    assert stale.returncode == 1
    assert json.loads(stale.stdout)["diagnostics"][0]["code"].endswith("HASH_STALE")


def test_status_uses_storyboard_validator(tmp_path: Path) -> None:
    workspace, _ = authored_workspace(tmp_path)
    payload = json.loads(run_cli("--project", str(workspace), "status", "--json").stdout)
    assert payload["layers"]["storyboard"]["state"] == "complete-with-warnings"
    assert payload["layers"]["storyboard"]["reasons"][0]["code"] == (
        "STORYBOARD_VALID_WITH_WARNINGS"
    )


def test_isolated_structure_survives_unavailable_catalog_but_integrated_fails(
    tmp_path: Path,
) -> None:
    workspace, _ = authored_workspace(tmp_path)
    catalog = workspace / "catalog.jsonl"
    catalog.write_text("not-json\n")

    isolated = run_cli("--project", str(workspace), "storyboard", "validate", "--json")
    payload = json.loads(isolated.stdout)
    assert isolated.returncode == 0
    assert payload["document"]["total_frames"] == 180
    assert "STORYBOARD_CATALOG_UNAVAILABLE" in {item["code"] for item in payload["warnings"]}

    integrated = run_cli("--project", str(workspace), "validate", "--json")
    assert integrated.returncode == 1
    assert json.loads(integrated.stdout)["diagnostics"][0]["code"] == "CATALOG_JSON_INVALID"
