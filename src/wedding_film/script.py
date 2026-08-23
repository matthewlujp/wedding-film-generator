from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Literal, TypedDict, cast

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


class ScriptBlock(TypedDict):
    block_id: str
    type: Literal["narration", "card", "caption"]
    story_moment: str
    body: str


class ScriptInputs(TypedDict):
    story: str


class ScriptDocument(TypedDict):
    schema_version: int
    title: str
    inputs: ScriptInputs
    blocks: list[ScriptBlock]


class ScriptValidationPayload(TypedDict):
    artifact: str
    state: str
    document: ScriptDocument | None
    diagnostics: list[Diagnostic]
    warnings: list[WarningMessage]


_BLOCK = re.compile(r"^ {0,3}## (.+)$")
_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_METADATA = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*:.*")
_RICH_BLOCK_LINE = re.compile(
    r"^(?: {4}| {0,3}(?:#{1,6}\s|>|(?:[-+*]|\d+[.)])\s|`{3,}|~{3,}|"
    r"(?:[-*_]\s*){3,}$|\[[^]]+\]:\s))"
)
_SETEXT_UNDERLINE = re.compile(r"^ {0,3}(?:=+|-+)\s*$")
_RICH_SPAN = re.compile(
    r"(`+)(?=\S)(?:(?!\1).)+?\1|"
    r"!\[|\[[^]]*\](?:\([^)]*\)|\[[^]]*\])|"
    r"</?[A-Za-z][^>]*>|<[!?]|<(?:https?://|mailto:)[^>]*>|<[^ >]+@[^ >]+>|"
    r"<!--|-->|&(?:#\d+|#x[0-9A-Fa-f]+|\w+);|"
    r"\\(?:[!\"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~]|$)|"
    r"\*(?=\S)(?:(?!\n\n).)*?(?<=\S)\*|"
    r"(?<!\w)_(?=\S)(?:(?!\n\n).)*?(?<=\S)_(?!\w)|"
    r"~~(?=\S)(?:(?!\n\n).)*?(?<=\S)~~| {2,}(?:\n|$)",
    re.DOTALL,
)


def _diagnostic(path: Path, code: str, location: str, message: str) -> Diagnostic:
    return {"artifact": str(path), "code": code, "location": location, "message": message}


def _warning(path: Path, code: str, location: str, message: str) -> WarningMessage:
    return {"artifact": str(path), "code": code, "location": location, "message": message}


def _invalid(
    path: Path, code: str, location: str, message: str
) -> tuple[None, list[Diagnostic]]:
    return None, [_diagnostic(path, code, location, message)]


def _is_plain_unicode(lines: list[str]) -> bool:
    for index, line in enumerate(lines):
        if _RICH_BLOCK_LINE.match(line):
            return False
        if index > 0 and lines[index - 1].strip() and _SETEXT_UNDERLINE.fullmatch(line):
            return False
        if any(unicodedata.category(character) in {"Cc", "Cf"} for character in line):
            return False
    return _RICH_SPAN.search("\n".join(lines)) is None


def parse_script(path: Path) -> tuple[ScriptDocument | None, list[Diagnostic]]:
    """Parse and structurally validate Script without loading any upstream layer."""
    try:
        if not path.exists():
            return _invalid(path, "SCRIPT_MISSING", "$", "script.md is absent")
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            return _invalid(
                path,
                "SCRIPT_INVALID_ARTIFACT",
                "$",
                "script.md must be a non-empty regular file",
            )
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return _invalid(path, "SCRIPT_IO_ERROR", "$", "script.md could not be read")

    lines = text.splitlines()
    if len(lines) < 3 or lines[0] != "---":
        return _invalid(path, "SCRIPT_FRONTMATTER_INVALID", "$", "YAML frontmatter is required")
    try:
        closing = lines.index("---", 1)
        frontmatter = "\n".join(lines[1:closing])
        if any(
            isinstance(token, AliasToken | AnchorToken | TagToken)
            for token in yaml.scan(frontmatter)
        ):
            raise yaml.YAMLError("frontmatter aliases, anchors, and tags are unsupported")
        metadata_value: object = yaml.load(frontmatter, Loader=StrictLoader)
    except DuplicateFieldError as error:
        return _invalid(
            path,
            "SCRIPT_FRONTMATTER_DUPLICATE_FIELD",
            f"frontmatter.{error.field}",
            f"frontmatter field {error.field} appears more than once",
        )
    except (ValueError, yaml.YAMLError):
        return _invalid(path, "SCRIPT_FRONTMATTER_INVALID", "$", "frontmatter is invalid")
    if not isinstance(metadata_value, dict):
        return _invalid(
            path, "SCRIPT_FRONTMATTER_INVALID", "$", "frontmatter must be a mapping"
        )
    metadata = cast(dict[object, object], metadata_value)
    expected = {"schema_version", "title", "inputs"}
    unknown = sorted(str(key) for key in metadata if key not in expected)
    if unknown:
        return _invalid(
            path,
            "SCRIPT_FRONTMATTER_UNKNOWN_FIELD",
            f"frontmatter.{unknown[0]}",
            f"unknown frontmatter field {unknown[0]}",
        )
    missing = sorted(expected - metadata.keys())
    if missing:
        return _invalid(
            path,
            "SCRIPT_FRONTMATTER_MISSING_FIELD",
            f"frontmatter.{missing[0]}",
            f"required frontmatter field {missing[0]} is missing",
        )
    version = metadata["schema_version"]
    if type(version) is not int or version != 1:
        return _invalid(
            path,
            "SCRIPT_VERSION_UNSUPPORTED",
            "frontmatter.schema_version",
            "schema_version must be the supported integer 1",
        )
    title = metadata["title"]
    if not isinstance(title, str) or not title.strip():
        return _invalid(
            path,
            "SCRIPT_FRONTMATTER_INVALID_VALUE",
            "frontmatter.title",
            "title must be a non-empty string",
        )
    inputs_value = metadata["inputs"]
    if not isinstance(inputs_value, dict) or set(inputs_value) != {"story"}:
        return _invalid(
            path,
            "SCRIPT_INPUTS_INVALID",
            "frontmatter.inputs",
            "inputs must contain exactly the story hash",
        )
    inputs = cast(dict[object, object], inputs_value)
    recorded_hash = inputs["story"]
    if not isinstance(recorded_hash, str) or _HASH.fullmatch(recorded_hash) is None:
        return _invalid(
            path,
            "SCRIPT_STORY_HASH_INVALID",
            "frontmatter.inputs.story",
            "story input must be sha256 followed by 64 lowercase hexadecimal digits",
        )

    body_lines = lines[closing + 1 :]
    block_starts = [index for index, line in enumerate(body_lines) if _BLOCK.fullmatch(line)]
    if not block_starts:
        return _invalid(
            path,
            "SCRIPT_BLOCKS_EMPTY",
            "blocks",
            "script.md must contain at least one Script Block",
        )
    if any(line.strip() for line in body_lines[: block_starts[0]]):
        return _invalid(
            path,
            "SCRIPT_BLOCK_ORDER",
            "blocks.order",
            "only blank lines may appear before the first Script Block",
        )

    blocks: list[ScriptBlock] = []
    seen: set[str] = set()
    for position, start in enumerate(block_starts):
        match = _BLOCK.fullmatch(body_lines[start])
        assert match is not None
        block_id = match.group(1)
        location = f"blocks.{block_id}"
        if _ID.fullmatch(block_id) is None:
            return _invalid(
                path,
                "SCRIPT_BLOCK_ID_INVALID",
                location,
                "Script Block ID must be lowercase kebab-case",
            )
        if block_id in seen:
            return _invalid(
                path,
                "SCRIPT_BLOCK_ID_DUPLICATE",
                location,
                f"Script Block ID {block_id} appears more than once",
            )
        seen.add(block_id)
        end = block_starts[position + 1] if position + 1 < len(block_starts) else len(body_lines)
        content = body_lines[start + 1 : end]
        non_empty = [index for index, line in enumerate(content) if line.strip()]
        if (
            len(non_empty) < 2
            or not content[non_empty[0]].startswith("type: ")
            or not content[non_empty[1]].startswith("story_moment: ")
        ):
            return _invalid(
                path,
                "SCRIPT_BLOCK_METADATA_ORDER",
                location,
                "type and story_moment must be the first two non-empty lines in order",
            )
        type_line = content[non_empty[0]]
        moment_line = content[non_empty[1]]
        body_start = non_empty[1] + 1
        metadata_tail = [content[non_empty[2]]] if len(non_empty) > 2 else []
        unknown_metadata = next(
            (line.partition(":")[0] for line in metadata_tail if _METADATA.fullmatch(line)),
            None,
        )
        if unknown_metadata is not None:
            return _invalid(
                path,
                "SCRIPT_BLOCK_METADATA_UNKNOWN",
                f"{location}.{unknown_metadata}",
                f"unknown Script Block metadata field {unknown_metadata}",
            )
        block_type = type_line.removeprefix("type: ")
        if block_type not in {"narration", "card", "caption"}:
            return _invalid(
                path,
                "SCRIPT_BLOCK_TYPE_INVALID",
                f"{location}.type",
                "type must be narration, card, or caption",
            )
        moment = moment_line.removeprefix("story_moment: ")
        if _ID.fullmatch(moment) is None:
            return _invalid(
                path,
                "SCRIPT_STORY_MOMENT_ID_INVALID",
                f"{location}.story_moment",
                "story_moment must be a lowercase kebab-case ID",
            )
        block_body = content[body_start:]
        while block_body and not block_body[0].strip():
            block_body.pop(0)
        while block_body and not block_body[-1].strip():
            block_body.pop()
        if not block_body or not any(line.strip() for line in block_body):
            return _invalid(
                path,
                "SCRIPT_BLOCK_BODY_EMPTY",
                f"{location}.body",
                f"Script Block {block_id} must contain non-empty plain Unicode text",
            )
        if not _is_plain_unicode(block_body):
            return _invalid(
                path,
                "SCRIPT_BLOCK_BODY_RICH_TEXT",
                f"{location}.body",
                f"Script Block {block_id} body must be plain Unicode text",
            )
        blocks.append(
            {
                "block_id": block_id,
                "type": cast(Literal["narration", "card", "caption"], block_type),
                "story_moment": moment,
                "body": "\n".join(block_body),
            }
        )
    return {
        "schema_version": version,
        "title": title,
        "inputs": {"story": recorded_hash},
        "blocks": blocks,
    }, []


def validate_script(
    script_path: Path, story_path: Path | None
) -> tuple[ScriptDocument | None, list[Diagnostic], list[WarningMessage]]:
    document, diagnostics = parse_script(script_path)
    if document is None:
        return None, diagnostics, []
    if story_path is None:
        return document, [], []
    story_diagnostics = validate_story(story_path)
    if story_diagnostics:
        upstream = story_diagnostics[0]
        code = (
            "SCRIPT_STORY_MISSING"
            if upstream["code"] == "STORY_MISSING"
            else "SCRIPT_STORY_INVALID"
        )
        return document, [], [
            _warning(
                script_path,
                code,
                "frontmatter.inputs.story",
                "cross-reference validation unavailable: "
                f"{upstream['code']} at {upstream['location']}",
            )
        ]
    try:
        moments = story_moment_ids(story_path)
        current_hash = "sha256:" + hashlib.sha256(story_path.read_bytes()).hexdigest()
    except (OSError, UnicodeError, ValueError):
        return document, [], [
            _warning(
                script_path,
                "SCRIPT_STORY_UNAVAILABLE",
                "frontmatter.inputs.story",
                "story.md could not be read for cross-reference validation",
            )
        ]
    for block in document["blocks"]:
        if block["story_moment"] not in moments:
            return document, [
                _diagnostic(
                    script_path,
                    "SCRIPT_STORY_MOMENT_UNKNOWN",
                    f"blocks.{block['block_id']}.story_moment",
                    f"Story Moment {block['story_moment']} does not exist in story.md",
                )
            ], []
    warnings: list[WarningMessage] = []
    if document["inputs"]["story"] != current_hash:
        warnings.append(
            _warning(
                script_path,
                "SCRIPT_STORY_HASH_STALE",
                "frontmatter.inputs.story",
                "script.md was authored against different story.md bytes",
            )
        )
    return document, [], warnings


def write_script_validation(workspace: Path, as_json: bool, strict: bool) -> int:
    script = workspace / "script.md"
    document, diagnostics, warnings = validate_script(script, workspace / "story.md")
    if strict and warnings and not diagnostics:
        diagnostics = [Diagnostic(**warnings[0])]
        warnings = []
    payload: ScriptValidationPayload = {
        "artifact": str(script),
        "state": "invalid" if diagnostics else "complete-with-warnings" if warnings else "ready",
        "document": document,
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
