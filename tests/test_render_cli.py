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


def extract_frame(artifact: Path, index: int, out: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(artifact),
            "-vf",
            f"select=eq(n\\,{index})",
            "-vframes",
            "1",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def foreground_width(png_path: Path, *, channel: int, threshold: int) -> int:
    image = Image.open(png_path).convert("RGB")
    y = image.height // 2
    xs = [x for x in range(image.width) if image.getpixel((x, y))[channel] > threshold]
    assert xs, "no foreground pixels detected"
    return max(xs) - min(xs) + 1


def center_pixel(png_path: Path) -> tuple[int, int, int]:
    image = Image.open(png_path).convert("RGB")
    return image.getpixel((image.width // 2, image.height // 2))


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


def set_orientation(workspace: Path, filename: str, orientation: int) -> None:
    catalog_path = workspace / "catalog.jsonl"
    updated_lines = []
    for line in catalog_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if Path(record["locators"][0]).name == filename:
            record["runs"] = {
                "orientation-fixture": {
                    "kind": "extraction",
                    "tool": "test-fixture",
                    "version": "1",
                    "executed_at": "2026-08-23T10:00:00+08:00",
                    "outcome": "success",
                }
            }
            record["observations"] = {
                "orientation": {"value": orientation, "run_id": "orientation-fixture"}
            }
        updated_lines.append(json.dumps(record, separators=(",", ":")))
    catalog_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")


def two_photo_workspace(
    tmp_path: Path,
    *,
    color_a: tuple[int, int, int] = (200, 100, 50),
    color_b: tuple[int, int, int] = (40, 160, 200),
    motion_a: str = "static",
    motion_b: str = "static",
    duration_a: int = 12,
    duration_b: int = 10,
    orientation_b: int | None = None,
    crossfade_frames: int | None = None,
) -> Path:
    workspace = tmp_path / "movie"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    materials = workspace / "materials"
    materials.mkdir()
    Image.new("RGB", (400, 300), color_a).save(materials / "photo-a.jpg")
    Image.new("RGB", (400, 300), color_b).save(materials / "photo-b.jpg")
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    if orientation_b is not None:
        set_orientation(workspace, "photo-b.jpg", orientation_b)
    records = [
        json.loads(line)
        for line in (workspace / "catalog.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    by_name = {Path(record["locators"][0]).name: record["asset_id"] for record in records}
    asset_a = by_name["photo-a.jpg"]
    asset_b = by_name["photo-b.jpg"]
    story = valid_story()
    script = valid_script(story)
    (workspace / "story.md").write_text(story)
    (workspace / "script.md").write_text(script)
    catalog = (workspace / "catalog.jsonl").read_text(encoding="utf-8")
    transition = (
        ""
        if crossfade_frames is None
        else f"    transition:\n      type: crossfade\n      duration_frames: {crossfade_frames}\n"
    )
    (workspace / "storyboard.yaml").write_text(
        f"""schema_version: 1
output:
  width: 1920
  height: 1080
  fps: 24
inputs:
  story: {digest(story)}
  script: {digest(script)}
  catalog: {digest(catalog)}
sequence:
  - item_id: first
    type: photo
    story_moment: ceremony
    duration_frames: {duration_a}
    asset_id: {asset_a}
    motion: {motion_a}
{transition}  - item_id: second
    type: photo
    story_moment: ceremony
    duration_frames: {duration_b}
    asset_id: {asset_b}
    motion: {motion_b}
"""
    )
    return workspace


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


def test_render_supports_zoom_motion_and_crossfade_transitions(tmp_path: Path) -> None:
    zoom_in_workspace, _ = authored_workspace(tmp_path / "zoom-in", motion="slow-zoom-in")
    zoom_in_result = run_cli("--project", str(zoom_in_workspace), "render", "rough-cut")
    assert zoom_in_result.returncode == 0, zoom_in_result.stderr
    assert probe(zoom_in_workspace / "renders" / "rough-cut.mp4")["streams"][0][
        "nb_read_frames"
    ] == "15"

    zoom_out_workspace, _ = authored_workspace(tmp_path / "zoom-out", motion="slow-zoom-out")
    zoom_out_result = run_cli("--project", str(zoom_out_workspace), "render", "rough-cut")
    assert zoom_out_result.returncode == 0, zoom_out_result.stderr
    assert probe(zoom_out_workspace / "renders" / "rough-cut.mp4")["streams"][0][
        "nb_read_frames"
    ] == "15"

    crossfade_transition = (
        "  - item_id: opening\n    type: card\n    story_moment: preparation\n"
        "    duration_frames: 6\n    script_block: opening-card\n    transition:\n"
        "      type: crossfade\n      duration_frames: 3\n"
    )
    crossfade_workspace, _ = authored_workspace(
        tmp_path / "crossfade", transition=crossfade_transition
    )
    crossfade_result = run_cli("--project", str(crossfade_workspace), "render", "rough-cut")
    assert crossfade_result.returncode == 0, crossfade_result.stderr
    assert probe(crossfade_workspace / "renders" / "rough-cut.mp4")["streams"][0][
        "nb_read_frames"
    ] == "12"


def test_render_zoom_in_moves_linearly_with_exact_endpoint_frames(tmp_path: Path) -> None:
    workspace, _ = authored_workspace(tmp_path, motion="slow-zoom-in")
    result = run_cli("--project", str(workspace), "render", "rough-cut")
    assert result.returncode == 0, result.stderr
    artifact = workspace / "renders" / "rough-cut.mp4"

    first_frame = tmp_path / "first.png"
    last_frame = tmp_path / "last.png"
    extract_frame(artifact, 6, first_frame)
    extract_frame(artifact, 14, last_frame)

    first_width = foreground_width(first_frame, channel=0, threshold=150)
    last_width = foreground_width(last_frame, channel=0, threshold=150)

    assert abs(first_width - 1368) <= 4
    assert abs(last_width - 1440) <= 4
    assert last_width > first_width


def test_render_zoom_out_reverses_the_zoom_in_ramp(tmp_path: Path) -> None:
    workspace, _ = authored_workspace(tmp_path, motion="slow-zoom-out")
    result = run_cli("--project", str(workspace), "render", "rough-cut")
    assert result.returncode == 0, result.stderr
    artifact = workspace / "renders" / "rough-cut.mp4"

    first_frame = tmp_path / "first.png"
    last_frame = tmp_path / "last.png"
    extract_frame(artifact, 6, first_frame)
    extract_frame(artifact, 14, last_frame)

    first_width = foreground_width(first_frame, channel=0, threshold=150)
    last_width = foreground_width(last_frame, channel=0, threshold=150)

    assert abs(first_width - 1440) <= 4
    assert abs(last_width - 1368) <= 4
    assert first_width > last_width


def test_render_crossfade_blends_between_photos_with_exact_frame_count(tmp_path: Path) -> None:
    workspace = two_photo_workspace(tmp_path, crossfade_frames=4)

    result = run_cli("--project", str(workspace), "render", "rough-cut")

    assert result.returncode == 0, result.stderr
    artifact = workspace / "renders" / "rough-cut.mp4"
    assert probe(artifact)["streams"][0]["nb_read_frames"] == "18"

    pure_first = tmp_path / "pure-first.png"
    blended = tmp_path / "blended.png"
    pure_second = tmp_path / "pure-second.png"
    extract_frame(artifact, 0, pure_first)
    extract_frame(artifact, 9, blended)
    extract_frame(artifact, 17, pure_second)

    first_pixel = center_pixel(pure_first)
    blended_pixel = center_pixel(blended)
    second_pixel = center_pixel(pure_second)

    assert first_pixel[0] > 150
    assert second_pixel[2] > 150
    assert abs(blended_pixel[0] - first_pixel[0]) > 10
    assert abs(blended_pixel[2] - second_pixel[2]) > 10


def test_render_applies_orientation_before_layout_for_portrait_and_landscape(
    tmp_path: Path,
) -> None:
    workspace = two_photo_workspace(tmp_path, orientation_b=6)

    result = run_cli("--project", str(workspace), "render", "rough-cut")

    assert result.returncode == 0, result.stderr
    artifact = workspace / "renders" / "rough-cut.mp4"

    landscape_frame = tmp_path / "landscape.png"
    portrait_frame = tmp_path / "portrait.png"
    extract_frame(artifact, 0, landscape_frame)
    extract_frame(artifact, 12, portrait_frame)

    landscape_width = foreground_width(landscape_frame, channel=0, threshold=150)
    portrait_width = foreground_width(portrait_frame, channel=2, threshold=150)

    assert abs(landscape_width - 1440) <= 4
    assert abs(portrait_width - 810) <= 4


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
