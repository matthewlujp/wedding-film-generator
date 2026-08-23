from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class AdapterAsset:
    asset_id: str
    byte_size: int


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
class AdapterResponse:
    candidate: object | None
    refusal: str | None
    usage: dict[str, int]
    provider: str


class VisionAdapter(Protocol):
    name: str
    provider: str

    def analyze(
        self,
        asset: AdapterAsset,
        derivative: AnalysisDerivative,
        schema: OutputSchema,
        settings: AdapterSettings,
    ) -> AdapterResponse: ...


class FakeVisionAdapter:
    """Deterministic offline contract fixture; it has no Catalog dependency."""

    name = "fake"
    provider = "deterministic-fake"

    def analyze(
        self,
        asset: AdapterAsset,
        derivative: AnalysisDerivative,
        schema: OutputSchema,
        settings: AdapterSettings,
    ) -> AdapterResponse:
        del asset, derivative, schema
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
            return AdapterResponse(None, "fixture refusal", {}, self.provider)
        if model == "fixture-incomplete":
            candidate.pop("setting")
        elif model == "fixture-invalid-enum":
            candidate["shot_type"]["value"] = "extreme"
        elif model == "fixture-invalid-confidence":
            candidate["mood"]["confidence"] = 1.5
        elif model == "fixture-empty":
            candidate = {}
        elif model == "fixture-nulls":
            candidate["setting"] = {"value": None, "confidence": 0.7}
        return AdapterResponse(
            candidate=candidate,
            refusal=None,
            usage={"input_images": 1, "output_fields": len(candidate)},
            provider=self.provider,
        )


def adapter_for(name: str) -> VisionAdapter:
    if name == "fake":
        return FakeVisionAdapter()
    raise ValueError(f"vision adapter {name!r} cannot analyze assets")
