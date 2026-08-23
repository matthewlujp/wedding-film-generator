from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

AdapterFailureCategory = Literal[
    "refusal",
    "provider_unavailable",
    "rate_limited",
    "authentication",
    "invalid_response",
    "unsupported_schema",
    "provider_error",
]


@dataclass(frozen=True)
class AnalysisDerivative:
    content: bytes
    sha256: str
    pixel_width: int
    pixel_height: int
    media_type: str
    recipe_version: str


@dataclass(frozen=True)
class OutputSchema:
    version: str
    fields: tuple[str, ...]
    definition: dict[str, object]


@dataclass(frozen=True)
class AdapterSettings:
    model: str
    prompt_version: str
    prompt: str
    parameters: dict[str, object]


@dataclass(frozen=True)
class AdapterSuccess:
    outcome: Literal["success"]
    candidate: object
    usage: dict[str, int] | None = None
    provider_metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class AdapterFailure:
    outcome: Literal["failure"]
    category: AdapterFailureCategory
    retryable: bool
    message: str
    usage: dict[str, int] | None = None
    provider_metadata: dict[str, object] | None = None


AdapterResult = AdapterSuccess | AdapterFailure


class VisionAdapter(Protocol):
    name: str
    provider: str

    def analyze(
        self,
        derivative: AnalysisDerivative,
        schema: OutputSchema,
        settings: AdapterSettings,
    ) -> AdapterResult: ...


class FakeVisionAdapter:
    """Deterministic offline contract fixture; it has no Catalog dependency."""

    name = "fake"
    provider = "deterministic-fake"

    def analyze(
        self,
        derivative: AnalysisDerivative,
        schema: OutputSchema,
        settings: AdapterSettings,
    ) -> AdapterResult:
        del derivative
        if not _supports_candidate_schema(schema):
            return AdapterFailure(
                outcome="failure",
                category="unsupported_schema",
                retryable=False,
                message="fake adapter does not support the requested output schema",
            )
        candidate: dict[str, Any] = {
            "description": {"value": "A wedding gathering", "confidence": 0.96},
            "wedding_moment": {"value": "reception", "confidence": 0.84},
            "subject_roles": {"value": ["guests"], "confidence": 0.8},
            "setting": {"value": "indoor venue", "confidence": 0.91},
            "mood": {"value": ["joyful"], "confidence": 0.88},
            "shot_type": {"value": "medium", "confidence": 0.93},
            "quality_flags": {"value": [], "confidence": 0.99},
        }
        model = settings.model
        if model == "fixture-refusal":
            return AdapterFailure(
                outcome="failure",
                category="refusal",
                retryable=False,
                message="fixture refusal",
            )
        if model == "fixture-transient-failure":
            return AdapterFailure(
                outcome="failure",
                category="provider_unavailable",
                retryable=True,
                message="fixture provider is temporarily unavailable",
                usage={"input_images": 1},
                provider_metadata={"fixture": model},
            )
        if model == "fixture-incomplete":
            candidate.pop("setting")
        elif model == "fixture-invalid-enum":
            candidate["shot_type"]["value"] = "extreme"
        elif model == "fixture-invalid-confidence":
            candidate["mood"]["confidence"] = 1.5
        elif model == "fixture-empty":
            candidate = {}
        elif model == "fixture-malformed-claim":
            candidate["description"] = {"value": "Missing confidence"}
        elif model == "fixture-invalid-type":
            candidate["description"]["value"] = 42
        elif model == "fixture-nulls":
            candidate["setting"] = {"value": None, "confidence": 0.7}
        return AdapterSuccess(
            outcome="success",
            candidate=candidate,
            usage={"input_images": 1, "output_fields": len(candidate)},
            provider_metadata={"fixture": model},
        )


def _supports_candidate_schema(schema: OutputSchema) -> bool:
    definition = schema.definition
    if set(definition) != {"type", "required", "additionalProperties", "properties"}:
        return False
    if definition.get("type") != "object" or definition.get("additionalProperties") is not False:
        return False
    if definition.get("required") != list(schema.fields):
        return False
    properties = definition.get("properties")
    if not isinstance(properties, dict) or set(properties) != set(schema.fields):
        return False
    for field in schema.fields:
        claim = properties[field]
        if not isinstance(claim, dict):
            return False
        if claim.get("type") != "object" or claim.get("additionalProperties") is not False:
            return False
        if claim.get("required") != ["value", "confidence"]:
            return False
        claim_properties = claim.get("properties")
        if not isinstance(claim_properties, dict) or set(claim_properties) != {
            "value",
            "confidence",
        }:
            return False
    return True


def adapter_for(name: str) -> VisionAdapter:
    if name == "fake":
        return FakeVisionAdapter()
    raise ValueError(f"vision adapter {name!r} cannot analyze assets")
