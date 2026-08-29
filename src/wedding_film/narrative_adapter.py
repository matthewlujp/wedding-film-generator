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
    # JSON-serializable prompt payload; shape depends on the narrative task
    # (Story generation vs. Script generation consume different upstream layers).
    context: dict[str, object]


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

_SCRIPT_FIXTURE_CANDIDATE: dict[str, object] = {
    "title": "Fixture Wedding Script",
    "blocks": [
        {
            "id": "opening-card",
            "type": "card",
            "story_moment": "getting-ready",
            "body": "Their story begins here.",
        },
        {
            "id": "ceremony-narration",
            "type": "narration",
            "story_moment": "joyful-ceremony",
            "body": "Surrounded by family and friends, they share their vows.",
        },
        {
            "id": "ceremony-caption",
            "type": "caption",
            "story_moment": "joyful-ceremony",
            "body": "Forever begins today.",
        },
    ],
}

_SCRIPT_FIXTURE_ALTERNATE_CANDIDATE: dict[str, object] = {
    "title": "Fixture Wedding Script, Revisited",
    "blocks": [
        {
            "id": "opening-card",
            "type": "card",
            "story_moment": "getting-ready",
            "body": "Their story begins here, with quiet hope.",
        },
        {
            "id": "ceremony-narration",
            "type": "narration",
            "story_moment": "joyful-ceremony",
            "body": "Surrounded by family and friends, they share their vows.",
        },
        {
            "id": "reception-caption",
            "type": "caption",
            "story_moment": "joyful-ceremony",
            "body": "The celebration begins.",
        },
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
        candidate = (
            self._script_candidate(model)
            if "blocks" in schema.fields
            else self._story_candidate(model)
        )
        return AdapterSuccess(
            outcome="success",
            adapter_version=self.version,
            candidate=candidate,
            usage={"input_tokens": 120, "output_tokens": 80},
            provider_metadata={"fixture": model},
        )

    def _story_candidate(self, model: str) -> dict[str, object]:
        if model == "fixture-alternate":
            return dict(_FIXTURE_ALTERNATE_CANDIDATE)
        if model == "fixture-invalid-empty-moments":
            return {**_FIXTURE_CANDIDATE, "moments": []}
        if model == "fixture-invalid-duplicate-id":
            return {
                **_FIXTURE_CANDIDATE,
                "moments": [
                    {"id": "getting-ready", "prose": "First."},
                    {"id": "getting-ready", "prose": "Second."},
                ],
            }
        if model == "fixture-invalid-bad-id":
            return {
                **_FIXTURE_CANDIDATE,
                "moments": [{"id": "Getting_Ready", "prose": "Bad id."}],
            }
        if model == "fixture-invalid-empty-intent":
            return {**_FIXTURE_CANDIDATE, "intent": "   "}
        if model == "fixture-invalid-formatting-only-intent":
            return {**_FIXTURE_CANDIDATE, "intent": "***"}
        if model == "fixture-invalid-type":
            return {**_FIXTURE_CANDIDATE, "target_duration_seconds": "not-a-number"}
        return dict(_FIXTURE_CANDIDATE)

    def _script_candidate(self, model: str) -> dict[str, object]:
        if model == "fixture-alternate":
            return dict(_SCRIPT_FIXTURE_ALTERNATE_CANDIDATE)
        if model == "fixture-invalid-empty-blocks":
            return {**_SCRIPT_FIXTURE_CANDIDATE, "blocks": []}
        if model == "fixture-invalid-duplicate-id":
            return {
                **_SCRIPT_FIXTURE_CANDIDATE,
                "blocks": [
                    {
                        "id": "opening-card",
                        "type": "card",
                        "story_moment": "getting-ready",
                        "body": "First.",
                    },
                    {
                        "id": "opening-card",
                        "type": "caption",
                        "story_moment": "getting-ready",
                        "body": "Second.",
                    },
                ],
            }
        if model == "fixture-invalid-bad-id":
            return {
                **_SCRIPT_FIXTURE_CANDIDATE,
                "blocks": [
                    {
                        "id": "Opening_Card",
                        "type": "card",
                        "story_moment": "getting-ready",
                        "body": "Bad id.",
                    }
                ],
            }
        if model == "fixture-invalid-block-type":
            return {
                **_SCRIPT_FIXTURE_CANDIDATE,
                "blocks": [
                    {
                        "id": "opening-card",
                        "type": "quote",
                        "story_moment": "getting-ready",
                        "body": "Bad type.",
                    }
                ],
            }
        if model == "fixture-invalid-empty-body":
            return {
                **_SCRIPT_FIXTURE_CANDIDATE,
                "blocks": [
                    {
                        "id": "opening-card",
                        "type": "card",
                        "story_moment": "getting-ready",
                        "body": "   ",
                    }
                ],
            }
        if model == "fixture-invalid-rich-body":
            return {
                **_SCRIPT_FIXTURE_CANDIDATE,
                "blocks": [
                    {
                        "id": "opening-card",
                        "type": "card",
                        "story_moment": "getting-ready",
                        "body": "**bold**",
                    }
                ],
            }
        return dict(_SCRIPT_FIXTURE_CANDIDATE)


def narrative_adapter_for(name: str) -> NarrativeAdapter:
    if name == "fake":
        return FakeNarrativeAdapter()
    if name == "openai":
        from wedding_film.openai_narrative_adapter import OpenAINarrativeAdapter

        return OpenAINarrativeAdapter()
    raise ValueError(f"narrative adapter {name!r} cannot generate a Story candidate")
