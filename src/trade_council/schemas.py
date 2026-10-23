"""Shared configuration and extension schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    """Base model that rejects unknown configuration fields."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class AppConfig(StrictModel):
    name: str
    version: str
    run_dir: str = "runs"
    default_mode: Literal["full", "quick", "data"] = "full"
    default_profile: Literal["batch", "deep"] = "batch"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class DataConfig(StrictModel):
    quant_data_root: str = "~/quant-data"
    contract_path: str = "~/quant-data/DATA_CONTRACT.md"
    registry_path: str = "configs/datasets.yaml"
    asof_timezone: str = "Asia/Shanghai"
    default_cutoff: str = "23:59:59"
    news_default_enabled: bool = False
    quant_db_enabled: bool = False
    web_supplement_provider: Literal["http_json", "cninfo"] = "cninfo"
    web_supplement_endpoint: str | None = None
    web_supplement_api_key_env: str = "TRADE_COUNCIL_WEB_SEARCH_KEY"
    web_supplement_timeout_seconds: float = 20.0


class LeadConfig(StrictModel):
    id: str
    type: Literal["code_orchestrator"]


class RoleDefinition(StrictModel):
    id: str
    stage: str
    domains: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("id", "stage")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


class MethodSelection(StrictModel):
    enabled: bool = True


class TeamConfig(StrictModel):
    lead: LeadConfig
    methods: dict[str, MethodSelection] = Field(default_factory=dict)
    roles: list[RoleDefinition] = Field(default_factory=list)


class MethodRequires(StrictModel):
    datasets: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)


class MethodOutputs(StrictModel):
    schema_path: str = Field(default="schema.json", alias="schema")
    evidence_policy: Literal["required", "optional"] = "required"


class AnalysisMethodManifest(StrictModel):
    id: str
    name: str
    version: str
    domain: str
    type: Literal["prompt", "code", "hybrid"]
    enabled: bool = False
    requires: MethodRequires = Field(default_factory=MethodRequires)
    outputs: MethodOutputs = Field(default_factory=MethodOutputs)
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "name", "version", "domain")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


class RequirementResult(StrictModel):
    ok: bool
    missing_datasets: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    message: str = ""


class KeyFinding(StrictModel):
    claim_id: str
    claim: str
    evidence_ids: list[str] = Field(default_factory=list)
    self_reported_confidence: Literal["high", "medium", "low"] = "medium"
    kind: Literal["fact", "inference"] = "inference"
    status: Literal["intact", "weakened", "retracted"] = "intact"


class MethodResult(StrictModel):
    method_id: str
    version: str
    domain: str
    signal: str
    self_reported_confidence: Literal["high", "medium", "low"] = "medium"
    key_findings: list[KeyFinding] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    structured_output: dict[str, Any] = Field(default_factory=dict)


class RoleOutput(StrictModel):
    model_config = ConfigDict(extra="ignore")

    role_id: str
    stance: Literal["bullish", "neutral", "bearish", "mixed", "not_applicable"]
    summary: str
    key_findings: list[KeyFinding] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)


class DecisionPlan(StrictModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["BUY", "SELL", "HOLD", "INSUFFICIENT_DATA"]
    confidence: Literal["high", "medium", "low"]
    rationale: str
    key_points: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)


class TradeProposal(StrictModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["BUY", "SELL", "HOLD", "INSUFFICIENT_DATA"]
    confidence: Literal["high", "medium", "low"]
    entry_zone: str | None = None
    target_price: str | None = None
    stop_loss: str | None = None
    position_pct: float | None = None
    position_shares: int | None = None
    position_amount: float | None = None
    position_opened_date: str | None = None
    position_size_blocked_reason: str | None = None
    rationale: str
    key_points: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)


class FinalDecision(StrictModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["BUY", "SELL", "HOLD", "INSUFFICIENT_DATA"]
    confidence: Literal["high", "medium", "low"]
    risk_level: Literal["high", "medium", "low"]
    rationale: str
    entry_zone: str | None = None
    target_price: str | None = None
    stop_loss: str | None = None
    position_pct: float | None = None
    position_shares: int | None = None
    position_amount: float | None = None
    position_opened_date: str | None = None
    degraded_from_action: str | None = None
    rule_checks: list[str] = Field(default_factory=list)
    key_points: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    capability_flags: dict[str, bool] = Field(default_factory=dict)
    data_gaps: list[str] = Field(default_factory=list)
