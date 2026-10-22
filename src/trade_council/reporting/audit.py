"""Machine-checkable report gates."""

from __future__ import annotations

import html as html_module
import json
import re
from pathlib import Path
from typing import Any

from ..decision.risk_rules import TradeContext, load_risk_policy, validate_decision


def _attr(tag: str, name: str) -> str | None:
    match = re.search(rf'{name}="([^"]*)"', tag)
    return match.group(1) if match else None


def audit_report(
    html: str,
    *,
    decision: dict[str, Any],
    enabled_domains: list[str],
    role_outputs: dict[str, dict[str, Any]],
    data_gaps: list[str],
    data_pack_dir: Path,
    risk_policy_path: Path,
    allow_trade_instructions: bool,
    trade_context: TradeContext | None = None,
    market_rules_path: Path | None = None,
    asof: str | None = None,
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []

    def add(level: str, message: str) -> None:
        issues.append({"level": level, "message": message})

    if not html.rstrip().lower().endswith("</html>"):
        add("FAIL", "HTML is not closed")
    if "traceable-number" not in html:
        add("FAIL", "No traceable numbers found")
    tags = re.findall(r"<[^>]+>", html)
    decision_fields = {
        value for tag in tags if (value := _attr(tag, "data-decision-field")) is not None
    }
    required_decisions = {"action", "confidence", "risk_level"}
    missing_decisions = sorted(required_decisions - decision_fields)
    if missing_decisions:
        add("FAIL", f"Decision fields missing: {', '.join(missing_decisions)}")
    for field in sorted(required_decisions):
        expected = str(decision.get(field, ""))
        match = re.search(
            rf'data-decision-field="{re.escape(field)}"[^>]*>(.*?)</span>',
            html,
            re.DOTALL,
        )
        if match is None:
            continue
        rendered = html_module.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()
        if rendered != expected:
            add("FAIL", f"Decision field {field} differs from decision.json")
    if decision.get("action") in {"BUY", "SELL"}:
        required_trade_fields = {
            "position_shares",
            "position_amount",
            "position_opened_date",
            "t_plus_one_status",
        }
        missing_trade_fields = sorted(required_trade_fields - decision_fields)
        if missing_trade_fields:
            add("FAIL", f"Trade fields missing: {', '.join(missing_trade_fields)}")

    domains = {
        value for tag in tags if (value := _attr(tag, "data-domain")) is not None
    }
    missing_domains = sorted(set(enabled_domains) - domains)
    if missing_domains:
        add("FAIL", f"Enabled domains missing: {', '.join(missing_domains)}")

    chart_tags = [tag for tag in tags if _attr(tag, "data-chart") is not None]
    if not chart_tags:
        add("FAIL", "No chart elements found")
    for tag in chart_tags:
        points = _attr(tag, "data-points")
        try:
            point_count = int(points or "0")
        except ValueError:
            point_count = 0
        if point_count < 5:
            add("FAIL", f"Chart has fewer than 5 points: {_attr(tag, 'data-chart')}")

    if sum(1 for tag in tags if _attr(tag, "data-reference") is not None) < 2:
        add("FAIL", "Price chart lacks reference lines")
    if "technical" in enabled_domains and not ("Wilder" in html and "SMA" in html):
        add("FAIL", "RSI dual-definition disclosure is missing")
    for role_id in ("bull-researcher", "bear-researcher"):
        if role_id in role_outputs and f'data-role="{role_id}"' not in html:
            add("FAIL", f"Debate section missing: {role_id}")

    gap_markers = len(re.findall(r'data-gap="true"', html))
    if gap_markers != len(data_gaps):
        add("FAIL", f"Data-gap markers {gap_markers} != {len(data_gaps)}")

    traceable_tags = re.findall(r'<span[^>]*class="[^"]*traceable-number[^"]*"[^>]*>', html)
    if any(_attr(tag, "data-source") is None for tag in traceable_tags):
        add("FAIL", "A traceable number has no data-source")
    evidence_path = data_pack_dir / "evidence.json"
    evidence_values: dict[str, Any] = {}
    if evidence_path.is_file():
        evidence_data = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence_values = {
            str(item.get("evidence_id")): item.get("value")
            for item in evidence_data.get("items", [])
        }
    traceable_spans = re.findall(
        r'<span[^>]*class="[^"]*traceable-number[^"]*"[^>]*>(.*?)</span>',
        html,
        re.DOTALL,
    )
    traceable_opens = re.findall(
        r'<span[^>]*class="[^"]*traceable-number[^"]*"[^>]*>',
        html,
    )
    if len(traceable_opens) != len(traceable_spans):
        add("FAIL", "Traceable number markup is malformed")
    for tag, body in zip(traceable_opens, traceable_spans):
        evidence_id = _attr(tag, "data-evidence-id")
        if evidence_id is None:
            continue
        expected_value = evidence_values.get(evidence_id)
        if expected_value is None:
            add("FAIL", f"Traceable number references unknown evidence: {evidence_id}")
            continue
        rendered_value = html_module.unescape(re.sub(r"<[^>]+>", "", body)).strip()
        try:
            actual_number = float(rendered_value)
        except ValueError:
            add("FAIL", f"Traceable evidence number is not numeric: {evidence_id}")
            continue
        try:
            expected_number = float(expected_value)
        except (TypeError, ValueError):
            continue
        tolerance = max(1e-6, abs(expected_number) * 1e-6)
        if abs(actual_number - expected_number) > tolerance:
            add("FAIL", f"Traceable evidence number was tampered: {evidence_id}")

    check = validate_decision(
        decision,
        data_pack_dir,
        load_risk_policy(risk_policy_path),
        allow_trade_instructions=allow_trade_instructions,
        trade_context=trade_context,
        market_rules_path=market_rules_path,
        asof=asof,
    )
    if not check.ok:
        add("FAIL", "Decision consistency: " + "; ".join(check.errors))
    for warning in check.warnings:
        add("WARN", warning)

    fails = [issue for issue in issues if issue["level"] == "FAIL"]
    warns = [issue for issue in issues if issue["level"] == "WARN"]
    return {
        "verdict": "FAIL" if fails else ("WARN" if warns else "PASS"),
        "score": max(0, 100 - 20 * len(fails) - 8 * len(warns)),
        "issues": issues,
        "tradability_status": check.tradability_status,
        "trade_disclosures": check.disclosures,
        "rule_checks": check.rule_checks,
        "traceable_numbers": len(traceable_tags),
        "enabled_domains": enabled_domains,
    }
