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

from wedding_film.catalog import CatalogProblem, scan_catalog
from wedding_film.exif import extract_exif
from wedding_film.script import write_script_validation
from wedding_film.status import write_status
from wedding_film.story import validate_story, write_story_validation
from wedding_film.workspace import unsafe_destination_reason

SUCCESS = 0
INVALID_OR_PREFLIGHT = 1
PARTIAL_OR_BUDGET_STOP = 2
INTERRUPTED = 130


def _workspace_hint() -> str:
    arguments = sys.argv[1:]
    for index, argument in enumerate(arguments):
        if argument == "--project" and index + 1 < len(arguments):
            return arguments[index + 1].replace("\n", "\\n").replace("\r", "\\r")
        if argument.startswith("--project="):
            return argument.partition("=")[2].replace("\n", "\\n").replace("\r", "\\r")
    return "<unspecified>"


class CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(
            f"workspace={_workspace_hint()} phase=cli artifact=<none> "
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


def _emit(workspace: Path, code: str, message: str, *, stream: TextIO = sys.stdout) -> None:
    print(
        f"workspace={workspace} phase=project artifact=project.yaml code={code} message={message}",
        file=stream,
    )


def _emit_catalog(workspace: Path, code: str, message: str, *, stream: TextIO = sys.stdout) -> None:
    print(
        f"workspace={workspace} phase=catalog artifact=catalog.jsonl code={code} message={message}",
        file=stream,
    )


def scan(workspace: Path) -> int:
    try:
        count = scan_catalog(workspace)
    except CatalogProblem as problem:
        _emit_catalog(workspace, problem.code, problem.message, stream=sys.stderr)
        return INVALID_OR_PREFLIGHT
    _emit_catalog(workspace, "CATALOG_SCANNED", f"catalog contains {count} Original Assets")
    return SUCCESS


def extract(workspace: Path) -> int:
    try:
        result = extract_exif(workspace)
    except CatalogProblem as problem:
        _emit_catalog(workspace, problem.code, problem.message, stream=sys.stderr)
        return INVALID_OR_PREFLIGHT
    _emit_catalog(
        workspace,
        "EXIF_EXTRACTION_COMPLETED",
        f"EXIF extraction succeeded={result.succeeded} "
        f"reused={result.reused} failed={result.failed}",
    )
    return PARTIAL_OR_BUDGET_STOP if result.failed else SUCCESS


def initialize(workspace: Path) -> int:
    reason = unsafe_destination_reason(workspace)
    if reason is not None:
        _emit(workspace, "UNSAFE_DESTINATION", reason, stream=sys.stderr)
        return INVALID_OR_PREFLIGHT

    staging: Path | None = None
    try:
        workspace.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{workspace.name}.init-", dir=workspace.parent))
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
    except OSError:
        _emit(
            workspace,
            "PROJECT_INIT_IO_ERROR",
            "Project Workspace could not be initialized",
            stream=sys.stderr,
        )
        return INVALID_OR_PREFLIGHT
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    _emit(workspace, "PROJECT_INITIALIZED", "Project Workspace initialized")
    return SUCCESS


def write_validation(workspace: Path, as_json: bool, strict: bool) -> int:
    story = workspace / "story.md"
    if validate_story(story):
        return write_story_validation(workspace, as_json)
    script = workspace / "script.md"
    if script.exists() or script.is_symlink():
        return write_script_validation(workspace, as_json, strict)
    return write_story_validation(workspace, as_json)


def build_parser() -> argparse.ArgumentParser:
    parser = CliParser(prog="wedding-film")
    parser.add_argument("--project", type=Path, required=True, help="explicit Project Workspace")
    commands = parser.add_subparsers(dest="command", required=True)
    project = commands.add_parser("project")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_commands.add_parser("init")
    catalog = commands.add_parser("catalog")
    catalog_commands = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_commands.add_parser("scan")
    catalog_commands.add_parser("extract")
    status = commands.add_parser("status")
    status.add_argument("--json", action="store_true", dest="as_json")
    validate = commands.add_parser("validate")
    validate.add_argument("--json", action="store_true", dest="as_json")
    validate.add_argument("--strict", action="store_true")
    script = commands.add_parser("script")
    script_commands = script.add_subparsers(dest="script_command", required=True)
    script_validate = script_commands.add_parser("validate")
    script_validate.add_argument("--json", action="store_true", dest="as_json")
    script_validate.add_argument("--strict", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.command == "project" and arguments.project_command == "init":
            return initialize(arguments.project)
        if arguments.command == "catalog" and arguments.catalog_command == "scan":
            return scan(arguments.project)
        if arguments.command == "catalog" and arguments.catalog_command == "extract":
            return extract(arguments.project)
        if arguments.command == "status":
            return write_status(arguments.project, arguments.as_json)
        if arguments.command == "validate":
            return write_validation(arguments.project, arguments.as_json, arguments.strict)
        if arguments.command == "script" and arguments.script_command == "validate":
            return write_script_validation(arguments.project, arguments.as_json, arguments.strict)
        return INVALID_OR_PREFLIGHT
    except KeyboardInterrupt:
        return INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
