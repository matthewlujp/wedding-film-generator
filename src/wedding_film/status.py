from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Literal, TypedDict, cast

import yaml

from wedding_film.config import ConfigProblem, ProjectConfig, load_project_config

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


def _configuration_fact(workspace: Path) -> tuple[Fact, ProjectConfig | None]:
    artifact = str(workspace / "project.yaml")
    try:
        config = load_project_config(workspace)
    except ConfigProblem as problem:
        state: State = "missing" if problem.code == "CONFIG_MISSING" else "invalid"
        next_commands = []
        if state == "missing" and not workspace.exists():
            next_commands = [_command(workspace, "project init")]
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
    openai_selected = config.vision.name == "openai" or config.narrative.name == "openai"
    if openai_selected and not os.environ.get("OPENAI_API_KEY"):
        return _fact(
            "missing",
            "CREDENTIAL_MISSING",
            "the selected adapter requires OPENAI_API_KEY in the process environment",
            ["process-environment:OPENAI_API_KEY"],
            phase="project",
        )
    return _fact(
        "ready",
        "CREDENTIALS_AVAILABLE" if openai_selected else "CREDENTIALS_NOT_REQUIRED",
        "required process-environment credentials are available"
        if openai_selected
        else "selected adapters require no credentials",
        ["process-environment"],
        phase="project",
    )


def _executable_fact(command: str) -> Fact:
    executable = shutil.which(command)
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
        warnings.append({"code": "MATERIALS_EMPTY", "message": "Materials contains no entries"})
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


def _current_hashes(artifacts: dict[str, Path]) -> dict[str, str]:
    return {
        name: _sha256(path)
        for name, path in artifacts.items()
        if path.is_file() and not path.is_symlink()
    }


def _frontmatter(path: Path) -> tuple[dict[str, object], str]:
    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ValueError("artifact is not readable UTF-8 text") from None
    if not contents.startswith("---\n") or "\n---\n" not in contents[4:]:
        raise ValueError("artifact is missing YAML frontmatter")
    frontmatter, body = contents[4:].split("\n---\n", 1)
    try:
        metadata = yaml.safe_load(frontmatter)
    except yaml.YAMLError:
        raise ValueError("artifact frontmatter is invalid YAML") from None
    if not isinstance(metadata, dict) or not all(isinstance(key, str) for key in metadata):
        raise ValueError("artifact frontmatter must be a mapping")
    return metadata, body


def _valid_input_hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def _validate_catalog(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("catalog.jsonl must contain valid UTF-8 JSON objects") from None
    if not records or not all(isinstance(record, dict) for record in records):
        raise ValueError("catalog.jsonl must contain at least one JSON object")


def _validate_story(path: Path) -> None:
    metadata, body = _frontmatter(path)
    title = metadata.get("title")
    target_duration = metadata.get("target_duration_seconds")
    if (
        metadata.get("schema_version") != 1
        or not isinstance(title, str)
        or not title.strip()
        or type(target_duration) is not int
        or target_duration <= 0
    ):
        raise ValueError("story.md frontmatter is invalid")
    headings = re.findall(r"^## (.+)$", body, flags=re.MULTILINE)
    if headings != ["Intent", "Emotional Arc", "Moments"]:
        raise ValueError("story.md requires Intent, Emotional Arc, and Moments in order")
    sections = re.split(r"^## .+$", body, flags=re.MULTILINE)[1:]
    if len(sections) != 3 or any(not section.strip() for section in sections):
        raise ValueError("story.md sections must be non-empty")
    if re.search(r"^### [a-z0-9]+(?:-[a-z0-9]+)*$", sections[2], re.MULTILINE) is None:
        raise ValueError("story.md requires at least one valid Story Moment")


def _validate_script(path: Path) -> None:
    metadata, body = _frontmatter(path)
    title = metadata.get("title")
    inputs = metadata.get("inputs")
    if (
        metadata.get("schema_version") != 1
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(inputs, dict)
        or not _valid_input_hash(inputs.get("story"))
    ):
        raise ValueError("script.md frontmatter is invalid")
    blocks = re.split(r"^## [a-z0-9]+(?:-[a-z0-9]+)*\s*$", body, flags=re.MULTILINE)[1:]
    if not blocks:
        raise ValueError("script.md requires at least one Script Block")
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if (
            len(lines) < 3
            or lines[0] not in ("type: narration", "type: card", "type: caption")
            or re.fullmatch(r"story_moment: [a-z0-9]+(?:-[a-z0-9]+)*", lines[1]) is None
        ):
            raise ValueError("script.md contains an invalid Script Block")


def _validate_storyboard(path: Path) -> None:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ValueError("storyboard.yaml is not valid UTF-8 YAML") from None
    if not isinstance(loaded, dict) or loaded.get("schema_version") != 1:
        raise ValueError("storyboard.yaml root is invalid")
    output = loaded.get("output")
    inputs = loaded.get("inputs")
    sequence = loaded.get("sequence")
    if (
        not isinstance(output, dict)
        or any(
            type(output.get(key)) is not int or output[key] <= 0
            for key in ("width", "height", "fps")
        )
        or not isinstance(inputs, dict)
        or any(not _valid_input_hash(inputs.get(key)) for key in ("story", "script", "catalog"))
        or not isinstance(sequence, list)
        or not sequence
        or not all(isinstance(item, dict) for item in sequence)
    ):
        raise ValueError("storyboard.yaml structure is invalid")


def _validate_canonical(name: str, path: Path) -> None:
    validators = {
        "semantic_catalog": _validate_catalog,
        "story": _validate_story,
        "script": _validate_script,
        "storyboard": _validate_storyboard,
    }
    validators[name](path)


def _recorded_hashes(name: str, path: Path) -> dict[str, str]:
    if name == "script":
        metadata, _ = _frontmatter(path)
        inputs = metadata["inputs"]
        assert isinstance(inputs, dict)
        return {"story": str(inputs["story"])}
    if name == "storyboard":
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        inputs = loaded["inputs"]
        return {key: str(value) for key, value in inputs.items()}
    return {}


def _canonical_fact(
    workspace: Path,
    name: str,
    filename: str,
    dependencies: dict[str, tuple[Path, Fact]],
) -> Fact:
    artifact = workspace / filename
    hashes = _current_hashes({key: value[0] for key, value in dependencies.items()})
    if not artifact.exists():
        return _fact(
            "missing",
            f"{name.upper()}_MISSING",
            f"{filename} is absent",
            [str(artifact)],
            phase=name,
            upstream_hashes=hashes,
        )
    if artifact.is_symlink() or not artifact.is_file() or artifact.stat().st_size == 0:
        return _fact(
            "invalid",
            f"{name.upper()}_INVALID_ARTIFACT",
            f"{filename} must be a non-empty regular file",
            [str(artifact)],
            phase=name,
            upstream_hashes=hashes,
        )
    try:
        _validate_canonical(name, artifact)
    except ValueError as problem:
        return _fact(
            "invalid",
            f"{name.upper()}_INVALID_CONTENT",
            str(problem),
            [str(artifact)],
            phase=name,
            upstream_hashes=hashes,
        )
    blocked = [key for key, (_, fact) in dependencies.items() if not _usable(fact)]
    if blocked:
        return _fact(
            "stale",
            f"{name.upper()}_UPSTREAM_NOT_READY",
            f"{filename} cannot be current while upstream {blocked[0]} is not ready",
            [str(artifact)],
            phase=name,
            upstream_hashes=hashes,
        )
    recorded_hashes = _recorded_hashes(name, artifact)
    mismatches = [
        dependency
        for dependency, digest in hashes.items()
        if dependency in recorded_hashes and recorded_hashes[dependency] != f"sha256:{digest}"
    ]
    if mismatches:
        return _fact(
            "stale",
            f"{name.upper()}_UPSTREAM_HASH_MISMATCH",
            f"{filename} records an older {mismatches[0]} hash",
            [str(artifact)],
            phase=name,
            upstream_hashes=hashes,
        )
    return _fact(
        "ready",
        f"{name.upper()}_READY",
        f"{filename} is present with ready upstream artifacts",
        [str(artifact)],
        phase=name,
        upstream_hashes=hashes,
    )


def _probe_rough_cut(executable: str, artifact: Path) -> bool:
    try:
        result = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,avg_frame_rate",
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
    streams_value = payload.get("streams")
    if not isinstance(streams_value, list) or not all(
        isinstance(stream, dict) for stream in streams_value
    ):
        return False
    streams = cast(list[dict[str, object]], streams_value)
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(videos) != 1 or audios:
        return False
    video = videos[0]
    return (
        video.get("codec_name") == "h264"
        and video.get("width") == 1920
        and video.get("height") == 1080
        and video.get("pix_fmt") == "yuv420p"
        and video.get("r_frame_rate") == "24/1"
        and video.get("avg_frame_rate") == "24/1"
    )


def _rough_cut_fact(
    workspace: Path, storyboard_path: Path, storyboard: Fact, ffmpeg: Fact, ffprobe: Fact
) -> Fact:
    artifact = workspace / "renders" / "rough-cut.mp4"
    hashes = _current_hashes({"storyboard": storyboard_path})
    if not artifact.exists():
        return _fact(
            "missing",
            "ROUGH_CUT_MISSING",
            "renders/rough-cut.mp4 is absent",
            [str(artifact)],
            phase="render",
            upstream_hashes=hashes,
        )
    if artifact.is_symlink() or not artifact.is_file() or artifact.stat().st_size == 0:
        return _fact(
            "invalid",
            "ROUGH_CUT_INVALID_ARTIFACT",
            "rough-cut.mp4 must be a non-empty regular file",
            [str(artifact)],
            phase="render",
            upstream_hashes=hashes,
        )
    if not _usable(storyboard):
        return _fact(
            "stale",
            "ROUGH_CUT_UPSTREAM_NOT_READY",
            "rough-cut.mp4 cannot be current while Storyboard is not ready",
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
    ffprobe_path = ffprobe["artifacts"][0]
    if not _probe_rough_cut(ffprobe_path, artifact):
        return _fact(
            "invalid",
            "ROUGH_CUT_INVALID_MEDIA",
            "rough-cut.mp4 does not match the fixed delivery contract",
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

    materials_path = workspace / "materials"
    catalog_path = workspace / "catalog.jsonl"
    story_path = workspace / "story.md"
    script_path = workspace / "script.md"
    storyboard_path = workspace / "storyboard.yaml"
    materials = _materials_fact(workspace)
    catalog = _canonical_fact(
        workspace,
        "semantic_catalog",
        "catalog.jsonl",
        {"materials": (materials_path, materials)},
    )
    story = _canonical_fact(
        workspace,
        "story",
        "story.md",
        {"semantic_catalog": (catalog_path, catalog)},
    )
    script = _canonical_fact(
        workspace,
        "script",
        "script.md",
        {"story": (story_path, story)},
    )
    storyboard = _canonical_fact(
        workspace,
        "storyboard",
        "storyboard.yaml",
        {
            "semantic_catalog": (catalog_path, catalog),
            "story": (story_path, story),
            "script": (script_path, script),
        },
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
