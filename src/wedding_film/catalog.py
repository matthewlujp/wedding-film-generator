from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast

from wedding_film.config import ConfigProblem, load_project_config

SCHEMA_VERSION = 1
ASSET_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
PARTICIPANT_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
MEDIA_TYPE_PATTERN = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*\Z")
RFC3339_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)

TOP_LEVEL_KEYS = (
    "schema_version",
    "asset_id",
    "byte_size",
    "locators",
    "observations",
    "inferences",
    "runs",
    "corrections",
)
REQUIRED_KEYS = frozenset(TOP_LEVEL_KEYS[:4])
OPTIONAL_KEYS = frozenset(TOP_LEVEL_KEYS[4:])
OBSERVATION_KEYS = frozenset(
    {
        "media_type",
        "format",
        "pixel_width",
        "pixel_height",
        "orientation",
        "capture_time",
        "camera_make",
        "camera_model",
        "location",
    }
)
INFERENCE_KEYS = frozenset(
    {
        "description",
        "wedding_moment",
        "subject_roles",
        "setting",
        "mood",
        "shot_type",
        "quality_flags",
    }
)
CORRECTION_TARGETS = frozenset(
    {*(f"/observations/{key}" for key in OBSERVATION_KEYS),
     *(f"/inferences/{key}" for key in INFERENCE_KEYS),
     "/subject_attributions"}
)
RUN_KEYS = (
    "kind",
    "tool",
    "adapter",
    "version",
    "provider",
    "model",
    "prompt_version",
    "settings",
    "outcome",
    "executed_at",
)

JsonObject = dict[str, Any]
ScannedAssets = dict[str, tuple[int, list[str]]]


@dataclass(frozen=True)
class MaterialsManifest:
    assets: ScannedAssets
    digest: str


class CatalogProblem(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _problem(code: str, message: str) -> CatalogProblem:
    return CatalogProblem(code, message)


def _unique_object(pairs: list[tuple[str, Any]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise _problem("CATALOG_DUPLICATE_FIELD", "catalog object contains a duplicate field")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> NoReturn:
    raise _problem("CATALOG_JSON_INVALID", "catalog contains a non-JSON numeric value")


def _canonical_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    return value


def _is_int(value: object) -> bool:
    return type(value) is int


def _reject_null(value: object) -> None:
    if value is None:
        raise _problem("CATALOG_NULL_FORBIDDEN", "catalog values must not be null")
    if isinstance(value, dict):
        for nested in value.values():
            _reject_null(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_null(nested)


def _object(value: object) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise _problem("CATALOG_FIELD_TYPE", "catalog field has an incorrect type")
    return value


def _string(value: object, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise _problem("CATALOG_FIELD_TYPE", "catalog field has an incorrect type")
    return value


def _validate_rfc3339(value: object) -> str:
    text = _string(value)
    if not RFC3339_PATTERN.fullmatch(text):
        raise _problem("CATALOG_FIELD_VALUE", "catalog timestamp must use RFC 3339")
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise _problem("CATALOG_FIELD_VALUE", "catalog timestamp must use RFC 3339") from error
    return text


def _validate_locator(value: object) -> str:
    locator = _string(value)
    if "\\" in locator:
        raise _problem("CATALOG_LOCATOR_INVALID", "Asset Locator is not project-relative")
    pure = PurePosixPath(locator)
    if (
        pure.is_absolute()
        or len(pure.parts) < 2
        or pure.parts[0] != "materials"
        or any(part in ("", ".", "..") for part in pure.parts)
        or pure.as_posix() != locator
    ):
        raise _problem("CATALOG_LOCATOR_INVALID", "Asset Locator is not project-relative")
    return locator


def _validate_run_reference(
    value: object, runs: Mapping[str, object], expected_kind: str
) -> str:
    run_id = _string(value)
    if run_id not in runs:
        raise _problem("CATALOG_PROVENANCE_DANGLING", "catalog claim references an absent run")
    run = _object(runs[run_id])
    if run.get("kind") != expected_kind or run.get("outcome") != "success":
        raise _problem(
            "CATALOG_PROVENANCE_INVALID",
            "catalog claim must reference a successful run of the correct kind",
        )
    return run_id


def _validate_runs(value: object) -> JsonObject:
    runs = _object(value)
    normalized: JsonObject = {}
    for run_id in sorted(runs):
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise _problem("CATALOG_FIELD_VALUE", "catalog run ID is invalid")
        run = _object(runs[run_id])
        unknown = set(run) - set(RUN_KEYS)
        if unknown:
            raise _problem("CATALOG_UNKNOWN_FIELD", "catalog run contains an unknown field")
        kind = run.get("kind")
        if kind == "extraction":
            required = {"kind", "version", "outcome", "executed_at"}
            if required - set(run) or not ({"tool", "adapter"} & set(run)):
                raise _problem(
                    "CATALOG_PROVENANCE_INVALID",
                    "extraction provenance is incomplete",
                )
        elif kind == "vision":
            required = {
                "kind",
                "provider",
                "model",
                "prompt_version",
                "settings",
                "outcome",
                "executed_at",
            }
            if required - set(run):
                raise _problem(
                    "CATALOG_PROVENANCE_INVALID", "vision provenance is incomplete"
                )
        else:
            raise _problem(
                "CATALOG_PROVENANCE_INVALID", "catalog run kind is unsupported"
            )
        if run.get("outcome") != "success":
            raise _problem(
                "CATALOG_PROVENANCE_INVALID",
                "record-local run provenance must have outcome success",
            )
        for key, item in run.items():
            if key == "settings":
                _object(item)
            elif key == "executed_at":
                _validate_rfc3339(item)
            else:
                _string(item)
        normalized[run_id] = {
            key: _canonical_value(run[key]) for key in RUN_KEYS if key in run
        }
    return normalized


def _number(value: object) -> int | float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise _problem("CATALOG_FIELD_TYPE", "catalog field must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise _problem("CATALOG_FIELD_VALUE", "catalog number must be finite")
    return value


def _string_set(value: object) -> list[str]:
    if not isinstance(value, list):
        raise _problem("CATALOG_FIELD_TYPE", "catalog field must be an array of strings")
    strings = [_string(item) for item in value]
    if strings != sorted(set(strings)):
        raise _problem("CATALOG_FIELD_VALUE", "catalog string array must be sorted and unique")
    return strings


def _validate_observation_value(name: str, value: object) -> Any:
    if name == "media_type":
        media_type = _string(value)
        if not MEDIA_TYPE_PATTERN.fullmatch(media_type):
            raise _problem("CATALOG_FIELD_VALUE", "media_type must be a lowercase media type")
        return media_type
    if name in {"format", "camera_make", "camera_model"}:
        return _string(value)
    if name in {"pixel_width", "pixel_height"}:
        if not _is_int(value):
            raise _problem("CATALOG_FIELD_TYPE", "pixel dimensions must be integers")
        integer = cast(int, value)
        if integer <= 0:
            raise _problem("CATALOG_FIELD_VALUE", "pixel dimensions must be positive")
        return integer
    if name == "orientation":
        if not _is_int(value):
            raise _problem("CATALOG_FIELD_TYPE", "orientation must be an integer")
        integer = cast(int, value)
        if not 1 <= integer <= 8:
            raise _problem("CATALOG_FIELD_VALUE", "orientation must be from 1 through 8")
        return integer
    if name == "capture_time":
        return _validate_rfc3339(value)
    if name == "location":
        location = _object(value)
        required = {"latitude", "longitude"}
        allowed = required | {"altitude"}
        if required - set(location):
            raise _problem("CATALOG_MISSING_FIELD", "location is missing coordinates")
        if set(location) - allowed:
            raise _problem("CATALOG_UNKNOWN_FIELD", "location contains an unknown field")
        latitude = _number(location["latitude"])
        longitude = _number(location["longitude"])
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise _problem("CATALOG_FIELD_VALUE", "location coordinates are out of range")
        normalized: JsonObject = {"latitude": latitude, "longitude": longitude}
        if "altitude" in location:
            normalized["altitude"] = _number(location["altitude"])
        return normalized
    raise _problem("CATALOG_UNKNOWN_FIELD", "Observation field is unsupported")


def _validate_inference_value(name: str, value: object) -> Any:
    if name in {"description", "setting"}:
        return _string(value)
    if name == "wedding_moment":
        moment = _string(value)
        if moment not in {"preparation", "ceremony", "portraits", "reception", "other"}:
            raise _problem("CATALOG_FIELD_VALUE", "wedding_moment is unsupported")
        return moment
    if name in {"subject_roles", "mood", "quality_flags"}:
        return _string_set(value)
    if name == "shot_type":
        shot_type = _string(value)
        if shot_type not in {"wide", "medium", "close-up", "detail"}:
            raise _problem("CATALOG_FIELD_VALUE", "shot_type is unsupported")
        return shot_type
    raise _problem("CATALOG_UNKNOWN_FIELD", "Inference field is unsupported")


def _validate_correction_value(target: str, value: object) -> Any:
    if "/" in target[1:]:
        section, name = target.removeprefix("/").split("/", maxsplit=1)
    else:
        section, name = "", ""
    if section == "observations":
        return _validate_observation_value(name, value)
    if section == "inferences":
        return _validate_inference_value(name, value)
    if target == "/subject_attributions":
        participants = _string_set(value)
        if not all(PARTICIPANT_ID_PATTERN.fullmatch(item) for item in participants):
            raise _problem("CATALOG_FIELD_VALUE", "Subject Attribution ID is invalid")
        return participants
    raise _problem("CATALOG_CORRECTION_INVALID", "Correction target is not allowed")


def _validate_observations(value: object, runs: Mapping[str, object]) -> JsonObject:
    observations = _object(value)
    unknown = set(observations) - OBSERVATION_KEYS
    if unknown:
        raise _problem("CATALOG_UNKNOWN_FIELD", "observations contains an unknown field")
    normalized: JsonObject = {}
    for name in sorted(observations):
        claim = _object(observations[name])
        if set(claim) != {"value", "run_id"}:
            code = (
                "CATALOG_MISSING_FIELD"
                if {"value", "run_id"} - set(claim)
                else "CATALOG_UNKNOWN_FIELD"
            )
            raise _problem(code, "Observation must contain value and run_id")
        normalized[name] = {
            "value": _validate_observation_value(name, claim["value"]),
            "run_id": _validate_run_reference(claim["run_id"], runs, "extraction"),
        }
    return normalized


def _validate_inferences(value: object, runs: Mapping[str, object]) -> JsonObject:
    inferences = _object(value)
    unknown = set(inferences) - INFERENCE_KEYS
    if unknown:
        raise _problem("CATALOG_UNKNOWN_FIELD", "inferences contains an unknown field")
    normalized: JsonObject = {}
    for name in sorted(inferences):
        claim = _object(inferences[name])
        required = {"value", "confidence", "run_id"}
        if set(claim) != required:
            code = "CATALOG_MISSING_FIELD" if required - set(claim) else "CATALOG_UNKNOWN_FIELD"
            raise _problem(code, "Inference must contain value, confidence, and run_id")
        confidence = _number(claim["confidence"])
        if not 0 <= confidence <= 1:
            raise _problem("CATALOG_FIELD_VALUE", "Inference confidence must be finite from 0 to 1")
        normalized[name] = {
            "value": _validate_inference_value(name, claim["value"]),
            "confidence": confidence,
            "run_id": _validate_run_reference(claim["run_id"], runs, "vision"),
        }
    return normalized


def _validate_corrections(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        raise _problem("CATALOG_FIELD_TYPE", "corrections must be an array")
    normalized: list[JsonObject] = []
    for item in value:
        correction = _object(item)
        allowed = {"target", "op", "value", "at", "actor", "reason"}
        unknown = set(correction) - allowed
        if unknown:
            raise _problem("CATALOG_UNKNOWN_FIELD", "Correction contains an unknown field")
        required = {"target", "op", "at", "actor"}
        if required - set(correction):
            raise _problem("CATALOG_MISSING_FIELD", "Correction is missing a required field")
        target = _string(correction["target"])
        if target not in CORRECTION_TARGETS:
            raise _problem("CATALOG_CORRECTION_INVALID", "Correction target is not allowed")
        op = _string(correction["op"])
        if op not in ("set", "remove"):
            raise _problem("CATALOG_CORRECTION_INVALID", "Correction operation is invalid")
        if (op == "set") != ("value" in correction):
            raise _problem(
                "CATALOG_CORRECTION_INVALID",
                "Correction value is required only for a set operation",
            )
        actor = _string(correction["actor"])
        ordered: JsonObject = {"target": target, "op": op}
        if op == "set":
            ordered["value"] = _validate_correction_value(target, correction["value"])
        ordered["at"] = _validate_rfc3339(correction["at"])
        ordered["actor"] = actor
        if "reason" in correction:
            ordered["reason"] = _string(correction["reason"])
        normalized.append(ordered)
    return normalized


def _validate_record(value: object) -> JsonObject:
    _reject_null(value)
    record = _object(value)
    unknown = set(record) - REQUIRED_KEYS - OPTIONAL_KEYS
    if unknown:
        raise _problem("CATALOG_UNKNOWN_FIELD", "catalog record contains an unknown field")
    if REQUIRED_KEYS - set(record):
        raise _problem("CATALOG_MISSING_FIELD", "catalog record is missing a required field")
    version = record["schema_version"]
    if not _is_int(version):
        raise _problem("CATALOG_FIELD_TYPE", "schema_version must be an integer")
    if version != SCHEMA_VERSION:
        raise _problem("CATALOG_UNSUPPORTED_VERSION", "catalog schema version is unsupported")
    asset_id = record["asset_id"]
    if not isinstance(asset_id, str) or not ASSET_ID_PATTERN.fullmatch(asset_id):
        raise _problem("CATALOG_ASSET_ID_INVALID", "asset_id must be a lowercase SHA-256 address")
    byte_size = record["byte_size"]
    if not _is_int(byte_size):
        raise _problem("CATALOG_FIELD_TYPE", "byte_size must be an integer")
    if byte_size < 0:
        raise _problem("CATALOG_FIELD_VALUE", "byte_size must not be negative")
    locator_values = record["locators"]
    if not isinstance(locator_values, list):
        raise _problem("CATALOG_FIELD_TYPE", "locators must be an array")
    locators = [_validate_locator(item) for item in locator_values]
    if not locators:
        raise _problem("CATALOG_LOCATOR_INVALID", "an asset needs at least one Asset Locator")
    if locators != sorted(set(locators)):
        raise _problem("CATALOG_LOCATOR_DUPLICATE", "Asset Locators must be sorted and unique")

    runs = _validate_runs(record.get("runs", {}))
    normalized: JsonObject = {
        "schema_version": version,
        "asset_id": asset_id,
        "byte_size": byte_size,
        "locators": locators,
    }
    if "observations" in record:
        normalized["observations"] = _validate_observations(record["observations"], runs)
    if "inferences" in record:
        normalized["inferences"] = _validate_inferences(record["inferences"], runs)
    if "runs" in record:
        normalized["runs"] = runs
    if "corrections" in record:
        normalized["corrections"] = _validate_corrections(record["corrections"])
    return normalized


def _load_catalog(path: Path) -> list[JsonObject]:
    try:
        if path.is_symlink() or not path.is_file():
            raise _problem("CATALOG_INVALID_ARTIFACT", "catalog must be a regular file")
        text = path.read_text(encoding="utf-8")
    except CatalogProblem:
        raise
    except (OSError, UnicodeError) as error:
        raise _problem("CATALOG_IO_ERROR", "catalog could not be read") from error
    records: list[JsonObject] = []
    if text and not text.endswith("\n"):
        raise _problem("CATALOG_JSONL_INVALID", "catalog JSONL must end with a newline")
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise _problem("CATALOG_JSONL_INVALID", "catalog JSONL contains a blank line")
        try:
            loaded = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except CatalogProblem:
            raise
        except json.JSONDecodeError as error:
            raise _problem(
                "CATALOG_JSON_INVALID", f"catalog line {line_number} is malformed"
            ) from error
        except ValueError as error:
            # CPython raises ValueError here when a JSON integer exceeds its
            # configured decimal digit conversion limit.
            raise _problem(
                "CATALOG_JSON_INVALID", f"catalog line {line_number} is malformed"
            ) from error
        records.append(_validate_record(loaded))
    return records


def _validate_whole_catalog(records: list[JsonObject]) -> None:
    asset_ids: set[str] = set()
    locators: set[str] = set()
    versions: set[int] = set()
    prior_asset_id = ""
    for record in records:
        asset_id: str = record["asset_id"]
        versions.add(record["schema_version"])
        if asset_id in asset_ids:
            raise _problem("CATALOG_ASSET_DUPLICATE", "catalog contains a duplicate asset_id")
        if asset_id < prior_asset_id:
            raise _problem("CATALOG_RECORD_ORDER", "catalog records must be sorted by asset_id")
        prior_asset_id = asset_id
        asset_ids.add(asset_id)
        for locator in record["locators"]:
            if locator in locators:
                raise _problem("CATALOG_LOCATOR_DUPLICATE", "catalog contains a duplicate locator")
            locators.add(locator)
    if len(versions) > 1:
        raise _problem("CATALOG_MIXED_VERSIONS", "catalog contains mixed schema versions")


def _sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise _problem("CATALOG_SOURCE_INTEGRITY", "Original Asset is not a regular file")
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        after = path.stat(follow_symlinks=False)
    except CatalogProblem:
        raise
    except OSError as error:
        raise _problem("CATALOG_SOURCE_INTEGRITY", "Original Asset could not be read") from error
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or size != after.st_size
    ):
        raise _problem("CATALOG_SOURCE_INTEGRITY", "Original Asset changed during inspection")
    return digest.hexdigest(), size


def _records_match_manifest(
    records: list[JsonObject], manifest: MaterialsManifest
) -> bool:
    catalog_assets: ScannedAssets = {
        record["asset_id"].removeprefix("sha256:"): (
            record["byte_size"],
            record["locators"],
        )
        for record in records
    }
    return catalog_assets == manifest.assets


def validate_catalog(
    path: Path,
    workspace: Path,
    *,
    manifest: MaterialsManifest | None = None,
) -> list[JsonObject]:
    records = _load_catalog(path)
    _validate_whole_catalog(records)
    current = manifest if manifest is not None else inspect_materials(workspace)
    if not _records_match_manifest(records, current):
        raise _problem(
            "CATALOG_SOURCE_INTEGRITY",
            "catalog does not match the complete current Materials manifest",
        )
    return records


def _scan_materials(workspace: Path) -> ScannedAssets:
    materials = workspace / "materials"
    try:
        if not materials.exists():
            raise _problem("MATERIALS_MISSING", "Materials directory is absent")
        if materials.is_symlink() or not materials.is_dir():
            raise _problem("MATERIALS_UNSAFE", "Materials must be a real directory")
        materials_real = materials.resolve(strict=True)
    except CatalogProblem:
        raise
    except OSError as error:
        raise _problem("MATERIALS_IO_ERROR", "Materials could not be inspected") from error

    assets: dict[str, tuple[int, list[str]]] = {}

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise _problem("MATERIALS_IO_ERROR", "Materials could not be scanned") from error
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    raise _problem(
                        "MATERIALS_SYMLINK_UNSUPPORTED",
                        "symlinks are unsupported inside Materials",
                    )
                if entry.is_dir(follow_symlinks=False):
                    visit(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise _problem(
                        "MATERIALS_NONREGULAR_UNSUPPORTED",
                        "only regular files are supported inside Materials",
                    )
                resolved = path.resolve(strict=True)
                resolved.relative_to(materials_real)
            except CatalogProblem:
                raise
            except (OSError, ValueError) as error:
                raise _problem("MATERIALS_OUTSIDE", "Material path escapes Materials") from error
            digest, size = _sha256_and_size(path)
            locator = path.relative_to(workspace).as_posix()
            existing = assets.get(digest)
            if existing is None:
                assets[digest] = (size, [locator])
            else:
                if existing[0] != size:
                    raise _problem(
                        "CATALOG_SOURCE_INTEGRITY",
                        "content identity has inconsistent size",
                    )
                existing[1].append(locator)

    visit(materials)
    return assets


def inspect_materials(workspace: Path) -> MaterialsManifest:
    assets = _scan_materials(workspace)
    base_records = [
        {
            "asset_id": f"sha256:{digest}",
            "byte_size": byte_size,
            "locators": sorted(set(locators)),
        }
        for digest, (byte_size, locators) in sorted(assets.items())
    ]
    serialized = json.dumps(
        base_records, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return MaterialsManifest(assets=assets, digest=hashlib.sha256(serialized).hexdigest())


def _serialize(records: list[JsonObject]) -> str:
    return "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )


def load_catalog(workspace: Path) -> list[JsonObject]:
    """Load a valid current catalog after checking its Original Assets."""
    try:
        load_project_config(workspace)
    except ConfigProblem as problem:
        raise _problem(problem.code, problem.message) from problem
    return validate_catalog(workspace / "catalog.jsonl", workspace)


def checkpoint_catalog(workspace: Path, records: list[JsonObject]) -> None:
    """Validate and atomically publish one complete catalog checkpoint."""
    normalized = [_validate_record(record) for record in records]
    _validate_whole_catalog(normalized)
    catalog = workspace / "catalog.jsonl"
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{catalog.name}.", suffix=".candidate", dir=catalog.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as candidate:
            candidate.write(_serialize(normalized))
            candidate.flush()
            os.fsync(candidate.fileno())
        validate_catalog(temporary, workspace)
        os.replace(temporary, catalog)
    except CatalogProblem:
        raise
    except OSError as error:
        raise _problem("CATALOG_IO_ERROR", "catalog could not be atomically replaced") from error
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def scan_catalog(workspace: Path) -> int:
    try:
        load_project_config(workspace)
    except ConfigProblem as problem:
        raise _problem(problem.code, problem.message) from problem
    catalog = workspace / "catalog.jsonl"
    previous: dict[str, JsonObject] = {}
    try:
        if catalog.exists() or catalog.is_symlink():
            existing_records = _load_catalog(catalog)
            _validate_whole_catalog(existing_records)
            previous = {record["asset_id"]: record for record in existing_records}
    except OSError as error:
        raise _problem("CATALOG_IO_ERROR", "catalog could not be inspected") from error

    manifest = inspect_materials(workspace)
    scanned = manifest.assets
    records: list[JsonObject] = []
    for digest, (byte_size, locators) in sorted(scanned.items()):
        asset_id = f"sha256:{digest}"
        old = previous.get(asset_id, {})
        record: JsonObject = {
            "schema_version": SCHEMA_VERSION,
            "asset_id": asset_id,
            "byte_size": byte_size,
            "locators": sorted(set(locators)),
        }
        for key in TOP_LEVEL_KEYS[4:]:
            if key in old:
                record[key] = old[key]
        records.append(_validate_record(record))
    _validate_whole_catalog(records)

    temporary: Path | None = None
    try:
        catalog.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{catalog.name}.", suffix=".candidate", dir=catalog.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as candidate:
            candidate.write(_serialize(records))
            candidate.flush()
            os.fsync(candidate.fileno())
        validate_catalog(temporary, workspace)
        os.replace(temporary, catalog)
    except CatalogProblem:
        raise
    except OSError as error:
        raise _problem("CATALOG_IO_ERROR", "catalog could not be atomically replaced") from error
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    return len(records)
