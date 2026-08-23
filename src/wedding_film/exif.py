from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import tempfile
import unicodedata
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import TextIO

from PIL import Image, UnidentifiedImageError
from PIL import __version__ as pillow_version

from wedding_film.catalog import CatalogProblem, JsonObject, checkpoint_catalog, load_catalog

EXTRACTOR_NAME = "pillow-exif"
EXTRACTOR_VERSION = f"1 ({pillow_version})"
TOOL_VERSION = version("wedding-film")
DATETIME_PATTERN = re.compile(r"\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}\Z")
OFFSET_PATTERN = re.compile(r"[+-]\d{2}:\d{2}\Z")

TAG_NAMES = {
    271: "Make",
    272: "Model",
    274: "Orientation",
    306: "DateTime",
    36867: "DateTimeOriginal",
    36868: "DateTimeDigitized",
    36880: "OffsetTime",
    36881: "OffsetTimeOriginal",
    36882: "OffsetTimeDigitized",
}


@dataclass(frozen=True)
class ExtractionResult:
    succeeded: int
    reused: int
    failed: int


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _write_event(stream: TextIO, event: JsonObject) -> None:
    stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def _run_id(asset_id: str) -> str:
    fingerprint = hashlib.sha256(
        f"{asset_id}\0{EXTRACTOR_NAME}\0{EXTRACTOR_VERSION}".encode()
    ).hexdigest()[:24]
    return f"exif:v1:{fingerprint}"


def _warning(field: str, tags: list[str], message: str) -> JsonObject:
    return {
        "code": "exif_field_invalid",
        "field": field,
        "tags": tags,
        "message": message,
    }


def _safe_tag(exif: Image.Exif, tag: int) -> tuple[object | None, bool]:
    try:
        if tag not in exif:
            return None, False
        return exif.get(tag), True
    except (KeyError, OSError, TypeError, ValueError, SyntaxError):
        return None, True


def _json_value(value: object) -> object:
    if isinstance(value, bytes):
        return value.decode("ascii", errors="replace")
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return repr(value)


def _normalized_string(value: object) -> str | None:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFC", value.strip())
    return normalized or None


def _capture_time(
    exif: Image.Exif, source_tags: JsonObject, warnings: list[JsonObject]
) -> str | None:
    choices = (
        (36867, 36881, "DateTimeOriginal", "OffsetTimeOriginal"),
        (36868, 36882, "DateTimeDigitized", "OffsetTimeDigitized"),
        (306, 36880, "DateTime", "OffsetTime"),
    )
    values: list[tuple[object | None, bool, object | None, bool, str, str]] = []
    for date_tag, offset_tag, date_name, offset_name in choices:
        date_value, date_present = _safe_tag(exif, date_tag)
        offset_value, offset_present = _safe_tag(exif, offset_tag)
        if date_present:
            source_tags[date_name] = _json_value(date_value)
        if offset_present:
            source_tags[offset_name] = _json_value(offset_value)
        values.append(
            (date_value, date_present, offset_value, offset_present, date_name, offset_name)
        )
    candidates: list[str] = []
    for date_value, date_present, offset_value, offset_present, date_name, offset_name in values:
        if not date_present:
            if offset_present:
                warnings.append(
                    _warning("capture_time", [offset_name], "offset has no corresponding datetime")
                )
            continue
        date_text = _normalized_string(date_value)
        offset_text = _normalized_string(offset_value) if offset_present else None
        if (
            date_text is None
            or offset_text is None
            or DATETIME_PATTERN.fullmatch(date_text) is None
            or OFFSET_PATTERN.fullmatch(offset_text) is None
        ):
            tags = [date_name] + ([offset_name] if offset_present else [])
            warnings.append(
                _warning(
                    "capture_time",
                    tags,
                    "capture datetime needs its valid corresponding embedded offset",
                )
            )
            continue
        candidate = f"{date_text[:10].replace(':', '-')}T{date_text[11:]}{offset_text}"
        try:
            datetime.fromisoformat(candidate)
        except ValueError:
            warnings.append(
                _warning("capture_time", [date_name, offset_name], "capture datetime is invalid")
            )
            continue
        candidates.append(candidate)
    return candidates[0] if candidates else None


def _is_reusable(record: JsonObject, run_id: str) -> bool:
    run = record.get("runs", {}).get(run_id)
    if not isinstance(run, dict) or run.get("outcome") != "success":
        return False
    observations = record.get("observations", {})
    if not isinstance(observations, dict):
        return False
    return all(
        isinstance(observations.get(field), dict) and observations[field].get("run_id") == run_id
        for field in ("media_type", "format", "pixel_width", "pixel_height")
    )


def _coordinate(value: object, reference: object, *, latitude: bool) -> float | None:
    ref = _normalized_string(reference)
    if ref is None:
        return None
    expected = {"N", "S"} if latitude else {"E", "W"}
    if ref.upper() not in expected or not isinstance(value, tuple | list) or len(value) != 3:
        return None
    try:
        degrees, minutes, seconds = (float(item) for item in value)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None
    if (
        not all(math.isfinite(item) for item in (degrees, minutes, seconds))
        or degrees < 0
        or not 0 <= minutes < 60
        or not 0 <= seconds < 60
    ):
        return None
    coordinate = degrees + minutes / 60 + seconds / 3600
    if ref.upper() in {"S", "W"}:
        coordinate = -coordinate
    limit = 90 if latitude else 180
    return coordinate if -limit <= coordinate <= limit else None


def _location(
    exif: Image.Exif, source_tags: JsonObject, warnings: list[JsonObject]
) -> JsonObject | None:
    try:
        gps = exif.get_ifd(34853)
    except (KeyError, OSError, TypeError, ValueError, SyntaxError):
        warnings.append(_warning("location", ["GPSInfo"], "GPS metadata could not be read"))
        return None
    if not gps:
        return None
    gps_names = {
        1: "GPSLatitudeRef",
        2: "GPSLatitude",
        3: "GPSLongitudeRef",
        4: "GPSLongitude",
        5: "GPSAltitudeRef",
        6: "GPSAltitude",
    }
    for tag, name in gps_names.items():
        if tag in gps:
            source_tags[name] = _json_value(gps[tag])
    latitude = _coordinate(gps.get(2), gps.get(1), latitude=True)
    longitude = _coordinate(gps.get(4), gps.get(3), latitude=False)
    if latitude is None or longitude is None:
        warnings.append(
            _warning(
                "location",
                ["GPSLatitude", "GPSLongitude"],
                "location requires a valid latitude and longitude pair",
            )
        )
        return None
    location: JsonObject = {"latitude": latitude, "longitude": longitude}
    if 6 in gps:
        try:
            altitude = float(gps[6])
            altitude_ref = gps.get(5, 0)
            if isinstance(altitude_ref, bytes):
                altitude_ref = altitude_ref[0] if altitude_ref else 0
            if not math.isfinite(altitude) or altitude_ref not in (0, 1):
                raise ValueError
            location["altitude"] = -altitude if altitude_ref == 1 else altitude
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            warnings.append(
                _warning("location.altitude", ["GPSAltitude"], "GPS altitude is invalid")
            )
    return location


def _decode(path: Path) -> tuple[JsonObject, JsonObject, list[JsonObject]]:
    observations: JsonObject | None = None
    source_tags: JsonObject = {}
    warnings: list[JsonObject] = []
    try:
        with Image.open(path) as image:
            image.load()
            image_format = image.format
            if image_format is None:
                raise UnidentifiedImageError("image format is unavailable")
            media_type = Image.MIME.get(image_format)
            if media_type is None:
                media_type = f"image/{image_format.lower()}"
            observations = {
                "format": image_format,
                "media_type": media_type,
                "pixel_height": image.height,
                "pixel_width": image.width,
            }
            try:
                exif = image.getexif()
            except (KeyError, OSError, TypeError, ValueError, SyntaxError):
                warnings.append(
                    _warning("exif", ["ExifIFD"], "embedded EXIF metadata could not be read")
                )
                return observations, source_tags, warnings
            for tag, field in ((274, "orientation"), (271, "camera_make"), (272, "camera_model")):
                value, present = _safe_tag(exif, tag)
                if not present:
                    continue
                source_tags[TAG_NAMES[tag]] = _json_value(value)
                if field == "orientation":
                    if type(value) is int and 1 <= value <= 8:
                        observations[field] = value
                    else:
                        warnings.append(
                            _warning(
                                field, [TAG_NAMES[tag]], "orientation must be from 1 through 8"
                            )
                        )
                else:
                    normalized = _normalized_string(value)
                    if normalized is None:
                        warnings.append(
                            _warning(field, [TAG_NAMES[tag]], "camera string is invalid")
                        )
                    else:
                        observations[field] = normalized
            capture_time = _capture_time(exif, source_tags, warnings)
            if capture_time is not None:
                observations["capture_time"] = capture_time
            location = _location(exif, source_tags, warnings)
            if location is not None:
                observations["location"] = location
            return observations, source_tags, warnings
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        IndexError,
        OSError,
        OverflowError,
        SyntaxError,
        TypeError,
        ValueError,
        struct.error,
    ) as error:
        if observations is not None:
            warnings.append(
                _warning("exif", ["ExifIFD"], "embedded EXIF metadata could not be read")
            )
            return observations, source_tags, warnings
        raise CatalogProblem(
            "image_decode_failed", "Original Asset could not be decoded"
        ) from error


def extract_exif(workspace: Path) -> ExtractionResult:
    records = load_catalog(workspace)
    started_at = _now()
    compact_time = started_at.translate(str.maketrans("", "", ":-.Z"))
    command_id = f"exif-{compact_time}-{uuid.uuid4().hex[:8]}"
    run_directory = workspace / "runs" / "analysis"
    if run_directory.is_symlink() or not run_directory.is_dir():
        raise CatalogProblem(
            "ANALYSIS_RUN_INVALID_ARTIFACT",
            "Analysis Run directory must be a regular workspace directory",
        )
    run_path = run_directory / f"{command_id}.jsonl"
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{run_path.name}.", suffix=".candidate", dir=run_directory
        )
    except OSError as error:
        raise CatalogProblem(
            "ANALYSIS_RUN_IO_ERROR", "Analysis Run could not be created"
        ) from error
    temporary = Path(temporary_name)
    succeeded = reused = failed = 0
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            _write_event(
                stream,
                {
                    "type": "command",
                    "command_id": command_id,
                    "command": "catalog extract",
                    "started_at": started_at,
                    "tool": "wedding-film",
                    "tool_version": TOOL_VERSION,
                    "extractor": EXTRACTOR_NAME,
                    "extractor_version": EXTRACTOR_VERSION,
                },
            )
            os.replace(temporary, run_path)
            temporary = run_path
            for record in records:
                asset_id: str = record["asset_id"]
                run_id = _run_id(asset_id)
                if _is_reusable(record, run_id):
                    reused += 1
                    _write_event(
                        stream,
                        {
                            "type": "asset_stage",
                            "asset_id": asset_id,
                            "stage": "exif",
                            "attempt": 1,
                            "outcome": "skipped",
                            "run_id": run_id,
                            "retryable": False,
                            "message": "identical successful extraction reused",
                            "source_tags": {},
                            "warnings": [],
                            "started_at": _now(),
                            "ended_at": _now(),
                        },
                    )
                    continue
                attempted_at = _now()
                source = workspace / record["locators"][0]
                try:
                    values, source_tags, warnings = _decode(source)
                except CatalogProblem as problem:
                    failed += 1
                    _write_event(
                        stream,
                        {
                            "type": "asset_stage",
                            "asset_id": asset_id,
                            "stage": "exif",
                            "attempt": 1,
                            "outcome": "permanent_failure",
                            "error_code": problem.code,
                            "retryable": False,
                            "message": problem.message,
                            "source_tags": {},
                            "warnings": [],
                            "started_at": attempted_at,
                            "ended_at": _now(),
                        },
                    )
                    continue
                runs = dict(record.get("runs", {}))
                runs[run_id] = {
                    "kind": "extraction",
                    "tool": EXTRACTOR_NAME,
                    "version": EXTRACTOR_VERSION,
                    "outcome": "success",
                    "executed_at": attempted_at,
                }
                observations = {
                    name: {"value": value, "run_id": run_id}
                    for name, value in sorted(values.items())
                }
                record["observations"] = observations
                record["runs"] = runs
                checkpoint_catalog(workspace, records)
                succeeded += 1
                _write_event(
                    stream,
                    {
                        "type": "asset_stage",
                        "asset_id": asset_id,
                        "stage": "exif",
                        "attempt": 1,
                        "outcome": "succeeded",
                        "run_id": run_id,
                        "retryable": False,
                        "message": "embedded metadata extracted",
                        "source_tags": source_tags,
                        "warnings": warnings,
                        "started_at": attempted_at,
                        "ended_at": _now(),
                    },
                )
            _write_event(
                stream,
                {
                    "type": "command_completed",
                    "command_id": command_id,
                    "outcome": "partial_failure" if failed else "succeeded",
                    "succeeded": succeeded,
                    "reused": reused,
                    "failed": failed,
                    "ended_at": _now(),
                },
            )
    except OSError as error:
        raise CatalogProblem(
            "ANALYSIS_RUN_IO_ERROR", "Analysis Run could not be written"
        ) from error
    finally:
        if temporary != run_path:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    return ExtractionResult(succeeded=succeeded, reused=reused, failed=failed)
