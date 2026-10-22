from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

import pytest

from trade_council.config import load_project_config
from trade_council.errors import ConfigError, ProviderUnavailableError
from trade_council.llm.base import (
    CompletionRequest,
    CompletionResult,
    Message,
    ModelInfo,
    ProviderStatus,
    Usage,
)
from trade_council.llm.client import LLMClient
from trade_council.llm.cost import CostLedger, enforce_budget
from trade_council.llm.json_output import parse_json_object
from trade_council.llm.models import BudgetConfig, ModelSpec, ProfileConfig, ProviderConfig
from trade_council.llm.prompts import load_prompt
from trade_council.llm.retry import retry_call
from trade_council.llm.router import ModelRouter

ROOT = Path(__file__).resolve().parents[1]


def test_model_routing_batch_and_deep() -> None:
    config = load_project_config(ROOT).models
    router = ModelRouter(config)
    assert router.route(role="market-analyst", profile_name="batch").model == "qwen2.5:14b"
    assert router.route(
        role="news-analyst", profile_name="deep", deep_tier="auto"
    ).model == "deepseek-flash"
    assert router.route(
        role="risk-manager", profile_name="deep", deep_tier="auto"
    ).model == "deepseek-v4-pro"
    assert router.route(
        role="risk-manager", profile_name="deep", deep_tier="flash"
    ).model == "deepseek-flash"


def test_denied_cloud_provider_is_rejected() -> None:
    config = load_project_config(ROOT).models.model_copy(deep=True)
    config.providers["glm"] = ProviderConfig(
        kind="openai_compatible",
        base_url="https://example.invalid",
        api_key_env="GLM_KEY",
        models=[],
    )
    config.profiles["bad"] = ProfileConfig(
        display_name="bad",
        provider="glm",
        models={"analysis": "glm"},
    )
    with pytest.raises(ConfigError, match="denied"):
        ModelRouter(config).route(role="market-analyst", profile_name="bad")


def test_json_parser_accepts_fenced_object() -> None:
    assert parse_json_object('```json\n{"ok": true}\n```') == {"ok": True}
    with pytest.raises(Exception, match="JSON"):
        parse_json_object("not json")


def test_prompt_hash_is_stable(tmp_path: Path) -> None:
    path = tmp_path / "role.md"
    path.write_text("role prompt\n", encoding="utf-8")
    first = load_prompt(path)
    second = load_prompt(path)
    assert first.sha256 == second.sha256


def test_cost_budget_abort() -> None:
    ledger = CostLedger()
    result = CompletionResult(
        provider="deepseek",
        model="deepseek-v4-pro",
        content="ok",
        usage=Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000),
    )
    spec = ModelSpec(
        id="deepseek-v4-pro",
        input_price_per_mtok=1.0,
        output_price_per_mtok=2.0,
    )
    snapshot = ledger.record(result, spec)
    with pytest.raises(Exception, match="max_run_cost"):
        enforce_budget(snapshot, BudgetConfig(max_run_cost=0.0001, on_exceed="abort"))


def test_unknown_pricing_is_not_reported_as_zero() -> None:
    ledger = CostLedger()
    result = CompletionResult(
        provider="deepseek",
        model="deepseek-flash",
        content="ok",
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )
    spec = ModelSpec(id="deepseek-flash")
    snapshot = ledger.record(result, spec)
    assert snapshot.estimated_cost is None
    assert snapshot.unknown_pricing_calls == 1


def test_retry_retries_transport_error() -> None:
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ProviderUnavailableError("temporary")
        return "ok"

    assert asyncio.run(retry_call(flaky, attempts=3, base_delay=0)) == "ok"
    assert calls["n"] == 3


class FakeProvider:
    name = "fake"
    model_specs: ClassVar[dict[str, Any]] = {}

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls += 1
        content = "not json" if self.calls == 1 else '{"ok": true}'
        return CompletionResult(
            provider="fake",
            model=request.model,
            content=content,
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="qwen3.8:27b")]

    async def preflight(self) -> ProviderStatus:
        return ProviderStatus(self.name, True, [])


def test_client_repairs_invalid_json() -> None:
    config = load_project_config(ROOT).models
    fake = FakeProvider()
    client = LLMClient(config, providers={"ollama": fake, "deepseek": fake})
    parsed, result, snapshot = asyncio.run(
        client.complete_json(
            role="market-analyst",
            profile="batch",
            messages=[Message(role="user", content="return json")],
        )
    )
    assert parsed == {"ok": True}
    assert result.model == "qwen2.5:14b"
    assert fake.calls == 2
    assert snapshot.calls == 2
    assert all(call["call_cost"] == 0 for call in client.calls)
    assert all(call["currency"] == "CNY" for call in client.calls)
