from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from wedding_film.adapters import is_supported_adapter


@dataclass(frozen=True)
class AdapterConfig:
    name: str
    model: str
    prompt_version: str


@dataclass(frozen=True)
class AnalysisDefaults:
    max_assets: int
    max_estimated_usd: float
    concurrency: int


@dataclass(frozen=True)
class ProjectConfig:
    schema_version: int
    project_id: str
    display_title: str
    generation_language: str
    vision: AdapterConfig
    narrative: AdapterConfig
    analysis_defaults: AnalysisDefaults


class ConfigProblem(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _reject_duplicate_keys(node: Node | None, location: str = "project") -> None:
    if isinstance(node, MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            if not isinstance(key_node, ScalarNode):
                raise ConfigProblem("CONFIG_INVALID_VALUE", f"{location} keys must be strings")
            key = key_node.value
            if key in seen:
                raise ConfigProblem("CONFIG_DUPLICATE_FIELD", f"duplicate field {location}.{key}")
            seen.add(key)
            _reject_duplicate_keys(value_node, f"{location}.{key}")
    elif isinstance(node, SequenceNode):
        for value_node in node.value:
            _reject_duplicate_keys(value_node, location)


def _mapping(value: object, location: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigProblem("CONFIG_INVALID_VALUE", f"{location} must be a mapping")
    return value


def _exact_fields(data: dict[str, Any], expected: tuple[str, ...], location: str) -> None:
    unknown = sorted(set(data) - set(expected))
    if unknown:
        raise ConfigProblem("CONFIG_UNKNOWN_FIELD", f"unknown field {location}.{unknown[0]}")
    missing = [field for field in expected if field not in data]
    if missing:
        raise ConfigProblem("CONFIG_MISSING_FIELD", f"missing field {location}.{missing[0]}")


def _non_empty_string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigProblem("CONFIG_INVALID_VALUE", f"{location} must be a non-empty string")
    return value


def _positive_int(value: object, location: str) -> int:
    if type(value) is not int or value <= 0:
        raise ConfigProblem("CONFIG_INVALID_VALUE", f"{location} must be a positive integer")
    return value


def _positive_number(value: object, location: str) -> float:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigProblem("CONFIG_INVALID_VALUE", f"{location} must be a positive number")
    return float(value)


def _slug(value: object, location: str) -> str:
    text = _non_empty_string(value, location)
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", text) is None:
        raise ConfigProblem("CONFIG_INVALID_VALUE", f"{location} must be lowercase kebab-case")
    return text


def _language(value: object, location: str) -> str:
    text = _non_empty_string(value, location)
    if re.fullmatch(r"[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|[0-9]{3}))?", text) is None:
        raise ConfigProblem("CONFIG_INVALID_VALUE", f"{location} must be a language tag")
    return text


def _adapter(value: object, location: str) -> AdapterConfig:
    data = _mapping(value, location)
    _exact_fields(data, ("name", "model", "prompt_version"), location)
    name = _slug(data["name"], f"{location}.name")
    if not is_supported_adapter(name):
        raise ConfigProblem("CONFIG_UNKNOWN_ADAPTER", f"{location}.name is not supported")
    return AdapterConfig(
        name=name,
        model=_non_empty_string(data["model"], f"{location}.model"),
        prompt_version=_non_empty_string(data["prompt_version"], f"{location}.prompt_version"),
    )


def load_project_config(workspace: Path) -> ProjectConfig:
    path = workspace / "project.yaml"
    if not path.is_file() or path.is_symlink():
        raise ConfigProblem("CONFIG_MISSING", "project.yaml is missing or is not a regular file")
    try:
        contents = path.read_text(encoding="utf-8")
        _reject_duplicate_keys(yaml.compose(contents, Loader=yaml.SafeLoader))
        loaded = yaml.safe_load(contents)
    except ConfigProblem:
        raise
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ConfigProblem("CONFIG_INVALID_YAML", "project.yaml is not valid UTF-8 YAML") from None

    data = _mapping(loaded, "project")
    _exact_fields(
        data,
        (
            "schema_version",
            "project_id",
            "display_title",
            "generation_language",
            "adapters",
            "analysis_defaults",
        ),
        "project",
    )
    schema_version = _positive_int(data["schema_version"], "project.schema_version")
    if schema_version != 1:
        raise ConfigProblem("CONFIG_UNSUPPORTED_VERSION", "project.schema_version is unsupported")

    adapters = _mapping(data["adapters"], "project.adapters")
    _exact_fields(adapters, ("vision", "narrative"), "project.adapters")
    defaults = _mapping(data["analysis_defaults"], "project.analysis_defaults")
    _exact_fields(
        defaults,
        ("max_assets", "max_estimated_usd", "concurrency"),
        "project.analysis_defaults",
    )
    return ProjectConfig(
        schema_version=schema_version,
        project_id=_slug(data["project_id"], "project.project_id"),
        display_title=_non_empty_string(data["display_title"], "project.display_title"),
        generation_language=_language(data["generation_language"], "project.generation_language"),
        vision=_adapter(adapters["vision"], "project.adapters.vision"),
        narrative=_adapter(adapters["narrative"], "project.adapters.narrative"),
        analysis_defaults=AnalysisDefaults(
            max_assets=_positive_int(
                defaults["max_assets"], "project.analysis_defaults.max_assets"
            ),
            max_estimated_usd=_positive_number(
                defaults["max_estimated_usd"],
                "project.analysis_defaults.max_estimated_usd",
            ),
            concurrency=_positive_int(
                defaults["concurrency"], "project.analysis_defaults.concurrency"
            ),
        ),
    )
