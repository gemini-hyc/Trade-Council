"""Versioned research structures: questions, claims, revisions, and decisions.

These models reject unknown fields on purpose. A typo must fail validation
instead of silently dropping a finding.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Priority = Literal["critical", "material", "minor"]
ClaimKind = Literal["fact", "assumption", "inference"]
ClaimStatus = Literal["intact", "weakened", "retracted", "unresolved"]
QuestionStatus = Literal["open", "answered", "unresolved"]
ExecutionStatus = Literal["not_assessed", "blocked", "conditional", "actionable"]
ValuationMethod = Literal[
    "historical_median_multiple", "scenario_earnings", "technical_level", "none"
]


class ResearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class ResearchQuestion(ResearchModel):
    question_id: str
    question: str
    why_it_matters: str
    required_evidence: list[str] = Field(default_factory=list)
    priority: Priority = "material"
    status: QuestionStatus = "open"
    attempted_paths: list[str] = Field(default_factory=list)
    answer: str | None = None
    affects: list[str] = Field(default_factory=list)


class QuestionSet(ResearchModel):
    schema_version: str = "1.0"
    run_id: str
    questions: list[ResearchQuestion] = Field(default_factory=list)


class Claim(ResearchModel):
    claim_id: str
    role_id: str
    revision: int = 1
    kind: ClaimKind = "inference"
    statement: str
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    counter_evidence_ids: list[str] = Field(default_factory=list)
    calc_refs: list[str] = Field(default_factory=list)
    applicable_period: str = ""
    status: ClaimStatus = "intact"
    status_reason: str = ""
    depends_on_claim_ids: list[str] = Field(default_factory=list)


class ClaimSet(ResearchModel):
    schema_version: str = "1.0"
    run_id: str
    revision_id: str = "r1"
    claims: list[Claim] = Field(default_factory=list)


class RevisionRecord(ResearchModel):
    revision_id: str
    created_at: str
    trigger_evidence_ids: list[str] = Field(default_factory=list)
    before: str
    after: str
    affected_claim_ids: list[str] = Field(default_factory=list)
    affected_outputs: list[str] = Field(default_factory=list)
    reviewed: bool = False
    review_note: str = ""


class ExecutionPlan(ResearchModel):
    """Account action, kept separate from the research verdict."""

    status: ExecutionStatus = "not_assessed"
    block_reasons: list[str] = Field(default_factory=list)
    target_account_weight: float | None = None
    sell_fraction_of_current: float | None = None
    cash_amount: float | None = None
    requires: list[str] = Field(default_factory=list)


class ValuationReference(ResearchModel):
    method: ValuationMethod = "none"
    reference_price: float | None = None
    multiple: float | None = None
    window_start: str | None = None
    window_end: str | None = None
    sample_size: int | None = None
    percentile_definition: str | None = None
    inputs: list[str] = Field(default_factory=list)
    note: str = ""


class ResearchDecision(ResearchModel):
    schema_version: str = "1.0"
    run_id: str
    revision_id: str = "r1"
    fundamental_assessment: Literal["positive", "mixed", "negative", "unresolved"]
    valuation_assessment: Literal["attractive", "neutral", "demanding", "unresolved"]
    timing_assessment: Literal["ready", "wait", "invalidated", "unresolved"]
    research_status: Literal["continue", "watch", "reject", "need_evidence"]
    key_claim_ids: list[str] = Field(default_factory=list)
    unresolved_question_ids: list[str] = Field(default_factory=list)
    next_checks: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    execution: ExecutionPlan = Field(default_factory=ExecutionPlan)
    valuation_reference: ValuationReference | None = None
    dividend_treatment: Literal["none", "announced_event", "assumption"] = "none"
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


def normalize_questions(payload: Any) -> QuestionSet:
    if isinstance(payload, dict) and "questions" in payload:
        return QuestionSet.model_validate(payload)
    return QuestionSet.model_validate({"questions": payload})
