from __future__ import annotations

import json
import math
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import yaml

from wedding_film.catalog import CatalogProblem, load_catalog
from wedding_film.catalog_review import effective_values
from wedding_film.config import ConfigProblem, load_project_config
from wedding_film.narrative_adapter import (
    AdapterFailure,
    AdapterSettings,
    AdapterSuccess,
    NarrativeRequest,
    OutputSchema,
    narrative_adapter_for,
)
from wedding_film.participants import ParticipantProblem, load_participants
from wedding_film.story import load_story_document, validate_story

PROMPT = (
    "Using only the provided effective Semantic Catalog summary and Participant roster, "
    "produce a Story candidate for a wedding film: a title, a positive target_duration_seconds, "
    "an intent (prose describing the narrative intent), an emotional_arc (prose describing the "
    "emotional journey), and an ordered list of Story Moments, each with a lowercase kebab-case "
    "id and non-empty prose. Never reference Original Assets, Asset Locators, filenames, or "
    "specific frame or time values; describe narrative intent only."
)
OUTPUT_SCHEMA_VERSION = "story-candidate-v1"
CANDIDATE_RELATIVE_PATH = Path(".work") / "candidates" / "story.candidate.md"
_MOMENT_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_CANDIDATE_FIELDS = {"title", "target_duration_seconds", "intent", "emotional_arc", "moments"}


class NarrativeProblem(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _problem(code: str, message: str) -> NarrativeProblem:
    return NarrativeProblem(code, message)


@dataclass(frozen=True)
class StoryCandidate:
    title: str
    target_duration_seconds: float
    intent: str
    emotional_arc: str
    moments: tuple[tuple[str, str], ...]


def _schema() -> OutputSchema:
    fields = ("title", "target_duration_seconds", "intent", "emotional_arc", "moments")
    definition: dict[str, object] = {
        "type": "object",
        "required": list(fields),
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "target_duration_seconds": {"type": "number"},
            "intent": {"type": "string"},
            "emotional_arc": {"type": "string"},
            "moments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "prose"],
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "prose": {"type": "string"},
                    },
                },
            },
        },
    }
    return OutputSchema(version=OUTPUT_SCHEMA_VERSION, fields=fields, definition=definition)


def _catalog_summary(workspace: Path) -> list[dict[str, object]]:
    records = load_catalog(workspace)
    summary: list[dict[str, object]] = []
    for record in records:
        effective = effective_values(record)
        entry = {
            target.removeprefix("/inferences/"): item.value
            for target, item in effective.items()
            if item.present and target.startswith("/inferences/")
        }
        if entry:
            summary.append(entry)
    summary.sort(key=lambda entry: json.dumps(entry, sort_keys=True, ensure_ascii=False))
    return summary


def _participant_context(workspace: Path) -> list[dict[str, object]]:
    try:
        participants = load_participants(workspace)
    except ParticipantProblem as problem:
        raise _problem(problem.code, problem.message) from problem
    return [
        {
            "id": participant.id,
            "display_name": participant.display_name,
            "role": participant.role,
            "principal": participant.principal,
        }
        for participant in participants
    ]


def _normalize_candidate(payload: object) -> StoryCandidate:
    if not isinstance(payload, dict):
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "candidate must be a JSON object")
    if set(payload) != _CANDIDATE_FIELDS:
        raise _problem(
            "NARRATIVE_CANDIDATE_INVALID", "candidate must contain exactly the expected fields"
        )
    title = payload["title"]
    duration = payload["target_duration_seconds"]
    intent = payload["intent"]
    arc = payload["emotional_arc"]
    moments = payload["moments"]
    if not isinstance(title, str) or not title.strip():
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "candidate title must be a non-empty string")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, int | float)
        or duration <= 0
        or not math.isfinite(duration)
    ):
        raise _problem(
            "NARRATIVE_CANDIDATE_INVALID",
            "candidate target_duration_seconds must be a positive finite number",
        )
    if not isinstance(intent, str) or not intent.strip():
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "candidate intent must be a non-empty string")
    if not isinstance(arc, str) or not arc.strip():
        raise _problem(
            "NARRATIVE_CANDIDATE_INVALID", "candidate emotional_arc must be a non-empty string"
        )
    if not isinstance(moments, list) or not moments:
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "candidate moments must be a non-empty array")
    normalized_moments: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in moments:
        if not isinstance(item, dict) or set(item) != {"id", "prose"}:
            raise _problem("NARRATIVE_CANDIDATE_INVALID", "each Story Moment needs id and prose")
        moment_id = item["id"]
        prose = item["prose"]
        if not isinstance(moment_id, str) or not _MOMENT_ID_PATTERN.fullmatch(moment_id):
            raise _problem(
                "NARRATIVE_CANDIDATE_INVALID", "Story Moment ID must be lowercase kebab-case"
            )
        if moment_id in seen:
            raise _problem(
                "NARRATIVE_CANDIDATE_INVALID",
                f"Story Moment ID {moment_id} appears more than once",
            )
        seen.add(moment_id)
        if not isinstance(prose, str) or not prose.strip():
            raise _problem(
                "NARRATIVE_CANDIDATE_INVALID", f"Story Moment {moment_id} prose must be non-empty"
            )
        normalized_moments.append((moment_id, prose))
    return StoryCandidate(
        title=title,
        target_duration_seconds=float(duration),
        intent=intent,
        emotional_arc=arc,
        moments=tuple(normalized_moments),
    )


def render_story_markdown(candidate: StoryCandidate) -> str:
    frontmatter = yaml.safe_dump(
        {
            "schema_version": 1,
            "title": candidate.title,
            "target_duration_seconds": candidate.target_duration_seconds,
        },
        sort_keys=False,
        allow_unicode=True,
    ).rstrip("\n")
    lines = [
        "---",
        frontmatter,
        "---",
        "",
        "## Intent",
        "",
        candidate.intent.strip(),
        "",
        "## Emotional Arc",
        "",
        candidate.emotional_arc.strip(),
        "",
        "## Moments",
        "",
    ]
    for index, (moment_id, prose) in enumerate(candidate.moments):
        lines.append(f"### {moment_id}")
        lines.append("")
        lines.append(prose.strip())
        if index != len(candidate.moments) - 1:
            lines.append("")
    return "\n".join(lines) + "\n"


def generate_candidate(workspace: Path) -> StoryCandidate:
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
    try:
        catalog_summary = _catalog_summary(workspace)
    except CatalogProblem as problem:
        raise _problem(problem.code, problem.message) from problem
    participants = _participant_context(workspace)
    request = NarrativeRequest(
        context={"catalog_summary": catalog_summary, "participants": participants}
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
    return _normalize_candidate(response.candidate)


def write_candidate_file(workspace: Path, markdown: str) -> Path:
    candidate_path = workspace / CANDIDATE_RELATIVE_PATH
    temporary: Path | None = None
    try:
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{candidate_path.name}.", suffix=".tmp", dir=candidate_path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(markdown)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, candidate_path)
    except OSError as error:
        raise _problem(
            "NARRATIVE_CANDIDATE_IO_ERROR", "candidate Story could not be written"
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
            "NARRATIVE_CANDIDATE_MISSING", "no disposable Story candidate is available to adopt"
        )
    diagnostics = validate_story(candidate_path)
    if diagnostics:
        problem = diagnostics[0]
        raise _problem(problem["code"], f"location={problem['location']} {problem['message']}")
    story_path = workspace / "story.md"
    if story_path.exists() and not force:
        raise _problem(
            "STORY_ADOPTION_REQUIRES_FORCE",
            "story.md already exists; adopt requires --force to replace it",
        )
    markdown = candidate_path.read_text(encoding="utf-8")
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{story_path.name}.", suffix=".tmp", dir=story_path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(markdown)
            stream.flush()
            os.fsync(stream.fileno())
        temp_diagnostics = validate_story(temporary)
        if temp_diagnostics:
            problem = temp_diagnostics[0]
            raise _problem(
                problem["code"], f"location={problem['location']} {problem['message']}"
            )
        os.replace(temporary, story_path)
    except OSError as error:
        raise _problem("STORY_IO_ERROR", "story.md could not be atomically replaced") from error
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    return story_path


def _extract_prose(markdown: str, start_marker: str, end_marker: str | None) -> str:
    start = markdown.index(start_marker) + len(start_marker)
    end = markdown.index(end_marker, start) if end_marker is not None else len(markdown)
    return markdown[start:end].strip()


def diff_summary(workspace: Path, candidate: StoryCandidate) -> list[str]:
    """Summarize meaningful differences between the canonical Story and a candidate.

    A best-effort textual diff over the fixed section layout that validate_story
    already enforces on the canonical file; it is not a semantic diff.
    """
    story_path = workspace / "story.md"
    existing_doc = load_story_document(story_path)
    existing_text = story_path.read_text(encoding="utf-8")
    existing_intent = _extract_prose(existing_text, "\n## Intent\n", "\n## Emotional Arc\n")
    existing_arc = _extract_prose(existing_text, "\n## Emotional Arc\n", "\n## Moments\n")

    differences: list[str] = []
    if existing_doc["title"] != candidate.title:
        differences.append(f"title: {existing_doc['title']!r} -> {candidate.title!r}")
    if existing_doc["target_duration_seconds"] != candidate.target_duration_seconds:
        differences.append(
            "target_duration_seconds: "
            f"{existing_doc['target_duration_seconds']!r} -> {candidate.target_duration_seconds!r}"
        )
    if existing_intent != candidate.intent.strip():
        differences.append("intent changed")
    if existing_arc != candidate.emotional_arc.strip():
        differences.append("emotional_arc changed")

    new_ids = {moment_id for moment_id, _ in candidate.moments}
    existing_ids = existing_doc["moment_ids"]
    added = sorted(new_ids - existing_ids)
    removed = sorted(existing_ids - new_ids)
    if added:
        differences.append(f"moments added: {', '.join(added)}")
    if removed:
        differences.append(f"moments removed: {', '.join(removed)}")
    return differences
