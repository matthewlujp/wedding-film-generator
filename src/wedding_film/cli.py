from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn, TextIO

import yaml

from wedding_film.catalog import CatalogProblem, JsonObject, load_catalog, scan_catalog
from wedding_film.catalog_review import (
    apply_correction,
    default_actor,
    effective_values,
    filter_records,
    resolve_asset,
    storyboard_referenced_assets,
)
from wedding_film.exif import extract_exif
from wedding_film.participants import (
    UNSET,
    Participant,
    ParticipantProblem,
    add_participant,
    load_participants,
    remove_participant,
    update_participant,
)
from wedding_film.render import RenderProblem, render_rough_cut
from wedding_film.script import validate_script, write_script_validation
from wedding_film.status import write_status
from wedding_film.story import validate_story, write_story_validation
from wedding_film.story_generation import (
    NarrativeProblem,
    adopt_candidate,
    diff_summary,
    generate_candidate,
    render_story_markdown,
    write_candidate_file,
)
from wedding_film.storyboard import write_storyboard_validation
from wedding_film.vision import analyze_asset, run_batch, select_batch
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


def _emit_participant(
    workspace: Path, code: str, message: str, *, stream: TextIO = sys.stdout
) -> None:
    print(
        f"workspace={workspace} phase=participants artifact=participants.yaml "
        f"code={code} message={message}",
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


def analyze(workspace: Path, asset_id: str) -> int:
    try:
        result = analyze_asset(workspace, asset_id)
    except CatalogProblem as problem:
        _emit_catalog(workspace, problem.code, problem.message, stream=sys.stderr)
        return INVALID_OR_PREFLIGHT
    _emit_catalog(
        workspace,
        "VISION_ANALYSIS_COMPLETED",
        f"vision analysis succeeded={result.succeeded} "
        f"reused={result.reused} failed={result.failed}",
    )
    return SUCCESS


def analyze_batch(
    workspace: Path,
    asset_ids: list[str],
    dry_run: bool,
    max_assets: int | None,
    max_estimated_usd: float | None,
    concurrency: int | None,
) -> int:
    ids = asset_ids or None
    try:
        if dry_run:
            plan = select_batch(
                workspace,
                ids,
                max_assets=max_assets,
                max_estimated_usd=max_estimated_usd,
                concurrency=concurrency,
            )
            _emit_catalog(
                workspace,
                "VISION_BATCH_PLAN",
                f"selected={plan.target_count} estimated_cost_usd={plan.estimated_cost_usd:.2f} "
                f"max_assets={plan.max_assets} max_estimated_usd={plan.max_estimated_usd:.2f} "
                f"concurrency={plan.concurrency} asset_ids={','.join(plan.asset_ids)}",
            )
            return SUCCESS
        result = run_batch(
            workspace,
            ids,
            max_assets=max_assets,
            max_estimated_usd=max_estimated_usd,
            concurrency=concurrency,
        )
    except CatalogProblem as problem:
        _emit_catalog(workspace, problem.code, problem.message, stream=sys.stderr)
        return INVALID_OR_PREFLIGHT
    _emit_catalog(
        workspace,
        "VISION_BATCH_COMPLETED",
        f"succeeded={result.succeeded} reused={result.reused} "
        f"failed={result.failed} budget_stopped={result.budget_stopped}",
    )
    return PARTIAL_OR_BUDGET_STOP if (result.failed or result.budget_stopped) else SUCCESS


def _emit_render(
    workspace: Path, code: str, message: str, artifact: Path, *, stream: TextIO = sys.stdout
) -> None:
    print(
        f"workspace={workspace} phase=render artifact={artifact} code={code} message={message}",
        file=stream,
    )


def render(workspace: Path) -> int:
    try:
        result = render_rough_cut(workspace)
    except RenderProblem as problem:
        _emit_render(workspace, problem.code, problem.message, problem.artifact, stream=sys.stderr)
        return INVALID_OR_PREFLIGHT
    _emit_render(
        workspace,
        "ROUGH_CUT_RENDERED",
        f"rough-cut.mp4 rendered with {result.frame_count} frames",
        result.artifact,
    )
    return SUCCESS


def _list_summary(record: JsonObject) -> dict[str, Any]:
    inferences = record.get("inferences", {})
    confidences = [claim["confidence"] for claim in inferences.values()]
    return {
        "asset_id": record["asset_id"],
        "locators": record["locators"],
        "observation_count": len(record.get("observations", {})),
        "inference_count": len(inferences),
        "correction_count": len(record.get("corrections", [])),
        "min_confidence": min(confidences) if confidences else None,
    }


def catalog_list(
    workspace: Path,
    asset_ids: list[str],
    locator_globs: list[str],
    low_confidence: float | None,
    in_storyboard: bool,
    as_json: bool,
) -> int:
    try:
        records = load_catalog(workspace)
    except CatalogProblem as problem:
        _emit_catalog(workspace, problem.code, problem.message, stream=sys.stderr)
        return INVALID_OR_PREFLIGHT
    storyboard_assets = storyboard_referenced_assets(workspace) if in_storyboard else None
    if in_storyboard and storyboard_assets is None:
        storyboard_assets = set()
    filtered = filter_records(
        records,
        asset_ids=asset_ids or None,
        locator_globs=locator_globs or None,
        low_confidence_threshold=low_confidence,
        storyboard_assets=storyboard_assets,
    )
    summaries = [_list_summary(record) for record in filtered]
    if as_json:
        print(json.dumps(summaries, ensure_ascii=False))
    else:
        for summary in summaries:
            print(
                f"{summary['asset_id']} locator={summary['locators'][0]} "
                f"(+{len(summary['locators']) - 1}) "
                f"observations={summary['observation_count']} "
                f"inferences={summary['inference_count']} "
                f"corrections={summary['correction_count']} "
                f"min_confidence={summary['min_confidence']}"
            )
    return SUCCESS


def catalog_show(workspace: Path, asset: str, as_json: bool) -> int:
    try:
        records = load_catalog(workspace)
        record = resolve_asset(records, asset)
    except CatalogProblem as problem:
        _emit_catalog(workspace, problem.code, problem.message, stream=sys.stderr)
        return INVALID_OR_PREFLIGHT
    effective = effective_values(record)
    effective_payload = {
        target: {"value": item.value, "present": item.present, "source": item.source}
        for target, item in effective.items()
        if item.source != "none"
    }
    if as_json:
        payload = {
            "asset_id": record["asset_id"],
            "locators": record["locators"],
            "observations": record.get("observations", {}),
            "inferences": record.get("inferences", {}),
            "corrections": record.get("corrections", []),
            "effective": effective_payload,
        }
        print(json.dumps(payload, ensure_ascii=False))
        return SUCCESS

    print(f"asset_id={record['asset_id']}")
    print(f"locators={','.join(record['locators'])}")
    print("Observations:")
    for name, claim in sorted(record.get("observations", {}).items()):
        print(f"  {name} = {claim['value']!r} (run={claim['run_id']})")
    print("Inferences:")
    for name, claim in sorted(record.get("inferences", {}).items()):
        print(
            f"  {name} = {claim['value']!r} "
            f"(confidence={claim['confidence']}, run={claim['run_id']})"
        )
    print("Corrections:")
    for index, correction in enumerate(record.get("corrections", []), start=1):
        detail = f"value={correction['value']!r}" if correction["op"] == "set" else "(removed)"
        reason = f" reason={correction['reason']!r}" if "reason" in correction else ""
        print(
            f"  {index}. {correction['op']} {correction['target']} {detail} "
            f"at={correction['at']} actor={correction['actor']}{reason}"
        )
    print("Effective:")
    for target in sorted(effective_payload):
        item = effective_payload[target]
        value = item["value"] if item["present"] else "<removed>"
        print(f"  {target} = {value!r} (source={item['source']})")
    return SUCCESS


def catalog_correct(
    workspace: Path,
    op: str,
    target: str,
    value_json: str | None,
    actor: str,
    reason: str | None,
    asset_ids: list[str],
    locator_globs: list[str],
    dry_run: bool,
) -> int:
    value: Any = None
    if op == "set":
        try:
            value = json.loads(value_json) if value_json is not None else None
        except json.JSONDecodeError:
            _emit_catalog(
                workspace, "CATALOG_VALUE_INVALID", "--value must be valid JSON", stream=sys.stderr
            )
            return INVALID_OR_PREFLIGHT
    try:
        result = apply_correction(
            workspace,
            target=target,
            op=op,
            value=value,
            actor=actor,
            reason=reason,
            asset_ids=asset_ids,
            locator_globs=locator_globs,
            dry_run=dry_run,
        )
    except CatalogProblem as problem:
        _emit_catalog(workspace, problem.code, problem.message, stream=sys.stderr)
        return INVALID_OR_PREFLIGHT
    if dry_run:
        _emit_catalog(
            workspace,
            "CATALOG_CORRECTION_PLAN",
            f"resolved={result.resolved_count} target={target} op={op} "
            f"asset_ids={','.join(result.asset_ids)}",
        )
    else:
        _emit_catalog(
            workspace,
            "CATALOG_CORRECTED",
            f"applied={result.resolved_count} target={target} op={op} "
            f"asset_ids={','.join(result.asset_ids)}",
        )
    return SUCCESS


def _participant_payload(participant: Participant) -> dict[str, Any]:
    return {
        "id": participant.id,
        "display_name": participant.display_name,
        "role": participant.role,
        "principal": participant.principal,
    }


def participant_list(workspace: Path, as_json: bool) -> int:
    try:
        participants = load_participants(workspace)
    except ParticipantProblem as problem:
        _emit_participant(workspace, problem.code, problem.message, stream=sys.stderr)
        return INVALID_OR_PREFLIGHT
    payload = [_participant_payload(participant) for participant in participants]
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for entry in payload:
            print(
                f"{entry['id']} display_name={entry['display_name']!r} "
                f"role={entry['role']!r} principal={entry['principal']}"
            )
    return SUCCESS


def participant_add(
    workspace: Path,
    participant_id: str,
    display_name: str | None,
    role: str | None,
    principal: bool,
) -> int:
    try:
        add_participant(
            workspace,
            participant_id=participant_id,
            display_name=display_name,
            role=role,
            principal=principal,
        )
    except ParticipantProblem as problem:
        _emit_participant(workspace, problem.code, problem.message, stream=sys.stderr)
        return INVALID_OR_PREFLIGHT
    _emit_participant(workspace, "PARTICIPANT_ADDED", f"participant {participant_id} added")
    return SUCCESS


def participant_update(
    workspace: Path,
    participant_id: str,
    display_name: str | None,
    clear_display_name: bool,
    role: str | None,
    clear_role: bool,
    principal: bool | None,
) -> int:
    display_name_arg: Any = UNSET
    if clear_display_name:
        display_name_arg = None
    elif display_name is not None:
        display_name_arg = display_name
    role_arg: Any = UNSET
    if clear_role:
        role_arg = None
    elif role is not None:
        role_arg = role
    principal_arg: Any = UNSET if principal is None else principal
    try:
        update_participant(
            workspace,
            participant_id=participant_id,
            display_name=display_name_arg,
            role=role_arg,
            principal=principal_arg,
        )
    except ParticipantProblem as problem:
        _emit_participant(workspace, problem.code, problem.message, stream=sys.stderr)
        return INVALID_OR_PREFLIGHT
    _emit_participant(workspace, "PARTICIPANT_UPDATED", f"participant {participant_id} updated")
    return SUCCESS


def participant_remove(workspace: Path, participant_id: str) -> int:
    try:
        remove_participant(workspace, participant_id=participant_id)
    except ParticipantProblem as problem:
        _emit_participant(workspace, problem.code, problem.message, stream=sys.stderr)
        return INVALID_OR_PREFLIGHT
    _emit_participant(workspace, "PARTICIPANT_REMOVED", f"participant {participant_id} removed")
    return SUCCESS


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
    _, script_diagnostics, script_warnings = validate_script(
        workspace / "script.md", workspace / "story.md"
    )
    if script_diagnostics:
        return write_script_validation(workspace, as_json, strict)
    return write_storyboard_validation(
        workspace,
        as_json,
        strict,
        integrated=True,
        upstream_warnings=script_warnings,
    )


def _emit_narrative(
    workspace: Path, code: str, message: str, *, stream: TextIO = sys.stdout
) -> None:
    print(
        f"workspace={workspace} phase=story artifact=story.md code={code} message={message}",
        file=stream,
    )


def _report_narrative_problem(
    workspace: Path,
    problem: NarrativeProblem | CatalogProblem | ParticipantProblem,
    as_json: bool,
) -> int:
    payload = {"state": "failed", "code": problem.code, "message": problem.message}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        _emit_narrative(workspace, problem.code, problem.message, stream=sys.stderr)
    return INVALID_OR_PREFLIGHT


def story_generate(workspace: Path, as_json: bool) -> int:
    try:
        candidate = generate_candidate(workspace)
    except (NarrativeProblem, CatalogProblem, ParticipantProblem) as problem:
        return _report_narrative_problem(workspace, problem, as_json)

    markdown = render_story_markdown(candidate)
    try:
        candidate_path = write_candidate_file(workspace, markdown)
    except NarrativeProblem as problem:
        return _report_narrative_problem(workspace, problem, as_json)

    diagnostics = validate_story(candidate_path)
    story_path = workspace / "story.md"
    existing = story_path.is_file() and not story_path.is_symlink()

    if diagnostics:
        payload = {
            "state": "candidate-invalid",
            "candidate": str(candidate_path),
            "diagnostics": diagnostics,
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(f"candidate={candidate_path} state=candidate-invalid")
            for item in diagnostics:
                print(
                    f"  code={item['code']} location={item['location']} "
                    f"message={item['message']}"
                )
        return INVALID_OR_PREFLIGHT

    if existing:
        differences = diff_summary(workspace, candidate)
        payload = {
            "state": "candidate-differs",
            "candidate": str(candidate_path),
            "story": str(story_path),
            "differences": differences,
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(f"candidate={candidate_path} state=candidate-differs story={story_path}")
            if differences:
                for line in differences:
                    print(f"  difference: {line}")
            else:
                print("  difference: candidate is textually equivalent to story.md")
            print(f"  next=wedding-film --project {workspace} story adopt --force")
        return INVALID_OR_PREFLIGHT

    payload = {"state": "candidate-ready", "candidate": str(candidate_path)}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"candidate={candidate_path} state=candidate-ready")
        print(f"  next=wedding-film --project {workspace} story adopt")
    return SUCCESS


def story_adopt(workspace: Path, force: bool, as_json: bool) -> int:
    try:
        story_path = adopt_candidate(workspace, force=force)
    except NarrativeProblem as problem:
        return _report_narrative_problem(workspace, problem, as_json)
    payload = {"state": "adopted", "story": str(story_path)}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"story={story_path} state=adopted")
    return SUCCESS


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
    analyze_parser = catalog_commands.add_parser("analyze")
    analyze_parser.add_argument("--asset-id", required=True)
    analyze_batch_parser = catalog_commands.add_parser("analyze-batch")
    analyze_batch_parser.add_argument("--asset-id", action="append", default=[])
    analyze_batch_parser.add_argument("--dry-run", action="store_true")
    analyze_batch_parser.add_argument("--max-assets", type=int, default=None)
    analyze_batch_parser.add_argument("--max-estimated-usd", type=float, default=None)
    analyze_batch_parser.add_argument("--concurrency", type=int, default=None)

    list_parser = catalog_commands.add_parser("list")
    list_parser.add_argument("--asset-id", action="append", default=[])
    list_parser.add_argument("--locator", action="append", default=[])
    list_parser.add_argument("--low-confidence", type=float, default=None)
    list_parser.add_argument("--in-storyboard", action="store_true")
    list_parser.add_argument("--json", action="store_true", dest="as_json")

    show_parser = catalog_commands.add_parser("show")
    show_parser.add_argument("--asset", required=True)
    show_parser.add_argument("--json", action="store_true", dest="as_json")

    correct_parser = catalog_commands.add_parser("correct")
    correct_commands = correct_parser.add_subparsers(dest="correct_command", required=True)

    correct_set = correct_commands.add_parser("set")
    correct_set.add_argument("--target", required=True)
    correct_set.add_argument("--value", required=True)
    correct_set.add_argument("--asset-id", action="append", default=[])
    correct_set.add_argument("--locator", action="append", default=[])
    correct_set.add_argument("--actor", default=None)
    correct_set.add_argument("--reason", default=None)
    correct_set.add_argument("--dry-run", action="store_true")

    correct_remove = correct_commands.add_parser("remove")
    correct_remove.add_argument("--target", required=True)
    correct_remove.add_argument("--asset-id", action="append", default=[])
    correct_remove.add_argument("--locator", action="append", default=[])
    correct_remove.add_argument("--actor", default=None)
    correct_remove.add_argument("--reason", default=None)
    correct_remove.add_argument("--dry-run", action="store_true")

    participant = commands.add_parser("participant")
    participant_commands = participant.add_subparsers(dest="participant_command", required=True)

    participant_list_parser = participant_commands.add_parser("list")
    participant_list_parser.add_argument("--json", action="store_true", dest="as_json")

    participant_add_parser = participant_commands.add_parser("add")
    participant_add_parser.add_argument("--id", required=True, dest="participant_id")
    participant_add_parser.add_argument("--display-name", default=None)
    participant_add_parser.add_argument("--role", default=None)
    participant_add_parser.add_argument("--principal", action="store_true")

    participant_update_parser = participant_commands.add_parser("update")
    participant_update_parser.add_argument("--id", required=True, dest="participant_id")
    name_group = participant_update_parser.add_mutually_exclusive_group()
    name_group.add_argument("--display-name", default=None)
    name_group.add_argument("--clear-display-name", action="store_true")
    role_group = participant_update_parser.add_mutually_exclusive_group()
    role_group.add_argument("--role", default=None)
    role_group.add_argument("--clear-role", action="store_true")
    participant_update_parser.add_argument(
        "--principal", action=argparse.BooleanOptionalAction, default=None
    )

    participant_remove_parser = participant_commands.add_parser("remove")
    participant_remove_parser.add_argument("--id", required=True, dest="participant_id")

    status = commands.add_parser("status")
    status.add_argument("--json", action="store_true", dest="as_json")
    validate = commands.add_parser("validate")
    validate.add_argument("--json", action="store_true", dest="as_json")
    validate.add_argument("--strict", action="store_true")
    story = commands.add_parser("story")
    story_commands = story.add_subparsers(dest="story_command", required=True)
    story_validate = story_commands.add_parser("validate")
    story_validate.add_argument("--json", action="store_true", dest="as_json")
    story_generate_parser = story_commands.add_parser("generate")
    story_generate_parser.add_argument("--json", action="store_true", dest="as_json")
    story_adopt_parser = story_commands.add_parser("adopt")
    story_adopt_parser.add_argument("--force", action="store_true")
    story_adopt_parser.add_argument("--json", action="store_true", dest="as_json")
    script = commands.add_parser("script")
    script_commands = script.add_subparsers(dest="script_command", required=True)
    script_validate = script_commands.add_parser("validate")
    script_validate.add_argument("--json", action="store_true", dest="as_json")
    script_validate.add_argument("--strict", action="store_true")
    storyboard = commands.add_parser("storyboard")
    storyboard_commands = storyboard.add_subparsers(dest="storyboard_command", required=True)
    storyboard_validate = storyboard_commands.add_parser("validate")
    storyboard_validate.add_argument("--json", action="store_true", dest="as_json")
    storyboard_validate.add_argument("--strict", action="store_true")
    render_parser = commands.add_parser("render")
    render_commands = render_parser.add_subparsers(dest="render_command", required=True)
    render_commands.add_parser("rough-cut")
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
        if arguments.command == "catalog" and arguments.catalog_command == "analyze":
            return analyze(arguments.project, arguments.asset_id)
        if arguments.command == "catalog" and arguments.catalog_command == "analyze-batch":
            return analyze_batch(
                arguments.project,
                arguments.asset_id,
                arguments.dry_run,
                arguments.max_assets,
                arguments.max_estimated_usd,
                arguments.concurrency,
            )
        if arguments.command == "catalog" and arguments.catalog_command == "list":
            return catalog_list(
                arguments.project,
                arguments.asset_id,
                arguments.locator,
                arguments.low_confidence,
                arguments.in_storyboard,
                arguments.as_json,
            )
        if arguments.command == "catalog" and arguments.catalog_command == "show":
            return catalog_show(arguments.project, arguments.asset, arguments.as_json)
        if arguments.command == "catalog" and arguments.catalog_command == "correct":
            actor = arguments.actor or default_actor()
            if arguments.correct_command == "set":
                return catalog_correct(
                    arguments.project,
                    "set",
                    arguments.target,
                    arguments.value,
                    actor,
                    arguments.reason,
                    arguments.asset_id,
                    arguments.locator,
                    arguments.dry_run,
                )
            return catalog_correct(
                arguments.project,
                "remove",
                arguments.target,
                None,
                actor,
                arguments.reason,
                arguments.asset_id,
                arguments.locator,
                arguments.dry_run,
            )
        if arguments.command == "participant" and arguments.participant_command == "list":
            return participant_list(arguments.project, arguments.as_json)
        if arguments.command == "participant" and arguments.participant_command == "add":
            return participant_add(
                arguments.project,
                arguments.participant_id,
                arguments.display_name,
                arguments.role,
                arguments.principal,
            )
        if arguments.command == "participant" and arguments.participant_command == "update":
            return participant_update(
                arguments.project,
                arguments.participant_id,
                arguments.display_name,
                arguments.clear_display_name,
                arguments.role,
                arguments.clear_role,
                arguments.principal,
            )
        if arguments.command == "participant" and arguments.participant_command == "remove":
            return participant_remove(arguments.project, arguments.participant_id)
        if arguments.command == "status":
            return write_status(arguments.project, arguments.as_json)
        if arguments.command == "validate":
            return write_validation(arguments.project, arguments.as_json, arguments.strict)
        if arguments.command == "story" and arguments.story_command == "validate":
            return write_story_validation(arguments.project, arguments.as_json)
        if arguments.command == "story" and arguments.story_command == "generate":
            return story_generate(arguments.project, arguments.as_json)
        if arguments.command == "story" and arguments.story_command == "adopt":
            return story_adopt(arguments.project, arguments.force, arguments.as_json)
        if arguments.command == "script" and arguments.script_command == "validate":
            return write_script_validation(arguments.project, arguments.as_json, arguments.strict)
        if arguments.command == "storyboard" and arguments.storyboard_command == "validate":
            return write_storyboard_validation(
                arguments.project, arguments.as_json, arguments.strict
            )
        if arguments.command == "render" and arguments.render_command == "rough-cut":
            return render(arguments.project)
        return INVALID_OR_PREFLIGHT
    except KeyboardInterrupt:
        return INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
