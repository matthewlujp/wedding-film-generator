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
ZOOM_MIN_SCALE = 0.95
ZOOM_MAX_SCALE = 1.0

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
    motion: str


def _cover_fit(image: Image.Image, width: int, height: int) -> Image.Image:
    scale = max(width / image.width, height / image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    resized = image.resize(size, Image.Resampling.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _contain_fit(image: Image.Image, width: int, height: int, scale: float = 1.0) -> Image.Image:
    fit = min(width / image.width, height / image.height) * scale
    size = (max(1, round(image.width * fit)), max(1, round(image.height * fit)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _motion_scales(motion: str, duration_frames: int) -> list[float]:
    """Linear, endpoint-exact scale ramp: 95%->100% zoom-in, reversed for zoom-out."""
    if duration_frames <= 1 or motion == "static":
        return [1.0] * duration_frames
    span = ZOOM_MAX_SCALE - ZOOM_MIN_SCALE
    positions = [index / (duration_frames - 1) for index in range(duration_frames)]
    if motion == "slow-zoom-in":
        return [ZOOM_MIN_SCALE + span * position for position in positions]
    return [ZOOM_MAX_SCALE - span * position for position in positions]


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


def _photo_background(image: Image.Image) -> Image.Image:
    background = _cover_fit(image, WIDTH, HEIGHT)
    background = background.filter(ImageFilter.GaussianBlur(BACKGROUND_BLUR_SIGMA))
    black = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    return Image.blend(background, black, BACKGROUND_DARKEN_ALPHA)


def _compose_photo_frame(image: Image.Image, background: Image.Image, scale: float) -> Image.Image:
    foreground = _contain_fit(image, WIDTH, HEIGHT, scale)
    frame = background.copy()
    left = (WIDTH - foreground.width) // 2
    top = (HEIGHT - foreground.height) // 2
    frame.paste(foreground, (left, top))
    return frame


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


def _sequence_transitions(document: dict[str, Any]) -> list[tuple[str, int | None]]:
    """Transition[i] describes how sequence[i] joins into sequence[i + 1]."""
    transitions: list[tuple[str, int | None]] = []
    for item in document["sequence"][:-1]:
        transition = item.get("transition")
        if transition is None or transition["type"] == "cut":
            transitions.append(("cut", None))
        else:
            transitions.append(("crossfade", cast(int, transition["duration_frames"])))
    return transitions


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
                    duration_frames=cast(int, item["duration_frames"]),
                    source=None,
                    orientation=1,
                    motion="static",
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
                motion=cast(str, item["motion"]),
            )
        )
    return resolved


def _encode_static_segment(
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


def _encode_motion_segment(
    ffmpeg: str,
    frame_dir: Path,
    image: Image.Image,
    background: Image.Image,
    motion: str,
    duration_frames: int,
    segment: Path,
    storyboard_path: Path,
) -> None:
    frame_dir.mkdir()
    for frame_index, scale in enumerate(_motion_scales(motion, duration_frames)):
        frame = _compose_photo_frame(image, background, scale)
        frame.save(frame_dir / f"frame-{frame_index:04d}.png")
    _run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-framerate",
            str(FPS),
            "-i",
            str(frame_dir / "frame-%04d.png"),
            "-frames:v",
            str(duration_frames),
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


def _join_segments(
    ffmpeg: str,
    segments: list[Path],
    frame_counts: list[int],
    transitions: list[tuple[str, int | None]],
    candidate: Path,
    storyboard_path: Path,
) -> None:
    inputs: list[str] = []
    for segment in segments:
        inputs += ["-i", str(segment)]

    filters = [
        f"[{index}:v]fps={FPS},format=yuv420p,setsar=1,setpts=PTS-STARTPTS[n{index}]"
        for index in range(len(segments))
    ]

    current_label = "n0"
    current_frames = frame_counts[0]
    for index in range(1, len(segments)):
        transition_type, crossfade_frames = transitions[index - 1]
        next_label = f"j{index}"
        if transition_type == "crossfade":
            crossfade_frames = cast(int, crossfade_frames)
            offset = (current_frames - crossfade_frames) / FPS
            duration = crossfade_frames / FPS
            filters.append(
                f"[{current_label}][n{index}]xfade=transition=fade:"
                f"duration={duration:.9f}:offset={offset:.9f}[{next_label}]"
            )
            current_frames += frame_counts[index] - crossfade_frames
        else:
            filters.append(f"[{current_label}][n{index}]concat=n=2:v=1:a=0[{next_label}]")
            current_frames += frame_counts[index]
        current_label = next_label

    _run_ffmpeg(
        [
            ffmpeg,
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            f"[{current_label}]",
            "-r",
            str(FPS),
            "-pix_fmt",
            "yuv420p",
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
    expected_frames = cast(int, document["total_frames"])
    transitions = _sequence_transitions(document)

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
        frame_counts: list[int] = []
        for index, item in enumerate(resolved_items):
            frame_counts.append(item.duration_frames)
            segment_path = staging / f"segment-{index:04d}.mp4"
            if item.source is None:
                frame_path = staging / f"frame-{index:04d}.png"
                _card_frame().save(frame_path)
                _encode_static_segment(
                    ffmpeg, frame_path, item.duration_frames, segment_path, storyboard_path
                )
            elif item.motion == "static":
                image = _decode_source(item.source, item.orientation)
                frame_path = staging / f"frame-{index:04d}.png"
                _compose_photo_frame(image, _photo_background(image), 1.0).save(frame_path)
                _encode_static_segment(
                    ffmpeg, frame_path, item.duration_frames, segment_path, storyboard_path
                )
            else:
                image = _decode_source(item.source, item.orientation)
                background = _photo_background(image)
                _encode_motion_segment(
                    ffmpeg,
                    staging / f"frames-{index:04d}",
                    image,
                    background,
                    item.motion,
                    item.duration_frames,
                    segment_path,
                    storyboard_path,
                )
            segments.append(segment_path)

        if len(segments) == 1:
            _run_ffmpeg(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(segments[0]),
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
        else:
            _join_segments(ffmpeg, segments, frame_counts, transitions, candidate, storyboard_path)

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
