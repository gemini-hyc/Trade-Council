"""Configuration models for model providers and profiles."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ..schemas import StrictModel


class ModelSpec(StrictModel):
    id: str
    context_window: int | None = None
    input_price_per_mtok: float | None = None
    output_price_per_mtok: float | None = None
    default_max_tokens: int = 4096


class ProviderConfig(StrictModel):
    kind: Literal["ollama", "deepseek", "openai_compatible"]
    base_url: str
    api_key_env: str
    default_api_key: str | None = None
    enabled: bool = True
    models: list[ModelSpec] = Field(default_factory=list)


class BudgetConfig(StrictModel):
    currency: Literal["USD", "CNY", "NONE"] = "USD"
    max_run_cost: float | None = None
    on_exceed: Literal["abort", "degrade", "warn"] = "warn"


class ProfileConfig(StrictModel):
    display_name: str
    provider: str
    models: dict[str, str] = Field(default_factory=dict)
    debate_rounds: int = Field(default=1, ge=1)
    max_concurrency: int = Field(default=1, ge=1)
    max_tokens: int = Field(default=4096, ge=256)
    request_timeout_seconds: float = Field(default=120.0, gt=0)
    deep_tier: Literal["auto", "flash", "pro"] = "auto"
    allow_flash_fallback: bool = False
    allow_trade_instructions: bool = False
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None
    tier_models: dict[str, str] = Field(default_factory=dict)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)


class CloudPolicy(StrictModel):
    allowed_providers: list[str] = Field(default_factory=list)
    denied_providers: list[str] = Field(default_factory=list)


class ModelsConfig(StrictModel):
    providers: dict[str, ProviderConfig]
    profiles: dict[str, ProfileConfig]
    role_categories: dict[str, str]
    cloud_policy: CloudPolicy = Field(default_factory=CloudPolicy)
