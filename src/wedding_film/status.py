from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
from pathlib import Path
from typing import Any

from wedding_film.config import ConfigProblem, ProjectConfig, load_project_config

Fact = dict[str, Any]


def _fact(
    state: str,
    code: str,
    message: str,
    artifacts: list[str],
    *,
    phase: str,
    upstream_hashes: dict[str, str] | None = None,
    warnings: list[dict[str, str]] | None = None,
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
        state = "missing" if problem.code == "CONFIG_MISSING" else "invalid"
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
    if not materials.exists():
        return _fact(
            "missing",
            "MATERIALS_MISSING",
            "user-managed Materials directory is absent",
            [str(materials)],
            phase="catalog",
        )
    if materials.is_symlink() or not materials.is_dir():
        return _fact(
            "invalid",
            "MATERIALS_UNSAFE",
            "Materials must be a real directory inside the Project Workspace",
            [str(materials)],
            phase="catalog",
        )
    warnings: list[dict[str, str]] = []
    if not any(materials.iterdir()):
        warnings.append({"code": "MATERIALS_EMPTY", "message": "Materials contains no entries"})
    return _fact(
        "ready",
        "MATERIALS_READY",
        "user-managed Materials directory is available",
        [str(materials)],
        phase="catalog",
        warnings=warnings,
        next_commands=[_command(workspace, "catalog scan")],
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


def _canonical_fact(
    workspace: Path,
    name: str,
    filename: str,
    dependencies: dict[str, tuple[Path, Fact]],
    next_command: str,
) -> Fact:
    artifact = workspace / filename
    hashes = _current_hashes({key: value[0] for key, value in dependencies.items()})
    if not artifact.exists():
        commands = []
        if all(_usable(fact) for _, fact in dependencies.values()):
            commands = [_command(workspace, next_command)]
        return _fact(
            "missing",
            f"{name.upper()}_MISSING",
            f"{filename} is absent",
            [str(artifact)],
            phase=name,
            upstream_hashes=hashes,
            next_commands=commands,
        )
    if artifact.is_symlink() or not artifact.is_file() or artifact.stat().st_size == 0:
        return _fact(
            "invalid",
            f"{name.upper()}_INVALID_ARTIFACT",
            f"{filename} must be a non-empty regular file",
            [str(artifact)],
            phase=name,
            upstream_hashes=hashes,
            next_commands=[_command(workspace, "validate")],
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
            next_commands=[_command(workspace, "validate")],
        )
    return _fact(
        "ready",
        f"{name.upper()}_READY",
        f"{filename} is present with ready upstream artifacts",
        [str(artifact)],
        phase=name,
        upstream_hashes=hashes,
        next_commands=[_command(workspace, next_command)],
    )


def _rough_cut_fact(
    workspace: Path, storyboard_path: Path, storyboard: Fact, ffmpeg: Fact, ffprobe: Fact
) -> Fact:
    artifact = workspace / "renders" / "rough-cut.mp4"
    hashes = _current_hashes({"storyboard": storyboard_path})
    if not artifact.exists():
        commands = [_command(workspace, "render")] if _usable(storyboard) else []
        return _fact(
            "missing",
            "ROUGH_CUT_MISSING",
            "renders/rough-cut.mp4 is absent",
            [str(artifact)],
            phase="render",
            upstream_hashes=hashes,
            next_commands=commands,
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
            next_commands=[_command(workspace, "validate")],
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
    return _fact(
        "ready",
        "ROUGH_CUT_READY",
        "rough-cut.mp4 exists with ready inputs and prerequisites",
        [str(artifact)],
        phase="render",
        upstream_hashes=hashes,
        next_commands=[_command(workspace, "render")],
    )


def derive_status(workspace: Path) -> dict[str, Any]:
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
        "catalog scan",
    )
    story = _canonical_fact(
        workspace,
        "story",
        "story.md",
        {"semantic_catalog": (catalog_path, catalog)},
        "story generate",
    )
    script = _canonical_fact(
        workspace,
        "script",
        "script.md",
        {"story": (story_path, story)},
        "script generate",
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
        "storyboard generate",
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
    state = "ready"
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
