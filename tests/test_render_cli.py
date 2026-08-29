from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image
from test_script_cli import valid_script, valid_story


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("wedding-film")
    return subprocess.run(
        [str(executable), *args], check=False, capture_output=True, text=True, env=env
    )


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def probe(artifact: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-show_entries",
            "format=format_name:stream=codec_type,codec_name,width,height,pix_fmt,"
            "sample_aspect_ratio,r_frame_rate,avg_frame_rate,nb_read_frames",
            "-of",
            "json",
            str(artifact),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def storyboard_yaml(
    story: str, script: str, catalog: str, asset_id: str, *, motion: str = "static",
    transition: str = "  - item_id: opening\n    type: card\n    story_moment: preparation\n"
    "    duration_frames: 6\n    script_block: opening-card\n    transition:\n"
    "      type: cut\n",
) -> str:
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
{transition}  - item_id: portrait
    type: photo
    story_moment: ceremony
    duration_frames: 9
    asset_id: {asset_id}
    motion: {motion}
"""


def authored_workspace(
    tmp_path: Path, *, motion: str = "static", transition: str | None = None
) -> tuple[Path, str]:
    workspace = tmp_path / "movie"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    Image.new("RGB", (400, 300), (200, 100, 50)).save(materials / "photo.jpg")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    catalog = (workspace / "catalog.jsonl").read_text()
    asset_id = json.loads(catalog)["asset_id"]
    story = valid_story()
    script = valid_script(story)
    (workspace / "story.md").write_text(story)
    (workspace / "script.md").write_text(script)
    kwargs = {"motion": motion}
    if transition is not None:
        kwargs["transition"] = transition
    (workspace / "storyboard.yaml").write_text(
        storyboard_yaml(story, script, catalog, asset_id, **kwargs)
    )
    return workspace, asset_id


def test_render_produces_exact_delivery_contract_and_frame_count(tmp_path: Path) -> None:
    workspace, _ = authored_workspace(tmp_path)

    result = run_cli("--project", str(workspace), "render", "rough-cut")

    assert result.returncode == 0, result.stderr
    assert "ROUGH_CUT_RENDERED" in result.stdout
    artifact = workspace / "renders" / "rough-cut.mp4"
    assert artifact.is_file()

    payload = probe(artifact)
    streams = payload["streams"]
    assert len(streams) == 1
    video = streams[0]
    assert video["codec_name"] == "h264"
    assert video["width"] == 1920
    assert video["height"] == 1080
    assert video["pix_fmt"] == "yuv420p"
    assert video["sample_aspect_ratio"] == "1:1"
    assert video["r_frame_rate"] == "24/1"
    assert video["avg_frame_rate"] == "24/1"
    assert video["nb_read_frames"] == "15"
    assert "mp4" in payload["format"]["format_name"].split(",")


def test_render_is_rerunnable_from_unchanged_sources(tmp_path: Path) -> None:
    workspace, _ = authored_workspace(tmp_path)
    materials_bytes = (workspace / "materials" / "photo.jpg").read_bytes()

    first = run_cli("--project", str(workspace), "render", "rough-cut")
    second = run_cli("--project", str(workspace), "render", "rough-cut")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert (workspace / "renders" / "rough-cut.mp4").is_file()
    assert (workspace / "materials" / "photo.jpg").read_bytes() == materials_bytes


def test_render_refuses_invalid_storyboard_before_encoding(tmp_path: Path) -> None:
    workspace, _ = authored_workspace(tmp_path)
    storyboard = workspace / "storyboard.yaml"
    storyboard.write_text(storyboard.read_text().replace("motion: static", "motion: pan-left"))

    result = run_cli("--project", str(workspace), "render", "rough-cut")

    assert result.returncode == 1
    assert "STORYBOARD_MOTION_UNSUPPORTED" in result.stderr
    assert not (workspace / "renders" / "rough-cut.mp4").exists()
    assert not any((workspace / ".work" / "candidates").iterdir())


def test_render_rejects_out_of_scope_motion_and_transition(tmp_path: Path) -> None:
    workspace, _ = authored_workspace(tmp_path, motion="slow-zoom-in")
    motion_result = run_cli("--project", str(workspace), "render", "rough-cut")
    assert motion_result.returncode == 1
    assert "RENDER_MOTION_UNSUPPORTED" in motion_result.stderr
    assert not (workspace / "renders" / "rough-cut.mp4").exists()

    crossfade_transition = (
        "  - item_id: opening\n    type: card\n    story_moment: preparation\n"
        "    duration_frames: 6\n    script_block: opening-card\n    transition:\n"
        "      type: crossfade\n      duration_frames: 3\n"
    )
    workspace, _ = authored_workspace(
        tmp_path / "crossfade", transition=crossfade_transition
    )
    transition_result = run_cli("--project", str(workspace), "render", "rough-cut")
    assert transition_result.returncode == 1
    assert "RENDER_TRANSITION_UNSUPPORTED" in transition_result.stderr
    assert not (workspace / "renders" / "rough-cut.mp4").exists()


def test_render_preserves_prior_rough_cut_and_sources_on_later_failure(tmp_path: Path) -> None:
    workspace, _ = authored_workspace(tmp_path)
    materials_bytes = (workspace / "materials" / "photo.jpg").read_bytes()

    first = run_cli("--project", str(workspace), "render", "rough-cut")
    assert first.returncode == 0, first.stderr
    artifact = workspace / "renders" / "rough-cut.mp4"
    prior_bytes = artifact.read_bytes()

    storyboard = workspace / "storyboard.yaml"
    storyboard.write_text(storyboard.read_text().replace("motion: static", "motion: pan-left"))

    second = run_cli("--project", str(workspace), "render", "rough-cut")

    assert second.returncode == 1
    assert artifact.read_bytes() == prior_bytes
    assert (workspace / "materials" / "photo.jpg").read_bytes() == materials_bytes


def test_render_rejects_undecodable_asset_and_leaves_no_candidate(tmp_path: Path) -> None:
    workspace = tmp_path / "movie"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    (materials / "photo.jpg").write_bytes(b"not a real image")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    catalog = (workspace / "catalog.jsonl").read_text()
    asset_id = json.loads(catalog)["asset_id"]
    story = valid_story()
    script = valid_script(story)
    (workspace / "story.md").write_text(story)
    (workspace / "script.md").write_text(script)
    (workspace / "storyboard.yaml").write_text(storyboard_yaml(story, script, catalog, asset_id))

    result = run_cli("--project", str(workspace), "render", "rough-cut")

    assert result.returncode == 1
    assert "RENDER_ASSET_DECODE_FAILED" in result.stderr
    assert not (workspace / "renders" / "rough-cut.mp4").exists()
    assert not any((workspace / ".work" / "candidates").iterdir())


def test_render_reports_missing_ffmpeg_toolchain(tmp_path: Path) -> None:
    workspace, _ = authored_workspace(tmp_path)
    stripped_env = {key: value for key, value in os.environ.items() if key != "PATH"}
    stripped_env["PATH"] = str(tmp_path / "empty-bin")
    (tmp_path / "empty-bin").mkdir()

    result = run_cli("--project", str(workspace), "render", "rough-cut", env=stripped_env)

    assert result.returncode == 1
    assert "RENDER_FFMPEG_MISSING" in result.stderr
    assert not (workspace / "renders" / "rough-cut.mp4").exists()
