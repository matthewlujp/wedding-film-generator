from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("wedding-film")
    return subprocess.run([str(executable), *args], check=False, capture_output=True, text=True)


def valid_story() -> str:
    return """---
schema_version: 1
title: 私たちの Wedding
target_duration_seconds: 300
---

## Intent

二人の歩みと **家族への感謝** を伝える。

## Emotional Arc

Quiet anticipationから、祝福に満ちた喜びへ。

## Moments

### getting-ready

静かな朝。指輪を手に、これから始まる一日を思う。

### joyful-ceremony

誓いと笑顔を家族や友人と分かち合う。
"""


def test_validate_accepts_a_unicode_markdown_story_deterministically(tmp_path: Path) -> None:
    workspace = tmp_path / "story-project"
    workspace.mkdir()
    story = workspace / "story.md"
    story.write_text(valid_story(), encoding="utf-8")

    first = run_cli("--project", str(workspace), "validate", "--json")
    second = run_cli("--project", str(workspace), "validate", "--json")

    assert first.returncode == second.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    payload = json.loads(first.stdout)
    assert payload == {
        "artifact": str(story),
        "diagnostics": [],
        "state": "ready",
    }


def test_validate_treats_headings_in_fenced_code_as_ordinary_markdown(tmp_path: Path) -> None:
    workspace = tmp_path / "fenced-markdown"
    workspace.mkdir()
    story = workspace / "story.md"
    story.write_text(
        valid_story().replace(
            "二人の歩みと **家族への感謝** を伝える。",
            "二人の歩みを伝える。\n\n```markdown\n## Example\n### Not-a-moment\n```",
        ),
        encoding="utf-8",
    )

    result = run_cli("--project", str(workspace), "validate", "--json")

    assert result.returncode == 0, result.stdout


def test_validate_keeps_html_comment_openers_literal_inside_fenced_code(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "literal-comment-in-fence"
    workspace.mkdir()
    (workspace / "story.md").write_text(
        valid_story().replace(
            "二人の歩みと **家族への感謝** を伝える。",
            "```text\n<!-- literal and intentionally unterminated\n```",
        ),
        encoding="utf-8",
    )

    result = run_cli("--project", str(workspace), "validate", "--json")

    assert result.returncode == 0, result.stdout


def test_validate_ignores_headings_inside_closed_and_unterminated_html_comments(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "commented-headings"
    workspace.mkdir()
    (workspace / "story.md").write_text(
        valid_story()
        .replace(
            "二人の歩みと **家族への感謝** を伝える。",
            "二人の歩みを伝える。\n\n<!--\n## Draft Section\n### Draft_Moment\n-->",
        )
        .replace(
            "誓いと笑顔を家族や友人と分かち合う。",
            "誓いと笑顔を分かち合う。\n\n<!-- unfinished\n## Hidden Section\n### hidden-moment",
        ),
        encoding="utf-8",
    )

    result = run_cli("--project", str(workspace), "validate", "--json")

    assert result.returncode == 0, result.stdout


def test_validate_counts_markdown_autolinks_as_visible_prose(tmp_path: Path) -> None:
    workspace = tmp_path / "autolink-prose"
    workspace.mkdir()
    (workspace / "story.md").write_text(
        valid_story()
        .replace("二人の歩みと **家族への感謝** を伝える。", "<https://example.com/story>")
        .replace("Quiet anticipationから、祝福に満ちた喜びへ。", "<editor@example.com>"),
        encoding="utf-8",
    )

    result = run_cli("--project", str(workspace), "validate", "--json")

    assert result.returncode == 0, result.stdout


def test_validate_counts_link_labels_but_not_link_metadata_as_visible_prose(
    tmp_path: Path,
) -> None:
    visible_workspace = tmp_path / "visible-link-label"
    visible_workspace.mkdir()
    (visible_workspace / "story.md").write_text(
        valid_story().replace(
            "二人の歩みと **家族への感謝** を伝える。",
            "[家族の物語](https://example.com/story)",
        ),
        encoding="utf-8",
    )

    visible = run_cli("--project", str(visible_workspace), "validate", "--json")

    assert visible.returncode == 0, visible.stdout

    cases = {
        "empty-inline-link": "[](https://example.com/story)",
        "reference-definition": '[story]: https://example.com/story "editor note"',
    }
    for name, prose in cases.items():
        workspace = tmp_path / name
        workspace.mkdir()
        (workspace / "story.md").write_text(
            valid_story().replace("二人の歩みと **家族への感謝** を伝える。", prose),
            encoding="utf-8",
        )

        result = run_cli("--project", str(workspace), "validate", "--json")

        assert result.returncode == 1
        diagnostic = json.loads(result.stdout)["diagnostics"][0]
        assert diagnostic["code"] == "STORY_SECTION_EMPTY"
        assert diagnostic["location"] == "sections.intent"


def test_validate_accepts_markdown_headings_indented_by_up_to_three_spaces(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "indented-headings"
    workspace.mkdir()
    story = workspace / "story.md"
    story.write_text(
        valid_story()
        .replace("## Intent", "   ## Intent")
        .replace("## Emotional Arc", "  ## Emotional Arc")
        .replace("## Moments", " ## Moments")
        .replace("### getting-ready", "   ### getting-ready")
        .replace("### joyful-ceremony", "  ### joyful-ceremony"),
        encoding="utf-8",
    )

    result = run_cli("--project", str(workspace), "validate", "--json")

    assert result.returncode == 0, result.stdout


def test_validate_rejects_four_space_heading_indentation(tmp_path: Path) -> None:
    workspace = tmp_path / "over-indented-heading"
    workspace.mkdir()
    (workspace / "story.md").write_text(
        valid_story().replace("## Intent", "    ## Intent"),
        encoding="utf-8",
    )

    result = run_cli("--project", str(workspace), "validate", "--json")

    assert result.returncode == 1
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "STORY_SECTION_ORDER"


def test_validate_rejects_non_strict_or_unsupported_frontmatter(tmp_path: Path) -> None:
    replacements = {
        "unsupported": ("schema_version: 1", "schema_version: 2", "STORY_VERSION_UNSUPPORTED"),
        "unknown": (
            "title: 私たちの Wedding",
            "title: 私たちの Wedding\nlanguage: ja",
            "STORY_FRONTMATTER_UNKNOWN_FIELD",
        ),
        "missing": ("title: 私たちの Wedding\n", "", "STORY_FRONTMATTER_MISSING_FIELD"),
        "mistyped": (
            "target_duration_seconds: 300",
            'target_duration_seconds: "300"',
            "STORY_FRONTMATTER_INVALID_VALUE",
        ),
        "null": ("title: 私たちの Wedding", "title: null", "STORY_FRONTMATTER_INVALID_VALUE"),
        "duplicate": (
            "title: 私たちの Wedding",
            "title: first\ntitle: second",
            "STORY_FRONTMATTER_DUPLICATE_FIELD",
        ),
        "duration": (
            "target_duration_seconds: 300",
            "target_duration_seconds: 0",
            "STORY_FRONTMATTER_INVALID_VALUE",
        ),
    }

    for name, (old, new, code) in replacements.items():
        workspace = tmp_path / name
        workspace.mkdir()
        (workspace / "story.md").write_text(valid_story().replace(old, new), encoding="utf-8")

        result = run_cli("--project", str(workspace), "validate", "--json")
        diagnostic = json.loads(result.stdout)["diagnostics"][0]

        assert result.returncode == 1
        assert diagnostic["artifact"] == str(workspace / "story.md")
        assert diagnostic["code"] == code
        assert diagnostic["location"].startswith("frontmatter.")


def test_validate_accepts_positive_finite_decimal_target_duration(tmp_path: Path) -> None:
    workspace = tmp_path / "decimal-duration"
    workspace.mkdir()
    (workspace / "story.md").write_text(
        valid_story().replace("target_duration_seconds: 300", "target_duration_seconds: 300.5"),
        encoding="utf-8",
    )

    result = run_cli("--project", str(workspace), "validate", "--json")

    assert result.returncode == 0, result.stdout


def test_validate_rejects_non_finite_and_boolean_target_durations(tmp_path: Path) -> None:
    for name, value in (("nan", ".nan"), ("infinity", ".inf"), ("boolean", "true")):
        workspace = tmp_path / name
        workspace.mkdir()
        (workspace / "story.md").write_text(
            valid_story().replace(
                "target_duration_seconds: 300", f"target_duration_seconds: {value}"
            ),
            encoding="utf-8",
        )

        result = run_cli("--project", str(workspace), "validate", "--json")

        assert result.returncode == 1
        diagnostic = json.loads(result.stdout)["diagnostics"][0]
        assert diagnostic["code"] == "STORY_FRONTMATTER_INVALID_VALUE"
        assert diagnostic["location"] == "frontmatter.target_duration_seconds"


def test_validate_reports_malformed_yaml_without_a_traceback(tmp_path: Path) -> None:
    workspace = tmp_path / "malformed-yaml"
    workspace.mkdir()
    story = workspace / "story.md"
    story.write_text(
        valid_story().replace("schema_version: 1", "? [schema_version]\n: 1"),
        encoding="utf-8",
    )

    result = run_cli("--project", str(workspace), "validate", "--json")

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert json.loads(result.stdout)["diagnostics"][0] == {
        "artifact": str(story),
        "code": "STORY_FRONTMATTER_INVALID",
        "location": "$",
        "message": "frontmatter is invalid",
    }


def test_validate_rejects_invalid_section_and_moment_structure(tmp_path: Path) -> None:
    base = valid_story()
    intent = "## Intent\n\n二人の歩みと **家族への感謝** を伝える。"
    arc = "## Emotional Arc\n\nQuiet anticipationから、祝福に満ちた喜びへ。"
    cases = {
        "duplicate-section": (base + "\n## Intent\n\nAgain.\n", "STORY_SECTION_ORDER"),
        "reordered-sections": (
            base.replace(f"{intent}\n\n{arc}", f"{arc}\n\n{intent}"),
            "STORY_SECTION_ORDER",
        ),
        "empty-intent": (
            base.replace("二人の歩みと **家族への感謝** を伝える。", ""),
            "STORY_SECTION_EMPTY",
        ),
        "empty-arc": (
            base.replace("Quiet anticipationから、祝福に満ちた喜びへ。", ""),
            "STORY_SECTION_EMPTY",
        ),
        "empty-moments": (base.split("### getting-ready")[0], "STORY_MOMENTS_EMPTY"),
        "malformed-id": (
            base.replace("### getting-ready", "### Getting_ready"),
            "STORY_MOMENT_ID_INVALID",
        ),
        "duplicate-id": (
            base.replace("### joyful-ceremony", "### getting-ready"),
            "STORY_MOMENT_ID_DUPLICATE",
        ),
        "empty-moment": (
            base.replace("静かな朝。指輪を手に、これから始まる一日を思う。", ""),
            "STORY_MOMENT_EMPTY",
        ),
    }

    for name, (contents, code) in cases.items():
        workspace = tmp_path / name
        workspace.mkdir()
        story = workspace / "story.md"
        story.write_text(contents, encoding="utf-8")

        result = run_cli("--project", str(workspace), "validate", "--json")
        diagnostic = json.loads(result.stdout)["diagnostics"][0]

        assert result.returncode == 1
        assert diagnostic["artifact"] == str(story)
        assert diagnostic["code"] == code
        assert diagnostic["location"].startswith("sections.")


def test_validate_rejects_formatting_without_visible_prose(tmp_path: Path) -> None:
    base = valid_story()
    cases = {
        "comment-only-intent": (
            base.replace("二人の歩みと **家族への感謝** を伝える。", "<!-- author note only -->"),
            "STORY_SECTION_EMPTY",
        ),
        "unfinished-comment-moment": (
            base.replace(
                "誓いと笑顔を家族や友人と分かち合う。",
                "<!-- unfinished comment",
            ),
            "STORY_MOMENT_EMPTY",
        ),
        "empty-fence-arc": (
            base.replace(
                "Quiet anticipationから、祝福に満ちた喜びへ。",
                "```markdown\n\n```",
            ),
            "STORY_SECTION_EMPTY",
        ),
        "non-breaking-space-arc": (
            base.replace("Quiet anticipationから、祝福に満ちた喜びへ。", "&nbsp;"),
            "STORY_SECTION_EMPTY",
        ),
        "comment-only-moment": (
            base.replace("静かな朝。指輪を手に、これから始まる一日を思う。", "<!-- TODO -->"),
            "STORY_MOMENT_EMPTY",
        ),
        "empty-fence-moment": (
            base.replace("静かな朝。指輪を手に、これから始まる一日を思う。", "~~~\n\n~~~"),
            "STORY_MOMENT_EMPTY",
        ),
        "non-breaking-space-moment": (
            base.replace("静かな朝。指輪を手に、これから始まる一日を思う。", "&#160;"),
            "STORY_MOMENT_EMPTY",
        ),
        "zero-width-space-intent": (
            base.replace("二人の歩みと **家族への感謝** を伝える。", "\u200b"),
            "STORY_SECTION_EMPTY",
        ),
        "zero-width-space-moment": (
            base.replace("静かな朝。指輪を手に、これから始まる一日を思う。", "\u200b\u200d"),
            "STORY_MOMENT_EMPTY",
        ),
    }

    for name, (contents, code) in cases.items():
        workspace = tmp_path / name
        workspace.mkdir()
        (workspace / "story.md").write_text(contents, encoding="utf-8")

        result = run_cli("--project", str(workspace), "validate", "--json")

        assert result.returncode == 1
        assert json.loads(result.stdout)["diagnostics"][0]["code"] == code


def test_validate_reports_stable_human_and_json_diagnostics(tmp_path: Path) -> None:
    missing_workspace = tmp_path / "missing"
    missing_workspace.mkdir()
    missing = run_cli("--project", str(missing_workspace), "validate", "--json")

    invalid_workspace = tmp_path / "invalid"
    invalid_workspace.mkdir()
    story = invalid_workspace / "story.md"
    story.write_text(
        valid_story().replace("### getting-ready", "### Getting_ready"),
        encoding="utf-8",
    )
    human = run_cli("--project", str(invalid_workspace), "validate")
    machine = run_cli("--project", str(invalid_workspace), "validate", "--json")
    diagnostic = json.loads(machine.stdout)["diagnostics"][0]

    assert missing.returncode == 1
    assert json.loads(missing.stdout)["diagnostics"][0] == {
        "artifact": str(missing_workspace / "story.md"),
        "code": "STORY_MISSING",
        "location": "$",
        "message": "story.md is absent",
    }
    assert human.returncode == machine.returncode == 1
    assert diagnostic["artifact"] in human.stdout
    assert f"location={diagnostic['location']}" in human.stdout
    assert f"code={diagnostic['code']}" in human.stdout


def test_validate_preserves_every_canonical_source_and_requires_only_story(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "isolated-story"
    workspace.mkdir()
    canonical = {
        "catalog.jsonl": '{"asset_id":"sha256:untouched"}\n',
        "story.md": valid_story(),
        "script.md": "script stays untouched\n",
        "storyboard.yaml": "storyboard stays untouched\n",
    }
    for filename, contents in canonical.items():
        (workspace / filename).write_text(contents, encoding="utf-8")

    result = run_cli("--project", str(workspace), "validate", "--json")

    assert result.returncode == 0, result.stdout
    assert {
        filename: (workspace / filename).read_text(encoding="utf-8") for filename in canonical
    } == canonical


def test_status_reports_local_story_validity_and_a_safe_validation_command(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "story-status"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    story = workspace / "story.md"
    story.write_text(valid_story(), encoding="utf-8")

    valid_status = json.loads(run_cli("--project", str(workspace), "status", "--json").stdout)

    assert valid_status["layers"]["story"]["state"] == "stale"
    assert valid_status["layers"]["story"]["reasons"][0]["code"] == ("STORY_UPSTREAM_NOT_READY")
    assert valid_status["layers"]["story"]["next_commands"] == [
        f"wedding-film --project {workspace} validate"
    ]
    assert valid_status["safe_next_commands"] == [f"wedding-film --project {workspace} validate"]

    story.write_text("# malformed story\n", encoding="utf-8")
    invalid_status = json.loads(run_cli("--project", str(workspace), "status", "--json").stdout)

    assert invalid_status["layers"]["story"]["state"] == "invalid"
    assert invalid_status["layers"]["story"]["reasons"][0]["code"] == ("STORY_FRONTMATTER_INVALID")
    assert "location=$" in invalid_status["layers"]["story"]["reasons"][0]["message"]
