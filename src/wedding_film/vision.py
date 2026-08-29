from __future__ import annotations

import hashlib
import io
import json
import math
import os
import random
import sys
import tempfile
import time
import uuid
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

from wedding_film.catalog import (
    INFERENCE_KEYS,
    CatalogProblem,
    checkpoint_catalog,
    load_catalog,
)
from wedding_film.config import ConfigProblem, ProjectConfig, load_project_config
from wedding_film.vision_adapter import (
    AdapterFailure,
    AdapterSettings,
    AdapterSuccess,
    AnalysisDerivative,
    OutputSchema,
    VisionAdapter,
    adapter_for,
)

RECIPE_VERSION = "analysis-input-jpeg-v1"
OUTPUT_SCHEMA_VERSION = "vision-inference-v1"
PROMPT = (
    "Describe only visible facts, wedding moment, generic subject roles, setting, mood, "
    "shot type, and quality flags. Do not identify people or infer sensitive attributes."
)
MAX_EDGE = 2048
ESTIMATED_COST_PER_ASSET_USD = 0.01
COST_ESTIMATE_VERSION = "flat-rate-v1"
MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 0.02
RETRY_CAP_SECONDS = 0.5
PER_ASSET_ELAPSED_LIMIT_SECONDS = 5.0


@dataclass(frozen=True)
class AnalysisResult:
    succeeded: int
    reused: int
    failed: int


@dataclass(frozen=True)
class BatchPlan:
    asset_ids: tuple[str, ...]
    target_count: int
    estimated_cost_usd: float
    max_assets: int
    max_estimated_usd: float
    concurrency: int


@dataclass(frozen=True)
class BatchResult:
    succeeded: int
    reused: int
    failed: int
    budget_stopped: bool


@dataclass
class _Attempt:
    outcome: Literal["success", "reused", "transient_failure", "permanent_failure"]
    claims: dict[str, object] | None = None
    run_id: str | None = None
    fingerprint: str | None = None
    derivative: AnalysisDerivative | None = None
    usage: dict[str, int] | None = None
    provider_metadata: dict[str, object] | None = None
    retryable: bool = False
    error_code: str | None = None
    message: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_event(stream: Any, event: dict[str, object]) -> None:
    stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def _sync_stream(stream: Any) -> None:
    stream.flush()
    os.fsync(stream.fileno())


def _make_derivative(source: Path, destination: Path) -> AnalysisDerivative:
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            icc = opened.info.get("icc_profile")
            if isinstance(icc, bytes):
                try:
                    converted = ImageCms.profileToProfile(
                        image,
                        ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                        ImageCms.createProfile("sRGB"),
                        outputMode="RGBA" if "A" in image.getbands() else "RGB",
                    )
                    if converted is None:
                        raise ValueError("ICC conversion returned no image")
                    image = converted
                except (ImageCms.PyCMSError, OSError, ValueError):
                    image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            if "A" in image.getbands():
                rgba = image.convert("RGBA")
                white = Image.new("RGBA", rgba.size, "white")
                white.alpha_composite(rgba)
                image = white.convert("RGB")
            else:
                image = image.convert("RGB")
            image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
            image.save(
                destination,
                format="JPEG",
                quality=85,
                subsampling=2,
                optimize=False,
                progressive=False,
            )
            width, height = image.size
    except (OSError, UnidentifiedImageError, ValueError) as error:
        raise CatalogProblem(
            "VISION_INPUT_INVALID", "Original Asset could not be decoded"
        ) from error
    try:
        content = destination.read_bytes()
    except OSError as error:
        raise CatalogProblem("VISION_INPUT_IO_ERROR", "Analysis Input could not be read") from error
    return AnalysisDerivative(
        content=content,
        sha256=f"sha256:{hashlib.sha256(content).hexdigest()}",
        pixel_width=width,
        pixel_height=height,
        media_type="image/jpeg",
        recipe_version=RECIPE_VERSION,
    )


def _schema() -> OutputSchema:
    nullable_string: dict[str, object] = {"type": ["string", "null"]}
    nullable_string_array: dict[str, object] = {
        "anyOf": [
            {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            {"type": "null"},
        ]
    }
    values: dict[str, object] = {
        "description": nullable_string,
        "wedding_moment": {
            "anyOf": [
                {
                    "enum": [
                        "preparation",
                        "ceremony",
                        "portraits",
                        "reception",
                        "other",
                    ]
                },
                {"type": "null"},
            ]
        },
        "subject_roles": nullable_string_array,
        "setting": nullable_string,
        "mood": nullable_string_array,
        "shot_type": {
            "anyOf": [
                {"enum": ["wide", "medium", "close-up", "detail"]},
                {"type": "null"},
            ]
        },
        "quality_flags": nullable_string_array,
    }
    fields = tuple(sorted(INFERENCE_KEYS))
    properties = {
        name: {
            "type": "object",
            "required": ["value", "confidence"],
            "additionalProperties": False,
            "properties": {
                "value": values[name],
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        }
        for name in fields
    }
    definition: dict[str, object] = {
        "type": "object",
        "required": list(fields),
        "additionalProperties": False,
        "properties": properties,
    }
    return OutputSchema(
        version=OUTPUT_SCHEMA_VERSION,
        fields=fields,
        definition=definition,
    )


def _fingerprint(
    original_asset_id: str,
    original_byte_size: int,
    derivative: AnalysisDerivative,
    adapter_name: str,
    adapter_provider: str,
    adapter_version: str,
    settings: AdapterSettings,
    schema: OutputSchema,
) -> str:
    contract = {
        "original_asset": {
            "asset_id": original_asset_id,
            "byte_size": original_byte_size,
        },
        "derivative": {
            "sha256": derivative.sha256,
            "recipe_version": derivative.recipe_version,
            "pixel_width": derivative.pixel_width,
            "pixel_height": derivative.pixel_height,
            "media_type": derivative.media_type,
        },
        "adapter": {
            "name": adapter_name,
            "provider": adapter_provider,
            "version": adapter_version,
        },
        "model_settings": {"model": settings.model, "parameters": settings.parameters},
        "prompt": {"version": settings.prompt_version, "text": settings.prompt},
        "output_schema": {
            "version": schema.version,
            "fields": schema.fields,
            "definition": schema.definition,
        },
    }
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _json_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _text_sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _candidate(candidate: object, run_id: str) -> dict[str, object]:
    if not isinstance(candidate, dict) or set(candidate) != INFERENCE_KEYS:
        raise CatalogProblem("VISION_CANDIDATE_INCOMPLETE", "candidate must contain every field")
    normalized: dict[str, object] = {}
    for name in sorted(INFERENCE_KEYS):
        claim = candidate[name]
        if not isinstance(claim, dict) or set(claim) != {"value", "confidence"}:
            raise CatalogProblem("VISION_CANDIDATE_SCHEMA", "candidate claim schema is invalid")
        confidence = claim["confidence"]
        if (
            not isinstance(confidence, int | float)
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1
        ):
            raise CatalogProblem("VISION_CANDIDATE_CONFIDENCE", "confidence must be from 0 to 1")
        value = claim["value"]
        if value is not None:
            normalized[name] = {"value": value, "confidence": confidence, "run_id": run_id}
    return normalized


def _is_reusable(
    record: dict[str, Any],
    run_id: str,
    fingerprint: str,
    derivative: AnalysisDerivative,
    adapter_name: str,
    adapter_provider: str,
    adapter_version: str,
    settings: AdapterSettings,
    schema: OutputSchema,
) -> bool:
    run = record.get("runs", {}).get(run_id)
    if (
        not isinstance(run, dict)
        or run.get("kind") != "vision"
        or run.get("outcome") != "success"
        or run.get("fingerprint") != fingerprint
        or run_id != f"vision:{fingerprint.removeprefix('sha256:')}"
        or run.get("adapter") != adapter_name
        or run.get("provider") != adapter_provider
        or run.get("version") != adapter_version
        or run.get("model") != settings.model
        or run.get("prompt_version") != settings.prompt_version
    ):
        return False
    stored_settings = run.get("settings")
    if not isinstance(stored_settings, dict):
        return False
    expected_input = {
        "sha256": derivative.sha256,
        "pixel_width": derivative.pixel_width,
        "pixel_height": derivative.pixel_height,
        "media_type": derivative.media_type,
        "recipe_version": derivative.recipe_version,
    }
    if (
        stored_settings.get("parameters") != settings.parameters
        or stored_settings.get("adapter_version") != adapter_version
        or stored_settings.get("analysis_input") != expected_input
        or stored_settings.get("output_schema_version") != schema.version
        or stored_settings.get("output_schema_sha256") != _json_sha256(schema.definition)
        or stored_settings.get("prompt_sha256") != _text_sha256(settings.prompt)
    ):
        return False
    resolved = stored_settings.get("resolved_fields")
    null_fields = stored_settings.get("null_fields")
    if not isinstance(resolved, list) or not all(isinstance(name, str) for name in resolved):
        return False
    if not isinstance(null_fields, list) or not all(
        isinstance(name, str) for name in null_fields
    ):
        return False
    if resolved != sorted(set(resolved)) or null_fields != sorted(set(null_fields)):
        return False
    if set(resolved).isdisjoint(null_fields) is False:
        return False
    if set(resolved) | set(null_fields) != INFERENCE_KEYS:
        return False
    inferences = record.get("inferences", {})
    if not isinstance(inferences, dict) or set(inferences) != set(resolved):
        return False
    return all(
        isinstance(claim, dict) and claim.get("run_id") == run_id
        for claim in inferences.values()
    )


def _remove_derivative(path: Path) -> None:
    if os.environ.get("WEDDING_FILM_TEST_FAIL_DERIVATIVE_CLEANUP") == "1":
        raise CatalogProblem(
            "VISION_INPUT_CLEANUP_FAILED", "temporary Analysis Input could not be deleted"
        )
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        raise CatalogProblem(
            "VISION_INPUT_CLEANUP_FAILED", "temporary Analysis Input could not be deleted"
        ) from error


def _append_run(path: Path, events: list[dict[str, object]]) -> None:
    try:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            for event in events:
                _write_event(stream, event)
            _sync_stream(stream)
    except OSError as error:
        raise CatalogProblem("VISION_IO_ERROR", "Analysis Run could not be appended") from error


def _response_context(
    usage: dict[str, int] | None, metadata: dict[str, object] | None
) -> tuple[dict[str, int], dict[str, object]]:
    normalized_usage = usage or {}
    if not all(
        isinstance(key, str)
        and type(value) is int
        and value >= 0
        for key, value in normalized_usage.items()
    ):
        raise CatalogProblem("VISION_ADAPTER_FAILURE", "adapter usage is invalid")
    normalized_metadata = metadata or {}
    if not all(isinstance(key, str) for key in normalized_metadata):
        raise CatalogProblem("VISION_ADAPTER_FAILURE", "adapter metadata is invalid")
    try:
        encoded = json.dumps(normalized_metadata, allow_nan=False, separators=(",", ":"))
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise CatalogProblem("VISION_ADAPTER_FAILURE", "adapter metadata is invalid") from error
    if not isinstance(decoded, dict):
        raise CatalogProblem("VISION_ADAPTER_FAILURE", "adapter metadata is invalid")
    return normalized_usage, cast(dict[str, object], decoded)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise CatalogProblem(
            "VISION_IO_ERROR", "Analysis Run directory could not be synced"
        ) from error


def analyze_asset(workspace: Path, asset_id: str) -> AnalysisResult:
    records = load_catalog(workspace)
    try:
        config = load_project_config(workspace)
    except ConfigProblem as problem:
        raise CatalogProblem(problem.code, problem.message) from problem
    record = next((item for item in records if item["asset_id"] == asset_id), None)
    if record is None:
        raise CatalogProblem("VISION_ASSET_NOT_FOUND", "asset_id is absent from the Catalog")
    if config.vision.name == "none":
        raise CatalogProblem("VISION_ADAPTER_DISABLED", "vision adapter is not configured")
    try:
        adapter = adapter_for(config.vision.name)
    except ValueError as error:
        raise CatalogProblem("VISION_ADAPTER_UNAVAILABLE", str(error)) from error

    run_directory = workspace / "runs" / "analysis"
    candidate_directory = workspace / ".work" / "candidates"
    if run_directory.is_symlink() or not run_directory.is_dir():
        raise CatalogProblem("ANALYSIS_RUN_INVALID_ARTIFACT", "Analysis Run directory is unsafe")
    if candidate_directory.is_symlink() or not candidate_directory.is_dir():
        raise CatalogProblem("VISION_INPUT_INVALID_ARTIFACT", "candidate directory is unsafe")
    command_id = f"vision-{uuid.uuid4().hex}"
    run_path = run_directory / f"{command_id}.jsonl"
    derivative_path: Path | None = None
    derivative: AnalysisDerivative | None = None
    run_temp: Path | None = None
    response_usage: dict[str, int] = {}
    provider_metadata: dict[str, object] = {}
    failure_retryable = False
    failure_category = "local_input"
    adapter_attempted = False
    attempt_started_at = _now()
    try:
        run_descriptor, run_temp_name = tempfile.mkstemp(
            prefix=f".{run_path.name}.", suffix=".candidate", dir=run_directory
        )
        run_temp = Path(run_temp_name)
        with os.fdopen(run_descriptor, "w", encoding="utf-8", newline="\n") as stream:
            _write_event(
                stream,
                {
                    "type": "command",
                    "command_id": command_id,
                    "command": "catalog analyze",
                    "started_at": _now(),
                },
            )
            # The command journal must reach stable storage before its final name is visible.
            _sync_stream(stream)
        os.replace(run_temp, run_path)
        _fsync_directory(run_directory)
        run_temp = None
        attempt_started_at = _now()
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".analysis-input-", suffix=".jpg", dir=candidate_directory
            )
            os.close(descriptor)
            derivative_path = Path(temporary_name)
        except OSError as error:
            raise CatalogProblem(
                "VISION_INPUT_IO_ERROR", "temporary Analysis Input could not be created"
            ) from error
        derivative = _make_derivative(workspace / record["locators"][0], derivative_path)
        settings = AdapterSettings(
            model=config.vision.model,
            prompt_version=config.vision.prompt_version,
            prompt=PROMPT,
            parameters={},
        )
        schema = _schema()
        fingerprint = _fingerprint(
            asset_id,
            record["byte_size"],
            derivative,
            adapter.name,
            adapter.provider,
            adapter.version,
            settings,
            schema,
        )
        run_id = f"vision:{fingerprint.removeprefix('sha256:')}"
        reusable = _is_reusable(
            record,
            run_id,
            fingerprint,
            derivative,
            adapter.name,
            adapter.provider,
            adapter.version,
            settings,
            schema,
        )
        if reusable:
            _remove_derivative(derivative_path)
            derivative_path = None
            _append_run(
                run_path,
                [
                    {
                        "type": "asset_stage",
                        "asset_id": asset_id,
                        "stage": "vision",
                        "attempt": 0,
                        "outcome": "skipped",
                        "run_id": run_id,
                        "fingerprint": fingerprint,
                        "usage": {},
                        "derivative_sha256": derivative.sha256,
                        "started_at": attempt_started_at,
                        "ended_at": _now(),
                    },
                    {
                        "type": "command_completed",
                        "outcome": "succeeded",
                        "succeeded": 0,
                        "reused": 1,
                        "failed": 0,
                        "ended_at": _now(),
                    },
                ],
            )
            return AnalysisResult(0, 1, 0)
        try:
            adapter_attempted = True
            response = adapter.analyze(derivative, schema, settings)
        except Exception as error:
            failure_retryable = True
            failure_category = "adapter_exception"
            raise CatalogProblem(
                "VISION_ADAPTER_FAILURE", "vision adapter raised an unexpected error"
            ) from error
        if not isinstance(response, AdapterSuccess | AdapterFailure) or (
            isinstance(response, AdapterSuccess) and response.outcome != "success"
        ) or (isinstance(response, AdapterFailure) and response.outcome != "failure"):
            failure_category = "invalid_response"
            raise CatalogProblem(
                "VISION_ADAPTER_FAILURE", "adapter returned an invalid result"
            )
        response_usage, provider_metadata = _response_context(
            response.usage, response.provider_metadata
        )
        if response.adapter_version != adapter.version:
            failure_category = "invalid_response"
            raise CatalogProblem(
                "VISION_ADAPTER_FAILURE", "adapter returned a mismatched implementation version"
            )
        if isinstance(response, AdapterFailure):
            failure_retryable = response.retryable
            failure_category = response.category
            code = (
                "VISION_ADAPTER_REFUSAL"
                if response.category == "refusal"
                else "VISION_ADAPTER_FAILURE"
            )
            raise CatalogProblem(code, response.message)
        failure_category = "invalid_candidate"
        claims = _candidate(response.candidate, run_id)
        previous_runs = cast(dict[str, object], record.get("runs", {}))
        runs = {
            prior_id: prior
            for prior_id, prior in previous_runs.items()
            if not isinstance(prior, dict) or prior.get("kind") != "vision"
        }
        runs[run_id] = {
            "kind": "vision",
            "adapter": adapter.name,
            "provider": adapter.provider,
            "version": adapter.version,
            "model": settings.model,
            "prompt_version": settings.prompt_version,
            "settings": {
                "parameters": settings.parameters,
                "adapter_version": adapter.version,
                "analysis_input": {
                    "sha256": derivative.sha256,
                    "pixel_width": derivative.pixel_width,
                    "pixel_height": derivative.pixel_height,
                    "media_type": derivative.media_type,
                    "recipe_version": derivative.recipe_version,
                },
                "output_schema_version": schema.version,
                "output_schema_sha256": _json_sha256(schema.definition),
                "prompt_sha256": _text_sha256(settings.prompt),
                "resolved_fields": sorted(claims),
                "null_fields": sorted(INFERENCE_KEYS - set(claims)),
            },
            "fingerprint": fingerprint,
            "outcome": "success",
            "executed_at": _now(),
        }
        candidate_record = dict(record)
        candidate_record["runs"] = runs
        candidate_record["inferences"] = claims
        candidate_records = [
            candidate_record if item["asset_id"] == asset_id else item for item in records
        ]
        _remove_derivative(derivative_path)
        derivative_path = None
        _append_run(
            run_path,
            [
                {
                    "type": "catalog_checkpoint",
                    "asset_id": asset_id,
                    "stage": "vision",
                    "outcome": "prepared",
                    "run_id": run_id,
                    "fingerprint": fingerprint,
                    "at": _now(),
                }
            ],
        )
        try:
            checkpoint_catalog(workspace, candidate_records)
        except CatalogProblem as problem:
            raise CatalogProblem(
                "VISION_CANDIDATE_INVALID", "candidate failed Inference validation"
            ) from problem
        _append_run(
            run_path,
            [
                {
                    "type": "asset_stage",
                    "asset_id": asset_id,
                    "stage": "vision",
                    "attempt": 1,
                    "outcome": "succeeded",
                    "run_id": run_id,
                    "fingerprint": fingerprint,
                    "usage": response_usage,
                    "provider_metadata": provider_metadata,
                    "derivative_sha256": derivative.sha256,
                    "started_at": attempt_started_at,
                    "ended_at": _now(),
                },
                {
                    "type": "command_completed",
                    "outcome": "succeeded",
                    "succeeded": 1,
                    "reused": 0,
                    "failed": 0,
                    "ended_at": _now(),
                },
            ],
        )
        return AnalysisResult(1, 0, 0)
    except (KeyboardInterrupt, SystemExit):
        cleanup_error: str | None = None
        if derivative_path is not None:
            try:
                _remove_derivative(derivative_path)
                derivative_path = None
            except CatalogProblem as problem:
                cleanup_error = problem.code
        if run_path.is_file():
            interrupted: dict[str, object] = {
                "type": "asset_stage",
                "asset_id": asset_id,
                "stage": "vision",
                "attempt": 1 if adapter_attempted else 0,
                "outcome": "interrupted",
                "error_code": "VISION_INTERRUPTED",
                "retryable": True,
                "usage": response_usage,
                "provider_metadata": provider_metadata,
                "started_at": attempt_started_at,
                "ended_at": _now(),
            }
            if derivative is not None:
                interrupted["derivative_sha256"] = derivative.sha256
            if cleanup_error is not None:
                interrupted["cleanup_error_code"] = cleanup_error
            with suppress(CatalogProblem):
                _append_run(
                    run_path,
                    [
                        interrupted,
                        {
                            "type": "command_completed",
                            "outcome": "interrupted",
                            "succeeded": 0,
                            "reused": 0,
                            "failed": 1,
                            "ended_at": _now(),
                        },
                    ],
                )
        raise
    except CatalogProblem as problem:
        if problem.code == "VISION_INPUT_CLEANUP_FAILED":
            failure_category = "cleanup"
        if derivative_path is not None and problem.code != "VISION_INPUT_CLEANUP_FAILED":
            try:
                _remove_derivative(derivative_path)
                derivative_path = None
            except CatalogProblem as cleanup_problem:
                problem = cleanup_problem
                failure_retryable = False
                failure_category = "cleanup"
        if run_path.is_file():
            failure: dict[str, object] = {
                "type": "asset_stage",
                "asset_id": asset_id,
                "stage": "vision",
                "attempt": 1 if adapter_attempted else 0,
                "outcome": "transient_failure" if failure_retryable else "permanent_failure",
                "failure_category": failure_category,
                "error_code": problem.code,
                "retryable": failure_retryable,
                "usage": response_usage,
                "provider_metadata": provider_metadata,
                "started_at": attempt_started_at,
                "ended_at": _now(),
            }
            if derivative is not None:
                failure["derivative_sha256"] = derivative.sha256
            _append_run(
                run_path,
                [
                    failure,
                    {
                        "type": "command_completed",
                        "outcome": "partial_failure",
                        "succeeded": 0,
                        "reused": 0,
                        "failed": 1,
                        "ended_at": _now(),
                    },
                ],
            )
        raise problem
    except OSError as error:
        raise CatalogProblem(
            "VISION_IO_ERROR", "vision analysis artifact could not be written"
        ) from error
    finally:
        if derivative_path is not None:
            active_exception = sys.exc_info()[0] is not None
            try:
                _remove_derivative(derivative_path)
                derivative_path = None
            except CatalogProblem:
                if not active_exception:
                    raise
        if run_temp is not None:
            with suppress(OSError):
                run_temp.unlink(missing_ok=True)


def _batch_limits(
    config: ProjectConfig,
    max_assets: int | None,
    max_estimated_usd: float | None,
    concurrency: int | None,
) -> tuple[int, float, int]:
    defaults = config.analysis_defaults
    resolved_max_assets = defaults.max_assets if max_assets is None else max_assets
    resolved_budget = defaults.max_estimated_usd if max_estimated_usd is None else max_estimated_usd
    resolved_concurrency = defaults.concurrency if concurrency is None else concurrency
    if resolved_max_assets <= 0:
        raise CatalogProblem("VISION_BATCH_INVALID_LIMIT", "max assets must be a positive integer")
    if not math.isfinite(resolved_budget) or resolved_budget <= 0:
        raise CatalogProblem(
            "VISION_BATCH_INVALID_LIMIT", "max estimated cost must be a positive number"
        )
    if resolved_concurrency <= 0:
        raise CatalogProblem("VISION_BATCH_INVALID_LIMIT", "concurrency must be a positive integer")
    return resolved_max_assets, resolved_budget, resolved_concurrency


def select_batch(
    workspace: Path,
    asset_ids: Sequence[str] | None = None,
    *,
    max_assets: int | None = None,
    max_estimated_usd: float | None = None,
    concurrency: int | None = None,
) -> BatchPlan:
    try:
        config = load_project_config(workspace)
    except ConfigProblem as problem:
        raise CatalogProblem(problem.code, problem.message) from problem
    resolved_max_assets, resolved_budget, resolved_concurrency = _batch_limits(
        config, max_assets, max_estimated_usd, concurrency
    )
    records = load_catalog(workspace)
    by_id: dict[str, dict[str, Any]] = {record["asset_id"]: record for record in records}
    selected: list[str]
    if asset_ids:
        unique_ids = sorted(set(asset_ids))
        missing = [asset_id for asset_id in unique_ids if asset_id not in by_id]
        if missing:
            raise CatalogProblem(
                "VISION_ASSET_NOT_FOUND", f"asset_id {missing[0]} is absent from the Catalog"
            )
        selected = unique_ids[:resolved_max_assets]
    else:
        selected = sorted(
            asset_id for asset_id, record in by_id.items() if not record.get("inferences")
        )[:resolved_max_assets]
    estimated_cost = round(len(selected) * ESTIMATED_COST_PER_ASSET_USD, 2)
    return BatchPlan(
        asset_ids=tuple(selected),
        target_count=len(selected),
        estimated_cost_usd=estimated_cost,
        max_assets=resolved_max_assets,
        max_estimated_usd=resolved_budget,
        concurrency=resolved_concurrency,
    )


def _worker_attempt(
    workspace: Path,
    record: dict[str, Any],
    adapter: VisionAdapter,
    settings: AdapterSettings,
    schema: OutputSchema,
    candidate_directory: Path,
) -> _Attempt:
    derivative_path: Path | None = None
    derivative: AnalysisDerivative | None = None
    try:
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".analysis-input-", suffix=".jpg", dir=candidate_directory
            )
            os.close(descriptor)
            derivative_path = Path(temporary_name)
        except OSError as error:
            raise CatalogProblem(
                "VISION_INPUT_IO_ERROR", "temporary Analysis Input could not be created"
            ) from error
        derivative = _make_derivative(workspace / record["locators"][0], derivative_path)
        fingerprint = _fingerprint(
            record["asset_id"],
            record["byte_size"],
            derivative,
            adapter.name,
            adapter.provider,
            adapter.version,
            settings,
            schema,
        )
        run_id = f"vision:{fingerprint.removeprefix('sha256:')}"
        if _is_reusable(
            record,
            run_id,
            fingerprint,
            derivative,
            adapter.name,
            adapter.provider,
            adapter.version,
            settings,
            schema,
        ):
            return _Attempt(
                outcome="reused", run_id=run_id, fingerprint=fingerprint, derivative=derivative
            )
        response = adapter.analyze(derivative, schema, settings)
        usage, provider_metadata = _response_context(response.usage, response.provider_metadata)
        if response.adapter_version != adapter.version:
            return _Attempt(
                outcome="permanent_failure",
                error_code="VISION_ADAPTER_FAILURE",
                message="adapter returned a mismatched implementation version",
                usage=usage,
                provider_metadata=provider_metadata,
                derivative=derivative,
            )
        if isinstance(response, AdapterFailure):
            code = (
                "VISION_ADAPTER_REFUSAL"
                if response.category == "refusal"
                else "VISION_ADAPTER_FAILURE"
            )
            outcome: Literal["transient_failure", "permanent_failure"] = (
                "transient_failure" if response.retryable else "permanent_failure"
            )
            return _Attempt(
                outcome=outcome,
                retryable=response.retryable,
                error_code=code,
                message=response.message,
                usage=usage,
                provider_metadata=provider_metadata,
                derivative=derivative,
            )
        claims = _candidate(response.candidate, run_id)
        return _Attempt(
            outcome="success",
            claims=claims,
            run_id=run_id,
            fingerprint=fingerprint,
            usage=usage,
            provider_metadata=provider_metadata,
            derivative=derivative,
        )
    except CatalogProblem as problem:
        return _Attempt(
            outcome="permanent_failure",
            error_code=problem.code,
            message=problem.message,
            derivative=derivative,
        )
    except Exception as error:
        return _Attempt(
            outcome="transient_failure",
            retryable=True,
            error_code="VISION_ADAPTER_FAILURE",
            message=str(error),
            derivative=derivative,
        )
    finally:
        if derivative_path is not None:
            with suppress(CatalogProblem):
                _remove_derivative(derivative_path)


def run_batch(
    workspace: Path,
    asset_ids: Sequence[str] | None = None,
    *,
    max_assets: int | None = None,
    max_estimated_usd: float | None = None,
    concurrency: int | None = None,
) -> BatchResult:
    try:
        config = load_project_config(workspace)
    except ConfigProblem as problem:
        raise CatalogProblem(problem.code, problem.message) from problem
    if config.vision.name == "none":
        raise CatalogProblem("VISION_ADAPTER_DISABLED", "vision adapter is not configured")
    try:
        adapter = adapter_for(config.vision.name)
    except ValueError as error:
        raise CatalogProblem("VISION_ADAPTER_UNAVAILABLE", str(error)) from error

    plan = select_batch(
        workspace,
        asset_ids,
        max_assets=max_assets,
        max_estimated_usd=max_estimated_usd,
        concurrency=concurrency,
    )

    run_directory = workspace / "runs" / "analysis"
    candidate_directory = workspace / ".work" / "candidates"
    if run_directory.is_symlink() or not run_directory.is_dir():
        raise CatalogProblem("ANALYSIS_RUN_INVALID_ARTIFACT", "Analysis Run directory is unsafe")
    if candidate_directory.is_symlink() or not candidate_directory.is_dir():
        raise CatalogProblem("VISION_INPUT_INVALID_ARTIFACT", "candidate directory is unsafe")

    command_id = f"vision-batch-{uuid.uuid4().hex}"
    run_path = run_directory / f"{command_id}.jsonl"
    run_descriptor, run_temp_name = tempfile.mkstemp(
        prefix=f".{run_path.name}.", suffix=".candidate", dir=run_directory
    )
    run_temp: Path | None = None
    run_temp = Path(run_temp_name)
    with os.fdopen(run_descriptor, "w", encoding="utf-8", newline="\n") as stream:
        _write_event(
            stream,
            {
                "type": "command",
                "command_id": command_id,
                "command": "catalog analyze-batch",
                "started_at": _now(),
                "planned_assets": list(plan.asset_ids),
                "estimated_cost_usd": plan.estimated_cost_usd,
                "cost_estimate_version": COST_ESTIMATE_VERSION,
            },
        )
        _sync_stream(stream)
    os.replace(run_temp, run_path)
    _fsync_directory(run_directory)
    run_temp = None

    settings = AdapterSettings(
        model=config.vision.model,
        prompt_version=config.vision.prompt_version,
        prompt=PROMPT,
        parameters={},
    )
    schema = _schema()

    records = load_catalog(workspace)
    by_id: dict[str, dict[str, Any]] = {record["asset_id"]: record for record in records}

    succeeded = reused = failed = 0
    reserved = 0.0
    budget_stopped = False
    interrupted = False
    pending = iter(plan.asset_ids)

    def try_submit(executor: ThreadPoolExecutor) -> tuple[str, Future[_Attempt]] | None:
        nonlocal reserved, budget_stopped
        try:
            asset_id = next(pending)
        except StopIteration:
            return None
        if reserved + ESTIMATED_COST_PER_ASSET_USD > plan.max_estimated_usd:
            budget_stopped = True
            return None
        reserved += ESTIMATED_COST_PER_ASSET_USD
        future = executor.submit(
            _worker_attempt,
            workspace,
            by_id[asset_id],
            adapter,
            settings,
            schema,
            candidate_directory,
        )
        return asset_id, future

    with ThreadPoolExecutor(max_workers=plan.concurrency) as executor:
        in_flight: dict[Future[_Attempt], str] = {}
        for _ in range(plan.concurrency):
            submission = try_submit(executor)
            if submission is None:
                break
            asset_id, future = submission
            in_flight[future] = asset_id

        while in_flight:
            done = next(iter(as_completed(list(in_flight))))
            asset_id = in_flight.pop(done)
            try:
                attempt = done.result()
                started = time.monotonic()
                attempt_number = 1
                while attempt.outcome == "transient_failure" and attempt_number < MAX_ATTEMPTS:
                    if time.monotonic() - started >= PER_ASSET_ELAPSED_LIMIT_SECONDS:
                        break
                    delay = min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * (2**attempt_number))
                    if attempt.provider_metadata:
                        raw_retry_after = attempt.provider_metadata.get("retry_after_seconds", 0)
                        retry_after = (
                            float(raw_retry_after)
                            if isinstance(raw_retry_after, int | float)
                            else 0.0
                        )
                        delay = max(delay, min(retry_after, RETRY_CAP_SECONDS))
                    time.sleep(delay + random.uniform(0, delay * 0.1))
                    attempt_number += 1
                    attempt = _worker_attempt(
                        workspace, by_id[asset_id], adapter, settings, schema, candidate_directory
                    )
            except KeyboardInterrupt:
                interrupted = True
                _append_run(
                    run_path,
                    [
                        {
                            "type": "asset_stage",
                            "asset_id": asset_id,
                            "stage": "vision",
                            "outcome": "interrupted",
                            "error_code": "VISION_INTERRUPTED",
                            "retryable": True,
                            "at": _now(),
                        }
                    ],
                )
                continue

            if attempt.outcome == "reused":
                reserved -= ESTIMATED_COST_PER_ASSET_USD
                reused += 1
                _append_run(
                    run_path,
                    [
                        {
                            "type": "asset_stage",
                            "asset_id": asset_id,
                            "stage": "vision",
                            "attempt": attempt_number,
                            "outcome": "skipped",
                            "run_id": attempt.run_id,
                            "fingerprint": attempt.fingerprint,
                            "ended_at": _now(),
                        }
                    ],
                )
            elif attempt.outcome == "success":
                assert attempt.claims is not None
                assert attempt.run_id is not None
                assert attempt.derivative is not None
                record = by_id[asset_id]
                previous_runs = cast(dict[str, Any], record.get("runs", {}))
                runs = {
                    prior_id: prior
                    for prior_id, prior in previous_runs.items()
                    if not isinstance(prior, dict) or prior.get("kind") != "vision"
                }
                runs[attempt.run_id] = {
                    "kind": "vision",
                    "adapter": adapter.name,
                    "provider": adapter.provider,
                    "version": adapter.version,
                    "model": settings.model,
                    "prompt_version": settings.prompt_version,
                    "settings": {
                        "parameters": settings.parameters,
                        "adapter_version": adapter.version,
                        "analysis_input": {
                            "sha256": attempt.derivative.sha256,
                            "pixel_width": attempt.derivative.pixel_width,
                            "pixel_height": attempt.derivative.pixel_height,
                            "media_type": attempt.derivative.media_type,
                            "recipe_version": attempt.derivative.recipe_version,
                        },
                        "output_schema_version": schema.version,
                        "output_schema_sha256": _json_sha256(schema.definition),
                        "prompt_sha256": _text_sha256(settings.prompt),
                        "resolved_fields": sorted(attempt.claims),
                        "null_fields": sorted(INFERENCE_KEYS - set(attempt.claims)),
                    },
                    "fingerprint": attempt.fingerprint,
                    "outcome": "success",
                    "executed_at": _now(),
                }
                updated_record = dict(record)
                updated_record["runs"] = runs
                updated_record["inferences"] = attempt.claims
                by_id[asset_id] = updated_record
                checkpoint_catalog(workspace, list(by_id.values()))
                succeeded += 1
                _append_run(
                    run_path,
                    [
                        {
                            "type": "asset_stage",
                            "asset_id": asset_id,
                            "stage": "vision",
                            "attempt": attempt_number,
                            "outcome": "succeeded",
                            "run_id": attempt.run_id,
                            "fingerprint": attempt.fingerprint,
                            "usage": attempt.usage or {},
                            "derivative_sha256": attempt.derivative.sha256,
                            "ended_at": _now(),
                        }
                    ],
                )
            else:
                failed += 1
                _append_run(
                    run_path,
                    [
                        {
                            "type": "asset_stage",
                            "asset_id": asset_id,
                            "stage": "vision",
                            "attempt": attempt_number,
                            "outcome": "permanent_failure",
                            "error_code": attempt.error_code,
                            "retryable": attempt.retryable,
                            "ended_at": _now(),
                        }
                    ],
                )

            submission = try_submit(executor)
            if submission is not None:
                new_asset_id, new_future = submission
                in_flight[new_future] = new_asset_id

    _append_run(
        run_path,
        [
            {
                "type": "command_completed",
                "outcome": (
                    "interrupted"
                    if interrupted
                    else "partial_failure"
                    if (failed or budget_stopped)
                    else "succeeded"
                ),
                "succeeded": succeeded,
                "reused": reused,
                "failed": failed,
                "budget_stopped": budget_stopped,
                "estimated_cost_used_usd": round(reserved, 2),
                "ended_at": _now(),
            }
        ],
    )
    if interrupted:
        raise KeyboardInterrupt
    return BatchResult(
        succeeded=succeeded, reused=reused, failed=failed, budget_stopped=budget_stopped
    )
