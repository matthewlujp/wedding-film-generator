from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from PIL import Image, ImageFilter, UnidentifiedImageError

from wedding_film.catalog import CatalogProblem, JsonObject, validate_catalog
from wedding_film.script import validate_script
from wedding_film.status import probe_rough_cut
from wedding_film.story import validate_story
from wedding_film.storyboard import validate_storyboard

WIDTH = 1920
HEIGHT = 1080
FPS = 24
BACKGROUND_BLUR_SIGMA = 30
BACKGROUND_DARKEN_ALPHA = 0.45
CARD_PLACEHOLDER_COLOR = (24, 24, 32)
ENCODE_TIMEOUT_SECONDS = 120

_ORIENTATION_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


class RenderProblem(Exception):
    def __init__(self, code: str, artifact: Path, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.artifact = artifact
        self.message = message


def _problem(code: str, artifact: Path, message: str) -> RenderProblem:
    return RenderProblem(code, artifact, message)


@dataclass(frozen=True)
class RenderResult:
    artifact: Path
    frame_count: int


@dataclass(frozen=True)
class _ResolvedItem:
    duration_frames: int
    source: Path | None
    orientation: int


def _cover_fit(image: Image.Image, width: int, height: int) -> Image.Image:
    scale = max(width / image.width, height / image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    resized = image.resize(size, Image.Resampling.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _contain_fit(image: Image.Image, width: int, height: int) -> Image.Image:
    scale = min(width / image.width, height / image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _decode_source(source: Path, orientation: int) -> Image.Image:
    try:
        with Image.open(source) as raw:
            raw.load()
            image = raw.convert("RGB")
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as error:
        raise _problem(
            "RENDER_ASSET_DECODE_FAILED", source, "Original Asset could not be decoded"
        ) from error
    transpose = _ORIENTATION_TRANSPOSE.get(orientation)
    return image if transpose is None else image.transpose(transpose)


def _photo_frame(source: Path, orientation: int) -> Image.Image:
    image = _decode_source(source, orientation)
    background = _cover_fit(image, WIDTH, HEIGHT)
    background = background.filter(ImageFilter.GaussianBlur(BACKGROUND_BLUR_SIGMA))
    black = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    background = Image.blend(background, black, BACKGROUND_DARKEN_ALPHA)
    foreground = _contain_fit(image, WIDTH, HEIGHT)
    left = (WIDTH - foreground.width) // 2
    top = (HEIGHT - foreground.height) // 2
    background.paste(foreground, (left, top))
    return background


def _card_frame() -> Image.Image:
    return Image.new("RGB", (WIDTH, HEIGHT), CARD_PLACEHOLDER_COLOR)


def _run_ffmpeg(command: list[str], artifact: Path, code: str) -> None:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=ENCODE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _problem(code, artifact, "ffmpeg could not be executed") from error
    if result.returncode != 0:
        raise _problem(code, artifact, f"ffmpeg failed: {result.stderr.strip()[-500:]}")


def _preflight_document(
    story_path: Path, script_path: Path, storyboard_path: Path, catalog_path: Path, workspace: Path
) -> dict[str, Any]:
    story_diagnostics = validate_story(story_path)
    if story_diagnostics:
        problem = story_diagnostics[0]
        raise _problem(problem["code"], story_path, problem["message"])
    _, script_diagnostics, _ = validate_script(script_path, story_path)
    if script_diagnostics:
        problem = script_diagnostics[0]
        raise _problem(problem["code"], script_path, problem["message"])
    document, storyboard_diagnostics, _ = validate_storyboard(
        storyboard_path,
        story_path,
        script_path,
        catalog_path,
        workspace,
        require_catalog_integrity=True,
    )
    if storyboard_diagnostics or document is None:
        problem = storyboard_diagnostics[0]
        raise _problem(problem["code"], storyboard_path, problem["message"])
    return document


def _preflight_scope(document: dict[str, Any], storyboard_path: Path) -> None:
    output = document["output"]
    if (output["width"], output["height"], output["fps"]) != (WIDTH, HEIGHT, FPS):
        raise _problem(
            "RENDER_OUTPUT_UNSUPPORTED",
            storyboard_path,
            f"render only supports {WIDTH}x{HEIGHT}@{FPS}fps output",
        )
    for item in document["sequence"]:
        transition = item.get("transition")
        if transition is not None and transition["type"] != "cut":
            raise _problem(
                "RENDER_TRANSITION_UNSUPPORTED",
                storyboard_path,
                f"transition type {transition['type']} is not supported by this render",
            )
        if item["type"] == "photo" and item["motion"] != "static":
            raise _problem(
                "RENDER_MOTION_UNSUPPORTED",
                storyboard_path,
                f"motion {item['motion']} is not supported by this render",
            )


def _resolve_items(
    document: dict[str, Any], catalog_path: Path, workspace: Path
) -> list[_ResolvedItem]:
    try:
        records = validate_catalog(catalog_path, workspace)
    except CatalogProblem as problem:
        raise _problem(problem.code, catalog_path, problem.message) from problem
    records_by_id: dict[str, JsonObject] = {record["asset_id"]: record for record in records}

    resolved: list[_ResolvedItem] = []
    for item in document["sequence"]:
        if item["type"] == "card":
            resolved.append(
                _ResolvedItem(
                    duration_frames=cast(int, item["duration_frames"]), source=None, orientation=1
                )
            )
            continue
        asset_id = cast(str, item["asset_id"])
        record = records_by_id.get(asset_id)
        if record is None:
            raise _problem(
                "RENDER_ASSET_UNKNOWN", catalog_path, f"asset {asset_id} is not present in catalog"
            )
        source = workspace / record["locators"][0]
        orientation = cast(
            int, record.get("observations", {}).get("orientation", {}).get("value", 1)
        )
        _decode_source(source, orientation)
        resolved.append(
            _ResolvedItem(
                duration_frames=cast(int, item["duration_frames"]),
                source=source,
                orientation=orientation,
            )
        )
    return resolved


def _encode_segment(
    ffmpeg: str, frame: Path, duration_frames: int, segment: Path, storyboard_path: Path
) -> None:
    _run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-loop",
            "1",
            "-i",
            str(frame),
            "-frames:v",
            str(duration_frames),
            "-r",
            str(FPS),
            "-vf",
            "setsar=1",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-an",
            str(segment),
        ],
        storyboard_path,
        "RENDER_ENCODE_FAILED",
    )


def _concat_segments(
    ffmpeg: str, segments: list[Path], staging: Path, candidate: Path, storyboard_path: Path
) -> None:
    list_path = staging / "segments.txt"
    list_path.write_text(
        "".join(f"file '{segment.as_posix()}'\n" for segment in segments), encoding="utf-8"
    )
    _run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            "-an",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(candidate),
        ],
        storyboard_path,
        "RENDER_CONCAT_FAILED",
    )


def render_rough_cut(workspace: Path) -> RenderResult:
    story_path = workspace / "story.md"
    script_path = workspace / "script.md"
    storyboard_path = workspace / "storyboard.yaml"
    catalog_path = workspace / "catalog.jsonl"
    destination = workspace / "renders" / "rough-cut.mp4"

    document = _preflight_document(
        story_path, script_path, storyboard_path, catalog_path, workspace
    )
    _preflight_scope(document, storyboard_path)

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise _problem("RENDER_FFMPEG_MISSING", Path("ffmpeg"), "ffmpeg is not available on PATH")
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise _problem(
            "RENDER_FFPROBE_MISSING", Path("ffprobe"), "ffprobe is not available on PATH"
        )

    resolved_items = _resolve_items(document, catalog_path, workspace)
    expected_frames = sum(item.duration_frames for item in resolved_items)

    candidates_dir = workspace / ".work" / "candidates"
    if candidates_dir.is_symlink() or not candidates_dir.is_dir():
        raise _problem(
            "RENDER_CANDIDATES_DIR_INVALID",
            candidates_dir,
            "candidate render directory must be a regular workspace directory",
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    candidate = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.candidate"
    staging = Path(tempfile.mkdtemp(prefix=f".rough-cut.{uuid.uuid4().hex}.", dir=candidates_dir))
    try:
        segments: list[Path] = []
        for index, item in enumerate(resolved_items):
            frame_path = staging / f"frame-{index:04d}.png"
            frame = _photo_frame(item.source, item.orientation) if item.source else _card_frame()
            frame.save(frame_path)
            segment_path = staging / f"segment-{index:04d}.mp4"
            _encode_segment(ffmpeg, frame_path, item.duration_frames, segment_path, storyboard_path)
            segments.append(segment_path)

        _concat_segments(ffmpeg, segments, staging, candidate, storyboard_path)

        if not probe_rough_cut(ffprobe, candidate, expected_frames):
            raise _problem(
                "RENDER_VERIFICATION_FAILED",
                candidate,
                "rendered candidate failed delivery-contract verification",
            )
        candidate.replace(destination)
    except BaseException:
        with suppress(OSError):
            candidate.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return RenderResult(artifact=destination, frame_count=expected_frames)
