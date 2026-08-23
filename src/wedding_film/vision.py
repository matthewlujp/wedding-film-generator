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
    AdapterAsset,
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
    definition: dict[str, object] = {
        "description": {"type": "string", "nullable": True},
        "wedding_moment": {
            "enum": ["preparation", "ceremony", "portraits", "reception", "other"],
            "nullable": True,
        },
        "subject_roles": {"type": "sorted_unique_string_array", "nullable": True},
        "setting": {"type": "string", "nullable": True},
        "mood": {"type": "sorted_unique_string_array", "nullable": True},
        "shot_type": {"enum": ["wide", "medium", "close-up", "detail"], "nullable": True},
        "quality_flags": {"type": "sorted_unique_string_array", "nullable": True},
        "confidence": {"type": "finite_number", "minimum": 0, "maximum": 1},
    }
    return OutputSchema(
        version=OUTPUT_SCHEMA_VERSION,
        fields=tuple(sorted(INFERENCE_KEYS)),
        definition=definition,
    )


def _fingerprint(
    asset: AdapterAsset,
    derivative: AnalysisDerivative,
    adapter_name: str,
    adapter_provider: str,
    settings: AdapterSettings,
    schema: OutputSchema,
) -> str:
    contract = {
        "original_asset": {"asset_id": asset.asset_id, "byte_size": asset.byte_size},
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
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".analysis-input-", suffix=".jpg", dir=candidate_directory
        )
        os.close(descriptor)
        derivative_path = Path(temporary_name)
        derivative = _make_derivative(workspace / record["locators"][0], derivative_path)
        asset = AdapterAsset(asset_id=asset_id, byte_size=record["byte_size"])
        settings = AdapterSettings(
            model=config.vision.model,
            prompt_version=config.vision.prompt_version,
            prompt=PROMPT,
            parameters={},
        )
        schema = _schema()
        fingerprint = _fingerprint(
            asset, derivative, adapter.name, adapter.provider, settings, schema
        )
        run_id = f"vision:{fingerprint.removeprefix('sha256:')}"
        reusable = _is_reusable(record, run_id)
        run_descriptor, run_temp_name = tempfile.mkstemp(
            prefix=f".{run_path.name}.", suffix=".candidate", dir=run_directory
        )
        run_temp = Path(run_temp_name)
        with os.fdopen(run_descriptor, "w", encoding="utf-8", newline="\n") as stream:
            _write_event(stream, {"type": "command", "command_id": command_id,
                                  "command": "catalog analyze", "started_at": _now()})
            if reusable:
                _write_event(stream, {"type": "asset_stage", "asset_id": asset_id,
                                      "stage": "vision", "attempt": 1,
                                      "outcome": "skipped", "run_id": run_id,
                                      "fingerprint": fingerprint, "usage": {},
                                      "derivative_sha256": derivative.sha256,
                                      "started_at": _now(), "ended_at": _now()})
                _write_event(stream, {"type": "command_completed", "outcome": "succeeded",
                                      "succeeded": 0, "reused": 1, "failed": 0, "ended_at": _now()})
                os.replace(run_temp, run_path)
                run_temp = None
                return AnalysisResult(0, 1, 0)
            response = adapter.analyze(asset, derivative, schema, settings)
            response_usage = response.usage
            if response.refusal is not None or response.candidate is None:
                raise CatalogProblem("VISION_ADAPTER_REFUSAL", "adapter refused the request")
            claims = _candidate(response.candidate, run_id)
            runs = dict(cast(dict[str, object], record.get("runs", {})))
            runs[run_id] = {
                "kind": "vision",
                "adapter": adapter.name,
                "provider": response.provider,
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
                candidate_record if item["asset_id"] == asset_id else item
                for item in records
            ]
            try:
                checkpoint_catalog(workspace, candidate_records)
            except CatalogProblem as problem:
                raise CatalogProblem(
                    "VISION_CANDIDATE_INVALID", "candidate failed Inference validation"
                ) from problem
            _write_event(stream, {"type": "asset_stage", "asset_id": asset_id,
                                  "stage": "vision", "attempt": 1,
                                  "outcome": "succeeded", "run_id": run_id,
                                  "fingerprint": fingerprint, "usage": response.usage,
                                  "derivative_sha256": derivative.sha256,
                                  "started_at": _now(), "ended_at": _now()})
            _write_event(stream, {"type": "command_completed", "outcome": "succeeded",
                                  "succeeded": 1, "reused": 0, "failed": 0, "ended_at": _now()})
        os.replace(run_temp, run_path)
        run_temp = None
        return AnalysisResult(1, 0, 0)
    except CatalogProblem as problem:
        if run_temp is not None and run_temp.is_file():
            try:
                with run_temp.open("a", encoding="utf-8", newline="\n") as stream:
                    failure: dict[str, object] = {
                        "type": "asset_stage",
                        "asset_id": asset_id,
                        "stage": "vision",
                        "attempt": 1,
                        "outcome": "permanent_failure",
                        "error_code": problem.code,
                        "retryable": False,
                        "usage": response_usage,
                        "started_at": _now(),
                        "ended_at": _now(),
                    }
                    if derivative is not None:
                        failure["derivative_sha256"] = derivative.sha256
                    _write_event(stream, failure)
                    _write_event(
                        stream,
                        {
                            "type": "command_completed",
                            "outcome": "partial_failure",
                            "succeeded": 0,
                            "reused": 0,
                            "failed": 1,
                            "ended_at": _now(),
                        },
                    )
                os.replace(run_temp, run_path)
                run_temp = None
            except OSError as error:
                raise CatalogProblem(
                    "VISION_IO_ERROR", "vision failure could not be recorded"
                ) from error
        raise
    except OSError as error:
        raise CatalogProblem(
            "VISION_IO_ERROR", "vision analysis artifact could not be written"
        ) from error
    finally:
        if derivative_path is not None:
            with suppress(OSError):
                derivative_path.unlink(missing_ok=True)
        if run_temp is not None:
            with suppress(OSError):
                run_temp.unlink(missing_ok=True)
