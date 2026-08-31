from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from wedding_film.vision_adapter import (
    AdapterFailure,
    AdapterFailureCategory,
    AdapterResult,
    AdapterSettings,
    AdapterSuccess,
    AnalysisDerivative,
    OutputSchema,
)

ADAPTER_NAME = "openai"
ADAPTER_PROVIDER = "openai"
ADAPTER_VERSION = "1"

DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_REASONING_EFFORT = "low"
DEFAULT_MAX_OUTPUT_TOKENS = 800
DEFAULT_IMAGE_DETAIL = "high"
DEFAULT_STORE = False

API_URL = "https://api.openai.com/v1/responses"
API_KEY_ENV = "OPENAI_API_KEY"
STUB_TRANSPORT_ENV = "WEDDING_FILM_OPENAI_STUB_TRANSPORT"
REQUEST_TIMEOUT_SECONDS = 30.0

STUB_MODEL_REFUSAL = "stub-refusal"
STUB_MODEL_INVALID_SCHEMA = "stub-invalid-schema"
STUB_MODEL_AUTH_FAILURE = "stub-auth-failure"
STUB_MODEL_RATE_LIMITED = "stub-rate-limited"
STUB_MODEL_UNAVAILABLE = "stub-unavailable"
STUB_MODEL_TOKEN_LIMIT = "stub-token-limit"
STUB_MODEL_CONNECTION_ERROR = "stub-connection-error"
STUB_MODEL_MALFORMED = "stub-malformed"

_STUB_CANDIDATE: dict[str, Any] = {
    "description": {"value": "A couple exchanging vows outdoors", "confidence": 0.95},
    "wedding_moment": {"value": "ceremony", "confidence": 0.95},
    "subject_roles": {"value": ["couple", "officiant"], "confidence": 0.75},
    "setting": {"value": "outdoor garden", "confidence": 0.75},
    "mood": {"value": ["joyful"], "confidence": 0.5},
    "shot_type": {"value": "wide", "confidence": 0.95},
    "quality_flags": {"value": [], "confidence": 0.25},
}


class TransportNetworkError(Exception):
    """Raised by a Transport when a request never reached the provider.

    Covers both timeouts and connection failures.
    """


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    body: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    retry_after_seconds: float | None = None


class Transport(Protocol):
    def send(self, *, api_key: str, payload: dict[str, Any]) -> TransportResponse: ...


class HTTPTransport:
    """Talks to the OpenAI Responses API over HTTPS using only the standard library."""

    def __init__(self, timeout_seconds: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds

    def send(self, *, api_key: str, payload: dict[str, Any]) -> TransportResponse:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            API_URL,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        headers: Any = None
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as raw:
                body_bytes = raw.read()
                status_code = raw.status
                headers = raw.headers
        except urllib.error.HTTPError as error:
            body_bytes = error.read()
            status_code = error.code
            headers = error.headers
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise TransportNetworkError(str(error)) from error
        return TransportResponse(
            status_code=status_code,
            body=_decode_body(body_bytes),
            request_id=_header(headers, "x-request-id"),
            retry_after_seconds=_retry_after(headers),
        )


def _decode_body(body_bytes: bytes) -> dict[str, Any]:
    if not body_bytes:
        return {}
    try:
        decoded = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    value = headers.get(name)
    return value if isinstance(value, str) else None


def _retry_after(headers: Any) -> float | None:
    raw_value = _header(headers, "Retry-After")
    if raw_value is None:
        return None
    try:
        return float(raw_value)
    except ValueError:
        return None


class StubTransport:
    """Deterministic in-process transport used by the default test suite.

    Selected via WEDDING_FILM_OPENAI_STUB_TRANSPORT so the CLI-seam test suite
    never makes a live network call. The requested model name selects the
    scenario, mirroring how the fake adapter's fixture model names work.
    """

    def send(self, *, api_key: str, payload: dict[str, Any]) -> TransportResponse:
        model = payload.get("model")
        if model == STUB_MODEL_CONNECTION_ERROR:
            raise TransportNetworkError("stub connection reset")
        if model == STUB_MODEL_AUTH_FAILURE:
            return TransportResponse(
                status_code=401,
                body={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Incorrect API key provided.",
                    }
                },
            )
        if model == STUB_MODEL_RATE_LIMITED:
            return TransportResponse(
                status_code=429,
                body={
                    "error": {
                        "type": "rate_limit_exceeded",
                        "message": "Rate limit reached for requests.",
                    }
                },
                retry_after_seconds=1.0,
            )
        if model == STUB_MODEL_UNAVAILABLE:
            return TransportResponse(
                status_code=503,
                body={"error": {"type": "server_error", "message": "The server is overloaded."}},
            )
        if model == STUB_MODEL_INVALID_SCHEMA:
            return TransportResponse(
                status_code=400,
                body={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Invalid schema for response format.",
                        "param": "text.format.schema",
                    }
                },
            )
        if model == STUB_MODEL_TOKEN_LIMIT:
            return TransportResponse(
                status_code=200,
                body={
                    "id": "resp_stub_incomplete",
                    "model": model,
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "usage": {"input_tokens": 900, "output_tokens": 800, "total_tokens": 1700},
                    "output": [],
                },
                request_id="resp_stub_incomplete",
            )
        if model == STUB_MODEL_REFUSAL:
            return TransportResponse(
                status_code=200,
                body={
                    "id": "resp_stub_refusal",
                    "model": model,
                    "status": "completed",
                    "usage": {"input_tokens": 900, "output_tokens": 12, "total_tokens": 912},
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "refusal",
                                    "refusal": "I can't help identify people in this photo.",
                                }
                            ],
                        }
                    ],
                },
                request_id="resp_stub_refusal",
            )
        if model == STUB_MODEL_MALFORMED:
            return TransportResponse(
                status_code=200,
                body={
                    "id": "resp_stub_malformed",
                    "model": model,
                    "status": "completed",
                    "usage": {"input_tokens": 900, "output_tokens": 5, "total_tokens": 905},
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "not json"}],
                        }
                    ],
                },
                request_id="resp_stub_malformed",
            )
        return TransportResponse(
            status_code=200,
            body={
                "id": "resp_stub_success",
                "model": model,
                "status": "completed",
                "usage": {"input_tokens": 912, "output_tokens": 143, "total_tokens": 1055},
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": json.dumps(_STUB_CANDIDATE)}
                        ],
                    }
                ],
            },
            request_id="resp_stub_success",
        )


def _default_transport() -> Transport:
    if os.environ.get(STUB_TRANSPORT_ENV) == "1":
        return StubTransport()
    return HTTPTransport()


def _build_payload(
    derivative: AnalysisDerivative, schema: OutputSchema, settings: AdapterSettings
) -> dict[str, Any]:
    parameters = settings.parameters
    reasoning_effort = parameters.get("reasoning_effort", DEFAULT_REASONING_EFFORT)
    max_output_tokens = parameters.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    image_detail = parameters.get("image_detail", DEFAULT_IMAGE_DETAIL)
    store = parameters.get("store", DEFAULT_STORE)
    encoded_image = base64.b64encode(derivative.content).decode("ascii")
    return {
        "model": settings.model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": settings.prompt},
                    {
                        "type": "input_image",
                        "image_url": f"data:{derivative.media_type};base64,{encoded_image}",
                        "detail": image_detail,
                    },
                ],
            }
        ],
        "reasoning": {"effort": reasoning_effort},
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema.version,
                "schema": schema.definition,
                "strict": True,
            }
        },
        "max_output_tokens": max_output_tokens,
        "store": store,
        "tools": [],
    }


def _usage(body: dict[str, Any]) -> dict[str, int]:
    usage_body = body.get("usage")
    if not isinstance(usage_body, dict):
        return {}
    usage: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage_body.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            usage[key] = value
    return usage


def _error_message(body: dict[str, Any], default: str) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    return default


class OpenAIVisionAdapter:
    """Connects the provider-neutral Vision Adapter boundary to the OpenAI Responses API.

    Never reads or writes the Semantic Catalog; it only turns an Analysis Input into a
    candidate Inference snapshot or a classified failure.
    """

    name = ADAPTER_NAME
    provider = ADAPTER_PROVIDER
    version = ADAPTER_VERSION
    default_parameters: dict[str, object] = {
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "image_detail": DEFAULT_IMAGE_DETAIL,
        "store": DEFAULT_STORE,
    }

    def __init__(self, transport: Transport | None = None) -> None:
        self._transport = transport if transport is not None else _default_transport()

    def analyze(
        self,
        derivative: AnalysisDerivative,
        schema: OutputSchema,
        settings: AdapterSettings,
    ) -> AdapterResult:
        api_key = os.environ.get(API_KEY_ENV, "").strip()
        if not api_key:
            return self._failure(
                "authentication",
                False,
                f"{API_KEY_ENV} is not set in the process environment",
            )
        payload = _build_payload(derivative, schema, settings)
        try:
            response = self._transport.send(api_key=api_key, payload=payload)
        except TransportNetworkError as error:
            return self._failure(
                "provider_unavailable", True, f"request could not reach the provider: {error}"
            )
        return self._interpret(response)

    def _interpret(self, response: TransportResponse) -> AdapterResult:
        usage = _usage(response.body)
        provider_metadata: dict[str, object] = {
            "response_model": response.body.get("model"),
            "response_id": response.request_id or response.body.get("id"),
        }
        if response.status_code in (401, 403):
            return self._failure(
                "authentication",
                False,
                _error_message(response.body, "authentication failed"),
                usage,
                provider_metadata,
            )
        if response.status_code == 429:
            if response.retry_after_seconds is not None:
                provider_metadata["retry_after_seconds"] = response.retry_after_seconds
            return self._failure(
                "rate_limited",
                True,
                _error_message(response.body, "rate limit exceeded"),
                usage,
                provider_metadata,
            )
        if response.status_code >= 500:
            return self._failure(
                "provider_unavailable",
                True,
                _error_message(response.body, "provider unavailable"),
                usage,
                provider_metadata,
            )
        if response.status_code >= 400:
            error = response.body.get("error")
            param = error.get("param") if isinstance(error, dict) else None
            message = _error_message(response.body, "provider rejected the request")
            if isinstance(param, str) and "schema" in param:
                return self._failure(
                    "unsupported_schema", False, message, usage, provider_metadata
                )
            return self._failure("provider_error", False, message, usage, provider_metadata)
        return self._interpret_success(response.body, usage, provider_metadata)

    def _interpret_success(
        self,
        body: dict[str, Any],
        usage: dict[str, int],
        provider_metadata: dict[str, object],
    ) -> AdapterResult:
        if body.get("status") == "incomplete":
            details = body.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            return self._failure(
                "invalid_response",
                False,
                f"response was incomplete: {reason or 'unknown reason'}",
                usage,
                provider_metadata,
            )
        output = body.get("output")
        message_item = next(
            (
                item
                for item in output
                if isinstance(item, dict) and item.get("type") == "message"
            ),
            None,
        ) if isinstance(output, list) else None
        if message_item is None:
            return self._failure(
                "invalid_response", False, "response is missing message output", usage,
                provider_metadata,
            )
        content = message_item.get("content")
        first = content[0] if isinstance(content, list) and content else None
        if not isinstance(first, dict):
            return self._failure(
                "invalid_response", False, "response message has no content", usage,
                provider_metadata,
            )
        if first.get("type") == "refusal":
            refusal = first.get("refusal")
            message = refusal if isinstance(refusal, str) and refusal else (
                "provider refused to analyze the asset"
            )
            return self._failure("refusal", False, message, usage, provider_metadata)
        if first.get("type") != "output_text" or not isinstance(first.get("text"), str):
            return self._failure(
                "invalid_response", False, "response content type is unsupported", usage,
                provider_metadata,
            )
        try:
            candidate = json.loads(first["text"])
        except ValueError:
            return self._failure(
                "invalid_response", False, "response text is not valid JSON", usage,
                provider_metadata,
            )
        if not isinstance(candidate, dict):
            return self._failure(
                "invalid_response", False, "response candidate must be a JSON object", usage,
                provider_metadata,
            )
        return AdapterSuccess(
            outcome="success",
            adapter_version=self.version,
            candidate=candidate,
            usage=usage,
            provider_metadata=provider_metadata,
        )

    def _failure(
        self,
        category: AdapterFailureCategory,
        retryable: bool,
        message: str,
        usage: dict[str, int] | None = None,
        provider_metadata: dict[str, object] | None = None,
    ) -> AdapterFailure:
        return AdapterFailure(
            outcome="failure",
            adapter_version=self.version,
            category=category,
            retryable=retryable,
            message=message,
            usage=usage,
            provider_metadata=provider_metadata,
        )
