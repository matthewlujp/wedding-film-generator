from __future__ import annotations

import hashlib
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml

from wedding_film.config import ConfigProblem, load_project_config
from wedding_film.interview import InterviewProblem, load_effective_brief, narrative_summary
from wedding_film.narrative_adapter import (
    AdapterFailure,
    AdapterSettings,
    AdapterSuccess,
    NarrativeRequest,
    OutputSchema,
    narrative_adapter_for,
)
from wedding_film.script import ScriptBlock, WarningMessage, parse_script, validate_script
from wedding_film.story import Diagnostic, load_story_document, validate_story
from wedding_film.story_generation import NarrativeProblem

PROMPT = (
    "Using only the provided validated Story (its title and the prose of its Story "
    "Moments) and the Interview summary of what the couple said about themselves, "
    "produce a Script candidate for a wedding film: a title and an ordered list of "
    "Script Blocks, each with a lowercase kebab-case id, a type of narration, card, or "
    "caption, a story_moment referencing an existing Story Moment id, and a non-empty "
    "plain-Unicode body with no rich Markdown formatting. Prefer the couple's own "
    "phrasing, nicknames, and anecdotes from the Interview summary where they fit "
    "naturally, and never contradict its constraints. Never reference Original Assets, "
    "Asset Locators, filenames, or specific frame or time values."
)
OUTPUT_SCHEMA_VERSION = "script-candidate-v1"
CANDIDATE_RELATIVE_PATH = Path(".work") / "candidates" / "script.candidate.md"
_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_MOMENT_HEADING = re.compile(r"^### (.+)$", re.MULTILINE)
_CANDIDATE_FIELDS = {"title", "blocks"}
_BLOCK_FIELDS = {"id", "type", "story_moment", "body"}
_BLOCK_TYPES = {"narration", "card", "caption"}


def _problem(code: str, message: str) -> NarrativeProblem:
    return NarrativeProblem(code, message)


@dataclass(frozen=True)
class ScriptBlockCandidate:
    block_id: str
    type: Literal["narration", "card", "caption"]
    story_moment: str
    body: str


@dataclass(frozen=True)
class ScriptCandidate:
    title: str
    story_hash: str
    blocks: tuple[ScriptBlockCandidate, ...]


def _schema() -> OutputSchema:
    fields = ("title", "blocks")
    definition: dict[str, object] = {
        "type": "object",
        "required": list(fields),
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "blocks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "type", "story_moment", "body"],
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "type": {"type": "string", "enum": sorted(_BLOCK_TYPES)},
                        "story_moment": {"type": "string"},
                        "body": {"type": "string"},
                    },
                },
            },
        },
    }
    return OutputSchema(version=OUTPUT_SCHEMA_VERSION, fields=fields, definition=definition)


def _story_moments(story_path: Path) -> list[dict[str, object]]:
    """Best-effort Story Moment prose for the generation prompt, not for validation.

    validate_script cross-references the real Story Moment IDs afterward, so a
    fence-naive extraction here only affects prompt context, never correctness.
    """
    text = story_path.read_text(encoding="utf-8")
    marker = "\n## Moments\n"
    moments_start = text.index(marker) + len(marker)
    moments_text = text[moments_start:]
    matches = list(_MOMENT_HEADING.finditer(moments_text))
    moments: list[dict[str, object]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(moments_text)
        moments.append({"id": match.group(1).strip(), "prose": moments_text[start:end].strip()})
    return moments


def _normalize_candidate(payload: object) -> tuple[str, tuple[ScriptBlockCandidate, ...]]:
    if not isinstance(payload, dict):
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "candidate must be a JSON object")
    if set(payload) != _CANDIDATE_FIELDS:
        raise _problem(
            "NARRATIVE_CANDIDATE_INVALID", "candidate must contain exactly the expected fields"
        )
    title = payload["title"]
    blocks = payload["blocks"]
    if not isinstance(title, str) or not title.strip():
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "candidate title must be a non-empty string")
    if not isinstance(blocks, list) or not blocks:
        raise _problem("NARRATIVE_CANDIDATE_INVALID", "candidate blocks must be a non-empty array")
    normalized: list[ScriptBlockCandidate] = []
    seen: set[str] = set()
    for item in blocks:
        if not isinstance(item, dict) or set(item) != _BLOCK_FIELDS:
            raise _problem(
                "NARRATIVE_CANDIDATE_INVALID",
                "each Script Block needs id, type, story_moment, and body",
            )
        block_id = item["id"]
        block_type = item["type"]
        story_moment = item["story_moment"]
        body = item["body"]
        if not isinstance(block_id, str) or not _ID_PATTERN.fullmatch(block_id):
            raise _problem(
                "NARRATIVE_CANDIDATE_INVALID", "Script Block ID must be lowercase kebab-case"
            )
        if block_id in seen:
            raise _problem(
                "NARRATIVE_CANDIDATE_INVALID", f"Script Block ID {block_id} appears more than once"
            )
        seen.add(block_id)
        if block_type not in _BLOCK_TYPES:
            raise _problem(
                "NARRATIVE_CANDIDATE_INVALID",
                "Script Block type must be narration, card, or caption",
            )
        if not isinstance(story_moment, str) or not _ID_PATTERN.fullmatch(story_moment):
            raise _problem(
                "NARRATIVE_CANDIDATE_INVALID",
                "Script Block story_moment must be a lowercase kebab-case ID",
            )
        if not isinstance(body, str) or not body.strip():
            raise _problem(
                "NARRATIVE_CANDIDATE_INVALID", f"Script Block {block_id} body must be non-empty"
            )
        normalized.append(
            ScriptBlockCandidate(
                block_id=block_id,
                type=cast(Literal["narration", "card", "caption"], block_type),
                story_moment=story_moment,
                body=body,
            )
        )
    return title, tuple(normalized)


def render_script_markdown(candidate: ScriptCandidate) -> str:
    frontmatter = yaml.safe_dump(
        {
            "schema_version": 1,
            "title": candidate.title,
            "inputs": {"story": candidate.story_hash},
        },
        sort_keys=False,
        allow_unicode=True,
    ).rstrip("\n")
    lines = ["---", frontmatter, "---", ""]
    for index, block in enumerate(candidate.blocks):
        lines.append(f"## {block.block_id}")
        lines.append("")
        lines.append(f"type: {block.type}")
        lines.append(f"story_moment: {block.story_moment}")
        lines.append("")
        lines.append(block.body.strip())
        if index != len(candidate.blocks) - 1:
            lines.append("")
    return "\n".join(lines) + "\n"


def generate_candidate(workspace: Path) -> ScriptCandidate:
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
            "SCRIPT_SOURCE_STORY_MISSING"
            if upstream["code"] == "STORY_MISSING"
            else "SCRIPT_SOURCE_STORY_INVALID"
        )
        raise _problem(code, f"location={upstream['location']} {upstream['message']}")

    try:
        brief = load_effective_brief(workspace)
    except InterviewProblem as problem:
        raise _problem(problem.code, problem.message) from problem

    story_document = load_story_document(story_path)
    story_hash = "sha256:" + hashlib.sha256(story_path.read_bytes()).hexdigest()
    request = NarrativeRequest(
        context={
            "title": story_document["title"],
            "moments": _story_moments(story_path),
            "interview": narrative_summary(brief),
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
    title, blocks = _normalize_candidate(response.candidate)
    return ScriptCandidate(title=title, story_hash=story_hash, blocks=blocks)


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
            "NARRATIVE_CANDIDATE_IO_ERROR", "candidate Script could not be written"
        ) from error
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    return candidate_path


def _require_complete(diagnostics: list[Diagnostic], warnings: list[WarningMessage]) -> None:
    if diagnostics:
        problem = diagnostics[0]
        raise _problem(problem["code"], f"location={problem['location']} {problem['message']}")
    if warnings:
        warning = warnings[0]
        raise _problem(warning["code"], f"location={warning['location']} {warning['message']}")


def adopt_candidate(workspace: Path, *, force: bool) -> Path:
    candidate_path = workspace / CANDIDATE_RELATIVE_PATH
    if candidate_path.is_symlink() or not candidate_path.is_file():
        raise _problem(
            "NARRATIVE_CANDIDATE_MISSING", "no disposable Script candidate is available to adopt"
        )
    story_path = workspace / "story.md"
    _, diagnostics, warnings = validate_script(candidate_path, story_path)
    _require_complete(diagnostics, warnings)

    script_path = workspace / "script.md"
    if script_path.exists() and not force:
        raise _problem(
            "SCRIPT_ADOPTION_REQUIRES_FORCE",
            "script.md already exists; adopt requires --force to replace it",
        )
    markdown = candidate_path.read_text(encoding="utf-8")
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{script_path.name}.", suffix=".tmp", dir=script_path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(markdown)
            stream.flush()
            os.fsync(stream.fileno())
        _, temp_diagnostics, temp_warnings = validate_script(temporary, story_path)
        _require_complete(temp_diagnostics, temp_warnings)
        os.replace(temporary, script_path)
    except OSError as error:
        raise _problem("SCRIPT_IO_ERROR", "script.md could not be atomically replaced") from error
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    return script_path


def diff_summary(workspace: Path, candidate: ScriptCandidate) -> list[str]:
    """Summarize meaningful differences between the canonical Script and a candidate.

    A best-effort textual diff over the fixed block layout that parse_script already
    enforces on the canonical file; it is not a semantic diff.
    """
    script_path = workspace / "script.md"
    existing_document, _ = parse_script(script_path)
    assert existing_document is not None
    existing_blocks: dict[str, ScriptBlock] = {
        block["block_id"]: block for block in existing_document["blocks"]
    }
    new_blocks = {block.block_id: block for block in candidate.blocks}

    differences: list[str] = []
    if existing_document["title"] != candidate.title:
        differences.append(f"title: {existing_document['title']!r} -> {candidate.title!r}")

    added = sorted(set(new_blocks) - set(existing_blocks))
    removed = sorted(set(existing_blocks) - set(new_blocks))
    if added:
        differences.append(f"blocks added: {', '.join(added)}")
    if removed:
        differences.append(f"blocks removed: {', '.join(removed)}")

    for block_id in sorted(set(existing_blocks) & set(new_blocks)):
        old_block = existing_blocks[block_id]
        new_block = new_blocks[block_id]
        if old_block["type"] != new_block.type:
            differences.append(
                f"blocks.{block_id} type: {old_block['type']!r} -> {new_block.type!r}"
            )
        if old_block["story_moment"] != new_block.story_moment:
            differences.append(
                f"blocks.{block_id} story_moment: "
                f"{old_block['story_moment']!r} -> {new_block.story_moment!r}"
            )
        if old_block["body"] != new_block.body.strip():
            differences.append(f"blocks.{block_id} text changed")
    return differences
