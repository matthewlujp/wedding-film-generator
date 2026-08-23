from __future__ import annotations

import hashlib
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

家族への感謝を伝える。

## Emotional Arc

静かな期待から祝福へ。

## Moments

### preparation

式を迎える朝。

### ceremony

誓いを分かち合う。
"""


def story_hash(story: str) -> str:
    return hashlib.sha256(story.encode("utf-8")).hexdigest()


def valid_script(story: str) -> str:
    return f"""---
schema_version: 1
title: 私たちの Wedding
inputs:
  story: sha256:{story_hash(story)}
---

## opening-card

type: card
story_moment: preparation

ふたりの物語
ここから始まる

## ceremony-caption

type: caption
story_moment: ceremony

永遠のはじまり

## ceremony-narration

type: narration
story_moment: ceremony

家族と友人に見守られ、
ふたりは誓いを交わします。
"""


def test_script_validate_accepts_all_block_types_and_japanese_deterministically(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "valid-script"
    workspace.mkdir()
    story = valid_story()
    (workspace / "story.md").write_text(story, encoding="utf-8")
    script = workspace / "script.md"
    script.write_text(valid_script(story), encoding="utf-8")

    first = run_cli("--project", str(workspace), "script", "validate", "--json")
    second = run_cli("--project", str(workspace), "script", "validate", "--json")

    assert first.returncode == second.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    assert json.loads(first.stdout) == {
        "artifact": str(script),
        "diagnostics": [],
        "document": {
            "blocks": [
                {
                    "block_id": "opening-card",
                    "body": "ふたりの物語\nここから始まる",
                    "story_moment": "preparation",
                    "type": "card",
                },
                {
                    "block_id": "ceremony-caption",
                    "body": "永遠のはじまり",
                    "story_moment": "ceremony",
                    "type": "caption",
                },
                {
                    "block_id": "ceremony-narration",
                    "body": "家族と友人に見守られ、\nふたりは誓いを交わします。",
                    "story_moment": "ceremony",
                    "type": "narration",
                },
            ],
            "inputs": {"story": f"sha256:{story_hash(story)}"},
            "schema_version": 1,
            "title": "私たちの Wedding",
        },
        "state": "ready",
        "warnings": [],
    }


def test_script_validate_rejects_strict_structure_and_cross_reference_errors(
    tmp_path: Path,
) -> None:
    story = valid_story()
    base = valid_script(story)
    cases = {
        "unsupported-version": (
            base.replace("schema_version: 1", "schema_version: 2"),
            "SCRIPT_VERSION_UNSUPPORTED",
        ),
        "null-title": (
            base.replace("title: 私たちの Wedding", "title: null"),
            "SCRIPT_FRONTMATTER_INVALID_VALUE",
        ),
        "unknown-frontmatter": (
            base.replace("title: 私たちの Wedding", "title: 私たちの Wedding\nlanguage: ja"),
            "SCRIPT_FRONTMATTER_UNKNOWN_FIELD",
        ),
        "duplicate-frontmatter": (
            base.replace("title: 私たちの Wedding", "title: first\ntitle: second"),
            "SCRIPT_FRONTMATTER_DUPLICATE_FIELD",
        ),
        "yaml-anchor": (
            base.replace("title: 私たちの Wedding", "title: &shared 私たちの Wedding"),
            "SCRIPT_FRONTMATTER_INVALID",
        ),
        "unknown-input": (
            base.replace("  story: sha256:", "  catalog: sha256:"),
            "SCRIPT_INPUTS_INVALID",
        ),
        "null-input": (
            base.replace(f"sha256:{story_hash(story)}", "null"),
            "SCRIPT_STORY_HASH_INVALID",
        ),
        "invalid-hash": (
            base.replace(story_hash(story), story_hash(story).upper()),
            "SCRIPT_STORY_HASH_INVALID",
        ),
        "invalid-id": (
            base.replace("## opening-card", "## Opening_Card"),
            "SCRIPT_BLOCK_ID_INVALID",
        ),
        "duplicate-id": (
            base.replace("## ceremony-caption", "## opening-card"),
            "SCRIPT_BLOCK_ID_DUPLICATE",
        ),
        "invalid-type": (base.replace("type: caption", "type: quote"), "SCRIPT_BLOCK_TYPE_INVALID"),
        "null-type": (base.replace("type: caption", "type: null"), "SCRIPT_BLOCK_TYPE_INVALID"),
        "metadata-order": (
            base.replace(
                "type: caption\nstory_moment: ceremony",
                "story_moment: ceremony\ntype: caption",
            ),
            "SCRIPT_BLOCK_METADATA_ORDER",
        ),
        "unknown-metadata": (
            base.replace(
                "story_moment: ceremony\n\n永遠", "story_moment: ceremony\nlanguage: ja\n\n永遠", 1
            ),
            "SCRIPT_BLOCK_METADATA_UNKNOWN",
        ),
        "broken-reference": (
            base.replace("story_moment: ceremony", "story_moment: reception", 1),
            "SCRIPT_STORY_MOMENT_UNKNOWN",
        ),
        "empty-body": (base.replace("ふたりの物語\nここから始まる", ""), "SCRIPT_BLOCK_BODY_EMPTY"),
        "rich-emphasis": (
            base.replace("永遠のはじまり", "**永遠のはじまり**"),
            "SCRIPT_BLOCK_BODY_RICH_TEXT",
        ),
        "rich-japanese-emphasis": (
            base.replace("永遠のはじまり", "これは*永遠*です"),
            "SCRIPT_BLOCK_BODY_RICH_TEXT",
        ),
        "rich-link": (
            base.replace("永遠のはじまり", "[永遠](https://example.test)"),
            "SCRIPT_BLOCK_BODY_RICH_TEXT",
        ),
        "rich-image": (
            base.replace("永遠のはじまり", "![写真](photo.jpg)"),
            "SCRIPT_BLOCK_BODY_RICH_TEXT",
        ),
        "rich-html": (
            base.replace("永遠のはじまり", "<strong>永遠</strong>"),
            "SCRIPT_BLOCK_BODY_RICH_TEXT",
        ),
        "rich-list": (
            base.replace("永遠のはじまり", "- 永遠のはじまり"),
            "SCRIPT_BLOCK_BODY_RICH_TEXT",
        ),
        "extra-heading": (
            base.replace("永遠のはじまり", "### 永遠のはじまり"),
            "SCRIPT_BLOCK_BODY_RICH_TEXT",
        ),
        "content-before-first-block": (
            base.replace("---\n\n## opening-card", "---\n\nDraft text\n\n## opening-card"),
            "SCRIPT_BLOCK_ORDER",
        ),
        "repeated-moment-metadata": (
            base.replace("永遠のはじまり", "story_moment: ceremony\n\n永遠のはじまり"),
            "SCRIPT_BLOCK_METADATA_UNKNOWN",
        ),
        "rich-reference-link": (
            base.replace("永遠のはじまり", "[永遠][forever]\n\n[forever]: /future"),
            "SCRIPT_BLOCK_BODY_RICH_TEXT",
        ),
        "rich-code": (
            base.replace("永遠のはじまり", "    永遠のはじまり"),
            "SCRIPT_BLOCK_BODY_RICH_TEXT",
        ),
        "format-control": (
            base.replace("永遠のはじまり", "永遠\u200bのはじまり"),
            "SCRIPT_BLOCK_BODY_RICH_TEXT",
        ),
    }

    for name, (contents, expected_code) in cases.items():
        workspace = tmp_path / name
        workspace.mkdir()
        (workspace / "story.md").write_text(story, encoding="utf-8")
        (workspace / "script.md").write_text(contents, encoding="utf-8")

        result = run_cli("--project", str(workspace), "script", "validate", "--json")

        assert result.returncode == 1, (name, result.stdout, result.stderr)
        diagnostic = json.loads(result.stdout)["diagnostics"][0]
        assert diagnostic["code"] == expected_code, name


def test_script_metadata_is_defined_by_the_first_two_non_empty_lines(tmp_path: Path) -> None:
    workspace = tmp_path / "metadata-blanks"
    workspace.mkdir()
    story = valid_story()
    (workspace / "story.md").write_text(story, encoding="utf-8")
    script = valid_script(story).replace(
        "type: caption\nstory_moment: ceremony",
        "type: caption\n\n\nstory_moment: ceremony",
    )
    (workspace / "script.md").write_text(script, encoding="utf-8")

    result = run_cli("--project", str(workspace), "script", "validate", "--json")

    assert result.returncode == 0, result.stdout


def test_script_references_only_real_story_moments_not_markdown_examples(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "fenced-story-heading"
    workspace.mkdir()
    story = valid_story().replace(
        "式を迎える朝。",
        "式を迎える朝。\n\n```markdown\n### example-only\n```",
    )
    (workspace / "story.md").write_text(story, encoding="utf-8")
    (workspace / "script.md").write_text(
        valid_script(story).replace("story_moment: ceremony", "story_moment: example-only", 1),
        encoding="utf-8",
    )

    result = run_cli("--project", str(workspace), "script", "validate", "--json")

    assert result.returncode == 1
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == (
        "SCRIPT_STORY_MOMENT_UNKNOWN"
    )


def test_script_hash_staleness_warns_by_default_and_fails_strict_without_rewriting(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "stale-script"
    workspace.mkdir()
    story = valid_story()
    story_path = workspace / "story.md"
    script_path = workspace / "script.md"
    story_path.write_text(story, encoding="utf-8")
    script_path.write_text(valid_script(story), encoding="utf-8")
    original_script = script_path.read_bytes()
    story_path.write_text(story.replace("式を迎える朝。", "新しい式を迎える朝。"), encoding="utf-8")

    default = run_cli("--project", str(workspace), "script", "validate", "--json")
    strict = run_cli("--project", str(workspace), "script", "validate", "--strict", "--json")

    assert default.returncode == 0
    assert json.loads(default.stdout)["state"] == "complete-with-warnings"
    assert json.loads(default.stdout)["warnings"][0]["code"] == "SCRIPT_STORY_HASH_STALE"
    assert strict.returncode == 1
    assert json.loads(strict.stdout)["diagnostics"][0]["code"] == "SCRIPT_STORY_HASH_STALE"
    assert script_path.read_bytes() == original_script


def test_script_text_edits_preserve_authored_line_breaks_and_stable_identity(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "edited-script"
    workspace.mkdir()
    story = valid_story()
    (workspace / "story.md").write_text(story, encoding="utf-8")
    script_path = workspace / "script.md"
    original = valid_script(story)
    edited = original.replace(
        "家族と友人に見守られ、\nふたりは誓いを交わします。",
        "静かに、\n心をこめて、\n誓いを交わします。",
    )
    script_path.write_text(edited, encoding="utf-8", newline="")
    before = script_path.read_bytes()

    result = run_cli("--project", str(workspace), "script", "validate", "--json")
    payload = json.loads(result.stdout)

    assert result.returncode == 0, result.stdout
    assert script_path.read_bytes() == before
    assert "\n心をこめて、\n".encode() in before
    assert [block["block_id"] for block in payload["document"]["blocks"]] == [
        "opening-card",
        "ceremony-caption",
        "ceremony-narration",
    ]
    assert payload["document"]["blocks"][2] == {
        "block_id": "ceremony-narration",
        "body": "静かに、\n心をこめて、\n誓いを交わします。",
        "story_moment": "ceremony",
        "type": "narration",
    }


def test_script_validation_reports_stable_human_diagnostics(tmp_path: Path) -> None:
    workspace = tmp_path / "broken-reference"
    workspace.mkdir()
    story = valid_story()
    (workspace / "story.md").write_text(story, encoding="utf-8")
    (workspace / "script.md").write_text(
        valid_script(story).replace("story_moment: ceremony", "story_moment: reception", 1),
        encoding="utf-8",
    )

    machine = run_cli("--project", str(workspace), "script", "validate", "--json")
    human = run_cli("--project", str(workspace), "script", "validate")
    diagnostic = json.loads(machine.stdout)["diagnostics"][0]

    assert machine.returncode == human.returncode == 1
    assert diagnostic["artifact"] in human.stdout
    assert f"location={diagnostic['location']}" in human.stdout
    assert f"code={diagnostic['code']}" in human.stdout


def test_status_reports_ready_and_stale_script_against_story_bytes(tmp_path: Path) -> None:
    workspace = tmp_path / "script-status"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    (workspace / "materials").mkdir()
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0
    story = valid_story()
    story_path = workspace / "story.md"
    story_path.write_text(story, encoding="utf-8")
    (workspace / "script.md").write_text(valid_script(story), encoding="utf-8")

    ready = json.loads(run_cli("--project", str(workspace), "status", "--json").stdout)

    assert ready["layers"]["script"]["state"] == "ready"
    assert ready["layers"]["script"]["reasons"][0]["code"] == "SCRIPT_VALID"
    assert ready["layers"]["script"]["next_commands"] == [
        f"wedding-film --project {workspace} script validate"
    ]

    story_path.write_text(story.replace("式を迎える朝。", "新しい朝。"), encoding="utf-8")
    stale = json.loads(run_cli("--project", str(workspace), "status", "--json").stdout)

    assert stale["layers"]["script"]["state"] == "complete-with-warnings"
    assert stale["layers"]["script"]["warnings"][0]["code"] == "SCRIPT_STORY_HASH_STALE"


def test_top_level_validate_checks_present_script_and_supports_strict_mode(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "integrated-validation"
    workspace.mkdir()
    story = valid_story()
    story_path = workspace / "story.md"
    script_path = workspace / "script.md"
    story_path.write_text(story, encoding="utf-8")
    script_path.write_text(valid_script(story), encoding="utf-8")

    valid = run_cli("--project", str(workspace), "validate", "--json")
    assert valid.returncode == 0
    assert json.loads(valid.stdout)["artifact"] == str(script_path)

    script_path.write_text(
        valid_script(story).replace("type: card", "type: rich-card"), encoding="utf-8"
    )
    invalid = run_cli("--project", str(workspace), "validate", "--json")
    assert invalid.returncode == 1
    assert json.loads(invalid.stdout)["diagnostics"][0]["code"] == "SCRIPT_BLOCK_TYPE_INVALID"

    script_path.write_text(valid_script(story), encoding="utf-8")
    story_path.write_text(story.replace("式を迎える朝。", "新しい朝。"), encoding="utf-8")
    strict = run_cli("--project", str(workspace), "validate", "--strict", "--json")
    assert strict.returncode == 1
    assert json.loads(strict.stdout)["diagnostics"][0]["code"] == "SCRIPT_STORY_HASH_STALE"


def test_top_level_validate_requires_script_after_valid_story(tmp_path: Path) -> None:
    workspace = tmp_path / "missing-script"
    workspace.mkdir()
    (workspace / "story.md").write_text(valid_story(), encoding="utf-8")

    result = run_cli("--project", str(workspace), "validate", "--json")

    assert result.returncode == 1
    assert json.loads(result.stdout)["diagnostics"][0] == {
        "artifact": str(workspace / "script.md"),
        "code": "SCRIPT_MISSING",
        "location": "$",
        "message": "script.md is absent",
    }


def test_isolated_script_validation_reports_structure_without_valid_story(
    tmp_path: Path,
) -> None:
    story = valid_story()
    for name, story_contents, warning_code in (
        ("missing-story", None, "SCRIPT_STORY_MISSING"),
        ("malformed-story", "# malformed\n", "SCRIPT_STORY_INVALID"),
    ):
        workspace = tmp_path / name
        workspace.mkdir()
        if story_contents is not None:
            (workspace / "story.md").write_text(story_contents, encoding="utf-8")
        (workspace / "script.md").write_text(valid_script(story), encoding="utf-8")

        result = run_cli("--project", str(workspace), "script", "validate", "--json")
        payload = json.loads(result.stdout)

        assert result.returncode == 0
        assert payload["state"] == "complete-with-warnings"
        assert payload["diagnostics"] == []
        assert payload["warnings"][0]["code"] == warning_code
        assert payload["document"]["blocks"][0]["type"] == "card"
