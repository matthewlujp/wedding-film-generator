from __future__ import annotations

import hashlib
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from wedding_film.catalog import CatalogProblem, JsonObject, load_catalog
from wedding_film.catalog_review import effective_values
from wedding_film.config import ConfigProblem, load_project_config
from wedding_film.interview import InterviewProblem, excluded_asset_ids, load_effective_brief
from wedding_film.narrative_adapter import (
    AdapterFailure,
    AdapterSettings,
    AdapterSuccess,
    NarrativeRequest,
    OutputSchema,
    narrative_adapter_for,
)
from wedding_film.render import FPS, HEIGHT, WIDTH
from wedding_film.script import validate_script
from wedding_film.story import load_story_document, validate_story
from wedding_film.story_generation import NarrativeProblem
from wedding_film.storyboard import parse_storyboard, validate_storyboard

PROMPT = (
    "Using only the provided validated Story Moments, Script Blocks, and effective "
    "Semantic Catalog asset summaries, produce a Storyboard candidate for a wedding "
    "film: an ordered sequence of items, each a card or a photo. A card item needs a "
    "lowercase kebab-case item_id, a story_moment referencing an existing Story "
    "Moment id, a positive integer duration_frames, and a script_block referencing an "
    "existing card Script Block id. A photo item needs the same plus an asset_id "
    "selected from the provided assets by content identity, a motion of static, "
    "slow-zoom-in, or slow-zoom-out, and may reference a matching caption Script "
    "Block id. An item may include a transition of cut or crossfade. Optionally "
    "include narration_cues referencing narration Script Block ids and music_cues "
    "carrying an unresolved music intent. Never reference Original Assets, Asset "
    "Locators, or filenames; select assets only by the provided asset_id."
)
OUTPUT_SCHEMA_VERSION = "storyboard-candidate-v1"
CANDIDATE_RELATIVE_PATH = Path(".work") / "candidates" / "storyboard.candidate.yaml"
_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_MOTIONS = {"static", "slow-zoom-in", "slow-zoom-out"}
_TRANSITION_TYPES = {"cut", "crossfade"}


def _problem(code: str, message: str) -> NarrativeProblem:
    return NarrativeProblem(code, message)


@dataclass(frozen=True)
class TransitionCandidate:
    type: Literal["cut", "crossfade"]
    duration_frames: int | None = None


@dataclass(frozen=True)
class StoryboardItemCandidate:
    item_id: str
    type: Literal["card", "photo"]
    story_moment: str
    duration_frames: int
    script_block: str | None = None
    asset_id: str | None = None
    motion: str | None = None
    transition: TransitionCandidate | None = None


@dataclass(frozen=True)
class NarrationCueCandidate:
    cue_id: str
    block_id: str
    start_frame: int
    duration_frames: int


@dataclass(frozen=True)
class MusicCueCandidate:
    cue_id: str
    start_frame: int
    duration_frames: int
    intent: str


@dataclass(frozen=True)
class StoryboardCandidate:
    story_hash: str
    script_hash: str
    catalog_hash: str
    sequence: tuple[StoryboardItemCandidate, ...]
    narration_cues: tuple[NarrationCueCandidate, ...] = ()
    music_cues: tuple[MusicCueCandidate, ...] = ()


def _schema() -> OutputSchema:
    fields = ("sequence", "narration_cues", "music_cues")
    transition_definition: dict[str, object] = {
        "type": "object",
        "required": ["type"],
        "additionalProperties": False,
        "properties": {
            "type": {"type": "string", "enum": ["cut", "crossfade"]},
            "duration_frames": {"type": "integer"},
        },
    }
    item_definition: dict[str, object] = {
        "type": "object",
        "required": ["item_id", "type", "story_moment", "duration_frames"],
        "additionalProperties": False,
        "properties": {
            "item_id": {"type": "string"},
            "type": {"type": "string", "enum": ["card", "photo"]},
            "story_moment": {"type": "string"},
            "duration_frames": {"type": "integer"},
            "script_block": {"type": "string"},
            "asset_id": {"type": "string"},
            "motion": {"type": "string", "enum": sorted(_MOTIONS)},
            "transition": transition_definition,
        },
    }
    definition: dict[str, object] = {
        "type": "object",
        "required": ["sequence"],
        "additionalProperties": False,
        "properties": {
            "sequence": {"type": "array", "items": item_definition},
            "narration_cues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["cue_id", "block_id", "start_frame", "duration_frames"],
                    "additionalProperties": False,
                    "properties": {
                        "cue_id": {"type": "string"},
                        "block_id": {"type": "string"},
                        "start_frame": {"type": "integer"},
                        "duration_frames": {"type": "integer"},
                    },
                },
            },
            "music_cues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["cue_id", "start_frame", "duration_frames", "intent"],
                    "additionalProperties": False,
                    "properties": {
                        "cue_id": {"type": "string"},
                        "start_frame": {"type": "integer"},
                        "duration_frames": {"type": "integer"},
                        "intent": {"type": "string"},
                    },
                },
            },
        },
    }
    return OutputSchema(version=OUTPUT_SCHEMA_VERSION, fields=fields, definition=definition)


def _asset_context(records: list[JsonObject]) -> list[dict[str, object]]:
    context: list[dict[str, object]] = []
    for record in records:
        effective = effective_values(record)
        inferences = {
            target.removeprefix("/inferences/"): item.value
            for target, item in effective.items()
            if item.present and target.startswith("/inferences/")
        }
        context.append({"asset_id": record["asset_id"], "inferences": inferences})
    return context


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _kebab(value: object) -> bool:
    return isinstance(value, str) and _ID_PATTERN.fullmatch(value) is not None


def _normalize_transition(value: object) -> TransitionCandidate:
    if not isinstance(value, dict):
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "transition must be a mapping")
    transition_type = value.get("type")
    if transition_type not in _TRANSITION_TYPES:
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "transition type must be cut or crossfade")
    if transition_type == "cut":
        if set(value) != {"type"}:
            raise _problem("NARRATIVE_CANDIDATE_INVALID", "a cut transition takes no other field")
        return TransitionCandidate(type="cut")
    if set(value) != {"type", "duration_frames"}:
        raise _problem(
            "NARRATIVE_CANDIDATE_INVALID", "a crossfade transition needs duration_frames"
        )
    duration = value["duration_frames"]
    if not _positive_int(duration):
        raise _problem(
            "NARRATIVE_CANDIDATE_INVALID", "crossfade duration_frames must be a positive integer"
        )
    return TransitionCandidate(type="crossfade", duration_frames=cast(int, duration))


def _normalize_item(item: object) -> StoryboardItemCandidate:
    if not isinstance(item, dict):
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "each sequence item must be a mapping")
    item_type = item.get("type")
    common = {"item_id", "type", "story_moment", "duration_frames"}
    required = common | ({"script_block"} if item_type == "card" else {"asset_id", "motion"})
    optional = {"transition"} | ({"script_block"} if item_type == "photo" else set())
    if item_type not in {"card", "photo"}:
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "sequence item type must be card or photo")
    if set(item) - required - optional:
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "sequence item contains an unknown field")
    if required - set(item):
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "sequence item is missing a required field")
    item_id = item["item_id"]
    story_moment = item["story_moment"]
    duration = item["duration_frames"]
    if not _kebab(item_id):
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "item_id must be lowercase kebab-case")
    if not _kebab(story_moment):
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "story_moment must be lowercase kebab-case")
    if not _positive_int(duration):
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "duration_frames must be a positive integer")
    transition = _normalize_transition(item["transition"]) if "transition" in item else None
    if item_type == "card":
        script_block = item["script_block"]
        if not _kebab(script_block):
            raise _problem("NARRATIVE_CANDIDATE_INVALID", "script_block must be kebab-case")
        return StoryboardItemCandidate(
            item_id=item_id,
            type="card",
            story_moment=story_moment,
            duration_frames=cast(int, duration),
            script_block=script_block,
            transition=transition,
        )
    asset_id = item["asset_id"]
    motion = item["motion"]
    if not isinstance(asset_id, str) or _HASH_PATTERN.fullmatch(asset_id) is None:
        raise _problem(
            "NARRATIVE_CANDIDATE_INVALID", "asset_id must be a lowercase sha256 address"
        )
    if motion not in _MOTIONS:
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "motion is unsupported")
    script_block = item.get("script_block")
    if script_block is not None and not _kebab(script_block):
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "script_block must be kebab-case")
    return StoryboardItemCandidate(
        item_id=item_id,
        type="photo",
        story_moment=story_moment,
        duration_frames=cast(int, duration),
        asset_id=asset_id,
        motion=cast(str, motion),
        script_block=cast("str | None", script_block),
        transition=transition,
    )


def _normalize_narration_cue(value: object) -> NarrationCueCandidate:
    if not isinstance(value, dict) or set(value) != {
        "cue_id",
        "block_id",
        "start_frame",
        "duration_frames",
    }:
        raise _problem(
            "NARRATIVE_CANDIDATE_INVALID",
            "each Narration Cue needs cue_id, block_id, start_frame, and duration_frames",
        )
    cue_id, block_id = value["cue_id"], value["block_id"]
    start, duration = value["start_frame"], value["duration_frames"]
    if not _kebab(cue_id) or not _kebab(block_id):
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "cue_id and block_id must be kebab-case")
    if type(start) is not int or start < 0 or not _positive_int(duration):
        raise _problem(
            "NARRATIVE_CANDIDATE_INVALID",
            "start_frame must be a non-negative integer and duration_frames positive",
        )
    return NarrationCueCandidate(
        cue_id=cue_id, block_id=block_id, start_frame=start, duration_frames=duration
    )


def _normalize_music_cue(value: object) -> MusicCueCandidate:
    if not isinstance(value, dict) or set(value) != {
        "cue_id",
        "start_frame",
        "duration_frames",
        "intent",
    }:
        raise _problem(
            "NARRATIVE_CANDIDATE_INVALID",
            "each Music Cue needs cue_id, start_frame, duration_frames, and intent",
        )
    cue_id = value["cue_id"]
    start, duration, intent = value["start_frame"], value["duration_frames"], value["intent"]
    if not _kebab(cue_id):
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "cue_id must be kebab-case")
    if type(start) is not int or start < 0 or not _positive_int(duration):
        raise _problem(
            "NARRATIVE_CANDIDATE_INVALID",
            "start_frame must be a non-negative integer and duration_frames positive",
        )
    if not isinstance(intent, str) or not intent.strip():
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "intent must be a non-empty string")
    return MusicCueCandidate(
        cue_id=cue_id, start_frame=start, duration_frames=duration, intent=intent
    )


def _normalize_candidate(
    payload: object,
) -> tuple[
    tuple[StoryboardItemCandidate, ...],
    tuple[NarrationCueCandidate, ...],
    tuple[MusicCueCandidate, ...],
]:
    if not isinstance(payload, dict):
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "candidate must be a JSON object")
    if set(payload) - {"sequence", "narration_cues", "music_cues"}:
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "candidate contains an unknown field")
    if "sequence" not in payload:
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "candidate is missing sequence")
    sequence_value = payload["sequence"]
    if not isinstance(sequence_value, list) or not sequence_value:
        raise _problem(
            "NARRATIVE_CANDIDATE_INVALID", "candidate sequence must be a non-empty array"
        )
    items: list[StoryboardItemCandidate] = []
    seen_items: set[str] = set()
    for raw_item in sequence_value:
        item = _normalize_item(raw_item)
        if item.item_id in seen_items:
            raise _problem(
                "NARRATIVE_CANDIDATE_INVALID", f"item_id {item.item_id} appears more than once"
            )
        seen_items.add(item.item_id)
        items.append(item)
    narration_cues: list[NarrationCueCandidate] = []
    seen_cues: set[str] = set()
    for raw_cue in payload.get("narration_cues", []):
        cue = _normalize_narration_cue(raw_cue)
        if cue.cue_id in seen_cues:
            raise _problem(
                "NARRATIVE_CANDIDATE_INVALID", f"cue ID {cue.cue_id} appears more than once"
            )
        seen_cues.add(cue.cue_id)
        narration_cues.append(cue)
    music_cues: list[MusicCueCandidate] = []
    for raw_music_cue in payload.get("music_cues", []):
        music_cue = _normalize_music_cue(raw_music_cue)
        if music_cue.cue_id in seen_cues:
            raise _problem(
                "NARRATIVE_CANDIDATE_INVALID", f"cue ID {music_cue.cue_id} appears more than once"
            )
        seen_cues.add(music_cue.cue_id)
        music_cues.append(music_cue)
    return tuple(items), tuple(narration_cues), tuple(music_cues)


def _item_dict(item: StoryboardItemCandidate) -> dict[str, Any]:
    document: dict[str, Any] = {
        "item_id": item.item_id,
        "type": item.type,
        "story_moment": item.story_moment,
        "duration_frames": item.duration_frames,
    }
    if item.type == "card":
        document["script_block"] = item.script_block
    else:
        document["asset_id"] = item.asset_id
        document["motion"] = item.motion
        if item.script_block is not None:
            document["script_block"] = item.script_block
    if item.transition is not None:
        transition: dict[str, Any] = {"type": item.transition.type}
        if item.transition.duration_frames is not None:
            transition["duration_frames"] = item.transition.duration_frames
        document["transition"] = transition
    return document


def render_storyboard_yaml(candidate: StoryboardCandidate) -> str:
    document: dict[str, Any] = {
        "schema_version": 1,
        "output": {"width": WIDTH, "height": HEIGHT, "fps": FPS},
        "inputs": {
            "story": candidate.story_hash,
            "script": candidate.script_hash,
            "catalog": candidate.catalog_hash,
        },
        "sequence": [_item_dict(item) for item in candidate.sequence],
    }
    if candidate.narration_cues:
        document["narration_cues"] = [
            {
                "cue_id": cue.cue_id,
                "block_id": cue.block_id,
                "start_frame": cue.start_frame,
                "duration_frames": cue.duration_frames,
            }
            for cue in candidate.narration_cues
        ]
    if candidate.music_cues:
        document["music_cues"] = [
            {
                "cue_id": cue.cue_id,
                "start_frame": cue.start_frame,
                "duration_frames": cue.duration_frames,
                "intent": cue.intent,
            }
            for cue in candidate.music_cues
        ]
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def generate_candidate(workspace: Path) -> StoryboardCandidate:
    try:
        config = load_project_config(workspace)
    except ConfigProblem as problem:
        raise _problem(problem.code, problem.message) from problem
    if config.narrative.name == "none":
        raise _problem("NARRATIVE_ADAPTER_DISABLED", "narrative adapter is not configured")
    try:
        adapter = narrative_adapter_for(config.narrative.name)
    except ValueError as error:
        raise _problem("NARRATIVE_ADAPTER_UNAVAILABLE", str(error)) from error

    story_path = workspace / "story.md"
    story_diagnostics = validate_story(story_path)
    if story_diagnostics:
        upstream = story_diagnostics[0]
        code = (
            "STORYBOARD_SOURCE_STORY_MISSING"
            if upstream["code"] == "STORY_MISSING"
            else "STORYBOARD_SOURCE_STORY_INVALID"
        )
        raise _problem(code, f"location={upstream['location']} {upstream['message']}")

    script_path = workspace / "script.md"
    script_document, script_diagnostics, _ = validate_script(script_path, story_path)
    if script_diagnostics:
        upstream = script_diagnostics[0]
        code = (
            "STORYBOARD_SOURCE_SCRIPT_MISSING"
            if upstream["code"] == "SCRIPT_MISSING"
            else "STORYBOARD_SOURCE_SCRIPT_INVALID"
        )
        raise _problem(code, f"location={upstream['location']} {upstream['message']}")
    assert script_document is not None

    try:
        records = load_catalog(workspace)
    except CatalogProblem as problem:
        raise _problem(problem.code, problem.message) from problem

    try:
        brief = load_effective_brief(workspace)
    except InterviewProblem as problem:
        raise _problem(problem.code, problem.message) from problem
    excluded = excluded_asset_ids(brief)
    available_records = [record for record in records if record["asset_id"] not in excluded]

    story_document = load_story_document(story_path)
    request = NarrativeRequest(
        context={
            "story": {
                "title": story_document["title"],
                "moments": sorted(story_document["moment_ids"]),
            },
            "script": {
                "title": script_document["title"],
                "blocks": [
                    {
                        "id": block["block_id"],
                        "type": block["type"],
                        "story_moment": block["story_moment"],
                        "body": block["body"],
                    }
                    for block in script_document["blocks"]
                ],
            },
            "assets": _asset_context(available_records),
        }
    )
    settings = AdapterSettings(
        model=config.narrative.model,
        prompt_version=config.narrative.prompt_version,
        prompt=PROMPT,
        parameters=dict(adapter.default_parameters),
    )
    schema = _schema()
    try:
        response = adapter.generate(request, schema, settings)
    except Exception as error:
        raise _problem(
            "NARRATIVE_ADAPTER_FAILURE", "narrative adapter raised an unexpected error"
        ) from error
    valid_success = isinstance(response, AdapterSuccess) and response.outcome == "success"
    valid_failure = isinstance(response, AdapterFailure) and response.outcome == "failure"
    if not valid_success and not valid_failure:
        raise _problem("NARRATIVE_ADAPTER_FAILURE", "adapter returned an invalid result")
    if response.adapter_version != adapter.version:
        raise _problem(
            "NARRATIVE_ADAPTER_FAILURE", "adapter returned a mismatched implementation version"
        )
    if isinstance(response, AdapterFailure):
        code = (
            "NARRATIVE_ADAPTER_REFUSAL"
            if response.category == "refusal"
            else "NARRATIVE_ADAPTER_FAILURE"
        )
        raise _problem(code, response.message)
    assert isinstance(response, AdapterSuccess)
    sequence, narration_cues, music_cues = _normalize_candidate(response.candidate)
    return StoryboardCandidate(
        story_hash=_sha256(story_path),
        script_hash=_sha256(script_path),
        catalog_hash=_sha256(workspace / "catalog.jsonl"),
        sequence=sequence,
        narration_cues=narration_cues,
        music_cues=music_cues,
    )


def write_candidate_file(workspace: Path, document: str) -> Path:
    candidate_path = workspace / CANDIDATE_RELATIVE_PATH
    temporary: Path | None = None
    try:
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{candidate_path.name}.", suffix=".tmp", dir=candidate_path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, candidate_path)
    except OSError as error:
        raise _problem(
            "NARRATIVE_CANDIDATE_IO_ERROR", "candidate Storyboard could not be written"
        ) from error
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    return candidate_path


def adopt_candidate(workspace: Path, *, force: bool) -> Path:
    candidate_path = workspace / CANDIDATE_RELATIVE_PATH
    if candidate_path.is_symlink() or not candidate_path.is_file():
        raise _problem(
            "NARRATIVE_CANDIDATE_MISSING",
            "no disposable Storyboard candidate is available to adopt",
        )
    story_path = workspace / "story.md"
    script_path = workspace / "script.md"
    catalog_path = workspace / "catalog.jsonl"
    _, diagnostics, _ = validate_storyboard(
        candidate_path,
        story_path,
        script_path,
        catalog_path,
        workspace,
        require_catalog_integrity=True,
    )
    if diagnostics:
        problem = diagnostics[0]
        raise _problem(problem["code"], f"location={problem['location']} {problem['message']}")

    storyboard_path = workspace / "storyboard.yaml"
    if storyboard_path.exists() and not force:
        raise _problem(
            "STORYBOARD_ADOPTION_REQUIRES_FORCE",
            "storyboard.yaml already exists; adopt requires --force to replace it",
        )
    document = candidate_path.read_text(encoding="utf-8")
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{storyboard_path.name}.", suffix=".tmp", dir=storyboard_path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        _, temp_diagnostics, _ = validate_storyboard(
            temporary,
            story_path,
            script_path,
            catalog_path,
            workspace,
            require_catalog_integrity=True,
        )
        if temp_diagnostics:
            problem = temp_diagnostics[0]
            raise _problem(
                problem["code"], f"location={problem['location']} {problem['message']}"
            )
        os.replace(temporary, storyboard_path)
    except OSError as error:
        raise _problem(
            "STORYBOARD_IO_ERROR", "storyboard.yaml could not be atomically replaced"
        ) from error
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    return storyboard_path


def diff_summary(workspace: Path, candidate: StoryboardCandidate) -> list[str]:
    """Summarize meaningful differences between the canonical Storyboard and a candidate.

    A best-effort textual diff over the parsed document shape; it is not a semantic diff.
    """
    storyboard_path = workspace / "storyboard.yaml"
    existing_document, _ = parse_storyboard(storyboard_path)
    assert existing_document is not None
    existing_items = {item["item_id"]: item for item in existing_document["sequence"]}
    new_items = {item.item_id: item for item in candidate.sequence}

    differences: list[str] = []
    if existing_document["inputs"]["story"] != candidate.story_hash:
        differences.append("inputs.story changed")
    if existing_document["inputs"]["script"] != candidate.script_hash:
        differences.append("inputs.script changed")
    if existing_document["inputs"]["catalog"] != candidate.catalog_hash:
        differences.append("inputs.catalog changed")

    existing_order = [item["item_id"] for item in existing_document["sequence"]]
    new_order = [item.item_id for item in candidate.sequence]
    added = sorted(set(new_items) - set(existing_items))
    removed = sorted(set(existing_items) - set(new_items))
    if added:
        differences.append(f"items added: {', '.join(added)}")
    if removed:
        differences.append(f"items removed: {', '.join(removed)}")
    if existing_order != new_order and not added and not removed:
        differences.append("item order changed")

    for item_id in sorted(set(existing_items) & set(new_items)):
        old_item = existing_items[item_id]
        new_item = new_items[item_id]
        if old_item["duration_frames"] != new_item.duration_frames:
            differences.append(
                f"items.{item_id} duration_frames: "
                f"{old_item['duration_frames']!r} -> {new_item.duration_frames!r}"
            )
        if old_item.get("asset_id") != new_item.asset_id:
            differences.append(f"items.{item_id} asset_id changed")
        if old_item.get("motion") != new_item.motion:
            differences.append(f"items.{item_id} motion changed")
        if old_item.get("script_block") != new_item.script_block:
            differences.append(f"items.{item_id} script_block changed")

    existing_narration = {cue["cue_id"] for cue in existing_document.get("narration_cues", [])}
    new_narration = {cue.cue_id for cue in candidate.narration_cues}
    if existing_narration != new_narration:
        differences.append("narration_cues changed")
    existing_music = {cue["cue_id"] for cue in existing_document.get("music_cues", [])}
    new_music = {cue.cue_id for cue in candidate.music_cues}
    if existing_music != new_music:
        differences.append("music_cues changed")
    return differences
