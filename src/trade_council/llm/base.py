"""Provider-independent request and response contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .models import ModelSpec


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class CompletionRequest:
    model: str
    messages: list[Message]
    temperature: float = 0.0
    max_tokens: int = 4096
    response_format: dict[str, Any] | None = None
    reasoning_effort: str | None = None
    timeout_seconds: float = 120.0


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class CompletionResult:
    provider: str
    model: str
    content: str
    reasoning: str = ""
    model_digest: str | None = None
    model_details: dict[str, Any] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelInfo:
    id: str
    digest: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    ok: bool
    models: list[ModelInfo]
    message: str = ""


class LLMProvider(Protocol):
    name: str
    model_specs: dict[str, ModelSpec]

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Run a chat completion."""

    async def list_models(self) -> list[ModelInfo]:
        """List models exposed by this provider."""

    async def preflight(self) -> ProviderStatus:
        """Check provider availability without making a paid generation call."""
