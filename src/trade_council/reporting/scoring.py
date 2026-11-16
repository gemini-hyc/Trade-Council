"""Explicit, versioned scorecards.

A composite score may only be rendered when the run supplies its own
dimensions, weights, missing-data handling, and formula. There is no built-in
default score, and the report never invents a fifth "veto" dimension.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ..schemas import StrictModel


class ScoreDimension(StrictModel):
    id: str
    label: str
    weight: float
    score: float | None = None
    basis: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class Scorecard(StrictModel):
    schema_version: str = "1.0"
    formula: str
    scale_max: float
    missing_handling: Literal["exclude_and_renormalise", "fail"]
    dimensions: list[ScoreDimension]
    source: str = ""

    def computed(self) -> dict[str, Any]:
        usable = [item for item in self.dimensions if item.score is not None]
        missing = [item.id for item in self.dimensions if item.score is None]
        if missing and self.missing_handling == "fail":
            return {
                "status": "unavailable",
                "total": None,
                "missing": missing,
                "reason": f"评分维度缺值且策略为 fail：{', '.join(missing)}",
            }
        total_weight = sum(item.weight for item in usable)
        if not usable or total_weight <= 0:
            return {
                "status": "unavailable",
                "total": None,
                "missing": missing,
                "reason": "没有可用评分维度，不能生成综合评分",
            }
        total = sum((item.score or 0.0) * item.weight for item in usable) / total_weight
        return {
            "status": "ok" if not missing else "partial",
            "total": total,
            "missing": missing,
            "renormalised_weight": total_weight,
            "usable_dimensions": [item.id for item in usable],
        }


def load_scorecard(path: Path) -> Scorecard | None:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Scorecard.model_validate(raw)
