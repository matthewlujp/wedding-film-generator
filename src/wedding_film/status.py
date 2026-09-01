from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Literal, TypedDict, cast

from wedding_film.adapters import required_credentials
from wedding_film.catalog import CatalogProblem, inspect_materials, validate_catalog
from wedding_film.config import (
    ConfigProblem,
    ProjectConfig,
    load_project_config,
)
from wedding_film.interview import validate_interview
from wedding_film.script import validate_script
from wedding_film.story import validate_story
from wedding_film.storyboard import parse_storyboard, validate_storyboard
from wedding_film.workspace import unsafe_destination_reason

State = Literal["missing", "invalid", "stale", "ready", "complete-with-warnings"]


class StatusMessage(TypedDict):
    code: str
    message: str


class Fact(TypedDict):
    phase: str
    state: State
    reasons: list[StatusMessage]
    artifacts: list[str]
    upstream_hashes: dict[str, str]
    warnings: list[StatusMessage]
    next_commands: list[str]


class StatusPayload(TypedDict):
    schema_version: int
    workspace: str
    state: State
    prerequisites: dict[str, Fact]
    layers: dict[str, Fact]
    warnings: list[StatusMessage]
    safe_next_commands: list[str]


def _fact(
    state: State,
    code: str,
    message: str,
    artifacts: list[str],
    *,
    phase: str,
    upstream_hashes: dict[str, str] | None = None,
    warnings: list[StatusMessage] | None = None,
    next_commands: list[str] | None = None,
) -> Fact:
    return {
        "phase": phase,
        "state": state,
        "reasons": [{"code": code, "message": message}],
        "artifacts": artifacts,
        "upstream_hashes": upstream_hashes or {},
        "warnings": warnings or [],
        "next_commands": next_commands or [],
    }


def _command(workspace: Path, suffix: str) -> str:
    return f"wedding-film --project {shlex.quote(str(workspace))} {suffix}"


def _io_error_fact(code_prefix: str, artifact: Path, phase: str) -> Fact:
    return _fact(
        "invalid",
        f"{code_prefix}_IO_ERROR",
        f"{artifact.name} could not be inspected",
        [str(artifact)],
        phase=phase,
    )


def _configuration_fact(workspace: Path) -> tuple[Fact, ProjectConfig | None]:
    config_path = workspace / "project.yaml"
    artifact = str(config_path)
    try:
        config = load_project_config(workspace)
    except ConfigProblem as problem:
        state: State = "missing" if problem.code == "CONFIG_MISSING" else "invalid"
        next_commands = []
        if state == "missing":
            try:
                if unsafe_destination_reason(workspace) is None:
                    next_commands = [_command(workspace, "project init")]
            except OSError:
                return _io_error_fact("CONFIG", config_path, "project"), None
        fact = _fact(
            state,
            problem.code,
            problem.message,
            [artifact],
            phase="project",
            next_commands=next_commands,
        )
        return fact, None
    return _fact(
        "ready",
        "CONFIG_VALID",
        "project configuration is valid",
        [artifact],
        phase="project",
    ), config


def _credentials_fact(config: ProjectConfig | None) -> Fact:
    if config is None:
        return _fact(
            "missing",
            "CREDENTIAL_CHECK_BLOCKED",
            "credentials cannot be checked until project configuration is valid",
            ["process-environment"],
            phase="project",
        )
    required = required_credentials((config.vision.name, config.narrative.name))
    missing = [variable for variable in required if not os.environ.get(variable)]
    if missing:
        return _fact(
            "missing",
            "CREDENTIAL_MISSING",
            f"the selected adapter requires {missing[0]} in the process environment",
            [f"process-environment:{missing[0]}"],
            phase="project",
        )
    return _fact(
        "ready",
        "CREDENTIALS_AVAILABLE" if required else "CREDENTIALS_NOT_REQUIRED",
        "required process-environment credentials are available"
        if required
        else "selected adapters require no credentials",
        ["process-environment"],
        phase="project",
    )


def _executable_fact(command: str) -> Fact:
    try:
        executable = shutil.which(command)
    except OSError:
        return _io_error_fact(command.upper(), Path(command), "render")
    if executable is None:
        return _fact(
            "missing",
            f"{command.upper()}_MISSING",
            f"{command} is not available on PATH",
            [command],
            phase="render",
        )
    return _fact(
        "ready",
        f"{command.upper()}_AVAILABLE",
        f"{command} is available",
        [executable],
        phase="render",
    )


def _usable(fact: Fact) -> bool:
    return fact["state"] in ("ready", "complete-with-warnings")


def _materials_fact(workspace: Path) -> Fact:
    materials = workspace / "materials"
    try:
        if materials.is_symlink():
            return _fact(
                "invalid",
                "MATERIALS_UNSAFE",
                "Materials must be a real directory inside the Project Workspace",
                [str(materials)],
                phase="catalog",
            )
        if not materials.exists():
            return _fact(
                "missing",
                "MATERIALS_MISSING",
                "user-managed Materials directory is absent",
                [str(materials)],
                phase="catalog",
            )
        if not materials.is_dir():
            return _fact(
                "invalid",
                "MATERIALS_UNSAFE",
                "Materials must be a real directory inside the Project Workspace",
                [str(materials)],
                phase="catalog",
            )
        warnings: list[StatusMessage] = []
        if not any(materials.iterdir()):
            warnings.append(
                {"code": "MATERIALS_EMPTY", "message": "Materials contains no entries"}
            )
    except OSError:
        return _io_error_fact("MATERIALS", materials, "catalog")
    return _fact(
        "ready",
        "MATERIALS_READY",
        "user-managed Materials directory is available",
        [str(materials)],
        phase="catalog",
        warnings=warnings,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _current_hashes(
    artifacts: dict[str, Path], code_prefix: str, phase: str
) -> tuple[dict[str, str], Fact | None]:
    hashes: dict[str, str] = {}
    for name, path in artifacts.items():
        try:
            if path.is_file() and not path.is_symlink():
                hashes[name] = _sha256(path)
        except OSError:
            return {}, _io_error_fact(code_prefix, path, phase)
    return hashes, None


def _artifact_preflight(
    artifact: Path,
    code_prefix: str,
    phase: str,
    dependencies: dict[str, Fact],
    upstream_hashes: dict[str, str],
    *,
    allow_empty: bool = False,
) -> Fact | None:
    try:
        if not artifact.exists():
            return _fact(
                "missing",
                f"{code_prefix}_MISSING",
                f"{artifact.name} is absent",
                [str(artifact)],
                phase=phase,
                upstream_hashes=upstream_hashes,
            )
        if (
            artifact.is_symlink()
            or not artifact.is_file()
            or (artifact.stat().st_size == 0 and not allow_empty)
        ):
            return _fact(
                "invalid",
                f"{code_prefix}_INVALID_ARTIFACT",
                f"{artifact.name} must be a non-empty regular file",
                [str(artifact)],
                phase=phase,
                upstream_hashes=upstream_hashes,
            )
        with artifact.open("rb") as source:
            source.read(1)
    except OSError:
        return _io_error_fact(code_prefix, artifact, phase)
    blocked = [name for name, fact in dependencies.items() if not _usable(fact)]
    if blocked:
        return _fact(
            "stale",
            f"{code_prefix}_UPSTREAM_NOT_READY",
            f"{artifact.name} cannot be current while upstream {blocked[0]} is not ready",
            [str(artifact)],
            phase=phase,
            upstream_hashes=upstream_hashes,
        )
    return None


def _catalog_fact(workspace: Path, materials: Fact) -> Fact:
    catalog = workspace / "catalog.jsonl"
    manifest = None
    hashes: dict[str, str] = {}
    if _usable(materials):
        try:
            manifest = inspect_materials(workspace)
            hashes["materials"] = manifest.digest
        except CatalogProblem as problem:
            return _fact(
                "invalid",
                problem.code,
                problem.message,
                [str(workspace / "materials")],
                phase="semantic_catalog",
            )
    preflight = _artifact_preflight(
        catalog,
        "SEMANTIC_CATALOG",
        "semantic_catalog",
        {"materials": materials},
        hashes,
        allow_empty=True,
    )
    if preflight is not None:
        if preflight["state"] == "missing" and _usable(materials):
            preflight["next_commands"] = [_command(workspace, "catalog scan")]
        return preflight
    try:
        validate_catalog(catalog, workspace, manifest=manifest)
    except CatalogProblem as problem:
        if problem.code == "CATALOG_SOURCE_INTEGRITY":
            return _fact(
                "stale",
                "CATALOG_SCAN_REQUIRED",
                "Materials changed after the current catalog scan",
                [str(catalog)],
                phase="semantic_catalog",
                upstream_hashes=hashes,
                next_commands=[_command(workspace, "catalog scan")],
            )
        return _fact(
            "invalid",
            problem.code,
            problem.message,
            [str(catalog)],
            phase="semantic_catalog",
            upstream_hashes=hashes,
        )
    return _fact(
        "ready",
        "SEMANTIC_CATALOG_VALID",
        "catalog.jsonl is structurally valid and source-integrity checked",
        [str(catalog)],
        phase="semantic_catalog",
        upstream_hashes=hashes,
    )


def _interview_fact(workspace: Path, catalog_path: Path, catalog: Fact) -> Fact:
    artifact = workspace / "interview" / "brief.yaml"
    hashes, hash_error = _current_hashes(
        {"semantic_catalog": catalog_path}, "INTERVIEW", "interview"
    )
    if hash_error is not None:
        return hash_error
    preflight = _artifact_preflight(
        artifact, "INTERVIEW", "interview", {"semantic_catalog": catalog}, hashes
    )
    if preflight is not None and preflight["state"] != "stale":
        return preflight
    diagnostics = validate_interview(artifact)
    if diagnostics:
        problem = diagnostics[0]
        return _fact(
            "invalid",
            problem["code"],
            f"location={problem['location']} {problem['message']}",
            [str(artifact)],
            phase="interview",
            upstream_hashes=hashes,
        )
    if preflight is not None:
        return preflight
    return _fact(
        "ready",
        "INTERVIEW_VALID",
        "interview brief satisfies every required section",
        [str(artifact)],
        phase="interview",
        upstream_hashes=hashes,
    )


def _story_fact(workspace: Path, dependencies: dict[str, tuple[Path, Fact]]) -> Fact:
    artifact = workspace / "story.md"
    hashes, hash_error = _current_hashes(
        {key: value[0] for key, value in dependencies.items()}, "STORY", "story"
    )
    if hash_error is not None:
        return hash_error
    preflight = _artifact_preflight(
        artifact,
        "STORY",
        "story",
        {key: value[1] for key, value in dependencies.items()},
        hashes,
    )
    if preflight is not None and preflight["state"] != "stale":
        return preflight
    diagnostics = validate_story(artifact)
    if diagnostics:
        problem = diagnostics[0]
        return _fact(
            "invalid",
            problem["code"],
            f"location={problem['location']} {problem['message']}",
            [str(artifact)],
            phase="story",
            upstream_hashes=hashes,
            next_commands=[_command(workspace, "validate")],
        )
    if preflight is not None:
        preflight["next_commands"] = [_command(workspace, "validate")]
        return preflight
    return _fact(
        "ready",
        "STORY_VALID",
        "story.md is structurally valid",
        [str(artifact)],
        phase="story",
        upstream_hashes=hashes,
        next_commands=[_command(workspace, "validate")],
    )


def _script_fact(workspace: Path, story_path: Path, story: Fact) -> Fact:
    artifact = workspace / "script.md"
    hashes, hash_error = _current_hashes({"story": story_path}, "SCRIPT", "script")
    if hash_error is not None:
        return hash_error
    preflight = _artifact_preflight(
        artifact,
        "SCRIPT",
        "script",
        {"story": story},
        hashes,
    )
    if preflight is not None:
        if preflight["state"] != "missing":
            preflight["next_commands"] = [_command(workspace, "script validate")]
        return preflight
    _, diagnostics, validation_warnings = validate_script(artifact, story_path)
    if diagnostics:
        problem = diagnostics[0]
        return _fact(
            "invalid",
            problem["code"],
            f"location={problem['location']} {problem['message']}",
            [str(artifact)],
            phase="script",
            upstream_hashes=hashes,
            next_commands=[_command(workspace, "script validate")],
        )
    warnings: list[StatusMessage] = [
        {"code": warning["code"], "message": warning["message"]}
        for warning in validation_warnings
    ]
    if warnings:
        return _fact(
            "complete-with-warnings",
            "SCRIPT_VALID_WITH_WARNINGS",
            "script.md is structurally valid with provenance warnings",
            [str(artifact)],
            phase="script",
            upstream_hashes=hashes,
            warnings=warnings,
            next_commands=[_command(workspace, "script validate")],
        )
    return _fact(
        "ready",
        "SCRIPT_VALID",
        "script.md is structurally and cross-reference valid",
        [str(artifact)],
        phase="script",
        upstream_hashes=hashes,
        next_commands=[_command(workspace, "script validate")],
    )


def _storyboard_fact(
    workspace: Path,
    catalog_path: Path,
    catalog: Fact,
    interview_path: Path,
    interview: Fact,
    story_path: Path,
    story: Fact,
    script_path: Path,
    script: Fact,
) -> Fact:
    artifact = workspace / "storyboard.yaml"
    dependencies = {
        "semantic_catalog": (catalog_path, catalog),
        "interview": (interview_path, interview),
        "story": (story_path, story),
        "script": (script_path, script),
    }
    hashes, hash_error = _current_hashes(
        {name: value[0] for name, value in dependencies.items()},
        "STORYBOARD",
        "storyboard",
    )
    if hash_error is not None:
        return hash_error
    preflight = _artifact_preflight(
        artifact,
        "STORYBOARD",
        "storyboard",
        {name: value[1] for name, value in dependencies.items()},
        hashes,
    )
    if preflight is not None:
        if preflight["state"] != "missing":
            preflight["next_commands"] = [_command(workspace, "storyboard validate")]
        return preflight
    _, diagnostics, validation_warnings = validate_storyboard(
        artifact,
        story_path,
        script_path,
        catalog_path,
        workspace,
        require_catalog_integrity=True,
    )
    if diagnostics:
        problem = diagnostics[0]
        return _fact(
            "invalid",
            problem["code"],
            f"location={problem['location']} {problem['message']}",
            [str(artifact)],
            phase="storyboard",
            upstream_hashes=hashes,
            next_commands=[_command(workspace, "storyboard validate")],
        )
    warnings: list[StatusMessage] = [
        {"code": warning["code"], "message": warning["message"]}
        for warning in validation_warnings
    ]
    if warnings:
        return _fact(
            "complete-with-warnings",
            "STORYBOARD_VALID_WITH_WARNINGS",
            "storyboard.yaml is valid with editorial warnings",
            [str(artifact)],
            phase="storyboard",
            upstream_hashes=hashes,
            warnings=warnings,
            next_commands=[_command(workspace, "storyboard validate")],
        )
    return _fact(
        "ready",
        "STORYBOARD_VALID",
        "storyboard.yaml is structurally and cross-reference valid",
        [str(artifact)],
        phase="storyboard",
        upstream_hashes=hashes,
        next_commands=[_command(workspace, "storyboard validate")],
    )


def _expected_storyboard_frames(storyboard: Path) -> int | None:
    document, diagnostics = parse_storyboard(storyboard)
    if diagnostics or document is None:
        return None
    return cast(int, document["total_frames"])


def probe_rough_cut(executable: str, artifact: Path, expected_frames: int) -> bool:
    """Check artifact against the fixed Rough Cut delivery contract via ffprobe."""
    try:
        result = subprocess.run(
            [
                executable,
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
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    format_value = payload.get("format")
    if not isinstance(format_value, dict):
        return False
    format_name = format_value.get("format_name")
    if not isinstance(format_name, str) or "mp4" not in format_name.split(","):
        return False
    streams_value = payload.get("streams")
    if not isinstance(streams_value, list) or not all(
        isinstance(stream, dict) for stream in streams_value
    ):
        return False
    streams = cast(list[dict[str, object]], streams_value)
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    if len(streams) != 1 or len(videos) != 1:
        return False
    video = videos[0]
    return (
        video.get("codec_name") == "h264"
        and video.get("width") == 1920
        and video.get("height") == 1080
        and video.get("pix_fmt") == "yuv420p"
        and video.get("sample_aspect_ratio") == "1:1"
        and video.get("r_frame_rate") == "24/1"
        and video.get("avg_frame_rate") == "24/1"
        and video.get("nb_read_frames") == str(expected_frames)
    )


def _rough_cut_fact(
    workspace: Path, storyboard_path: Path, storyboard: Fact, ffmpeg: Fact, ffprobe: Fact
) -> Fact:
    artifact = workspace / "renders" / "rough-cut.mp4"
    hashes, hash_error = _current_hashes(
        {"storyboard": storyboard_path}, "ROUGH_CUT", "render"
    )
    if hash_error is not None:
        return hash_error
    preflight = _artifact_preflight(
        artifact, "ROUGH_CUT", "render", {"storyboard": storyboard}, hashes
    )
    if preflight is not None:
        return preflight
    try:
        storyboard_mtime = storyboard_path.stat().st_mtime_ns
        artifact_mtime = artifact.stat().st_mtime_ns
    except OSError:
        return _io_error_fact("ROUGH_CUT", artifact, "render")
    if storyboard_mtime > artifact_mtime:
        return _fact(
            "stale",
            "ROUGH_CUT_OLDER_THAN_STORYBOARD",
            "rough-cut.mp4 predates the current storyboard.yaml",
            [str(artifact)],
            phase="render",
            upstream_hashes=hashes,
        )
    unavailable = [
        name
        for name, fact in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe))
        if not _usable(fact)
    ]
    if unavailable:
        return _fact(
            "complete-with-warnings",
            "ROUGH_CUT_COMPLETE_WITH_WARNINGS",
            "rough-cut.mp4 exists but cannot be checked with the complete toolchain",
            [str(artifact)],
            phase="render",
            upstream_hashes=hashes,
            warnings=[
                {
                    "code": "TOOLCHAIN_UNAVAILABLE",
                    "message": f"{unavailable[0]} is unavailable for verification",
                }
            ],
        )
    expected_frames = _expected_storyboard_frames(storyboard_path)
    ffprobe_path = ffprobe["artifacts"][0]
    if expected_frames is None or not probe_rough_cut(ffprobe_path, artifact, expected_frames):
        return _fact(
            "invalid",
            "ROUGH_CUT_INVALID_MEDIA",
            "rough-cut.mp4 does not match the fixed delivery contract and frame count",
            [str(artifact)],
            phase="render",
            upstream_hashes=hashes,
        )
    return _fact(
        "ready",
        "ROUGH_CUT_READY",
        "rough-cut.mp4 exists with ready inputs and prerequisites",
        [str(artifact)],
        phase="render",
        upstream_hashes=hashes,
    )


def derive_status(workspace: Path) -> StatusPayload:
    configuration, config = _configuration_fact(workspace)
    ffmpeg = _executable_fact("ffmpeg")
    ffprobe = _executable_fact("ffprobe")
    credentials = _credentials_fact(config)

    catalog_path = workspace / "catalog.jsonl"
    interview_path = workspace / "interview" / "brief.yaml"
    story_path = workspace / "story.md"
    script_path = workspace / "script.md"
    storyboard_path = workspace / "storyboard.yaml"
    materials = _materials_fact(workspace)
    catalog = _catalog_fact(workspace, materials)
    interview = _interview_fact(workspace, catalog_path, catalog)
    story = _story_fact(
        workspace,
        {"interview": (interview_path, interview)},
    )
    script = _script_fact(workspace, story_path, story)
    storyboard = _storyboard_fact(
        workspace,
        catalog_path,
        catalog,
        interview_path,
        interview,
        story_path,
        story,
        script_path,
        script,
    )
    rough_cut = _rough_cut_fact(workspace, storyboard_path, storyboard, ffmpeg, ffprobe)
    prerequisites = {
        "project_configuration": configuration,
        "credentials": credentials,
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
    }
    layers = {
        "materials": materials,
        "semantic_catalog": catalog,
        "interview": interview,
        "story": story,
        "script": script,
        "storyboard": storyboard,
        "rough_cut": rough_cut,
    }
    facts = [*prerequisites.values(), *layers.values()]
    state: State = "ready"
    if any(fact["state"] == "invalid" for fact in facts):
        state = "invalid"
    elif any(fact["state"] == "stale" for fact in facts):
        state = "stale"
    elif any(fact["state"] == "missing" for fact in facts):
        state = "missing"
    elif any(fact["state"] == "complete-with-warnings" for fact in facts):
        state = "complete-with-warnings"
    all_warnings = [warning for fact in facts for warning in fact["warnings"]]
    all_commands = list(
        dict.fromkeys(command for fact in facts for command in fact["next_commands"])
    )
    return {
        "schema_version": 1,
        "workspace": str(workspace),
        "state": state,
        "prerequisites": prerequisites,
        "layers": layers,
        "warnings": all_warnings,
        "safe_next_commands": all_commands,
    }


def _render_fact(kind: str, name: str, fact: Fact) -> None:
    print(f"{kind}.{name} state={fact['state']} phase={fact['phase']}")
    for reason in fact["reasons"]:
        print(f"  reason={reason['code']} message={reason['message']}")
    for artifact in fact["artifacts"]:
        print(f"  artifact={artifact}")
    for upstream, digest in fact["upstream_hashes"].items():
        print(f"  upstream_hash={upstream}:{digest}")
    for warning in fact["warnings"]:
        print(f"  warning={warning['code']} message={warning['message']}")
    for command in fact["next_commands"]:
        print(f"  next={command}")


def write_status(workspace: Path, as_json: bool) -> int:
    status = derive_status(workspace)
    if as_json:
        print(json.dumps(status, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Project Workspace: {status['workspace']}")
        print(f"Pipeline state: {status['state']}")
        for kind in ("prerequisites", "layers"):
            for name, fact in status[kind].items():
                _render_fact(kind[:-1], name, fact)
    configuration = status["prerequisites"]["project_configuration"]
    return 1 if status["state"] == "invalid" or not _usable(configuration) else 0
