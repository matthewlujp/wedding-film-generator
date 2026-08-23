from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

from wedding_film.catalog import (
    INFERENCE_KEYS,
    CatalogProblem,
    checkpoint_catalog,
    load_catalog,
)
from wedding_film.config import ConfigProblem, load_project_config
from wedding_film.vision_adapter import (
    AdapterFailure,
    AdapterSettings,
    AnalysisDerivative,
    OutputSchema,
    adapter_for,
)

RECIPE_VERSION = "analysis-input-jpeg-v1"
OUTPUT_SCHEMA_VERSION = "vision-inference-v1"
PROMPT = (
    "Describe only visible facts, wedding moment, generic subject roles, setting, mood, "
    "shot type, and quality flags. Do not identify people or infer sensitive attributes."
)
MAX_EDGE = 2048


@dataclass(frozen=True)
class AnalysisResult:
    succeeded: int
    reused: int
    failed: int


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_event(stream: Any, event: dict[str, object]) -> None:
    stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
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
        "adapter": {"name": adapter_name, "provider": adapter_provider},
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


def _is_reusable(record: dict[str, Any], run_id: str) -> bool:
    run = record.get("runs", {}).get(run_id)
    if not isinstance(run, dict) or run.get("outcome") != "success":
        return False
    settings = run.get("settings")
    if not isinstance(settings, dict):
        return False
    resolved = settings.get("resolved_fields")
    if not isinstance(resolved, list) or not all(isinstance(name, str) for name in resolved):
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


def _append_and_publish_run(
    temporary: Path, destination: Path, events: list[dict[str, object]]
) -> None:
    try:
        with temporary.open("a", encoding="utf-8", newline="\n") as stream:
            for event in events:
                _write_event(stream, event)
        os.replace(temporary, destination)
    except OSError as error:
        raise CatalogProblem("VISION_IO_ERROR", "Analysis Run could not be finalized") from error


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
            settings,
            schema,
        )
        run_id = f"vision:{fingerprint.removeprefix('sha256:')}"
        reusable = _is_reusable(record, run_id)
        if reusable:
            _remove_derivative(derivative_path)
            derivative_path = None
            _append_and_publish_run(
                run_temp,
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
                        "started_at": _now(),
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
            run_temp = None
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
        response_usage = response.usage or {}
        provider_metadata = response.provider_metadata or {}
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
            "model": settings.model,
            "prompt_version": settings.prompt_version,
            "settings": {
                "parameters": settings.parameters,
                "analysis_input": {
                    "sha256": derivative.sha256,
                    "pixel_width": derivative.pixel_width,
                    "pixel_height": derivative.pixel_height,
                    "media_type": derivative.media_type,
                    "recipe_version": derivative.recipe_version,
                },
                "output_schema_version": schema.version,
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
        try:
            checkpoint_catalog(workspace, candidate_records)
        except CatalogProblem as problem:
            raise CatalogProblem(
                "VISION_CANDIDATE_INVALID", "candidate failed Inference validation"
            ) from problem
        _append_and_publish_run(
            run_temp,
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
                    "started_at": _now(),
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
        run_temp = None
        return AnalysisResult(1, 0, 0)
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
        if run_temp is not None and run_temp.is_file():
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
                "started_at": _now(),
                "ended_at": _now(),
            }
            if derivative is not None:
                failure["derivative_sha256"] = derivative.sha256
            _append_and_publish_run(
                run_temp,
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
            run_temp = None
        raise problem
    except OSError as error:
        raise CatalogProblem(
            "VISION_IO_ERROR", "vision analysis artifact could not be written"
        ) from error
    finally:
        if run_temp is not None:
            with suppress(OSError):
                run_temp.unlink(missing_ok=True)
