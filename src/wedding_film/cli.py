from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import NoReturn, TextIO

import yaml

from wedding_film.status import write_status

SUCCESS = 0
INVALID_OR_PREFLIGHT = 1
PARTIAL_OR_BUDGET_STOP = 2
INTERRUPTED = 130


class CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(
            "workspace=<unspecified> phase=cli artifact=<none> "
            f"code=CLI_INPUT_INVALID message={message}",
            file=sys.stderr,
        )
        raise SystemExit(INVALID_OR_PREFLIGHT)


def _project_id(workspace: Path) -> str:
    project_id = re.sub(r"[^a-z0-9]+", "-", workspace.name.lower()).strip("-")
    if project_id:
        return project_id
    suffix = hashlib.sha256(workspace.name.encode("utf-8")).hexdigest()[:8]
    return f"project-{suffix}"


def _configuration(workspace: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": _project_id(workspace),
        "display_title": workspace.name.replace("-", " ").replace("_", " ").title(),
        "generation_language": "ja",
        "adapters": {
            "vision": {"name": "none", "model": "none", "prompt_version": "v1"},
            "narrative": {"name": "none", "model": "none", "prompt_version": "v1"},
        },
        "analysis_defaults": {
            "max_assets": 100,
            "max_estimated_usd": 1.0,
            "concurrency": 5,
        },
    }


def _unsafe_destination(workspace: Path) -> str | None:
    absolute = workspace.absolute()
    if absolute == Path(absolute.anchor) or absolute == Path.home():
        return "destination is a protected directory"

    current = absolute
    while not current.exists() and current != current.parent:
        current = current.parent
    if current.is_symlink():
        return "destination has a symbolic-link ancestor"
    if not current.is_dir():
        return "destination parent is not a directory"

    if workspace.is_symlink():
        return "destination is a symbolic link"
    if workspace.exists() and not workspace.is_dir():
        return "destination is not a directory"
    if workspace.exists() and any(workspace.iterdir()):
        return "destination is not empty"
    return None


def _emit(workspace: Path, code: str, message: str, *, stream: TextIO = sys.stdout) -> None:
    print(
        f"workspace={workspace} phase=project artifact=project.yaml code={code} message={message}",
        file=stream,
    )


def initialize(workspace: Path) -> int:
    reason = _unsafe_destination(workspace)
    if reason is not None:
        _emit(workspace, "UNSAFE_DESTINATION", reason, stream=sys.stderr)
        return INVALID_OR_PREFLIGHT

    workspace.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{workspace.name}.init-", dir=workspace.parent))
    try:
        (staging / "runs" / "analysis").mkdir(parents=True)
        (staging / ".work" / "candidates").mkdir(parents=True)
        (staging / "renders").mkdir()
        (staging / "project.yaml").write_text(
            yaml.safe_dump(_configuration(workspace), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        (staging / "participants.yaml").write_text(
            yaml.safe_dump(
                {"schema_version": 1, "participants": []},
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        os.replace(staging, workspace)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    _emit(workspace, "PROJECT_INITIALIZED", "Project Workspace initialized")
    return SUCCESS


def build_parser() -> argparse.ArgumentParser:
    parser = CliParser(prog="wedding-film")
    parser.add_argument("--project", type=Path, required=True, help="explicit Project Workspace")
    commands = parser.add_subparsers(dest="command", required=True)
    project = commands.add_parser("project")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_commands.add_parser("init")
    status = commands.add_parser("status")
    status.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.command == "project" and arguments.project_command == "init":
            return initialize(arguments.project)
        if arguments.command == "status":
            return write_status(arguments.project, arguments.as_json)
        return INVALID_OR_PREFLIGHT
    except KeyboardInterrupt:
        return INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
