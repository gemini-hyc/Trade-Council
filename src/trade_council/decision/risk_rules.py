"""Deterministic decision and tradability checks."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


@dataclass(frozen=True)
class RiskPolicy:
    min_reward_risk: float = 2.0
    max_stop_atr_multiple: float = 2.5
    min_position_pct: float = 0.0
    max_position_pct: float = 20.0
    require_capital_for_position: bool = True


@dataclass(frozen=True)
class TradeContext:
    """Account and position facts supplied by the operator."""

    capital: float | None = None
    position_state: str | None = None
    position_shares: int | None = None
    position_opened_date: str | None = None
    market: str = "CN"
    board: str = "main"

    @property
    def position_size_allowed(self) -> bool:
        return (
            self.capital is not None
            and self.capital > 0
            and self.position_state in {"flat", "holding"}
        )


@dataclass(frozen=True)
class BoardRule:
    t_plus_one: bool = True
    lot_size: int = 100
    step: int = 100


@dataclass(frozen=True)
class DecisionCheck:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    disclosures: list[str] = field(default_factory=list)
    tradability_status: str = "unknown"
    rule_checks: list[str] = field(default_factory=list)


def load_risk_policy(path: Path) -> RiskPolicy:
    if not path.is_file():
        return RiskPolicy()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return RiskPolicy(
        min_reward_risk=float(data.get("min_reward_risk", 2.0)),
        max_stop_atr_multiple=float(data.get("max_stop_atr_multiple", 2.5)),
        min_position_pct=float(data.get("min_position_pct", 0.0)),
        max_position_pct=float(data.get("max_position_pct", 20.0)),
        require_capital_for_position=bool(data.get("require_capital_for_position", True)),
    )


def board_rule_for(
    market_rules_path: Path,
    *,
    market: str = "CN",
    board: str = "main",
) -> BoardRule:
    if not market_rules_path.is_file():
        return BoardRule()
    data = yaml.safe_load(market_rules_path.read_text(encoding="utf-8")) or {}
    market_config = data.get("markets", {}).get(market, {})
    board_config = market_config.get("boards", {}).get(board, {})
    return BoardRule(
        t_plus_one=bool(market_config.get("t_plus_one", True)),
        lot_size=int(board_config.get("lot_size", 100)),
        step=int(board_config.get("step", 100)),
    )


def lot_size_for_board(market_rules_path: Path, *, market: str = "CN", board: str = "main") -> int:
    return board_rule_for(market_rules_path, market=market, board=board).lot_size


def infer_board(code: str) -> str:
    raw = code.strip().upper()
    code6 = raw.split(".")[0] if "." in raw else raw
    code6 = code6.zfill(6)
    if raw.endswith(".BJ") or code6.startswith(("4", "8")):
        return "bj"
    if code6.startswith(("688", "689")):
        return "star"
    return "main"


def validate_trade_context(context: TradeContext, asof: str) -> list[str]:
    errors: list[str] = []
    if context.capital is not None and context.capital <= 0:
        errors.append("capital must be positive")
    if context.position_state not in {None, "flat", "holding"}:
        errors.append("position_state must be flat or holding")
    if context.position_state is None:
        if context.position_shares is not None or context.position_opened_date:
            errors.append("position details require --position-state")
    elif context.position_state == "flat":
        if context.position_shares not in {None, 0}:
            errors.append("flat position must not have positive position_shares")
        if context.position_opened_date:
            errors.append("flat position must not have position_opened_date")
    else:
        if context.position_shares is None or context.position_shares <= 0:
            errors.append("holding position requires positive --position-shares")
        if not context.position_opened_date:
            errors.append("holding position requires --position-opened-date")
    if context.position_opened_date:
        try:
            opened = date.fromisoformat(context.position_opened_date[:10])
            asof_date = date.fromisoformat(asof[:10])
            if opened > asof_date:
                errors.append("position_opened_date cannot be after asof")
        except ValueError:
            errors.append("position_opened_date must be YYYY-MM-DD")
    if context.position_shares is not None and context.position_shares < 0:
        errors.append("position_shares must be non-negative")
    return errors


def t_plus_one_blocked(opened_date: str | None, asof: str, *, enabled: bool = True) -> bool:
    if not enabled or not opened_date:
        return False
    try:
        return date.fromisoformat(opened_date[:10]) >= date.fromisoformat(asof[:10])
    except ValueError:
        return True


def first_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
    return float(match.group(0)) if match else None


def entry_price_for_sizing(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    numbers = re.findall(r"\d+(?:\.\d+)?", value.replace(",", ""))
    if not numbers:
        return None
    return max(float(number) for number in numbers)


def first_int(value: Any) -> int | None:
    number = first_number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def shares_follow_board_rule(shares: int, rule: BoardRule) -> bool:
    if shares < rule.lot_size or rule.step <= 0:
        return False
    return (shares - rule.lot_size) % rule.step == 0


def round_shares_to_board(raw_shares: float, rule: BoardRule) -> int:
    whole = math.floor(raw_shares)
    if whole < rule.lot_size:
        return 0
    return rule.lot_size + ((whole - rule.lot_size) // rule.step) * rule.step


def decision_asof(data_pack_dir: Path, fallback: str | None = None) -> str:
    if fallback:
        return fallback[:10]
    manifest_path = data_pack_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        value = manifest.get("asof")
        if value:
            return str(value)[:10]
    price_path = data_pack_dir / "price_daily.csv"
    if price_path.is_file():
        price = pd.read_csv(price_path)
        if not price.empty and "date" in price:
            return str(price.iloc[-1]["date"])[:10]
    return ""


def enrich_decision_with_trade_context(
    decision: dict[str, Any],
    context: TradeContext,
    market_rules_path: Path,
) -> dict[str, Any]:
    """Fill deterministic shares/amount fields without trusting model arithmetic."""
    result = dict(decision)
    action = result.get("action")
    entry = entry_price_for_sizing(result.get("entry_zone"))
    board_rule = board_rule_for(
        market_rules_path,
        market=context.market,
        board=context.board,
    )
    shares = first_int(result.get("position_shares"))
    pct = first_number(result.get("position_pct"))

    if action == "BUY":
        result["position_opened_date"] = None
        if shares is None and context.position_size_allowed and entry and pct and pct > 0:
            assert context.capital is not None
            raw_shares = context.capital * pct / 100 / entry
            shares = round_shares_to_board(raw_shares, board_rule)
            result["position_shares"] = shares
    elif action == "SELL":
        result["position_opened_date"] = context.position_opened_date
        if shares is None and context.position_shares and pct and pct > 0:
            raw_shares = context.position_shares * pct / 100
            shares = round_shares_to_board(raw_shares, board_rule)
            result["position_shares"] = shares

    if shares is not None and entry is not None:
        result["position_amount"] = round(shares * entry, 2)
    return result


def tradability_status(data_pack_dir: Path) -> str:
    price_path = data_pack_dir / "price_daily.csv"
    if not price_path.is_file():
        return "unknown"
    price = pd.read_csv(price_path)
    if price.empty:
        return "unknown"
    latest = price.iloc[-1]
    date_value = str(latest.get("date", ""))[:10]
    suspend_path = data_pack_dir / "suspend_d.csv"
    if suspend_path.is_file():
        suspended = pd.read_csv(suspend_path)
        if (
            "date" in suspended
            and not suspended.empty
            and pd.to_datetime(suspended["date"], errors="coerce")
            .dt.strftime("%Y-%m-%d")
            .eq(date_value)
            .any()
        ):
            return "suspended"
    limit_path = data_pack_dir / "stk_limit.csv"
    close = float(latest.get("close", "nan"))
    if limit_path.is_file():
        limits = pd.read_csv(limit_path)
        if "date" in limits and not limits.empty:
            limits["date"] = pd.to_datetime(limits["date"], errors="coerce")
            row = limits[limits["date"].dt.strftime("%Y-%m-%d") == date_value]
            if not row.empty:
                up = float(row.iloc[-1].get("up_limit", "nan"))
                down = float(row.iloc[-1].get("down_limit", "nan"))
                if pd.notna(up) and close >= up - 0.005:
                    return "limit_up"
                if pd.notna(down) and close <= down + 0.005:
                    return "limit_down"
    return "tradable"


def validate_decision(
    decision: dict[str, Any],
    data_pack_dir: Path,
    policy: RiskPolicy,
    *,
    allow_trade_instructions: bool,
    trade_context: TradeContext | None = None,
    market_rules_path: Path | None = None,
    asof: str | None = None,
) -> DecisionCheck:
    errors: list[str] = []
    warnings: list[str] = []
    disclosures: list[str] = []
    rule_checks: list[str] = []
    context = trade_context or TradeContext()
    rules_path = market_rules_path or Path("__missing_market_rules__.yaml")
    board_rule = board_rule_for(
        rules_path,
        market=context.market,
        board=context.board,
    )
    action = decision.get("action")
    status = tradability_status(data_pack_dir)
    if action in {"BUY", "SELL"} and not allow_trade_instructions:
        errors.append("profile does not allow executable trade instructions")
        rule_checks.append("risk.profile.trade_instructions")
    if action in {"BUY", "SELL"} and status not in {"tradable", "unknown"}:
        errors.append(f"instrument is not tradable: {status}")
        rule_checks.append("risk.tradability")
    if action == "BUY":
        entry = entry_price_for_sizing(decision.get("entry_zone"))
        stop = first_number(decision.get("stop_loss"))
        target = first_number(decision.get("target_price"))
        if entry is None or stop is None or target is None:
            errors.append("BUY requires numeric entry, stop, and target")
            rule_checks.append("risk.price.required")
        elif not (stop < entry < target):
            errors.append("BUY requires stop < entry < target")
            rule_checks.append("risk.price.order")
        elif abs(target - entry) / abs(entry - stop) < policy.min_reward_risk:
            errors.append("BUY reward/risk ratio is below policy")
            rule_checks.append("risk.rr.min")
    trade_fields = (
        "entry_zone",
        "stop_loss",
        "target_price",
        "position_pct",
        "position_shares",
        "position_amount",
        "position_opened_date",
    )
    if action in {"HOLD", "INSUFFICIENT_DATA"} and any(
        decision.get(field_value) not in (None, "") for field_value in trade_fields
    ):
        errors.append(f"{action} must not include executable trade parameters")
        rule_checks.append("risk.hold.semantics")

    position = decision.get("position_pct")
    position_number = first_number(position)
    if position_number is not None:
        if position_number < policy.min_position_pct:
            errors.append("position_pct is below policy minimum")
            rule_checks.append("risk.position.min_pct")
        if action == "BUY" and position_number > policy.max_position_pct:
            errors.append("position_pct exceeds policy")
            rule_checks.append("risk.position.max_pct")

    shares = first_int(decision.get("position_shares"))
    amount = first_number(decision.get("position_amount"))
    if decision.get("position_shares") is not None and shares is None:
        errors.append("position_shares must be a positive integer")
        rule_checks.append("risk.position.shares")
    if shares is not None:
        if shares <= 0:
            errors.append("position_shares must be positive")
            rule_checks.append("risk.position.shares")
        if not shares_follow_board_rule(shares, board_rule):
            errors.append(
                "position_shares violates board lot/step rule "
                f"(lot_size={board_rule.lot_size}, step={board_rule.step})"
            )
            rule_checks.append("risk.lot.board")
        if context.capital is None:
            errors.append("position_shares requires capital context")
            rule_checks.append("risk.capital.required")
        if context.position_state is None:
            errors.append("position_shares requires position_state context")
            rule_checks.append("risk.position.state")
    if decision.get("position_amount") is not None and amount is None:
        errors.append("position_amount must be numeric")
        rule_checks.append("risk.position.amount")
    if amount is not None:
        if amount <= 0:
            errors.append("position_amount must be positive")
            rule_checks.append("risk.position.amount")
        if shares is None:
            errors.append("position_amount requires position_shares")
            rule_checks.append("risk.position.shares")
        if context.capital is None:
            errors.append("position_amount requires capital context")
            rule_checks.append("risk.capital.required")
        elif action == "BUY":
            max_amount = context.capital * policy.max_position_pct / 100
            entry = entry_price_for_sizing(decision.get("entry_zone"))
            shares_amount = shares * entry if shares is not None and entry is not None else 0
            notional = max(amount, shares_amount)
            if notional > max_amount + 1e-6:
                errors.append("position_amount exceeds capital x position limit")
                rule_checks.append("risk.position.max_amount")

    if shares is not None and amount is not None:
        entry = entry_price_for_sizing(decision.get("entry_zone"))
        if entry is not None and entry > 0:
            expected_amount = shares * entry
            if abs(amount - expected_amount) / expected_amount > 0.02:
                errors.append("position_amount is inconsistent with shares x entry")
                rule_checks.append("risk.position.amount_consistency")

    if action == "SELL":
        if context.position_state != "holding":
            errors.append("SELL requires a holding position context")
            rule_checks.append("risk.position.state")
        if (
            context.position_shares is not None
            and shares is not None
            and shares > context.position_shares
        ):
            errors.append("SELL shares exceed current position")
            rule_checks.append("risk.position.available")
        opened_date = context.position_opened_date or decision.get("position_opened_date")
        if not opened_date:
            errors.append("SELL requires position_opened_date for T+1 validation")
            rule_checks.append("risk.trade.t_plus_one")
        elif t_plus_one_blocked(
            str(opened_date),
            decision_asof(data_pack_dir, asof),
            enabled=board_rule.t_plus_one,
        ):
            errors.append("T+1 blocks same-day sale of the opening position")
            rule_checks.append("risk.trade.t_plus_one")

    if action == "BUY" and decision.get("position_opened_date") not in (None, ""):
        errors.append("BUY must not reuse an existing position_opened_date")
        rule_checks.append("risk.trade.t_plus_one")
    if action == "BUY" and board_rule.t_plus_one and decision.get("stop_loss") not in (None, ""):
        disclosures.append(
            "T+1: a newly bought position cannot be stopped out until the next trading day"
        )
        rule_checks.append("risk.trade.t_plus_one.disclosure")
    if action in {"BUY", "SELL"} and context.capital is None:
        warnings.append("position sizing is disabled because capital is not provided")
    elif action in {"BUY", "SELL"} and position is None:
        warnings.append("position_pct is not provided; deterministic sizing is unavailable")
    if action in {"BUY", "SELL"} and context.position_state is None:
        warnings.append("position state is not provided; executable position sizing is disabled")
    return DecisionCheck(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        disclosures=disclosures,
        tradability_status=status,
        rule_checks=list(dict.fromkeys(rule_checks)),
    )
