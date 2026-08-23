from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Literal, TypedDict

import yaml
from yaml.tokens import AliasToken, AnchorToken, TagToken

from wedding_film.story import (
    Diagnostic,
    DuplicateFieldError,
    StrictLoader,
    story_moment_ids,
    validate_story,
)


class WarningMessage(TypedDict):
    artifact: str
    code: str
    location: str
    message: str


class ScriptValidationPayload(TypedDict):
    artifact: str
    state: str
    diagnostics: list[Diagnostic]
    warnings: list[WarningMessage]


class ScriptBlock(TypedDict):
    block_id: str
    type: Literal["narration", "card", "caption"]
    story_moment: str
    body: str


_BLOCK = re.compile(r"^ {0,3}## (.+)$")
_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_METADATA = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*:.*")
_RICH_LINE = re.compile(
    r"^(?: {4}| {0,3}(?:#{1,6}\s|>|(?:[-+*]|\d+[.)])\s|`{3,}|~{3,}|"
    r"(?:[-*_]\s*){3,}$|=+\s*$|\[[^]]+\]:\s))"
)
_RICH_INLINE = re.compile(
    r"`|!\[|\[[^]\n]*\](?:\([^\n]*\)|\[[^]\n]*\])|"
    r"</?[A-Za-z][^>]*>|<[!?]|<(?:https?://|mailto:)[^>]*>|<[^ >]+@[^ >]+>|"
    r"<!--|-->|&(?:#\d+|#x[0-9A-Fa-f]+|\w+);|"
    r"\\(?:[!\"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~]|$)|\*|_[^_\n]+_|~~| {2,}$"
)


def _diagnostic(path: Path, code: str, location: str, message: str) -> Diagnostic:
    return {"artifact": str(path), "code": code, "location": location, "message": message}


def _is_plain_unicode(lines: list[str]) -> bool:
    for line in lines:
        if _RICH_LINE.match(line) or _RICH_INLINE.search(line):
            return False
        if any(unicodedata.category(character) in {"Cc", "Cf"} for character in line):
            return False
    return True


def validate_script(
    script_path: Path, story_path: Path
) -> tuple[list[Diagnostic], list[WarningMessage]]:
    try:
        if not script_path.exists():
            return [_diagnostic(script_path, "SCRIPT_MISSING", "$", "script.md is absent")], []
        if script_path.is_symlink() or not script_path.is_file() or script_path.stat().st_size == 0:
            return [
                _diagnostic(
                    script_path,
                    "SCRIPT_INVALID_ARTIFACT",
                    "$",
                    "script.md must be a non-empty regular file",
                )
            ], []
        text = script_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return [_diagnostic(script_path, "SCRIPT_IO_ERROR", "$", "script.md could not be read")], []

    lines = text.splitlines()
    if len(lines) < 3 or lines[0] != "---":
        return [
            _diagnostic(
                script_path, "SCRIPT_FRONTMATTER_INVALID", "$", "YAML frontmatter is required"
            )
        ], []
    try:
        closing = lines.index("---", 1)
        frontmatter = "\n".join(lines[1:closing])
        if any(
            isinstance(token, AliasToken | AnchorToken | TagToken)
            for token in yaml.scan(frontmatter)
        ):
            raise yaml.YAMLError("frontmatter aliases, anchors, and tags are unsupported")
        metadata = yaml.load(frontmatter, Loader=StrictLoader)
    except DuplicateFieldError as error:
        return [
            _diagnostic(
                script_path,
                "SCRIPT_FRONTMATTER_DUPLICATE_FIELD",
                f"frontmatter.{error.field}",
                f"frontmatter field {error.field} appears more than once",
            )
        ], []
    except (ValueError, yaml.YAMLError):
        return [
            _diagnostic(script_path, "SCRIPT_FRONTMATTER_INVALID", "$", "frontmatter is invalid")
        ], []
    if not isinstance(metadata, dict):
        return [
            _diagnostic(
                script_path, "SCRIPT_FRONTMATTER_INVALID", "$", "frontmatter must be a mapping"
            )
        ], []
    expected = {"schema_version", "title", "inputs"}
    unknown = sorted(str(key) for key in metadata if key not in expected)
    if unknown:
        return [
            _diagnostic(
                script_path,
                "SCRIPT_FRONTMATTER_UNKNOWN_FIELD",
                f"frontmatter.{unknown[0]}",
                f"unknown frontmatter field {unknown[0]}",
            )
        ], []
    missing = sorted(expected - metadata.keys())
    if missing:
        return [
            _diagnostic(
                script_path,
                "SCRIPT_FRONTMATTER_MISSING_FIELD",
                f"frontmatter.{missing[0]}",
                f"required frontmatter field {missing[0]} is missing",
            )
        ], []
    if type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1:
        return [
            _diagnostic(
                script_path,
                "SCRIPT_VERSION_UNSUPPORTED",
                "frontmatter.schema_version",
                "schema_version must be the supported integer 1",
            )
        ], []
    if not isinstance(metadata["title"], str) or not metadata["title"].strip():
        return [
            _diagnostic(
                script_path,
                "SCRIPT_FRONTMATTER_INVALID_VALUE",
                "frontmatter.title",
                "title must be a non-empty string",
            )
        ], []
    inputs = metadata["inputs"]
    if not isinstance(inputs, dict) or set(inputs) != {"story"}:
        return [
            _diagnostic(
                script_path,
                "SCRIPT_INPUTS_INVALID",
                "frontmatter.inputs",
                "inputs must contain exactly the story hash",
            )
        ], []
    recorded_hash = inputs["story"]
    if not isinstance(recorded_hash, str) or _HASH.fullmatch(recorded_hash) is None:
        return [
            _diagnostic(
                script_path,
                "SCRIPT_STORY_HASH_INVALID",
                "frontmatter.inputs.story",
                "story input must be sha256 followed by 64 lowercase hexadecimal digits",
            )
        ], []

    body_lines = lines[closing + 1 :]
    block_starts = [index for index, line in enumerate(body_lines) if _BLOCK.fullmatch(line)]
    if not block_starts:
        return [
            _diagnostic(
                script_path,
                "SCRIPT_BLOCKS_EMPTY",
                "blocks",
                "script.md must contain at least one Script Block",
            )
        ], []
    if any(line.strip() for line in body_lines[: block_starts[0]]):
        return [
            _diagnostic(
                script_path,
                "SCRIPT_BLOCK_ORDER",
                "blocks.order",
                "only blank lines may appear before the first Script Block",
            )
        ], []
    story_diagnostics = validate_story(story_path)
    if story_diagnostics:
        upstream = story_diagnostics[0]
        return [
            _diagnostic(
                script_path,
                "SCRIPT_STORY_INVALID",
                "frontmatter.inputs.story",
                f"story.md is invalid: {upstream['code']} at {upstream['location']}",
            )
        ], []
    try:
        moments = story_moment_ids(story_path)
    except (OSError, UnicodeError, ValueError):
        return [
            _diagnostic(
                script_path,
                "SCRIPT_STORY_UNAVAILABLE",
                "frontmatter.inputs.story",
                "story.md could not be read for Script validation",
            )
        ], []
    seen: set[str] = set()
    for position, start in enumerate(block_starts):
        match = _BLOCK.fullmatch(body_lines[start])
        assert match is not None
        block_id = match.group(1)
        location = f"blocks.{block_id}"
        if _ID.fullmatch(block_id) is None:
            return [
                _diagnostic(
                    script_path,
                    "SCRIPT_BLOCK_ID_INVALID",
                    location,
                    "Script Block ID must be lowercase kebab-case",
                )
            ], []
        if block_id in seen:
            return [
                _diagnostic(
                    script_path,
                    "SCRIPT_BLOCK_ID_DUPLICATE",
                    location,
                    f"Script Block ID {block_id} appears more than once",
                )
            ], []
        seen.add(block_id)
        end = block_starts[position + 1] if position + 1 < len(block_starts) else len(body_lines)
        content = body_lines[start + 1 : end]
        non_empty = [index for index, line in enumerate(content) if line.strip()]
        if (
            len(non_empty) < 2
            or not content[non_empty[0]].startswith("type: ")
            or not content[non_empty[1]].startswith("story_moment: ")
        ):
            return [
                _diagnostic(
                    script_path,
                    "SCRIPT_BLOCK_METADATA_ORDER",
                    location,
                    "type and story_moment must be the first two non-empty lines in order",
                )
            ], []
        type_line = content[non_empty[0]]
        moment_line = content[non_empty[1]]
        body_start = non_empty[1] + 1
        metadata_tail = [content[non_empty[2]]] if len(non_empty) > 2 else []
        unknown_metadata = next(
            (line.partition(":")[0] for line in metadata_tail if _METADATA.fullmatch(line)),
            None,
        )
        if unknown_metadata is not None:
            return [
                _diagnostic(
                    script_path,
                    "SCRIPT_BLOCK_METADATA_UNKNOWN",
                    f"{location}.{unknown_metadata}",
                    f"unknown Script Block metadata field {unknown_metadata}",
                )
            ], []
        block_type = type_line.removeprefix("type: ")
        if block_type not in {"narration", "card", "caption"}:
            return [
                _diagnostic(
                    script_path,
                    "SCRIPT_BLOCK_TYPE_INVALID",
                    f"{location}.type",
                    "type must be narration, card, or caption",
                )
            ], []
        moment = moment_line.removeprefix("story_moment: ")
        if moment not in moments:
            return [
                _diagnostic(
                    script_path,
                    "SCRIPT_STORY_MOMENT_UNKNOWN",
                    f"{location}.story_moment",
                    f"Story Moment {moment} does not exist in story.md",
                )
            ], []
        block_body = content[body_start:]
        while block_body and not block_body[0].strip():
            block_body.pop(0)
        while block_body and not block_body[-1].strip():
            block_body.pop()
        if not block_body or not any(line.strip() for line in block_body):
            return [
                _diagnostic(
                    script_path,
                    "SCRIPT_BLOCK_BODY_EMPTY",
                    f"{location}.body",
                    f"Script Block {block_id} must contain non-empty plain Unicode text",
                )
            ], []
        if not _is_plain_unicode(block_body):
            return [
                _diagnostic(
                    script_path,
                    "SCRIPT_BLOCK_BODY_RICH_TEXT",
                    f"{location}.body",
                    f"Script Block {block_id} body must be plain Unicode text",
                )
            ], []

    warnings: list[WarningMessage] = []
    try:
        current_hash = "sha256:" + hashlib.sha256(story_path.read_bytes()).hexdigest()
    except OSError:
        return [
            _diagnostic(
                script_path,
                "SCRIPT_STORY_UNAVAILABLE",
                "frontmatter.inputs.story",
                "story.md could not be read for Script validation",
            )
        ], []
    if recorded_hash != current_hash:
        warnings.append(
            {
                "artifact": str(script_path),
                "code": "SCRIPT_STORY_HASH_STALE",
                "location": "frontmatter.inputs.story",
                "message": "script.md was authored against different story.md bytes",
            }
        )
    return [], warnings


def write_script_validation(workspace: Path, as_json: bool, strict: bool) -> int:
    script = workspace / "script.md"
    diagnostics, warnings = validate_script(script, workspace / "story.md")
    if strict and warnings and not diagnostics:
        warning = warnings[0]
        diagnostics = [Diagnostic(**warning)]
        warnings = []
    payload: ScriptValidationPayload = {
        "artifact": str(script),
        "state": "invalid" if diagnostics else "complete-with-warnings" if warnings else "ready",
        "diagnostics": diagnostics,
        "warnings": warnings,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"artifact={script} state={payload['state']}")
        for item in [*diagnostics, *warnings]:
            print(
                f"artifact={item['artifact']} location={item['location']} "
                f"code={item['code']} message={item['message']}"
            )
    return 1 if diagnostics else 0
