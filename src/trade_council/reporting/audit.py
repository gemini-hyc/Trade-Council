"""Machine-checkable report gates."""

from __future__ import annotations

import html as html_module
import json
import re
from pathlib import Path
from typing import Any

from ..dates import is_after
from ..decision.risk_rules import TradeContext, load_risk_policy, validate_decision
from ..engineering.evidence import infer_value_type


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
    evidence_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check structure, numbers, and decision consistency.

    The result deliberately carries no composite score: a 100-point "audit
    score" would suggest research quality that this checker cannot establish.
    Layers state what was checked and what remains outside program control.
    """

    issues: list[dict[str, str]] = []
    layers: dict[str, dict[str, Any]] = {
        "data_references": {"checks": 0, "fail": 0, "scope": "哈希、时点、单位、证据ID、计算血缘"},
        "calculation": {"checks": 0, "fail": 0, "scope": "季度差分、桥接、比例与路径闭合"},
        "decision_text": {"checks": 0, "fail": 0, "scope": "价格、动作、规则、期限与结构化结果一致"},
        "research_review": {
            "checks": 0,
            "fail": 0,
            "scope": "关键反证、假设与因果跳跃由 agent 自述，程序不能证明其正确",
        },
    }

    def add(level: str, message: str, layer: str = "decision_text") -> None:
        issues.append({"level": level, "message": message, "layer": layer})
        layers[layer]["checks"] += 1
        if level == "FAIL":
            layers[layer]["fail"] += 1

    def expect(message: str, layer: str, ok: bool) -> None:
        layers[layer]["checks"] += 1
        if not ok:
            add("FAIL", message, layer)

    expect("HTML is not closed", "decision_text", html.rstrip().lower().endswith("</html>"))
    expect("No traceable numbers found", "data_references", "traceable-number" in html)
    tags = re.findall(r"<[^>]+>", html)
    decision_fields = {
        value for tag in tags if (value := _attr(tag, "data-decision-field")) is not None
    }
    required_decisions = {"action", "confidence", "risk_level"}
    missing_decisions = sorted(required_decisions - decision_fields)
    expect(
        f"Decision fields missing: {', '.join(missing_decisions)}",
        "decision_text",
        not missing_decisions,
    )
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
        expect(
            f"Decision field {field} differs from decision.json",
            "decision_text",
            rendered == expected,
        )
    if decision.get("action") in {"BUY", "SELL"}:
        required_trade_fields = {
            "position_shares",
            "position_amount",
            "position_opened_date",
            "t_plus_one_status",
        }
        missing_trade_fields = sorted(required_trade_fields - decision_fields)
        expect(
            f"Trade fields missing: {', '.join(missing_trade_fields)}",
            "decision_text",
            not missing_trade_fields,
        )

    domains = {
        value for tag in tags if (value := _attr(tag, "data-domain")) is not None
    }
    missing_domains = sorted(set(enabled_domains) - domains)
    expect(
        f"Enabled domains missing: {', '.join(missing_domains)}",
        "decision_text",
        not missing_domains,
    )

    chart_tags = [tag for tag in tags if _attr(tag, "data-chart") is not None]
    expect("No chart elements found", "calculation", bool(chart_tags))
    for tag in chart_tags:
        points = _attr(tag, "data-points")
        try:
            point_count = int(points or "0")
        except ValueError:
            point_count = 0
        chart_kind = _attr(tag, "data-chart")
        # A radar needs at least three dimensions to be readable; a time-series
        # line needs at least five observations. Two-point "charts" are noise.
        minimum = 3 if chart_kind == "scorecard" else 5
        expect(
            f"Chart has fewer than {minimum} points: {chart_kind}",
            "calculation",
            point_count >= minimum,
        )

    expect(
        "Price chart lacks reference lines",
        "calculation",
        sum(1 for tag in tags if _attr(tag, "data-reference") is not None) >= 2,
    )
    if "technical" in enabled_domains:
        expect(
            "RSI dual-definition disclosure is missing",
            "calculation",
            "Wilder" in html and "SMA" in html,
        )
    for role_id in ("bull-researcher", "bear-researcher"):
        if role_id in role_outputs:
            expect(
                f"Debate section missing: {role_id}",
                "decision_text",
                f'data-role="{role_id}"' in html,
            )

    gap_markers = len(re.findall(r'data-gap="true"', html))
    expect(
        f"Data-gap markers {gap_markers} != {len(data_gaps)}",
        "decision_text",
        gap_markers == len(data_gaps),
    )

    traceable_tags = re.findall(r'<span[^>]*class="[^"]*traceable-number[^"]*"[^>]*>', html)
    expect(
        "A traceable number has no data-source",
        "data_references",
        not any(_attr(tag, "data-source") is None for tag in traceable_tags),
    )
    evidence_path = data_pack_dir / "evidence.json"
    evidence_values: dict[str, Any] = {}
    evidence_records: dict[str, dict[str, Any]] = {}
    if evidence_items is not None:
        evidence_values = {
            str(item.get("evidence_id")): item.get("value")
            for item in evidence_items
        }
        evidence_records = {
            str(item.get("evidence_id")): item
            for item in evidence_items
        }
    elif evidence_path.is_file():
        evidence_data = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence_values = {
            str(item.get("evidence_id")): item.get("value")
            for item in evidence_data.get("items", [])
        }
        evidence_records = {
            str(item.get("evidence_id")): item
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
    expect(
        "Traceable number markup is malformed",
        "data_references",
        len(traceable_opens) == len(traceable_spans),
    )
    for web_evidence_id, evidence_record in evidence_records.items():
        if evidence_record.get("source_level") == "web":
            expect(
                f"Web evidence has no source_url: {web_evidence_id}",
                "data_references",
                bool(evidence_record.get("source_url")),
            )
    for tag, body in zip(traceable_opens, traceable_spans):
        evidence_id = _attr(tag, "data-evidence-id")
        if evidence_id is None:
            continue
        expected_value = evidence_values.get(evidence_id)
        if expected_value is None:
            add(
                "FAIL",
                f"Traceable number references unknown evidence: {evidence_id}",
                "data_references",
            )
            continue
        rendered_value = html_module.unescape(re.sub(r"<[^>]+>", "", body)).strip()
        try:
            actual_number = float(rendered_value)
        except ValueError:
            add(
                "FAIL",
                f"Traceable evidence number is not numeric: {evidence_id}",
                "data_references",
            )
            continue
        try:
            expected_number = float(expected_value)
        except (TypeError, ValueError):
            add(
                "FAIL",
                f"Non-numeric evidence rendered as a number: {evidence_id}",
                "data_references",
            )
            continue
        tolerance = max(1e-6, abs(expected_number) * 1e-6)
        expect(
            f"Traceable evidence number was tampered: {evidence_id}",
            "data_references",
            abs(actual_number - expected_number) <= tolerance,
        )

    # Text/event evidence must be rendered as text, never as a numeric cell.
    text_items = [
        item
        for item in evidence_records.values()
        if str(item.get("value_type") or infer_value_type(item.get("value"))) != "number"
        and item.get("value") not in (None, "")
    ]
    text_spans = len(re.findall(r'class="[^"]*evidence-text[^"]*"', html))
    expect(
        f"Text evidence rendered as numbers: spans {text_spans} != items {len(text_items)}",
        "data_references",
        text_spans == len(text_items),
    )

    # Point-in-time: no evidence may be available after the run's as-of date.
    if asof:
        late = [
            str(item.get("evidence_id"))
            for item in evidence_records.values()
            if is_after(item.get("available_at"), asof)
        ]
        expect(
            "Evidence available after as-of: " + ", ".join(late[:5]),
            "data_references",
            not late,
        )

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
        add("FAIL", "Decision consistency: " + "; ".join(check.errors), "decision_text")
    for warning in check.warnings:
        add("WARN", warning, "decision_text")

    fails = [issue for issue in issues if issue["level"] == "FAIL"]
    warns = [issue for issue in issues if issue["level"] == "WARN"]
    return {
        "verdict": "FAIL" if fails else ("WARN" if warns else "PASS"),
        "issues": issues,
        "layers": layers,
        "scope_note": (
            "程序只校验结构、时点、数值与可计算关系；它不能证明推理正确、"
            "预测必然实现，也不构成数据供应商完整性保证。"
        ),
        "tradability_status": check.tradability_status,
        "trade_disclosures": check.disclosures,
        "rule_checks": check.rule_checks,
        "traceable_numbers": len(traceable_tags),
        "text_evidence": len(text_items),
        "enabled_domains": enabled_domains,
    }
