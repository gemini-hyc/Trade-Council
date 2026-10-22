"""High-level model client with routing, budget, retry, and JSON repair."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from ..errors import InvalidResponseError
from .base import CompletionRequest, CompletionResult, LLMProvider, Message
from .cost import CostLedger, CostSnapshot, enforce_budget
from .json_output import parse_json_object, repair_messages
from .models import ModelsConfig
from .providers import build_provider
from .retry import retry_call
from .router import ModelRouter, RouteDecision


class LLMClient:
    def __init__(
        self,
        config: ModelsConfig,
        *,
        providers: dict[str, LLMProvider] | None = None,
        ledger: CostLedger | None = None,
        trace_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config
        self.router = ModelRouter(config)
        self.providers = providers or {
            name: build_provider(name, provider)
            for name, provider in config.providers.items()
        }
        self.ledger = ledger or CostLedger()
        self.calls: list[dict[str, Any]] = []
        self.trace_sink = trace_sink

    def route(self, *, role: str, profile: str, deep_tier: str | None = None) -> RouteDecision:
        return self.router.route(role=role, profile_name=profile, deep_tier=deep_tier)

    async def complete(
        self,
        *,
        role: str,
        profile: str,
        phase: str | None = None,
        messages: list[Message],
        deep_tier: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> CompletionResult:
        decision = self.route(role=role, profile=profile, deep_tier=deep_tier)
        provider = self.providers[decision.provider]
        request = CompletionRequest(
            model=decision.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
        )
        previous_cost = self.ledger.snapshot.estimated_cost
        result = await retry_call(lambda: provider.complete(request))
        provider_config = self.config.providers[decision.provider]
        spec = next(
            (model for model in provider_config.models if model.id == decision.model),
            None,
        )
        snapshot = self.ledger.record(result, spec)
        call_cost = (
            snapshot.estimated_cost - previous_cost
            if snapshot.estimated_cost is not None and previous_cost is not None
            else None
        )
        call_record = {
            "call_id": str(uuid4()),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "role": role,
            "phase": phase,
            "profile": profile,
            "deep_tier": deep_tier,
            "provider": result.provider,
            "model": result.model,
            "model_digest": result.model_digest,
            "latency_ms": result.latency_ms,
            "usage": result.usage.__dict__,
            "estimated_cost": snapshot.estimated_cost,
            "call_cost": call_cost,
            "currency": self.config.profiles[profile].budget.currency,
        }
        self.calls.append(call_record)
        if self.trace_sink is not None:
            self.trace_sink(call_record)
        enforce_budget(snapshot, self.config.profiles[profile].budget)
        return result

    async def complete_json(
        self,
        *,
        role: str,
        profile: str,
        phase: str | None = None,
        messages: list[Message],
        deep_tier: str | None = None,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> tuple[dict[str, Any], CompletionResult, CostSnapshot]:
        result: CompletionResult | None = None
        last_raw: dict[str, Any] | None = None
        for attempt in range(4):
            result = await self.complete(
                role=role,
                profile=profile,
                phase=phase,
                messages=messages,
                deep_tier=deep_tier,
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                timeout_seconds=timeout_seconds,
            )
            if result.content.strip():
                break
            last_raw = result.raw
            await asyncio.sleep(0.5 * (attempt + 1))
        if result is None or not result.content.strip():
            raise InvalidResponseError(
                "Provider returned empty content after retries: "
                + json.dumps(last_raw or {}, ensure_ascii=False)[:1200]
            )
        try:
            parsed = parse_json_object(result.content)
        except InvalidResponseError:
            repaired = await self.complete(
                role=role,
                profile=profile,
                phase=phase,
                messages=repair_messages(result.content),
                deep_tier=deep_tier,
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                timeout_seconds=timeout_seconds,
            )
            parsed = parse_json_object(repaired.content)
            result = repaired
        return parsed, result, self.ledger.snapshot
