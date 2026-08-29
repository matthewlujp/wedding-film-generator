from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from wedding_film.catalog import PARTICIPANT_ID_PATTERN, CatalogProblem, JsonObject, load_catalog

SCHEMA_VERSION = 1
PARTICIPANT_FIELDS = ("id", "display_name", "role", "principal")


class ParticipantProblem(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _problem(code: str, message: str) -> ParticipantProblem:
    return ParticipantProblem(code, message)


class _UnsetType:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final[Any] = _UnsetType()


@dataclass(frozen=True)
class Participant:
    id: str
    display_name: str | None
    role: str | None
    principal: bool


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _problem("PARTICIPANT_FIELD_TYPE", f"{field} must be a non-empty string")
    return value


def _validate_participant(value: object) -> Participant:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise _problem("PARTICIPANT_FIELD_TYPE", "participant entry must be a mapping")
    unknown = set(value) - set(PARTICIPANT_FIELDS)
    if unknown:
        raise _problem(
            "PARTICIPANT_UNKNOWN_FIELD", f"unknown participant field {sorted(unknown)[0]!r}"
        )
    missing = {"id", "principal"} - set(value)
    if missing:
        raise _problem(
            "PARTICIPANT_MISSING_FIELD", f"participant is missing field {sorted(missing)[0]!r}"
        )
    participant_id = value["id"]
    if not isinstance(participant_id, str) or not PARTICIPANT_ID_PATTERN.fullmatch(participant_id):
        raise _problem(
            "PARTICIPANT_ID_INVALID", "Participant ID must be lowercase kebab-case"
        )
    principal = value["principal"]
    if not isinstance(principal, bool):
        raise _problem("PARTICIPANT_FIELD_TYPE", "principal must be a boolean")
    return Participant(
        id=participant_id,
        display_name=_optional_string(value.get("display_name"), "display_name"),
        role=_optional_string(value.get("role"), "role"),
        principal=principal,
    )


def _validate_roster(value: object) -> list[Participant]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise _problem("PARTICIPANT_FIELD_TYPE", "participants.yaml must be a mapping")
    unknown = set(value) - {"schema_version", "participants"}
    if unknown:
        raise _problem("PARTICIPANT_UNKNOWN_FIELD", f"unknown field {sorted(unknown)[0]!r}")
    missing = {"schema_version", "participants"} - set(value)
    if missing:
        raise _problem("PARTICIPANT_MISSING_FIELD", f"missing field {sorted(missing)[0]!r}")
    schema_version = value["schema_version"]
    if schema_version != SCHEMA_VERSION:
        raise _problem(
            "PARTICIPANT_UNSUPPORTED_VERSION", "participants.yaml schema_version is unsupported"
        )
    raw_participants = value["participants"]
    if not isinstance(raw_participants, list):
        raise _problem("PARTICIPANT_FIELD_TYPE", "participants must be an array")
    participants = [_validate_participant(item) for item in raw_participants]
    ids = [participant.id for participant in participants]
    if len(ids) != len(set(ids)):
        raise _problem("PARTICIPANT_DUPLICATE", "participant IDs must be unique")
    return sorted(participants, key=lambda participant: participant.id)


def load_participants(workspace: Path) -> list[Participant]:
    path = workspace / "participants.yaml"
    if not path.is_file() or path.is_symlink():
        raise _problem(
            "PARTICIPANT_FILE_MISSING", "participants.yaml is missing or is not a regular file"
        )
    try:
        contents = path.read_text(encoding="utf-8")
        loaded = yaml.safe_load(contents)
    except (OSError, UnicodeError, yaml.YAMLError):
        raise _problem(
            "PARTICIPANT_FILE_INVALID", "participants.yaml is not valid UTF-8 YAML"
        ) from None
    return _validate_roster(loaded)


def _to_raw(participants: list[Participant]) -> list[JsonObject]:
    raw: list[JsonObject] = []
    for participant in sorted(participants, key=lambda item: item.id):
        entry: JsonObject = {"id": participant.id}
        if participant.display_name is not None:
            entry["display_name"] = participant.display_name
        if participant.role is not None:
            entry["role"] = participant.role
        entry["principal"] = participant.principal
        raw.append(entry)
    return raw


def save_participants(workspace: Path, participants: list[Participant]) -> None:
    raw: JsonObject = {"schema_version": SCHEMA_VERSION, "participants": _to_raw(participants)}
    normalized = _validate_roster(raw)
    path = workspace / "participants.yaml"
    document = yaml.safe_dump(
        {"schema_version": SCHEMA_VERSION, "participants": _to_raw(normalized)},
        sort_keys=False,
        allow_unicode=True,
    )
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".candidate", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as candidate:
            candidate.write(document)
            candidate.flush()
            os.fsync(candidate.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise _problem(
            "PARTICIPANT_IO_ERROR", "participants.yaml could not be atomically replaced"
        ) from error
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def add_participant(
    workspace: Path,
    *,
    participant_id: str,
    display_name: str | None,
    role: str | None,
    principal: bool,
) -> Participant:
    participants = load_participants(workspace)
    if any(participant.id == participant_id for participant in participants):
        raise _problem(
            "PARTICIPANT_DUPLICATE", f"participant {participant_id!r} already exists"
        )
    new_participant = _validate_participant(
        {
            "id": participant_id,
            "display_name": display_name,
            "role": role,
            "principal": principal,
        }
    )
    save_participants(workspace, [*participants, new_participant])
    return new_participant


def update_participant(
    workspace: Path,
    *,
    participant_id: str,
    display_name: str | None | _UnsetType = UNSET,
    role: str | None | _UnsetType = UNSET,
    principal: bool | _UnsetType = UNSET,
) -> Participant:
    participants = load_participants(workspace)
    index = next(
        (position for position, item in enumerate(participants) if item.id == participant_id),
        None,
    )
    if index is None:
        raise _problem("PARTICIPANT_NOT_FOUND", f"participant {participant_id!r} was not found")
    current = participants[index]
    updated = _validate_participant(
        {
            "id": current.id,
            "display_name": current.display_name if display_name is UNSET else display_name,
            "role": current.role if role is UNSET else role,
            "principal": current.principal if principal is UNSET else principal,
        }
    )
    save_participants(
        workspace, [*participants[:index], updated, *participants[index + 1 :]]
    )
    return updated


def remove_participant(workspace: Path, *, participant_id: str) -> None:
    participants = load_participants(workspace)
    if not any(participant.id == participant_id for participant in participants):
        raise _problem("PARTICIPANT_NOT_FOUND", f"participant {participant_id!r} was not found")
    in_use = participant_ids_in_use(workspace)
    if participant_id in in_use:
        raise _problem(
            "PARTICIPANT_IN_USE",
            f"participant {participant_id!r} is referenced by a Subject Attribution",
        )
    remaining = [participant for participant in participants if participant.id != participant_id]
    save_participants(workspace, remaining)


def _effective_subject_attribution_ids(record: JsonObject) -> list[str]:
    latest: JsonObject | None = None
    for correction in record.get("corrections", []):
        if correction["target"] == "/subject_attributions":
            latest = correction
    if latest is None or latest["op"] != "set":
        return []
    return list(latest["value"])


def participant_ids_in_use(workspace: Path) -> set[str]:
    try:
        records = load_catalog(workspace)
    except CatalogProblem as problem:
        if problem.code == "CATALOG_INVALID_ARTIFACT":
            return set()
        raise
    ids: set[str] = set()
    for record in records:
        ids.update(_effective_subject_attribution_ids(record))
    return ids
