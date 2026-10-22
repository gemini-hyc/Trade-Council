"""OpenAI-compatible, Ollama, and DeepSeek providers."""

from __future__ import annotations

import os
import time
from typing import Any

from ..errors import InvalidResponseError, ProviderUnavailableError
from .base import (
    CompletionRequest,
    CompletionResult,
    LLMProvider,
    ModelInfo,
    ProviderStatus,
    Usage,
)
from .http import request_json
from .models import ModelSpec, ProviderConfig


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str | None,
        model_specs: dict[str, ModelSpec],
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_specs = model_specs

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        if request.model not in self.model_specs:
            raise InvalidResponseError(
                f"Model {request.model!r} is not configured for provider {self.name}"
            )
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        if request.response_format:
            payload["response_format"] = request.response_format
        if request.reasoning_effort:
            payload["reasoning_effort"] = request.reasoning_effort
        started = time.monotonic()
        raw = await request_json(
            f"{self.base_url}/chat/completions",
            payload=payload,
            headers=self._headers(),
            timeout=request.timeout_seconds,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        choices = raw.get("choices")
        if not isinstance(choices, list) or not choices:
            raise InvalidResponseError("Provider response has no choices")
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        if not isinstance(content, str):
            raise InvalidResponseError("Provider message content is not text")
        reasoning = message.get("reasoning", "") if isinstance(message, dict) else ""
        usage_raw = raw.get("usage", {})
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
            total_tokens=int(usage_raw.get("total_tokens", 0) or 0),
        )
        return CompletionResult(
            provider=self.name,
            model=request.model,
            content=content,
            reasoning=reasoning if isinstance(reasoning, str) else "",
            usage=usage,
            latency_ms=latency_ms,
            raw=raw,
        )

    async def list_models(self) -> list[ModelInfo]:
        raw = await request_json(
            f"{self.base_url}/models",
            payload=None,
            headers=self._headers(),
            timeout=10,
        )
        data = raw.get("data", [])
        if not isinstance(data, list):
            return []
        return [
            ModelInfo(id=str(item["id"]))
            for item in data
            if isinstance(item, dict) and "id" in item
        ]

    async def preflight(self) -> ProviderStatus:
        if not self.api_key:
            return ProviderStatus(self.name, False, [], "API key is not configured")
        return ProviderStatus(self.name, True, [], "Configuration present")


class OllamaProvider(OpenAICompatibleProvider):
    def __init__(self, *, name: str, base_url: str, api_key: str | None, model_specs: dict[str, ModelSpec]):
        super().__init__(name=name, base_url=base_url, api_key=api_key, model_specs=model_specs)
        self.native_base_url = self.base_url.removesuffix("/v1")

    async def list_models(self) -> list[ModelInfo]:
        raw = await request_json(
            f"{self.native_base_url}/api/tags",
            payload=None,
            headers={},
            timeout=10,
        )
        models = raw.get("models", [])
        if not isinstance(models, list):
            return []
        result = []
        for item in models:
            if not isinstance(item, dict) or "name" not in item:
                continue
            result.append(
                ModelInfo(
                    id=str(item["name"]),
                    digest=str(item.get("digest")) if item.get("digest") else None,
                    details=item.get("details", {}) if isinstance(item.get("details"), dict) else {},
                )
            )
        return result

    async def preflight(self) -> ProviderStatus:
        try:
            models = await self.list_models()
        except ProviderUnavailableError as exc:
            return ProviderStatus(self.name, False, [], str(exc))
        available = {model.id for model in models}
        missing = sorted(set(self.model_specs) - available)
        if missing:
            return ProviderStatus(
                self.name,
                False,
                models,
                f"Missing local models: {', '.join(missing)}",
            )
        return ProviderStatus(self.name, True, models)

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        result = await super().complete(request)
        models = await self.list_models()
        matched = next((model for model in models if model.id == request.model), None)
        return CompletionResult(
            provider=result.provider,
            model=result.model,
            content=result.content,
            reasoning=result.reasoning,
            model_digest=matched.digest if matched else None,
            model_details=matched.details if matched else {},
            usage=result.usage,
            latency_ms=result.latency_ms,
            raw=result.raw,
        )


class DeepSeekProvider(OpenAICompatibleProvider):
    async def preflight(self) -> ProviderStatus:
        if not self.api_key:
            return ProviderStatus(self.name, False, [], "DEEPSEEK_API_KEY is not configured")
        return ProviderStatus(
            self.name,
            True,
            [ModelInfo(id=model_id) for model_id in self.model_specs],
            "API key present; paid generation not executed by preflight",
        )

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        result = await super().complete(request)
        return CompletionResult(
            provider=result.provider,
            model=result.model,
            content=result.content,
            reasoning=result.reasoning,
            model_digest=request.model,
            usage=result.usage,
            latency_ms=result.latency_ms,
            raw=result.raw,
        )


def build_provider(name: str, config: ProviderConfig) -> LLMProvider:
    specs = {model.id: model for model in config.models}
    api_key = os.environ.get(config.api_key_env) or config.default_api_key
    if config.kind == "ollama":
        return OllamaProvider(
            name=name,
            base_url=config.base_url,
            api_key=api_key or "local",
            model_specs=specs,
        )
    if config.kind == "deepseek":
        return DeepSeekProvider(
            name=name,
            base_url=config.base_url,
            api_key=api_key,
            model_specs=specs,
        )
    return OpenAICompatibleProvider(
        name=name,
        base_url=config.base_url,
        api_key=api_key,
        model_specs=specs,
    )
