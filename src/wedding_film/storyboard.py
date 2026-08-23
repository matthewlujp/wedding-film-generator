from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, TypedDict, cast

import yaml
from yaml.tokens import AliasToken, AnchorToken, TagToken

from wedding_film.catalog import CatalogProblem, validate_catalog
from wedding_film.script import ScriptDocument, WarningMessage, validate_script
from wedding_film.story import (
    Diagnostic,
    DuplicateFieldError,
    StrictLoader,
    story_moment_ids,
    validate_story,
)


class StoryboardValidationPayload(TypedDict):
    artifact: str
    state: str
    document: dict[str, Any] | None
    diagnostics: list[Diagnostic]
    warnings: list[WarningMessage]


_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")


def _diagnostic(path: Path, code: str, location: str, message: str) -> Diagnostic:
    return {"artifact": str(path), "code": code, "location": location, "message": message}


def _warning(path: Path, code: str, location: str, message: str) -> WarningMessage:
    return {"artifact": str(path), "code": code, "location": location, "message": message}


def _fail(path: Path, code: str, location: str, message: str) -> tuple[None, list[Diagnostic]]:
    return None, [_diagnostic(path, code, location, message)]


def _mapping(value: object) -> dict[object, object] | None:
    return cast(dict[object, object], value) if isinstance(value, dict) else None


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _keys(
    path: Path,
    value: dict[object, object],
    required: set[str],
    optional: set[str],
    location: str,
) -> list[Diagnostic]:
    unknown = sorted(str(key) for key in value if key not in required | optional)
    if unknown:
        return [
            _diagnostic(
                path,
                "STORYBOARD_UNKNOWN_FIELD",
                f"{location}.{unknown[0]}",
                f"unknown field {unknown[0]}",
            )
        ]
    missing = sorted(required - set(value))
    if missing:
        return [
            _diagnostic(
                path,
                "STORYBOARD_MISSING_FIELD",
                f"{location}.{missing[0]}",
                f"required field {missing[0]} is missing",
            )
        ]
    return []


def _contains_null(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_contains_null(key) or _contains_null(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_null(item) for item in value)
    return False


def parse_storyboard(path: Path) -> tuple[dict[str, Any] | None, list[Diagnostic]]:
    """Parse and structurally validate authored Storyboard YAML without upstream files."""
    try:
        if not path.exists():
            return _fail(path, "STORYBOARD_MISSING", "$", "storyboard.yaml is absent")
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            return _fail(
                path,
                "STORYBOARD_INVALID_ARTIFACT",
                "$",
                "storyboard.yaml must be a non-empty regular file",
            )
        source = path.read_text(encoding="utf-8")
        if any(
            isinstance(token, AliasToken | AnchorToken | TagToken) for token in yaml.scan(source)
        ):
            raise yaml.YAMLError("aliases, anchors, and tags are unsupported")
        loaded: object = yaml.load(source, Loader=StrictLoader)
    except DuplicateFieldError as error:
        return _fail(
            path,
            "STORYBOARD_DUPLICATE_FIELD",
            "$",
            f"field {error.field} appears more than once",
        )
    except (OSError, UnicodeError):
        return _fail(path, "STORYBOARD_IO_ERROR", "$", "storyboard.yaml could not be read")
    except (ValueError, yaml.YAMLError):
        return _fail(path, "STORYBOARD_YAML_INVALID", "$", "storyboard.yaml is invalid")
    root = _mapping(loaded)
    if root is None:
        return _fail(path, "STORYBOARD_STRUCTURE_INVALID", "$", "Storyboard must be a mapping")
    if _contains_null(root):
        return _fail(path, "STORYBOARD_NULL_FORBIDDEN", "$", "null values are forbidden")
    problem = _keys(
        path,
        root,
        {"schema_version", "output", "inputs", "sequence"},
        {"narration_cues", "music_cues"},
        "$",
    )
    if problem:
        return None, problem
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        return _fail(
            path,
            "STORYBOARD_VERSION_UNSUPPORTED",
            "$.schema_version",
            "schema_version must be the supported integer 1",
        )
    output = _mapping(root["output"])
    if output is None or (
        problem := _keys(path, output, {"width", "height", "fps"}, set(), "$.output")
    ):
        return (
            (None, problem)
            if output is not None
            else _fail(path, "STORYBOARD_OUTPUT_INVALID", "$.output", "output must be a mapping")
        )
    if not all(_positive_int(output[key]) for key in ("width", "height", "fps")):
        return _fail(
            path,
            "STORYBOARD_OUTPUT_INVALID",
            "$.output",
            "width, height, and fps must be positive integers",
        )
    inputs = _mapping(root["inputs"])
    if inputs is None or (
        problem := _keys(path, inputs, {"story", "script", "catalog"}, set(), "$.inputs")
    ):
        return (
            (None, problem)
            if inputs is not None
            else _fail(path, "STORYBOARD_INPUTS_INVALID", "$.inputs", "inputs must be a mapping")
        )
    for name in ("story", "script", "catalog"):
        if not isinstance(inputs[name], str) or _HASH.fullmatch(cast(str, inputs[name])) is None:
            return _fail(
                path,
                "STORYBOARD_INPUT_HASH_INVALID",
                f"$.inputs.{name}",
                f"{name} must be a lowercase sha256 address",
            )
    sequence = root["sequence"]
    if not isinstance(sequence, list) or not sequence:
        return _fail(
            path,
            "STORYBOARD_SEQUENCE_EMPTY",
            "$.sequence",
            "sequence must be a non-empty ordered list",
        )
    seen_items: set[str] = set()
    total = 0
    normalized_sequence: list[dict[str, Any]] = []
    for index, raw_item in enumerate(sequence):
        item = _mapping(raw_item)
        location = f"$.sequence[{index}]"
        if item is None:
            return _fail(path, "STORYBOARD_ITEM_INVALID", location, "item must be a mapping")
        item_type = item.get("type")
        common = {"item_id", "type", "story_moment", "duration_frames"}
        required = common | ({"script_block"} if item_type == "card" else {"asset_id", "motion"})
        optional = {"transition"} | ({"caption"} if item_type == "photo" else set())
        if item_type not in {"card", "photo"}:
            return _fail(
                path,
                "STORYBOARD_ITEM_TYPE_INVALID",
                f"{location}.type",
                "type must be card or photo",
            )
        if problem := _keys(path, item, required, optional, location):
            return None, problem
        item_id = item["item_id"]
        if not isinstance(item_id, str) or _ID.fullmatch(item_id) is None:
            return _fail(
                path,
                "STORYBOARD_ITEM_ID_INVALID",
                f"{location}.item_id",
                "item_id must be kebab-case",
            )
        if item_id in seen_items:
            return _fail(
                path,
                "STORYBOARD_ITEM_ID_DUPLICATE",
                f"{location}.item_id",
                f"item_id {item_id} appears more than once",
            )
        seen_items.add(item_id)
        moment = item["story_moment"]
        if not isinstance(moment, str) or _ID.fullmatch(moment) is None:
            return _fail(
                path,
                "STORYBOARD_STORY_MOMENT_INVALID",
                f"{location}.story_moment",
                "story_moment must be kebab-case",
            )
        if not _positive_int(item["duration_frames"]):
            return _fail(
                path,
                "STORYBOARD_DURATION_INVALID",
                f"{location}.duration_frames",
                "duration_frames must be a positive integer",
            )
        duration = cast(int, item["duration_frames"])
        if item_type == "card":
            block = item["script_block"]
            if not isinstance(block, str) or _ID.fullmatch(block) is None:
                return _fail(
                    path,
                    "STORYBOARD_SCRIPT_BLOCK_INVALID",
                    f"{location}.script_block",
                    "script_block must be kebab-case",
                )
        else:
            asset = item["asset_id"]
            if not isinstance(asset, str) or _HASH.fullmatch(asset) is None:
                return _fail(
                    path,
                    "STORYBOARD_ASSET_ID_INVALID",
                    f"{location}.asset_id",
                    "asset_id must be a lowercase sha256 address",
                )
            if item["motion"] not in {"static", "slow-zoom-in", "slow-zoom-out"}:
                return _fail(
                    path,
                    "STORYBOARD_MOTION_UNSUPPORTED",
                    f"{location}.motion",
                    "motion is unsupported",
                )
            caption = item.get("caption")
            if caption is not None and (
                not isinstance(caption, str) or _ID.fullmatch(caption) is None
            ):
                return _fail(
                    path,
                    "STORYBOARD_SCRIPT_BLOCK_INVALID",
                    f"{location}.caption",
                    "caption must be kebab-case",
                )
        total += duration
        transition = item.get("transition")
        if transition is not None:
            transition_value = _mapping(transition)
            if transition_value is None or "type" not in transition_value:
                return _fail(
                    path,
                    "STORYBOARD_TRANSITION_INVALID",
                    f"{location}.transition",
                    "transition must have a type",
                )
            transition_type = transition_value["type"]
            expected = {"type"} if transition_type == "cut" else {"type", "duration_frames"}
            if transition_type not in {"cut", "crossfade"} or (
                problem := _keys(path, transition_value, expected, set(), f"{location}.transition")
            ):
                return (
                    (None, problem)
                    if transition_type in {"cut", "crossfade"}
                    else _fail(
                        path,
                        "STORYBOARD_TRANSITION_INVALID",
                        f"{location}.transition.type",
                        "transition type must be cut or crossfade",
                    )
                )
            if transition_type == "crossfade":
                crossfade = transition_value["duration_frames"]
                if index == len(sequence) - 1 or not _positive_int(crossfade):
                    return _fail(
                        path,
                        "STORYBOARD_TRANSITION_INVALID",
                        f"{location}.transition",
                        "crossfade must be positive and cannot be final",
                    )
                next_item = _mapping(sequence[index + 1])
                next_duration = next_item.get("duration_frames") if next_item else None
                if (
                    not _positive_int(next_duration)
                    or cast(int, crossfade) >= duration
                    or cast(int, crossfade) >= cast(int, next_duration)
                ):
                    return _fail(
                        path,
                        "STORYBOARD_TRANSITION_INVALID",
                        f"{location}.transition.duration_frames",
                        "crossfade must be shorter than both adjacent items",
                    )
                total -= cast(int, crossfade)
        normalized_sequence.append(cast(dict[str, Any], item))

    normalized: dict[str, Any] = {
        "schema_version": 1,
        "output": cast(dict[str, Any], output),
        "inputs": cast(dict[str, Any], inputs),
        "sequence": normalized_sequence,
        "total_frames": total,
    }
    for cue_kind in ("narration_cues", "music_cues"):
        if cue_kind not in root:
            continue
        cues = root[cue_kind]
        if not isinstance(cues, list):
            return _fail(path, "STORYBOARD_CUES_INVALID", f"$.{cue_kind}", "cues must be a list")
        normalized_cues: list[dict[str, Any]] = []
        seen_cues: set[str] = set()
        ranges: list[tuple[int, int]] = []
        for index, raw_cue in enumerate(cues):
            cue = _mapping(raw_cue)
            location = f"$.{cue_kind}[{index}]"
            required = (
                {"block_id", "start_frame", "duration_frames"}
                if cue_kind == "narration_cues"
                else {"cue_id", "start_frame", "duration_frames", "intent"}
            )
            if cue is None or (problem := _keys(path, cue, required, set(), location)):
                return (
                    (None, problem)
                    if cue is not None
                    else _fail(path, "STORYBOARD_CUE_INVALID", location, "cue must be a mapping")
                )
            cue_id = cue["block_id" if cue_kind == "narration_cues" else "cue_id"]
            if not isinstance(cue_id, str) or _ID.fullmatch(cue_id) is None:
                return _fail(
                    path, "STORYBOARD_CUE_ID_INVALID", location, "cue ID must be kebab-case"
                )
            if cue_kind == "music_cues" and cue_id in seen_cues:
                return _fail(
                    path,
                    "STORYBOARD_CUE_ID_DUPLICATE",
                    location,
                    f"cue ID {cue_id} appears more than once",
                )
            if cue_kind == "music_cues":
                seen_cues.add(cue_id)
            start, cue_duration = cue["start_frame"], cue["duration_frames"]
            if (
                not _nonnegative_int(start)
                or not _positive_int(cue_duration)
                or cast(int, start) + cast(int, cue_duration) > total
            ):
                return _fail(
                    path,
                    "STORYBOARD_CUE_BOUNDS_INVALID",
                    location,
                    "cue must fit within the Storyboard timeline",
                )
            end = cast(int, start) + cast(int, cue_duration)
            if any(
                cast(int, start) < prior_end and prior_start < end
                for prior_start, prior_end in ranges
            ):
                return _fail(
                    path, "STORYBOARD_CUE_OVERLAP", location, "same-kind cues must not overlap"
                )
            ranges.append((cast(int, start), end))
            if cue_kind == "music_cues" and (
                not isinstance(cue["intent"], str) or not cue["intent"].strip()
            ):
                return _fail(
                    path,
                    "STORYBOARD_MUSIC_INTENT_INVALID",
                    f"{location}.intent",
                    "intent must be a non-empty string",
                )
            normalized_cues.append(cast(dict[str, Any], cue))
        normalized[cue_kind] = normalized_cues
    return normalized, []


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def validate_storyboard(
    storyboard_path: Path,
    story_path: Path | None,
    script_path: Path | None,
    catalog_path: Path | None,
    workspace: Path | None,
    *,
    require_catalog_integrity: bool = False,
) -> tuple[dict[str, Any] | None, list[Diagnostic], list[WarningMessage]]:
    document, diagnostics = parse_storyboard(storyboard_path)
    if document is None:
        return None, diagnostics, []
    warnings: list[WarningMessage] = []
    moments: set[str] | None = None
    script: ScriptDocument | None = None
    assets: set[str] | None = None
    if story_path is not None and not validate_story(story_path):
        try:
            moments = set(story_moment_ids(story_path))
            if document["inputs"]["story"] != _sha256(story_path):
                warnings.append(
                    _warning(
                        storyboard_path,
                        "STORYBOARD_STORY_HASH_STALE",
                        "$.inputs.story",
                        "storyboard.yaml was authored against different story.md bytes",
                    )
                )
        except (OSError, UnicodeError, ValueError):
            moments = None
    elif story_path is not None:
        warnings.append(
            _warning(
                storyboard_path,
                "STORYBOARD_STORY_UNAVAILABLE",
                "$.inputs.story",
                "Story cross-reference validation is unavailable",
            )
        )
    if script_path is not None:
        script, script_diagnostics, _ = validate_script(script_path, story_path)
        if script_diagnostics:
            script = None
            warnings.append(
                _warning(
                    storyboard_path,
                    "STORYBOARD_SCRIPT_UNAVAILABLE",
                    "$.inputs.script",
                    "Script cross-reference validation is unavailable",
                )
            )
        elif script is not None:
            try:
                if document["inputs"]["script"] != _sha256(script_path):
                    warnings.append(
                        _warning(
                            storyboard_path,
                            "STORYBOARD_SCRIPT_HASH_STALE",
                            "$.inputs.script",
                            "storyboard.yaml was authored against different script.md bytes",
                        )
                    )
            except OSError:
                script = None
    if catalog_path is not None and workspace is not None:
        try:
            records = validate_catalog(catalog_path, workspace)
            assets = {cast(str, record["asset_id"]) for record in records}
            if document["inputs"]["catalog"] != _sha256(catalog_path):
                warnings.append(
                    _warning(
                        storyboard_path,
                        "STORYBOARD_CATALOG_HASH_STALE",
                        "$.inputs.catalog",
                        "storyboard.yaml was authored against different catalog.jsonl bytes",
                    )
                )
        except CatalogProblem as problem:
            if require_catalog_integrity and catalog_path.exists():
                return (
                    document,
                    [
                        _diagnostic(
                            storyboard_path,
                            problem.code,
                            "$.inputs.catalog",
                            problem.message,
                        )
                    ],
                    [],
                )
            assets = None
        except OSError:
            assets = None
        if assets is None:
            warnings.append(
                _warning(
                    storyboard_path,
                    "STORYBOARD_CATALOG_UNAVAILABLE",
                    "$.inputs.catalog",
                    "Semantic Catalog cross-reference validation is unavailable",
                )
            )
    for index, item in enumerate(document["sequence"]):
        location = f"$.sequence[{index}]"
        if moments is not None and item["story_moment"] not in moments:
            return (
                document,
                [
                    _diagnostic(
                        storyboard_path,
                        "STORYBOARD_STORY_MOMENT_UNKNOWN",
                        f"{location}.story_moment",
                        f"Story Moment {item['story_moment']} does not exist",
                    )
                ],
                [],
            )
        if item["type"] == "photo" and assets is not None and item["asset_id"] not in assets:
            return (
                document,
                [
                    _diagnostic(
                        storyboard_path,
                        "STORYBOARD_ASSET_UNKNOWN",
                        f"{location}.asset_id",
                        f"asset_id {item['asset_id']} does not exist",
                    )
                ],
                [],
            )
    blocks = {block["block_id"]: block for block in script["blocks"]} if script else None
    used_blocks: set[str] = set()
    if blocks is not None:
        for index, item in enumerate(document["sequence"]):
            references = (
                [("script_block", "card")]
                if item["type"] == "card"
                else ([("caption", "caption")] if "caption" in item else [])
            )
            for field, expected_type in references:
                block_id = item[field]
                used_blocks.add(block_id)
                block = blocks.get(block_id)
                if block is None:
                    return (
                        document,
                        [
                            _diagnostic(
                                storyboard_path,
                                "STORYBOARD_SCRIPT_BLOCK_UNKNOWN",
                                f"$.sequence[{index}].{field}",
                                f"Script Block {block_id} does not exist",
                            )
                        ],
                        [],
                    )
                if block["type"] != expected_type or block["story_moment"] != item["story_moment"]:
                    return (
                        document,
                        [
                            _diagnostic(
                                storyboard_path,
                                "STORYBOARD_SCRIPT_BLOCK_MISMATCH",
                                f"$.sequence[{index}].{field}",
                                "Script Block type and Story Moment must match the item",
                            )
                        ],
                        [],
                    )
        for index, cue in enumerate(document.get("narration_cues", [])):
            block_id = cue["block_id"]
            block = blocks.get(block_id)
            if block is None or block["type"] != "narration":
                warnings.append(
                    _warning(
                        storyboard_path,
                        "STORYBOARD_CUE_UNRESOLVED",
                        f"$.narration_cues[{index}].block_id",
                        f"Narration cue {block_id} is unresolved",
                    )
                )
            else:
                used_blocks.add(block_id)
        unused_blocks = sorted(set(blocks) - used_blocks)
        if unused_blocks:
            warnings.append(
                _warning(
                    storyboard_path,
                    "STORYBOARD_SCRIPT_BLOCK_UNUSED",
                    "$.sequence",
                    f"Script Block {unused_blocks[0]} is unused",
                )
            )
    if assets is not None:
        used_assets = {item["asset_id"] for item in document["sequence"] if item["type"] == "photo"}
        unused_assets = sorted(assets - used_assets)
        if unused_assets:
            warnings.append(
                _warning(
                    storyboard_path,
                    "STORYBOARD_ASSET_UNUSED",
                    "$.sequence",
                    f"asset_id {unused_assets[0]} is unused",
                )
            )
    if moments is not None:
        used_moments = {item["story_moment"] for item in document["sequence"]}
        unused_moments = sorted(moments - used_moments)
        if unused_moments:
            warnings.append(
                _warning(
                    storyboard_path,
                    "STORYBOARD_STORY_MOMENT_UNUSED",
                    "$.sequence",
                    f"Story Moment {unused_moments[0]} is unused",
                )
            )
    if story_path is not None and moments is not None:
        try:
            lines = story_path.read_text(encoding="utf-8").splitlines()
            duration_line = next(
                line for line in lines if line.startswith("target_duration_seconds:")
            )
            target = float(duration_line.partition(":")[2].strip())
            actual = document["total_frames"] / document["output"]["fps"]
            if actual != target:
                warnings.append(
                    _warning(
                        storyboard_path,
                        "STORYBOARD_RUNTIME_DEVIATION",
                        "$.sequence",
                        f"runtime {actual:g}s differs from Story target {target:g}s",
                    )
                )
        except (OSError, StopIteration, ValueError, ZeroDivisionError):
            pass
    return document, [], warnings


def write_storyboard_validation(
    workspace: Path, as_json: bool, strict: bool, *, integrated: bool = False
) -> int:
    storyboard = workspace / "storyboard.yaml"
    document, diagnostics, warnings = validate_storyboard(
        storyboard,
        workspace / "story.md",
        workspace / "script.md",
        workspace / "catalog.jsonl",
        workspace,
        require_catalog_integrity=integrated,
    )
    if strict and warnings and not diagnostics:
        diagnostics = [Diagnostic(**warnings[0])]
        warnings = []
    payload: StoryboardValidationPayload = {
        "artifact": str(storyboard),
        "state": "invalid" if diagnostics else "complete-with-warnings" if warnings else "ready",
        "document": document,
        "diagnostics": diagnostics,
        "warnings": warnings,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"artifact={storyboard} state={payload['state']}")
        for item in [*diagnostics, *warnings]:
            print(
                f"artifact={item['artifact']} location={item['location']} "
                f"code={item['code']} message={item['message']}"
            )
    return 1 if diagnostics else 0
