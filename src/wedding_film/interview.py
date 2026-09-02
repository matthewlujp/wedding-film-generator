from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from wedding_film.catalog import PARTICIPANT_ID_PATTERN
from wedding_film.participants import ParticipantProblem, load_participants
from wedding_film.story import Diagnostic, DuplicateFieldError, StrictLoader

SCHEMA_VERSION = 1
REQUIRED_SECTIONS = ("couple", "wedding", "film", "constraints")
RECOMMENDED_SECTIONS = ("people", "chronology", "texture")
DEFAULT_TARGET_DURATION_SECONDS = 300.0
_HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


class InterviewProblem(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _problem(code: str, message: str) -> InterviewProblem:
    return InterviewProblem(code, message)


@dataclass(frozen=True)
class Partner:
    name: str
    called_as: str


@dataclass(frozen=True)
class Couple:
    partner_a: Partner
    partner_b: Partner
    first_met: str
    relationship_years: float
    proposal: str
    turning_point: str


@dataclass(frozen=True)
class Wedding:
    date: str
    venue: str
    ceremony_style: str
    guests: str
    screening_moment: str


@dataclass(frozen=True)
class Film:
    target_duration_seconds: float
    audience: str
    tone_wanted: str
    tone_avoided: str
    music: str


@dataclass(frozen=True)
class ExcludedMaterial:
    description: str
    asset_ids: tuple[str, ...]


@dataclass(frozen=True)
class Constraints:
    forbidden_topics: tuple[str, ...]
    excluded_people: tuple[str, ...]
    excluded_materials: tuple[ExcludedMaterial, ...]
    notes: str


@dataclass(frozen=True)
class Person:
    participant_id: str
    relationship: str
    called_as: str
    anecdotes: tuple[str, ...]


@dataclass(frozen=True)
class ChronologyEntry:
    period: str
    what_happened: str


@dataclass(frozen=True)
class Texture:
    catchphrases: tuple[str, ...]
    nicknames: tuple[str, ...]
    inside_jokes: tuple[str, ...]
    speech_habits: tuple[str, ...]


@dataclass(frozen=True)
class SkippedSection:
    section: str
    reason: str
    actor: str


@dataclass(frozen=True)
class Brief:
    schema_version: int
    couple: Couple | None = None
    wedding: Wedding | None = None
    film: Film | None = None
    constraints: Constraints | None = None
    people: tuple[Person, ...] = ()
    chronology: tuple[ChronologyEntry, ...] = ()
    texture: Texture | None = None
    skipped_sections: tuple[SkippedSection, ...] = ()


def _mapping(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise _problem("INTERVIEW_INVALID_VALUE", f"{location} must be a mapping")
    return value


def _exact_fields(data: dict[str, object], expected: tuple[str, ...], location: str) -> None:
    unknown = sorted(set(data) - set(expected))
    if unknown:
        raise _problem("INTERVIEW_UNKNOWN_FIELD", f"unknown field {location}.{unknown[0]}")
    missing = [field_name for field_name in expected if field_name not in data]
    if missing:
        raise _problem("INTERVIEW_MISSING_FIELD", f"missing field {location}.{missing[0]}")


def _non_empty_string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _problem("INTERVIEW_INVALID_VALUE", f"{location} must be a non-empty string")
    return value


def _string(value: object, location: str) -> str:
    if not isinstance(value, str):
        raise _problem("INTERVIEW_INVALID_VALUE", f"{location} must be a string")
    return value


def _positive_number(value: object, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise _problem("INTERVIEW_INVALID_VALUE", f"{location} must be a positive number")
    return float(value)


def _string_list(value: object, location: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _problem("INTERVIEW_INVALID_VALUE", f"{location} must be an array")
    return tuple(
        _non_empty_string(item, f"{location}[{index}]") for index, item in enumerate(value)
    )


def _date(value: object, location: str) -> str:
    text = _non_empty_string(value, location)
    if _DATE_PATTERN.fullmatch(text) is None:
        raise _problem("INTERVIEW_INVALID_VALUE", f"{location} must be a YYYY-MM-DD date")
    return text


def _kebab_id(value: object, location: str) -> str:
    text = _non_empty_string(value, location)
    if PARTICIPANT_ID_PATTERN.fullmatch(text) is None:
        raise _problem("INTERVIEW_INVALID_VALUE", f"{location} must be lowercase kebab-case")
    return text


def _partner(value: object, location: str) -> Partner:
    data = _mapping(value, location)
    _exact_fields(data, ("name", "called_as"), location)
    return Partner(
        name=_non_empty_string(data["name"], f"{location}.name"),
        called_as=_non_empty_string(data["called_as"], f"{location}.called_as"),
    )


def _couple(value: object, location: str) -> Couple:
    data = _mapping(value, location)
    _exact_fields(
        data,
        ("partner_a", "partner_b", "first_met", "relationship_years", "proposal", "turning_point"),
        location,
    )
    return Couple(
        partner_a=_partner(data["partner_a"], f"{location}.partner_a"),
        partner_b=_partner(data["partner_b"], f"{location}.partner_b"),
        first_met=_non_empty_string(data["first_met"], f"{location}.first_met"),
        relationship_years=_positive_number(
            data["relationship_years"], f"{location}.relationship_years"
        ),
        proposal=_non_empty_string(data["proposal"], f"{location}.proposal"),
        turning_point=_non_empty_string(data["turning_point"], f"{location}.turning_point"),
    )


def _wedding(value: object, location: str) -> Wedding:
    data = _mapping(value, location)
    _exact_fields(data, ("date", "venue", "ceremony_style", "guests", "screening_moment"), location)
    return Wedding(
        date=_date(data["date"], f"{location}.date"),
        venue=_non_empty_string(data["venue"], f"{location}.venue"),
        ceremony_style=_non_empty_string(data["ceremony_style"], f"{location}.ceremony_style"),
        guests=_non_empty_string(data["guests"], f"{location}.guests"),
        screening_moment=_non_empty_string(
            data["screening_moment"], f"{location}.screening_moment"
        ),
    )


def _film(value: object, location: str) -> Film:
    data = _mapping(value, location)
    _exact_fields(
        data,
        ("target_duration_seconds", "audience", "tone_wanted", "tone_avoided", "music"),
        location,
    )
    return Film(
        target_duration_seconds=_positive_number(
            data["target_duration_seconds"], f"{location}.target_duration_seconds"
        ),
        audience=_non_empty_string(data["audience"], f"{location}.audience"),
        tone_wanted=_non_empty_string(data["tone_wanted"], f"{location}.tone_wanted"),
        tone_avoided=_non_empty_string(data["tone_avoided"], f"{location}.tone_avoided"),
        music=_non_empty_string(data["music"], f"{location}.music"),
    )


def _excluded_material(value: object, location: str) -> ExcludedMaterial:
    data = _mapping(value, location)
    _exact_fields(data, ("description", "asset_ids"), location)
    asset_ids_value = data["asset_ids"]
    if not isinstance(asset_ids_value, list):
        raise _problem("INTERVIEW_INVALID_VALUE", f"{location}.asset_ids must be an array")
    asset_ids: list[str] = []
    for index, item in enumerate(asset_ids_value):
        if not isinstance(item, str) or _HASH_PATTERN.fullmatch(item) is None:
            raise _problem(
                "INTERVIEW_INVALID_VALUE",
                f"{location}.asset_ids[{index}] must be a lowercase sha256 address",
            )
        asset_ids.append(item)
    return ExcludedMaterial(
        description=_non_empty_string(data["description"], f"{location}.description"),
        asset_ids=tuple(asset_ids),
    )


def _constraints(value: object, location: str) -> Constraints:
    data = _mapping(value, location)
    _exact_fields(
        data, ("forbidden_topics", "excluded_people", "excluded_materials", "notes"), location
    )
    materials_value = data["excluded_materials"]
    if not isinstance(materials_value, list):
        raise _problem(
            "INTERVIEW_INVALID_VALUE", f"{location}.excluded_materials must be an array"
        )
    return Constraints(
        forbidden_topics=_string_list(data["forbidden_topics"], f"{location}.forbidden_topics"),
        excluded_people=_string_list(data["excluded_people"], f"{location}.excluded_people"),
        excluded_materials=tuple(
            _excluded_material(item, f"{location}.excluded_materials[{index}]")
            for index, item in enumerate(materials_value)
        ),
        notes=_string(data["notes"], f"{location}.notes"),
    )


def _person(value: object, location: str) -> Person:
    data = _mapping(value, location)
    _exact_fields(data, ("participant_id", "relationship", "called_as", "anecdotes"), location)
    return Person(
        participant_id=_kebab_id(data["participant_id"], f"{location}.participant_id"),
        relationship=_non_empty_string(data["relationship"], f"{location}.relationship"),
        called_as=_non_empty_string(data["called_as"], f"{location}.called_as"),
        anecdotes=_string_list(data["anecdotes"], f"{location}.anecdotes"),
    )


def _chronology_entry(value: object, location: str) -> ChronologyEntry:
    data = _mapping(value, location)
    _exact_fields(data, ("period", "what_happened"), location)
    return ChronologyEntry(
        period=_non_empty_string(data["period"], f"{location}.period"),
        what_happened=_non_empty_string(data["what_happened"], f"{location}.what_happened"),
    )


def _texture(value: object, location: str) -> Texture:
    data = _mapping(value, location)
    _exact_fields(
        data, ("catchphrases", "nicknames", "inside_jokes", "speech_habits"), location
    )
    return Texture(
        catchphrases=_string_list(data["catchphrases"], f"{location}.catchphrases"),
        nicknames=_string_list(data["nicknames"], f"{location}.nicknames"),
        inside_jokes=_string_list(data["inside_jokes"], f"{location}.inside_jokes"),
        speech_habits=_string_list(data["speech_habits"], f"{location}.speech_habits"),
    )


_SKIPPABLE_SECTIONS = REQUIRED_SECTIONS + RECOMMENDED_SECTIONS


def _skipped_section(value: object, location: str) -> SkippedSection:
    data = _mapping(value, location)
    _exact_fields(data, ("section", "reason", "actor"), location)
    section = _non_empty_string(data["section"], f"{location}.section")
    if section not in _SKIPPABLE_SECTIONS:
        raise _problem("INTERVIEW_INVALID_VALUE", f"{location}.section is not a known section")
    return SkippedSection(
        section=section,
        reason=_non_empty_string(data["reason"], f"{location}.reason"),
        actor=_non_empty_string(data["actor"], f"{location}.actor"),
    )


def _parse_brief(loaded: object) -> Brief:
    data = _mapping(loaded, "$")
    known = (
        "schema_version",
        *REQUIRED_SECTIONS,
        *RECOMMENDED_SECTIONS,
        "skipped_sections",
    )
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise _problem("INTERVIEW_UNKNOWN_FIELD", f"unknown field $.{unknown[0]}")
    if "schema_version" not in data:
        raise _problem("INTERVIEW_MISSING_FIELD", "missing field $.schema_version")
    schema_version = data["schema_version"]
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise _problem("INTERVIEW_UNSUPPORTED_VERSION", "$.schema_version is unsupported")

    skipped_value = data.get("skipped_sections", [])
    if not isinstance(skipped_value, list):
        raise _problem("INTERVIEW_INVALID_VALUE", "$.skipped_sections must be an array")
    skipped_sections = tuple(
        _skipped_section(item, f"$.skipped_sections[{index}]")
        for index, item in enumerate(skipped_value)
    )

    people_value = data.get("people", [])
    if not isinstance(people_value, list):
        raise _problem("INTERVIEW_INVALID_VALUE", "$.people must be an array")
    people = tuple(_person(item, f"$.people[{index}]") for index, item in enumerate(people_value))
    seen_participants = [entry.participant_id for entry in people]
    if len(seen_participants) != len(set(seen_participants)):
        raise _problem("INTERVIEW_DUPLICATE_PARTICIPANT", "$.people participant_id must be unique")

    chronology_value = data.get("chronology", [])
    if not isinstance(chronology_value, list):
        raise _problem("INTERVIEW_INVALID_VALUE", "$.chronology must be an array")
    chronology = tuple(
        _chronology_entry(item, f"$.chronology[{index}]")
        for index, item in enumerate(chronology_value)
    )

    return Brief(
        schema_version=schema_version,
        couple=_couple(data["couple"], "$.couple") if "couple" in data else None,
        wedding=_wedding(data["wedding"], "$.wedding") if "wedding" in data else None,
        film=_film(data["film"], "$.film") if "film" in data else None,
        constraints=(
            _constraints(data["constraints"], "$.constraints") if "constraints" in data else None
        ),
        people=people,
        chronology=chronology,
        texture=_texture(data["texture"], "$.texture") if "texture" in data else None,
        skipped_sections=skipped_sections,
    )


def load_brief(path: Path) -> Brief:
    if not path.exists():
        raise _problem("INTERVIEW_BRIEF_MISSING", "interview/brief.yaml is absent")
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise _problem(
            "INTERVIEW_BRIEF_INVALID_ARTIFACT",
            "interview/brief.yaml must be a non-empty regular file",
        )
    try:
        text = path.read_text(encoding="utf-8")
        loaded = yaml.load(text, Loader=StrictLoader)
    except DuplicateFieldError as error:
        raise _problem(
            "INTERVIEW_DUPLICATE_FIELD", f"field {error.field} appears more than once"
        ) from None
    except (OSError, UnicodeError, yaml.YAMLError):
        raise _problem(
            "INTERVIEW_BRIEF_INVALID_YAML", "interview/brief.yaml is not valid UTF-8 YAML"
        ) from None
    return _parse_brief(loaded)


def unmet_required_sections(brief: Brief) -> list[str]:
    skipped = {entry.section for entry in brief.skipped_sections}
    return [
        name
        for name in REQUIRED_SECTIONS
        if getattr(brief, name) is None and name not in skipped
    ]


def _check_participant_references(workspace: Path, brief: Brief) -> None:
    try:
        known_ids = {participant.id for participant in load_participants(workspace)}
    except ParticipantProblem:
        return
    for index, person in enumerate(brief.people):
        if person.participant_id not in known_ids:
            raise _problem(
                "INTERVIEW_UNKNOWN_PARTICIPANT",
                f"$.people[{index}].participant_id references unknown participant "
                f"{person.participant_id!r}",
            )


def load_effective_brief(workspace: Path) -> Brief:
    """Load the interview brief, raising unless every required section is present or skipped."""
    brief = load_brief(workspace / "interview" / "brief.yaml")
    _check_participant_references(workspace, brief)
    unmet = unmet_required_sections(brief)
    if unmet:
        raise _problem(
            "INTERVIEW_SECTION_INCOMPLETE",
            f"required section {unmet[0]} is missing and not skipped",
        )
    return brief


def excluded_asset_ids(brief: Brief) -> set[str]:
    if brief.constraints is None:
        return set()
    return {
        asset_id
        for material in brief.constraints.excluded_materials
        for asset_id in material.asset_ids
    }


def narrative_summary(brief: Brief) -> dict[str, object]:
    """Interview content safe for a narrative-generation prompt: no asset_ids, no filenames."""
    summary: dict[str, object] = {}
    if brief.couple is not None:
        partner_a, partner_b = brief.couple.partner_a, brief.couple.partner_b
        summary["couple"] = {
            "partner_a": {"name": partner_a.name, "called_as": partner_a.called_as},
            "partner_b": {"name": partner_b.name, "called_as": partner_b.called_as},
            "first_met": brief.couple.first_met,
            "relationship_years": brief.couple.relationship_years,
            "proposal": brief.couple.proposal,
            "turning_point": brief.couple.turning_point,
        }
    if brief.wedding is not None:
        summary["wedding"] = {
            "date": brief.wedding.date,
            "venue": brief.wedding.venue,
            "ceremony_style": brief.wedding.ceremony_style,
            "guests": brief.wedding.guests,
            "screening_moment": brief.wedding.screening_moment,
        }
    if brief.film is not None:
        summary["film"] = {
            "audience": brief.film.audience,
            "tone_wanted": brief.film.tone_wanted,
            "tone_avoided": brief.film.tone_avoided,
            "music": brief.film.music,
        }
    if brief.constraints is not None:
        summary["constraints"] = {
            "forbidden_topics": list(brief.constraints.forbidden_topics),
            "excluded_people": list(brief.constraints.excluded_people),
            "notes": brief.constraints.notes,
        }
    if brief.people:
        summary["people"] = [
            {
                "participant_id": person.participant_id,
                "relationship": person.relationship,
                "called_as": person.called_as,
                "anecdotes": list(person.anecdotes),
            }
            for person in brief.people
        ]
    if brief.chronology:
        summary["chronology"] = [
            {"period": entry.period, "what_happened": entry.what_happened}
            for entry in brief.chronology
        ]
    if brief.texture is not None:
        summary["texture"] = {
            "catchphrases": list(brief.texture.catchphrases),
            "nicknames": list(brief.texture.nicknames),
            "inside_jokes": list(brief.texture.inside_jokes),
            "speech_habits": list(brief.texture.speech_habits),
        }
    return summary


def _diagnostic(path: Path, code: str, location: str, message: str) -> Diagnostic:
    return {"artifact": str(path), "code": code, "location": location, "message": message}


def validate_interview(workspace: Path) -> list[Diagnostic]:
    path = workspace / "interview" / "brief.yaml"
    try:
        brief = load_brief(path)
        _check_participant_references(workspace, brief)
    except InterviewProblem as problem:
        return [_diagnostic(path, problem.code, "$", problem.message)]
    unmet = unmet_required_sections(brief)
    if unmet:
        return [
            _diagnostic(
                path,
                "INTERVIEW_SECTION_INCOMPLETE",
                f"$.{unmet[0]}",
                f"required section {unmet[0]} is missing and not skipped",
            )
        ]
    return []


def write_interview_validation(workspace: Path, as_json: bool) -> int:
    path = workspace / "interview" / "brief.yaml"
    diagnostics = validate_interview(workspace)
    payload = {
        "artifact": str(path),
        "state": "invalid" if diagnostics else "ready",
        "diagnostics": diagnostics,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"artifact={path} state={payload['state']}")
        for item in diagnostics:
            print(
                f"artifact={item['artifact']} location={item['location']} "
                f"code={item['code']} message={item['message']}"
            )
    return 1 if diagnostics else 0
