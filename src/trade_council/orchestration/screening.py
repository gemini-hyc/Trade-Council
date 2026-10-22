"""Deterministic screening output used by quick mode."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ..engineering.evidence import EvidenceIndex
from ..schemas import StrictModel


class ScreeningResult(StrictModel):
    kind: Literal["screening_result"] = "screening_result"
    direction_bias: Literal["positive", "neutral", "negative", "insufficient"]
    evidence_ids: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    trade_parameters_allowed: bool = False


def make_screening_result(evidence: EvidenceIndex) -> ScreeningResult:
    by_field = {item.field: item for item in evidence.all() if item.field}
    required = ("adj_close", "ma20", "ma60", "rsi14")
    missing = [field for field in required if field not in by_field]
    if missing:
        return ScreeningResult(
            direction_bias="insufficient",
            data_gaps=[f"missing evidence fields: {', '.join(missing)}"],
        )

    close = float(by_field["adj_close"].value)
    ma20 = float(by_field["ma20"].value)
    ma60 = float(by_field["ma60"].value)
    rsi = float(by_field["rsi14"].value)
    score = 0
    if close > ma20:
        score += 1
    elif close < ma20:
        score -= 1
    if ma20 > ma60:
        score += 1
    elif ma20 < ma60:
        score -= 1
    if 40 <= rsi <= 70:
        score += 1
    elif rsi < 30:
        score -= 1

    evidence_ids = [
        by_field[field].evidence_id for field in ("adj_close", "ma20", "ma60", "rsi14")
    ]
    if score >= 2:
        direction: Literal["positive", "neutral", "negative", "insufficient"] = "positive"
    elif score <= -2:
        direction = "negative"
    else:
        direction = "neutral"
    return ScreeningResult(direction_bias=direction, evidence_ids=evidence_ids)

