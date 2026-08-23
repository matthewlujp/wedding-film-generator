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
