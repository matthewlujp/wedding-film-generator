from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

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
class NarrativeRequest:
    catalog_summary: list[dict[str, object]]
    participants: list[dict[str, object]]


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
    adapter_version: str
    candidate: object
    usage: dict[str, int] | None = None
    provider_metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class AdapterFailure:
    outcome: Literal["failure"]
    adapter_version: str
    category: AdapterFailureCategory
    retryable: bool
    message: str
    usage: dict[str, int] | None = None
    provider_metadata: dict[str, object] | None = None


AdapterResult = AdapterSuccess | AdapterFailure


class NarrativeAdapter(Protocol):
    name: str
    provider: str
    version: str
    default_parameters: dict[str, object]

    def generate(
        self,
        request: NarrativeRequest,
        schema: OutputSchema,
        settings: AdapterSettings,
    ) -> AdapterResult: ...


_FIXTURE_CANDIDATE: dict[str, object] = {
    "title": "Fixture Wedding Story",
    "target_duration_seconds": 300,
    "intent": "Celebrate their day surrounded by the people who love them.",
    "emotional_arc": "From quiet anticipation to joyful, shared celebration.",
    "moments": [
        {"id": "getting-ready", "prose": "A quiet morning of preparation before the day begins."},
        {"id": "joyful-ceremony", "prose": "Vows are shared with family and friends looking on."},
    ],
}

_FIXTURE_ALTERNATE_CANDIDATE: dict[str, object] = {
    "title": "Fixture Wedding Story, Revisited",
    "target_duration_seconds": 360,
    "intent": "Celebrate their day surrounded by the people who love them, with more warmth.",
    "emotional_arc": "From nervous excitement to overflowing joy.",
    "moments": [
        {"id": "getting-ready", "prose": "A quiet morning of preparation before the day begins."},
        {"id": "joyful-ceremony", "prose": "Vows are shared with family and friends looking on."},
        {"id": "reception-toast", "prose": "Friends raise a glass as the night begins."},
    ],
}


class FakeNarrativeAdapter:
    """Deterministic offline contract fixture; it has no Catalog dependency."""

    name = "fake"
    provider = "deterministic-fake"
    version = "1"
    default_parameters: dict[str, object] = {}

    def generate(
        self,
        request: NarrativeRequest,
        schema: OutputSchema,
        settings: AdapterSettings,
    ) -> AdapterResult:
        model = settings.model
        if model == "fixture-refusal":
            return AdapterFailure(
                outcome="failure",
                adapter_version=self.version,
                category="refusal",
                retryable=False,
                message="fixture refusal",
            )
        if model == "fixture-transient-failure":
            return AdapterFailure(
                outcome="failure",
                adapter_version=self.version,
                category="provider_unavailable",
                retryable=True,
                message="fixture provider is temporarily unavailable",
                usage={"input_tokens": 10},
                provider_metadata={"fixture": model},
            )
        if model == "fixture-alternate":
            candidate: dict[str, object] = dict(_FIXTURE_ALTERNATE_CANDIDATE)
        elif model == "fixture-invalid-empty-moments":
            candidate = {**_FIXTURE_CANDIDATE, "moments": []}
        elif model == "fixture-invalid-duplicate-id":
            candidate = {
                **_FIXTURE_CANDIDATE,
                "moments": [
                    {"id": "getting-ready", "prose": "First."},
                    {"id": "getting-ready", "prose": "Second."},
                ],
            }
        elif model == "fixture-invalid-bad-id":
            candidate = {
                **_FIXTURE_CANDIDATE,
                "moments": [{"id": "Getting_Ready", "prose": "Bad id."}],
            }
        elif model == "fixture-invalid-empty-intent":
            candidate = {**_FIXTURE_CANDIDATE, "intent": "   "}
        elif model == "fixture-invalid-formatting-only-intent":
            candidate = {**_FIXTURE_CANDIDATE, "intent": "***"}
        elif model == "fixture-invalid-type":
            candidate = {**_FIXTURE_CANDIDATE, "target_duration_seconds": "not-a-number"}
        else:
            candidate = dict(_FIXTURE_CANDIDATE)
        return AdapterSuccess(
            outcome="success",
            adapter_version=self.version,
            candidate=candidate,
            usage={"input_tokens": 120, "output_tokens": 80},
            provider_metadata={"fixture": model},
        )


def narrative_adapter_for(name: str) -> NarrativeAdapter:
    if name == "fake":
        return FakeNarrativeAdapter()
    if name == "openai":
        from wedding_film.openai_narrative_adapter import OpenAINarrativeAdapter

        return OpenAINarrativeAdapter()
    raise ValueError(f"narrative adapter {name!r} cannot generate a Story candidate")
