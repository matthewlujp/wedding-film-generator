from __future__ import annotations

import html
import json
import math
import re
import unicodedata
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
_HTML_TAG = re.compile(
    r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*?)?\s*/?>|<![A-Z][^<>]*>|<\?[^<>]*\?>"
)
_REFERENCE_LINK = re.compile(r"!?\[([^]\n]*)\]\[[^]\n]*\]")
_REFERENCE_DEFINITION = re.compile(
    r"^ {0,3}\[[^]\n]+\]:[ \t]*(?:<[^>\n]*>|\S+)"
    r"(?:[ \t]*(?:\n[ \t]{0,3})?(?:"
    r'\"[^\"\n]*(?:\n[ \t]*[^\"\n]*){0,32}\"|'
    r"'[^'\n]*(?:\n[ \t]*[^'\n]*){0,32}'|"
    r"\([^()\n]*(?:\n[ \t]*[^()\n]*){0,32}\)"
    r"))?[ \t]*(?:\n|$)",
    re.MULTILINE,
)
_MAX_LINK_PARENTHESES = 32
_DEFAULT_IGNORABLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0x1BCA0, 0x1BCAF),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


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


def _mask_html_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    masked = list(line)
    cursor = 0
    while cursor < len(line):
        if in_comment:
            end = line.find("-->", cursor)
            stop = len(line) if end == -1 else end + 3
            masked[cursor:stop] = " " * (stop - cursor)
            if end == -1:
                return "".join(masked), True
            in_comment = False
            cursor = stop
            continue
        start = line.find("<!--", cursor)
        if start == -1:
            break
        in_comment = True
        cursor = start
    return "".join(masked), in_comment


def _is_default_ignorable(character: str) -> bool:
    codepoint = ord(character)
    return unicodedata.category(character) in {"Cc", "Cf", "Cs"} or any(
        start <= codepoint <= end for start, end in _DEFAULT_IGNORABLE_RANGES
    )


def _inline_link_end(text: str, opening_parenthesis: int) -> int | None:
    cursor = opening_parenthesis + 1
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    if cursor < len(text) and text[cursor] == "<":
        cursor += 1
        while cursor < len(text) and text[cursor] not in ">\n":
            cursor += 2 if text[cursor] == "\\" and cursor + 1 < len(text) else 1
        if cursor >= len(text) or text[cursor] != ">":
            return None
        cursor += 1
    else:
        depth = 0
        while cursor < len(text):
            character = text[cursor]
            if character == "\\" and cursor + 1 < len(text):
                cursor += 2
                continue
            if character.isspace() and depth == 0:
                break
            if character == "(":
                depth += 1
                if depth > _MAX_LINK_PARENTHESES:
                    return None
            elif character == ")":
                if depth == 0:
                    return cursor + 1
                depth -= 1
            cursor += 1
        if depth != 0:
            return None
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    if cursor < len(text) and text[cursor] == ")":
        return cursor + 1
    if cursor >= len(text) or text[cursor] not in "\"'(":
        return None
    delimiter = text[cursor]
    closing = ")" if delimiter == "(" else delimiter
    cursor += 1
    while cursor < len(text) and text[cursor] != closing:
        cursor += 2 if text[cursor] == "\\" and cursor + 1 < len(text) else 1
    if cursor >= len(text):
        return None
    cursor += 1
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    return cursor + 1 if cursor < len(text) and text[cursor] == ")" else None


def _replace_inline_links(text: str) -> str:
    rendered: list[str] = []
    cursor = 0
    while cursor < len(text):
        label_start = text.find("[", cursor)
        if label_start == -1:
            rendered.append(text[cursor:])
            break
        label_end = text.find("]", label_start + 1)
        if label_end == -1 or label_end + 1 >= len(text) or text[label_end + 1] != "(":
            rendered.append(text[cursor : label_start + 1])
            cursor = label_start + 1
            continue
        link_end = _inline_link_end(text, label_end + 1)
        if link_end is None:
            rendered.append(text[cursor : label_start + 1])
            cursor = label_start + 1
            continue
        prefix_start = (
            label_start - 1
            if label_start > cursor and text[label_start - 1] == "!"
            else label_start
        )
        rendered.append(text[cursor:prefix_start])
        rendered.append(text[label_start + 1 : label_end])
        cursor = link_end
    return "".join(rendered)


def _scan_markdown(lines: list[str]) -> tuple[list[tuple[int, str]], list[str]]:
    structure: list[tuple[int, str]] = []
    content: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    in_comment = False
    for index, line in enumerate(lines):
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
        if not in_comment:
            match = _FENCE_OPEN.fullmatch(line)
            if match is not None:
                marker, info = match.groups()
                # Backtick info strings cannot themselves contain a backtick.
                if marker[0] == "~" or "`" not in info:
                    fence_character = marker[0]
                    fence_length = len(marker)
                    continue
        uncommented, in_comment = _mask_html_comments(line, in_comment)
        structure.append((index, uncommented))
        content.append(uncommented)
    return structure, content


def _markdown_structure(lines: list[str], start: int) -> list[tuple[int, str]]:
    """Return lines that can define structure, excluding fenced code blocks."""
    structure, _ = _scan_markdown(lines[start:])
    return [(index + start, line) for index, line in structure]


def _has_visible_prose(lines: list[str]) -> bool:
    _, content = _scan_markdown(lines)
    visible = _REFERENCE_DEFINITION.sub("", "\n".join(content))
    visible = _replace_inline_links(visible)
    visible = _REFERENCE_LINK.sub(lambda match: match.group(1), visible)
    visible = html.unescape(_HTML_TAG.sub("", visible))
    visible = re.sub(r"[\s*_~`#>\[\](){}.!:+\\|=-]", "", visible)
    return any(not _is_default_ignorable(character) for character in visible)


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
