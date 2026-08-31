from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

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

_STORYBOARD_CARD_DURATION_FRAMES = 24
_STORYBOARD_PHOTO_DURATION_FRAMES = 48
_STORYBOARD_MOTIONS = ("static", "slow-zoom-in", "slow-zoom-out")


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
        if "sequence" in schema.fields:
            candidate: dict[str, object] = self._storyboard_candidate(model, request.context)
        elif "blocks" in schema.fields:
            candidate = self._script_candidate(model)
        else:
            candidate = self._story_candidate(model)
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

    def _storyboard_candidate(self, model: str, context: dict[str, object]) -> dict[str, object]:
        story = context.get("story")
        script = context.get("script")
        assets = context.get("assets")
        moments = (
            cast("list[str]", cast(dict[str, Any], story)["moments"])
            if isinstance(story, dict)
            else []
        )
        blocks = (
            cast("list[dict[str, Any]]", cast(dict[str, Any], script)["blocks"])
            if isinstance(script, dict)
            else []
        )
        asset_entries = cast("list[dict[str, Any]]", assets) if isinstance(assets, list) else []

        card_blocks = [block for block in blocks if block.get("type") == "card"]
        caption_by_moment = {
            block["story_moment"]: block["id"] for block in blocks if block.get("type") == "caption"
        }
        narration_blocks = [block for block in blocks if block.get("type") == "narration"]

        card_duration = _STORYBOARD_CARD_DURATION_FRAMES
        motion_offset = 0
        if model == "fixture-alternate":
            card_duration = _STORYBOARD_CARD_DURATION_FRAMES * 2
            motion_offset = 1

        sequence: list[dict[str, object]] = []
        for block in card_blocks:
            sequence.append(
                {
                    "item_id": f"card-{block['id']}",
                    "type": "card",
                    "story_moment": block["story_moment"],
                    "duration_frames": card_duration,
                    "script_block": block["id"],
                    "transition": {"type": "cut"},
                }
            )
        for index, asset in enumerate(asset_entries):
            moment_id = moments[index % len(moments)] if moments else "unknown-moment"
            item: dict[str, object] = {
                "item_id": f"photo-{index}",
                "type": "photo",
                "story_moment": moment_id,
                "duration_frames": _STORYBOARD_PHOTO_DURATION_FRAMES,
                "asset_id": asset["asset_id"],
                "motion": _STORYBOARD_MOTIONS[(index + motion_offset) % len(_STORYBOARD_MOTIONS)],
            }
            caption_block_id = caption_by_moment.pop(moment_id, None)
            if caption_block_id is not None:
                item["script_block"] = caption_block_id
            if index != len(asset_entries) - 1:
                item["transition"] = {"type": "cut"}
            sequence.append(item)

        if model == "fixture-invalid-empty-sequence":
            sequence = []
        elif model == "fixture-invalid-duplicate-item-id" and len(sequence) >= 2:
            sequence[1]["item_id"] = sequence[0]["item_id"]
        elif model == "fixture-invalid-unknown-asset" and sequence:
            for item in sequence:
                if item["type"] == "photo":
                    item["asset_id"] = "sha256:" + "0" * 64
                    break
        elif model == "fixture-invalid-unknown-story-moment" and sequence:
            sequence[0]["story_moment"] = "no-such-moment"
        elif model == "fixture-invalid-bad-transition" and len(sequence) >= 2:
            sequence[0]["transition"] = {"type": "crossfade", "duration_frames": 10_000}

        candidate: dict[str, object] = {"sequence": sequence}
        total_frames = sum(cast(int, item["duration_frames"]) for item in sequence)
        if narration_blocks and total_frames:
            count = len(narration_blocks)
            share = total_frames // count
            cues: list[dict[str, object]] = []
            start = 0
            for index, block in enumerate(narration_blocks):
                length = total_frames - start if index == count - 1 else share
                cues.append(
                    {
                        "cue_id": f"narration-{block['id']}",
                        "block_id": block["id"],
                        "start_frame": start,
                        "duration_frames": length,
                    }
                )
                start += length
            candidate["narration_cues"] = cues
        if total_frames:
            candidate["music_cues"] = [
                {
                    "cue_id": "music-intent",
                    "start_frame": 0,
                    "duration_frames": total_frames,
                    "intent": "warm acoustic guitar",
                }
            ]
        return candidate


def narrative_adapter_for(name: str) -> NarrativeAdapter:
    if name == "fake":
        return FakeNarrativeAdapter()
    if name == "openai":
        from wedding_film.openai_narrative_adapter import OpenAINarrativeAdapter

        return OpenAINarrativeAdapter()
    raise ValueError(f"narrative adapter {name!r} cannot generate a Story candidate")
