from __future__ import annotations

import html
import json
import math
import re
from pathlib import Path
from typing import TypedDict

import yaml
from yaml.nodes import MappingNode, ScalarNode


class Diagnostic(TypedDict):
    artifact: str
    code: str
    location: str
    message: str


class ValidationPayload(TypedDict):
    artifact: str
    state: str
    diagnostics: list[Diagnostic]


_SECTION = re.compile(r"^ {0,3}## (.+)$")
_MOMENT = re.compile(r"^ {0,3}### (.+)$")
_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HTML_COMMENT = re.compile(r"<!--(?:.*?-->|.*\Z)", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")


class DuplicateFieldError(yaml.YAMLError):
    def __init__(self, field: str) -> None:
        self.field = field


class StrictLoader(yaml.SafeLoader):
    pass


def _strict_mapping(loader: StrictLoader, node: MappingNode) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        if not isinstance(key_node, ScalarNode):
            raise yaml.YAMLError("frontmatter keys must be scalars")
        key = loader.construct_object(key_node, deep=False)
        if key in result:
            raise DuplicateFieldError(str(key))
        result[key] = loader.construct_object(value_node, deep=False)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_mapping)


def _markdown_structure(lines: list[str], start: int) -> list[tuple[int, str]]:
    """Return lines that can define structure, excluding fenced code blocks."""
    visible: list[tuple[int, str]] = []
    fence_character: str | None = None
    fence_length = 0
    for index, line in enumerate(lines[start:], start=start):
        if fence_character is not None:
            stripped = line.lstrip(" ")
            indentation = len(line) - len(stripped)
            closing = stripped.rstrip(" ")
            if (
                indentation <= 3
                and closing
                and set(closing) == {fence_character}
                and len(closing) >= fence_length
            ):
                fence_character = None
                fence_length = 0
            continue
        match = _FENCE_OPEN.fullmatch(line)
        if match is not None:
            marker, info = match.groups()
            # Backtick info strings cannot themselves contain a backtick.
            if marker[0] == "~" or "`" not in info:
                fence_character = marker[0]
                fence_length = len(marker)
                continue
        visible.append((index, line))
    return visible


def _has_visible_prose(lines: list[str]) -> bool:
    without_comments = _HTML_COMMENT.sub("", "\n".join(lines)).splitlines()
    fence_character: str | None = None
    fence_length = 0
    content: list[str] = []
    for line in without_comments:
        if fence_character is not None:
            stripped = line.lstrip(" ")
            indentation = len(line) - len(stripped)
            closing = stripped.rstrip(" ")
            if (
                indentation <= 3
                and closing
                and set(closing) == {fence_character}
                and len(closing) >= fence_length
            ):
                fence_character = None
                fence_length = 0
            else:
                content.append(line)
            continue
        match = _FENCE_OPEN.fullmatch(line)
        if match is not None:
            marker, info = match.groups()
            if marker[0] == "~" or "`" not in info:
                fence_character = marker[0]
                fence_length = len(marker)
                continue
        content.append(line)
    visible = html.unescape(_HTML_TAG.sub("", "\n".join(content)))
    visible = re.sub(r"[\s*_~`#>\[\](){}.!:+\\|=-]", "", visible)
    return bool(visible)


def validate_story(path: Path) -> list[Diagnostic]:
    try:
        if not path.exists():
            return [_diagnostic(path, "STORY_MISSING", "$", "story.md is absent")]
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            return [
                _diagnostic(
                    path,
                    "STORY_INVALID_ARTIFACT",
                    "$",
                    "story.md must be a non-empty regular file",
                )
            ]
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return [_diagnostic(path, "STORY_IO_ERROR", "$", "story.md could not be read")]
    lines = text.splitlines()
    if len(lines) < 3 or lines[0] != "---":
        return [_diagnostic(path, "STORY_FRONTMATTER_INVALID", "$", "YAML frontmatter is required")]
    try:
        closing = lines.index("---", 1)
        metadata = yaml.load("\n".join(lines[1:closing]), Loader=StrictLoader)
    except DuplicateFieldError as error:
        return [
            _diagnostic(
                path,
                "STORY_FRONTMATTER_DUPLICATE_FIELD",
                f"frontmatter.{error.field}",
                f"frontmatter field {error.field} appears more than once",
            )
        ]
    except (ValueError, yaml.YAMLError):
        return [_diagnostic(path, "STORY_FRONTMATTER_INVALID", "$", "frontmatter is invalid")]
    if not isinstance(metadata, dict):
        return [
            _diagnostic(path, "STORY_FRONTMATTER_INVALID", "$", "frontmatter must be a mapping")
        ]
    expected = {"schema_version", "title", "target_duration_seconds"}
    unknown = sorted(str(key) for key in metadata if key not in expected)
    if unknown:
        return [
            _diagnostic(
                path,
                "STORY_FRONTMATTER_UNKNOWN_FIELD",
                f"frontmatter.{unknown[0]}",
                f"unknown frontmatter field {unknown[0]}",
            )
        ]
    missing = sorted(expected - metadata.keys())
    if missing:
        return [
            _diagnostic(
                path,
                "STORY_FRONTMATTER_MISSING_FIELD",
                f"frontmatter.{missing[0]}",
                f"required frontmatter field {missing[0]} is missing",
            )
        ]
    version = metadata["schema_version"]
    if type(version) is not int or version != 1:
        return [
            _diagnostic(
                path,
                "STORY_VERSION_UNSUPPORTED",
                "frontmatter.schema_version",
                "schema_version must be the supported integer 1",
            )
        ]
    title = metadata["title"]
    duration = metadata["target_duration_seconds"]
    if not isinstance(title, str) or not title.strip():
        return [
            _diagnostic(
                path,
                "STORY_FRONTMATTER_INVALID_VALUE",
                "frontmatter.title",
                "title must be a non-empty string",
            )
        ]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, int | float)
        or not math.isfinite(duration)
        or duration <= 0
    ):
        return [
            _diagnostic(
                path,
                "STORY_FRONTMATTER_INVALID_VALUE",
                "frontmatter.target_duration_seconds",
                "target_duration_seconds must be a positive finite number",
            )
        ]
    structure = _markdown_structure(lines, closing + 1)
    headings = [match.group(1) for _, line in structure if (match := _SECTION.fullmatch(line))]
    if headings != ["Intent", "Emotional Arc", "Moments"]:
        return [
            _diagnostic(
                path,
                "STORY_SECTION_ORDER",
                "sections.order",
                "required sections must appear exactly once and in order",
            )
        ]
    section_lines = [index for index, line in structure if _SECTION.fullmatch(line)]
    intent_body = lines[section_lines[0] + 1 : section_lines[1]]
    arc_body = lines[section_lines[1] + 1 : section_lines[2]]
    for name, body in (("intent", intent_body), ("emotional-arc", arc_body)):
        if not _has_visible_prose(body):
            return [
                _diagnostic(
                    path,
                    "STORY_SECTION_EMPTY",
                    f"sections.{name}",
                    f"{name} must contain non-empty Markdown prose",
                )
            ]
    moments_body = lines[section_lines[2] + 1 :]
    moments_start = section_lines[2] + 1
    moment_lines = [
        index - moments_start
        for index, line in _markdown_structure(lines, moments_start)
        if _MOMENT.fullmatch(line)
    ]
    if not moment_lines:
        return [
            _diagnostic(
                path,
                "STORY_MOMENTS_EMPTY",
                "sections.moments",
                "Moments must contain at least one Story Moment",
            )
        ]
    seen: set[str] = set()
    for position, start in enumerate(moment_lines):
        match = _MOMENT.fullmatch(moments_body[start])
        assert match is not None
        moment_id = match.group(1)
        location = f"sections.moments.{moment_id}"
        if _ID.fullmatch(moment_id) is None:
            return [
                _diagnostic(
                    path,
                    "STORY_MOMENT_ID_INVALID",
                    location,
                    "Story Moment ID must be lowercase kebab-case",
                )
            ]
        if moment_id in seen:
            return [
                _diagnostic(
                    path,
                    "STORY_MOMENT_ID_DUPLICATE",
                    location,
                    f"Story Moment ID {moment_id} appears more than once",
                )
            ]
        seen.add(moment_id)
        end = moment_lines[position + 1] if position + 1 < len(moment_lines) else len(moments_body)
        if not _has_visible_prose(moments_body[start + 1 : end]):
            return [
                _diagnostic(
                    path,
                    "STORY_MOMENT_EMPTY",
                    location,
                    f"Story Moment {moment_id} must contain non-empty Markdown prose",
                )
            ]
    return []


def _diagnostic(path: Path, code: str, location: str, message: str) -> Diagnostic:
    return {"artifact": str(path), "code": code, "location": location, "message": message}


def write_story_validation(workspace: Path, as_json: bool) -> int:
    story = workspace / "story.md"
    diagnostics = validate_story(story)
    payload: ValidationPayload = {
        "artifact": str(story),
        "state": "invalid" if diagnostics else "ready",
        "diagnostics": diagnostics,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"artifact={story} state={payload['state']}")
        for item in diagnostics:
            print(
                f"artifact={item['artifact']} location={item['location']} "
                f"code={item['code']} message={item['message']}"
            )
    return 1 if diagnostics else 0
