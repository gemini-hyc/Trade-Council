"""Structured trading rules and sell tranches.

One rule id must mean exactly one thing: same indicator, same threshold, same
price basis, same action, same denominator. Conflicting duplicates are rejected
instead of being resolved by document order.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ..schemas import StrictModel

Indicator = Literal["close", "ma", "atr", "volume", "amount", "limit_state", "custom"]
RuleAction = Literal["buy", "add", "reduce", "exit", "hold", "watch"]
ObservationFrequency = Literal["daily_close", "intraday_14_50", "weekly_close"]
DataFrequency = Literal["daily", "intraday"]
RatioBasis = Literal["account_weight", "fraction_of_current", "none"]
TrancheBasis = Literal["initial_position", "remaining_position"]

EXECUTABLE_ACTIONS = {"buy", "add", "reduce", "exit"}


class TradingRule(StrictModel):
    rule_id: str
    indicator: Indicator
    metric: str
    threshold: str
    price_basis: Literal["unadjusted", "forward_adjusted"]
    action: RuleAction
    observation_frequency: ObservationFrequency = "daily_close"
    data_frequency: DataFrequency = "daily"
    verified: bool = False
    ratio_basis: RatioBasis = "none"
    ratio: float | None = None
    priority: int = 100
    valid_from: str | None = None
    valid_until: str | None = None
    invalidation: str = ""

    def signature(self) -> tuple[Any, ...]:
        return (
            self.indicator,
            self.metric,
            self.threshold,
            self.price_basis,
            self.action,
            self.observation_frequency,
            self.ratio_basis,
            self.ratio,
        )


class SellTranche(StrictModel):
    tranche_id: str
    fraction: float
    basis: TrancheBasis = "initial_position"


class RuleTree(StrictModel):
    schema_version: str = "1.0"
    rules: list[TradingRule] = Field(default_factory=list)
    sell_tranches: list[SellTranche] = Field(default_factory=list)


def load_rule_tree(path: Path) -> RuleTree | None:
    if not path.is_file():
        return None
    return RuleTree.model_validate(json.loads(path.read_text(encoding="utf-8")))


def validate_rule_tree(
    tree: RuleTree,
    *,
    available_data: DataFrequency = "daily",
) -> tuple[list[str], list[str]]:
    """Return ``(errors, notes)``; errors mean the tree cannot drive an action."""

    errors: list[str] = []
    notes: list[str] = []

    seen: dict[str, tuple[Any, ...]] = {}
    for rule in tree.rules:
        if rule.rule_id in seen:
            if seen[rule.rule_id] != rule.signature():
                errors.append(
                    f"RULE_CONFLICT: {rule.rule_id} 被定义了两种不同的指标/阈值/动作，"
                    "同一 rule_id 只能有一个定义"
                )
            else:
                errors.append(f"RULE_DUPLICATED: {rule.rule_id} 重复定义")
            continue
        seen[rule.rule_id] = rule.signature()
        if rule.indicator == "volume" and "成交额" in rule.metric:
            errors.append(
                f"INDICATOR_MISMATCH: {rule.rule_id} 指标写成交量但口径是成交额，两者不能混用"
            )
        if rule.indicator == "amount" and "成交量" in rule.metric:
            errors.append(
                f"INDICATOR_MISMATCH: {rule.rule_id} 指标写成交额但口径是成交量，两者不能混用"
            )
        if rule.observation_frequency == "intraday_14_50" and available_data != "intraday":
            if rule.action in EXECUTABLE_ACTIONS:
                errors.append(
                    f"RULE_NEEDS_INTRADAY: {rule.rule_id} 需要 14:50 分时确认，"
                    "日线数据不能当作已验证触发"
                )
            else:
                notes.append(
                    f"RULE_UNVERIFIED_INTRADAY: {rule.rule_id} 依赖分时观测，"
                    "本次只有日线数据，仅作观察"
                )

    tranches = tree.sell_tranches
    if tranches:
        initial = [item for item in tranches if item.basis == "initial_position"]
        if initial:
            total = sum(item.fraction for item in initial)
            if total > 1 + 1e-9:
                errors.append(
                    f"TRANCHE_OVERFLOW: 按初始持仓计的减仓比例合计 {total:.2%}，超过 100%"
                )
        remaining = 1.0
        for item in tranches:
            if item.basis == "remaining_position":
                remaining *= 1 - item.fraction
        notes.append(f"按剩余持仓递进减仓后仍持有初始仓位的 {remaining:.2%}")
    return errors, notes
