from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from wedding_film.interview import excluded_asset_ids, load_brief, unmet_required_sections


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    executable = Path(sys.executable).with_name("wedding-film")
    return subprocess.run([str(executable), *args], check=False, capture_output=True, text=True)


def write_brief(workspace: Path, document: dict[str, object]) -> None:
    interview_dir = workspace / "interview"
    interview_dir.mkdir(exist_ok=True)
    (interview_dir / "brief.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def skip_all_required(reason: str = "test fixture", actor: str = "test") -> dict[str, object]:
    return {
        "schema_version": 1,
        "skipped_sections": [
            {"section": section, "reason": reason, "actor": actor}
            for section in ("couple", "wedding", "film", "constraints")
        ],
    }


def full_brief() -> dict[str, object]:
    return {
        "schema_version": 1,
        "couple": {
            "partner_a": {"name": "Jane Doe", "called_as": "Jane"},
            "partner_b": {"name": "Alex Chen", "called_as": "Alex"},
            "first_met": "at a coffee shop in 2018",
            "relationship_years": 5,
            "proposal": "on a beach at sunset",
            "turning_point": "moving in together",
        },
        "wedding": {
            "date": "2026-11-01",
            "venue": "Lakeside Hall",
            "ceremony_style": "traditional",
            "guests": "close family and friends",
            "screening_moment": "reception, after dinner",
        },
        "film": {
            "target_duration_seconds": 420,
            "audience": "family and friends at the reception",
            "tone_wanted": "warm, a little funny",
            "tone_avoided": "overly sentimental",
            "music": "acoustic guitar",
        },
        "constraints": {
            "forbidden_topics": [],
            "excluded_people": [],
            "excluded_materials": [],
            "notes": "",
        },
    }


def test_validate_reports_missing_when_the_brief_is_absent(tmp_path: Path) -> None:
    workspace = tmp_path / "no-interview"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0

    result = run_cli("--project", str(workspace), "interview", "validate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["state"] == "invalid"
    assert payload["diagnostics"][0]["code"] == "INTERVIEW_BRIEF_MISSING"


def test_validate_passes_when_every_required_section_is_explicitly_skipped(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "skipped-interview"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    write_brief(workspace, skip_all_required())

    result = run_cli("--project", str(workspace), "interview", "validate", "--json")

    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["state"] == "ready"
    assert payload["diagnostics"] == []


def test_validate_passes_when_every_required_section_is_answered(tmp_path: Path) -> None:
    workspace = tmp_path / "answered-interview"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    write_brief(workspace, full_brief())

    result = run_cli("--project", str(workspace), "interview", "validate", "--json")

    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["state"] == "ready"


def test_validate_reports_incomplete_when_a_required_section_is_neither_answered_nor_skipped(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "half-interview"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    document = skip_all_required()
    document["skipped_sections"] = [
        entry for entry in document["skipped_sections"] if entry["section"] != "constraints"  # type: ignore[index]
    ]
    write_brief(workspace, document)

    result = run_cli("--project", str(workspace), "interview", "validate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["state"] == "invalid"
    assert payload["diagnostics"][0]["code"] == "INTERVIEW_SECTION_INCOMPLETE"
    assert payload["diagnostics"][0]["location"] == "$.constraints"


def test_validate_rejects_an_unknown_top_level_field(tmp_path: Path) -> None:
    workspace = tmp_path / "unknown-field"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    document = skip_all_required()
    document["favourite_color"] = "blue"
    write_brief(workspace, document)

    result = run_cli("--project", str(workspace), "interview", "validate", "--json")

    assert result.returncode == 1
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "INTERVIEW_UNKNOWN_FIELD"


def test_status_transitions_interview_from_missing_to_ready(tmp_path: Path) -> None:
    workspace = tmp_path / "status-interview"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    (workspace / "materials").mkdir()
    assert run_cli("--project", str(workspace), "catalog", "scan").returncode == 0

    before = json.loads(run_cli("--project", str(workspace), "status", "--json").stdout)
    assert before["layers"]["interview"]["state"] == "missing"
    assert before["layers"]["story"]["state"] == "missing"

    write_brief(workspace, skip_all_required())
    after = json.loads(run_cli("--project", str(workspace), "status", "--json").stdout)
    assert after["layers"]["interview"]["state"] == "ready"


def test_excluded_asset_ids_reads_constraints_from_a_full_brief(tmp_path: Path) -> None:
    workspace = tmp_path / "excluded-materials"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    document = full_brief()
    document["constraints"] = {
        "forbidden_topics": ["ex-partner"],
        "excluded_people": ["ex-partner"],
        "excluded_materials": [
            {"description": "blurry test shots", "asset_ids": ["sha256:" + "0" * 64]}
        ],
        "notes": "keep it light",
    }
    write_brief(workspace, document)

    result = run_cli("--project", str(workspace), "interview", "validate", "--json")
    assert result.returncode == 0, result.stdout

    brief = load_brief(workspace / "interview" / "brief.yaml")
    assert unmet_required_sections(brief) == []
    assert excluded_asset_ids(brief) == {"sha256:" + "0" * 64}


def test_validate_rejects_a_people_entry_referencing_an_unknown_participant(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "unknown-participant"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    document = skip_all_required()
    document["people"] = [
        {
            "participant_id": "nobody-registered",
            "relationship": "friend",
            "called_as": "Sam",
            "anecdotes": [],
        }
    ]
    write_brief(workspace, document)

    result = run_cli("--project", str(workspace), "interview", "validate", "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["diagnostics"][0]["code"] == "INTERVIEW_UNKNOWN_PARTICIPANT"


def test_validate_accepts_a_people_entry_referencing_a_registered_participant(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "known-participant"
    assert run_cli("--project", str(workspace), "project", "init").returncode == 0
    assert run_cli(
        "--project", str(workspace), "participant", "add", "--id", "sam-lee",
    ).returncode == 0
    document = skip_all_required()
    document["people"] = [
        {
            "participant_id": "sam-lee",
            "relationship": "friend",
            "called_as": "Sam",
            "anecdotes": [],
        }
    ]
    write_brief(workspace, document)

    result = run_cli("--project", str(workspace), "interview", "validate", "--json")

    assert result.returncode == 0, result.stdout
