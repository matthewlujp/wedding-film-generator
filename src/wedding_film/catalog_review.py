from __future__ import annotations

import fnmatch
import getpass
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wedding_film.catalog import (
    CORRECTION_TARGETS,
    CatalogProblem,
    JsonObject,
    checkpoint_catalog,
    load_catalog,
)
from wedding_film.participants import ParticipantProblem, load_participants
from wedding_film.storyboard import parse_storyboard


def _problem(code: str, message: str) -> CatalogProblem:
    return CatalogProblem(code, message)


def default_actor() -> str:
    try:
        user = getpass.getuser()
    except OSError:
        user = ""
    return user or "cli-user"


@dataclass(frozen=True)
class EffectiveValue:
    target: str
    value: Any
    present: bool
    source: str  # "correction", "correction-removed", "observation", "inference", "none"


def _correction_index(record: JsonObject) -> dict[str, JsonObject]:
    latest: dict[str, JsonObject] = {}
    for correction in record.get("corrections", []):
        latest[correction["target"]] = correction
    return latest


def effective_values(record: JsonObject) -> dict[str, EffectiveValue]:
    """Resolve one effective value per allowed Correction target.

    The latest Correction for a target wins; otherwise the relevant
    Inference; otherwise the Observation. Underlying evidence is never
    consulted or altered once a Correction shadows it.
    """
    corrections = _correction_index(record)
    observations = record.get("observations", {})
    inferences = record.get("inferences", {})
    results: dict[str, EffectiveValue] = {}
    for target in sorted(CORRECTION_TARGETS):
        correction = corrections.get(target)
        if correction is not None:
            if correction["op"] == "set":
                results[target] = EffectiveValue(target, correction["value"], True, "correction")
            else:
                results[target] = EffectiveValue(target, None, False, "correction-removed")
            continue
        if target.startswith("/observations/"):
            claim = observations.get(target.removeprefix("/observations/"))
            if claim is not None:
                results[target] = EffectiveValue(target, claim["value"], True, "observation")
                continue
        elif target.startswith("/inferences/"):
            claim = inferences.get(target.removeprefix("/inferences/"))
            if claim is not None:
                results[target] = EffectiveValue(target, claim["value"], True, "inference")
                continue
        results[target] = EffectiveValue(target, None, False, "none")
    return results


def resolve_asset(records: list[JsonObject], identifier: str) -> JsonObject:
    for record in records:
        if record["asset_id"] == identifier:
            return record
    for record in records:
        if identifier in record["locators"]:
            return record
    raise _problem(
        "CATALOG_ASSET_NOT_FOUND", f"asset {identifier!r} was not found by ID or locator"
    )


def storyboard_referenced_assets(workspace: Path) -> set[str] | None:
    """Return asset IDs referenced by storyboard.yaml, or None when unavailable."""
    storyboard_path = workspace / "storyboard.yaml"
    try:
        if not storyboard_path.is_file():
            return None
        document, diagnostics = parse_storyboard(storyboard_path)
    except OSError:
        return None
    if document is None or diagnostics:
        return None
    return {
        item["asset_id"]
        for item in document.get("sequence", [])
        if item.get("type") == "photo"
    }


def filter_records(
    records: list[JsonObject],
    *,
    asset_ids: list[str] | None = None,
    locator_globs: list[str] | None = None,
    low_confidence_threshold: float | None = None,
    storyboard_assets: set[str] | None = None,
) -> list[JsonObject]:
    result = records
    if asset_ids:
        wanted = set(asset_ids)
        result = [record for record in result if record["asset_id"] in wanted]
    if locator_globs:
        result = [
            record
            for record in result
            if any(
                fnmatch.fnmatch(locator, pattern)
                for locator in record["locators"]
                for pattern in locator_globs
            )
        ]
    if low_confidence_threshold is not None:
        result = [
            record
            for record in result
            if any(
                claim["confidence"] < low_confidence_threshold
                for claim in record.get("inferences", {}).values()
            )
        ]
    if storyboard_assets is not None:
        result = [record for record in result if record["asset_id"] in storyboard_assets]
    return sorted(result, key=lambda record: record["asset_id"])


def resolve_selection(
    records: list[JsonObject],
    asset_ids: list[str],
    locator_globs: list[str],
) -> list[JsonObject]:
    """Resolve and fully validate a mutation selection before any write happens."""
    if not asset_ids and not locator_globs:
        raise _problem(
            "CATALOG_SELECTION_EMPTY",
            "a Correction requires at least one explicit --asset-id or --locator selector",
        )
    by_id = {record["asset_id"]: record for record in records}
    selected: dict[str, JsonObject] = {}
    for asset_id in asset_ids:
        record = by_id.get(asset_id)
        if record is None:
            raise _problem(
                "CATALOG_ASSET_NOT_FOUND", f"asset {asset_id!r} was not found by ID"
            )
        selected[asset_id] = record
    for pattern in locator_globs:
        matches = [
            record
            for record in records
            if any(fnmatch.fnmatch(locator, pattern) for locator in record["locators"])
        ]
        if not matches:
            raise _problem(
                "CATALOG_LOCATOR_SELECTION_EMPTY",
                f"locator pattern {pattern!r} matched no asset",
            )
        for record in matches:
            selected[record["asset_id"]] = record
    return [selected[asset_id] for asset_id in sorted(selected)]


def _validate_subject_attribution_participants(workspace: Path, value: Any) -> None:
    """Every Participant ID a Subject Attribution set names must exist in the roster."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return
    try:
        known_ids = {participant.id for participant in load_participants(workspace)}
    except ParticipantProblem as problem:
        raise _problem(problem.code, problem.message) from problem
    missing = sorted(set(value) - known_ids)
    if missing:
        raise _problem(
            "CATALOG_PARTICIPANT_NOT_FOUND",
            f"Subject Attribution references unknown Participant {missing[0]!r}",
        )


@dataclass(frozen=True)
class CorrectionResult:
    resolved_count: int
    asset_ids: list[str]
    correction: JsonObject
    applied: bool


def apply_correction(
    workspace: Path,
    *,
    target: str,
    op: str,
    value: Any,
    actor: str,
    reason: str | None,
    asset_ids: list[str],
    locator_globs: list[str],
    dry_run: bool,
) -> CorrectionResult:
    if target not in CORRECTION_TARGETS:
        raise _problem(
            "CATALOG_CORRECTION_INVALID", f"{target} is not an allowed Correction target"
        )
    if not actor.strip():
        raise _problem("CATALOG_CORRECTION_INVALID", "Correction actor must be non-empty")
    if target == "/subject_attributions" and op == "set":
        _validate_subject_attribution_participants(workspace, value)
    records = load_catalog(workspace)
    selected = resolve_selection(records, asset_ids, locator_globs)

    correction: JsonObject = {"target": target, "op": op}
    if op == "set":
        correction["value"] = value
    correction["at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    correction["actor"] = actor
    if reason:
        correction["reason"] = reason

    selected_ids = [record["asset_id"] for record in selected]
    if dry_run:
        return CorrectionResult(len(selected), selected_ids, correction, applied=False)

    selected_set = set(selected_ids)
    updated_records = [
        {**record, "corrections": [*record.get("corrections", []), dict(correction)]}
        if record["asset_id"] in selected_set
        else record
        for record in records
    ]
    checkpoint_catalog(workspace, updated_records)
    return CorrectionResult(len(selected), selected_ids, correction, applied=True)
