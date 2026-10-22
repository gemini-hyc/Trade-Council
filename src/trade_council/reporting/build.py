"""Render reports from completed workflow artifacts."""

from __future__ import annotations

import html as html_module
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..decision.risk_rules import BoardRule, TradeContext, board_rule_for, t_plus_one_blocked
from .audit import audit_report

DOMAIN_LABELS = {
    "technical": "技术面",
    "fundamental": "基本面",
    "news": "新闻/催化",
    "announcement": "公告",
    "sentiment": "资金/情绪",
    "risk": "风险",
}


def _trade_context_from_run(run: dict[str, Any]) -> TradeContext:
    raw = run.get("trade_context") or {}
    return TradeContext(
        capital=raw.get("capital"),
        position_state=raw.get("position_state"),
        position_shares=raw.get("position_shares"),
        position_opened_date=raw.get("position_opened_date"),
        market=str(raw.get("market", "CN")),
        board=str(raw.get("board", "main")),
    )


def _display_value(value: Any, suffix: str = "") -> str:
    if value is None or value == "":
        return "未提供"
    return f"{value}{suffix}"


def _t_plus_status(
    decision: dict[str, Any],
    context: TradeContext,
    board_rule: BoardRule,
    asof: str,
) -> str:
    if decision.get("action") == "SELL":
        if not context.position_opened_date:
            return "UNKNOWN"
        if t_plus_one_blocked(
            context.position_opened_date,
            asof,
            enabled=board_rule.t_plus_one,
        ):
            return "BLOCKED"
        return "PASS"
    if decision.get("action") == "BUY" and board_rule.t_plus_one:
        return "BUY: 当日不可卖出，止损最早于次一交易日生效"
    return "NOT_APPLICABLE"


def _read_roles(run_dir: Path) -> dict[str, dict[str, Any]]:
    roles: dict[str, dict[str, Any]] = {}
    for path in sorted((run_dir / "agents").glob("phase*/*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        roles[path.stem] = data
    return roles


def _enabled_domains(roles: dict[str, dict[str, Any]]) -> list[str]:
    domains = ["risk"]
    if "market-analyst" in roles:
        domains.append("technical")
    if "fundamentals-analyst" in roles:
        domains.append("fundamental")
    if "news-analyst" in roles:
        domains.append("news")
    if "sentiment-analyst" in roles:
        domains.append("sentiment")
    if "announcements" in roles:
        domains.append("announcement")
    return sorted(set(domains))


def _format_role(role: dict[str, Any]) -> str:
    if "summary" in role:
        return str(role["summary"])
    return str(role.get("rationale", ""))


def _role_section(role_id: str, role: dict[str, Any], domain: str) -> str:
    findings = "".join(
        f"<li><strong>{html_module.escape(str(item.get('claim_id', 'claim')))}</strong>: "
        f"{html_module.escape(str(item.get('claim', '')))} "
        f"<code>{html_module.escape(', '.join(item.get('evidence_ids', [])))}</code></li>"
        for item in role.get("key_findings", [])
    )
    return (
        f'<section data-role="{role_id}" data-domain="{domain}">'
        f"<h2>{DOMAIN_LABELS.get(domain, domain)} · {html_module.escape(role_id)}</h2>"
        f"<p>{html_module.escape(_format_role(role))}</p>"
        f"<ul>{findings or '<li>无结构化关键发现</li>'}</ul></section>"
    )


def _price_svg(datapack_dir: Path) -> tuple[str, int, int]:
    path = datapack_dir / "price_daily.csv"
    if not path.is_file():
        return '<div class="missing">价格图不可用</div>', 0, 0
    frame = pd.read_csv(path).tail(60)
    if len(frame) < 5:
        return '<div class="missing">价格数据不足</div>', 0, 0
    width, height, pad = 860, 230, 24
    values = frame["close"].astype(float).tolist()
    low, high = min(values), max(values)
    span = high - low or 1
    points = []
    for index, value in enumerate(values):
        x = pad + index * (width - 2 * pad) / (len(values) - 1)
        y = height - pad - (value - low) / span * (height - 2 * pad)
        points.append(f"{x:.1f},{y:.1f}")
    refs = 0
    ref_markup = []
    for column, color in (("ma20", "#2563eb"), ("ma60", "#9333ea")):
        if column not in frame or frame[column].dropna().empty:
            continue
        value = float(frame[column].dropna().iloc[-1])
        y = height - pad - (value - low) / span * (height - 2 * pad)
        ref_markup.append(
            f'<line data-reference="{column}" x1="{pad}" y1="{y:.1f}" '
            f'x2="{width-pad}" y2="{y:.1f}" stroke="{color}" stroke-dasharray="5 4"/>'
        )
        refs += 1
    svg = (
        f'<svg data-chart="price" data-points="{len(points)}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="近60日收盘价">'
        f'<rect width="{width}" height="{height}" fill="#f8fafc"/>'
        + "".join(ref_markup)
        + f'<polyline points="{" ".join(points)}" fill="none" stroke="#0f172a" stroke-width="2"/>'
        "</svg>"
    )
    return svg, len(points), refs


def _risk_svg(roles: dict[str, dict[str, Any]]) -> str:
    labels = ["技术", "基本面", "情绪", "风险", "裁决"]
    values = [3, 3, 3, 3, 3]
    for index, role_id in enumerate(("market-analyst", "fundamentals-analyst", "sentiment-analyst")):
        score = roles.get(role_id, {}).get("scores", {})
        if score:
            values[index] = min(5, max(1, int(sum(float(v) for v in score.values()) / len(score) / 2)))
    if "risk-manager" in roles:
        values[4] = 4
    points = []
    for index, value in enumerate(values):
        angle = -1.5708 + index * 2 * 3.14159 / len(values)
        radius = 30 + value * 10
        points.append(f"{100 + radius * __import__('math').cos(angle):.1f},{100 + radius * __import__('math').sin(angle):.1f}")
    return (
        f'<svg data-chart="risk" data-points="{len(labels)}" viewBox="0 0 200 200" '
        'role="img" aria-label="综合风险雷达">'
        '<circle cx="100" cy="100" r="80" fill="#eff6ff" stroke="#bfdbfe"/>'
        f'<polygon points="{" ".join(points)}" fill="#93c5fd" fill-opacity="0.7" stroke="#2563eb"/>'
        "</svg>"
    )


def _format_number(value: Any, source: str) -> str:
    evidence_attr = (
        f' data-evidence-id="{html_module.escape(source)}"'
        if source.startswith("F-")
        else ""
    )
    return (
        f'<span class="traceable-number" data-source="{html_module.escape(source)}"'
        f"{evidence_attr}>"
        f"{html_module.escape(str(value))}</span>"
    )


def render_markdown(
    *,
    run: dict[str, Any],
    decision: dict[str, Any],
    roles: dict[str, dict[str, Any]],
    evidence: dict[str, Any],
    market_rules_path: Path,
) -> str:
    context = _trade_context_from_run(run)
    board_rule = board_rule_for(
        market_rules_path,
        market=context.market,
        board=context.board,
    )
    t_plus_status = _t_plus_status(
        decision,
        context,
        board_rule,
        str(run.get("asof", "")),
    )
    lines = [
        f"# Trade-Council 分析摘要 · {run.get('symbol', '')}",
        "",
        f"- 基准日：{run.get('asof', '')}",
        f"- 模式：{run.get('mode')} / {run.get('profile')}",
        f"- 最终决策：**{decision.get('action', 'INSUFFICIENT_DATA')}**",
        f"- 信心：{decision.get('confidence', 'low')}",
        f"- 风险等级：{decision.get('risk_level', 'medium')}",
        f"- 账户资金：{_display_value(context.capital)}",
        f"- 持仓状态：{_display_value(context.position_state)}",
        f"- 当前持仓股数：{_display_value(context.position_shares)}",
        f"- 建仓日期：{_display_value(context.position_opened_date)}",
        f"- 板块/申报单位：{context.board} / lot={board_rule.lot_size}, step={board_rule.step}",
        f"- 拟交易股数：{_display_value(decision.get('position_shares'))}",
        f"- 拟交易金额：{_display_value(decision.get('position_amount'))}",
        f"- T+1 状态：{t_plus_status}",
        "",
    ]
    if decision.get("rationale"):
        lines.extend(["## 核心结论", "", str(decision["rationale"]), ""])
    for role_id in ("bull-researcher", "bear-researcher", "risk-manager"):
        if role_id in roles:
            lines.extend([f"## {role_id}", "", _format_role(roles[role_id]), ""])
    gaps = decision.get("data_gaps", [])
    if gaps:
        lines.extend(["## 数据缺口", ""])
        lines.extend(f"- {gap}" for gap in gaps)
        lines.append("")
    rule_checks = decision.get("rule_checks", [])
    if rule_checks:
        lines.extend(["## 风控规则命中", ""])
        lines.extend(f"- `{rule_id}`" for rule_id in rule_checks)
        lines.append("")
    lines.extend(
        [
            "## 证据",
            "",
            f"- 证据条数：{len(evidence.get('items', []))}",
            f"- Snapshot：`{evidence.get('snapshot_id', '')}`",
            "",
            "> 以上内容仅供研究参考，不构成投资建议。",
        ]
    )
    return "\n".join(lines)


def render_html(
    *,
    run: dict[str, Any],
    decision: dict[str, Any],
    roles: dict[str, dict[str, Any]],
    evidence: dict[str, Any],
    datapack_dir: Path,
    enabled_domains: list[str],
    market_rules_path: Path,
) -> tuple[str, int, int]:
    context = _trade_context_from_run(run)
    board_rule = board_rule_for(
        market_rules_path,
        market=context.market,
        board=context.board,
    )
    t_plus_status = _t_plus_status(
        decision,
        context,
        board_rule,
        str(run.get("asof", "")),
    )
    price_svg, price_points, refs = _price_svg(datapack_dir)
    risk_svg = _risk_svg(roles)
    action = html_module.escape(str(decision.get("action", "INSUFFICIENT_DATA")))
    confidence = html_module.escape(str(decision.get("confidence", "low")))
    risk_level = html_module.escape(str(decision.get("risk_level", "medium")))
    action_color = {"BUY": "#15803d", "SELL": "#b91c1c", "HOLD": "#a16207", "INSUFFICIENT_DATA": "#475569"}.get(
        str(decision.get("action")), "#475569"
    )
    domain_sections = []
    role_domain = {
        "market-analyst": "technical",
        "fundamentals-analyst": "fundamental",
        "news-analyst": "news",
        "sentiment-analyst": "sentiment",
    }
    for role_id, domain in role_domain.items():
        if role_id in roles:
            domain_sections.append(_role_section(role_id, roles[role_id], domain))
    # An announcement section is represented by the news/announcement role if present.
    domain_tags = " ".join(f'<span class="domain-pill" data-domain="{domain}">{DOMAIN_LABELS.get(domain, domain)}</span>' for domain in enabled_domains)
    gaps = decision.get("data_gaps", [])
    gap_html = "".join(
        f'<li data-gap="true">{html_module.escape(str(gap))}</li>' for gap in gaps
    ) or "<li>无已记录缺口</li>"
    rule_html = "".join(
        f"<li><code>{html_module.escape(str(rule_id))}</code></li>"
        for rule_id in decision.get("rule_checks", [])
    ) or "<li>无规则命中</li>"
    debate_html = "".join(
        f'<section data-role="{role_id}"><h2>{role_id}</h2><p>{html_module.escape(_format_role(roles[role_id]))}</p></section>'
        for role_id in ("bull-researcher", "bear-researcher")
        if role_id in roles
    )
    risk_html = ""
    if "risk-manager" in roles:
        risk_html = _role_section("risk-manager", roles["risk-manager"], "risk")
    trade_context_html = f"""
<section class="card" data-trade-context="true">
<h2>账户、持仓与交易制度</h2>
<table><tbody>
<tr><th>账户资金</th><td>{_format_number(_display_value(context.capital), "trade_context.capital")}</td></tr>
<tr><th>持仓状态</th><td>{html_module.escape(_display_value(context.position_state))}</td></tr>
<tr><th>当前持仓股数</th><td>{_format_number(_display_value(context.position_shares), "trade_context.position_shares")}</td></tr>
<tr><th>建仓日期</th><td>{html_module.escape(_display_value(context.position_opened_date))}</td></tr>
<tr><th>板块 / 申报单位</th><td>{html_module.escape(context.board)} / lot={board_rule.lot_size}, step={board_rule.step}</td></tr>
<tr><th>拟交易股数</th><td><span data-decision-field="position_shares">{_format_number(_display_value(decision.get("position_shares")), "decision.position_shares")}</span></td></tr>
<tr><th>拟交易金额</th><td><span data-decision-field="position_amount">{_format_number(_display_value(decision.get("position_amount")), "decision.position_shares*entry_zone")}</span></td></tr>
<tr><th>交易对应建仓日期</th><td><span data-decision-field="position_opened_date">{html_module.escape(_display_value(decision.get("position_opened_date")))}</span></td></tr>
<tr><th>T+1 状态</th><td><span data-decision-field="t_plus_one_status">{html_module.escape(t_plus_status)}</span></td></tr>
</tbody></table>
</section>
"""
    evidence_rows = "".join(
        "<tr>"
        f"<td><code>{html_module.escape(str(item.get('evidence_id')))}</code></td>"
        f"<td>{html_module.escape(str(item.get('dataset_id')))}</td>"
        f"<td>{html_module.escape(str(item.get('field', '—')))}</td>"
        f"<td>{_format_number(item.get('value'), str(item.get('evidence_id')))} "
        f"{html_module.escape(str(item.get('unit', '')))}</td>"
        f"<td>{html_module.escape(str(item.get('available_at', '—')))}</td>"
        "</tr>"
        for item in evidence.get("items", [])
    )
    document = f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trade-Council · {html_module.escape(str(run.get('symbol', '')))}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;background:#f1f5f9;color:#0f172a;margin:0;padding:24px}}
main{{max-width:1080px;margin:auto}} .card{{background:white;border-radius:14px;padding:20px;margin:16px 0;box-shadow:0 2px 10px #0f172a12}}
.decision{{background:{action_color};color:white;padding:28px}} .decision h1{{font-size:46px;margin:0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}} .metric{{background:#ffffff22;padding:12px;border-radius:8px}}
.domain-pill{{display:inline-block;background:#dbeafe;color:#1e40af;padding:4px 9px;border-radius:999px;margin:3px}}
table{{width:100%;border-collapse:collapse;font-size:13px}} td,th{{border-bottom:1px solid #e2e8f0;padding:7px;text-align:left}}
code{{font-size:11px;overflow-wrap:anywhere}} svg{{max-width:100%;height:auto}} .muted{{color:#64748b}} 
</style></head>
<body><main>
<section class="card decision">
<div class="muted">Trade-Council · {html_module.escape(str(run.get('symbol', '')))} · {html_module.escape(str(run.get('asof', '')))}</div>
<h1><span data-decision-field="action">{action}</span></h1>
<div class="grid">
<div class="metric">信心：<span data-decision-field="confidence">{confidence}</span></div>
<div class="metric">风险：<span data-decision-field="risk_level">{risk_level}</span></div>
<div class="metric">模式：{html_module.escape(str(run.get('profile', '')))}</div>
</div></section>
<section class="card"><h2>已启用分析域</h2>{domain_tags}</section>
<section class="card"><h2>最终裁决</h2><p>{html_module.escape(str(decision.get('rationale', '')))}</p></section>
{trade_context_html}
<section class="card"><h2>多空辩论</h2>{debate_html or '<p>当前运行未生成辩论产物</p>'}</section>
{''.join(domain_sections)}
{risk_html}
<section class="card" data-domain="technical"><h2>价格结构</h2>{price_svg}<p class="muted">近60日收盘价；虚线为 MA20/MA60 参考线。RSI 使用 Wilder 主口径，SMA RSI 仅作对照。</p></section>
<section class="card" data-domain="risk"><h2>风险视图</h2>{risk_svg}</section>
<section class="card" data-domain="risk"><h2>风控规则命中</h2><ul>{rule_html}</ul></section>
<section class="card"><h2>数据缺口</h2><ul>{gap_html}</ul></section>
<section class="card"><h2>证据索引</h2><table><thead><tr><th>Evidence ID</th><th>数据集</th><th>字段</th><th>值</th><th>可用时间</th></tr></thead><tbody>{evidence_rows}</tbody></table></section>
<p class="muted">本报告仅供研究参考，不构成投资建议。</p>
</main></body></html>"""
    return document, price_points, refs


def build_report(run_dir: Path, datapack_dir: Path) -> dict[str, Any]:
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    decision = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
    evidence = json.loads((datapack_dir / "evidence.json").read_text(encoding="utf-8"))
    roles = _read_roles(run_dir)
    enabled_domains = _enabled_domains(roles)
    risk_policy_path = run_dir.parent.parent / "configs" / "risk.yaml"
    market_rules_path = run_dir.parent.parent / "configs" / "market-rules.yaml"
    trade_context = _trade_context_from_run(run)
    markdown = render_markdown(
        run=run,
        decision=decision,
        roles=roles,
        evidence=evidence,
        market_rules_path=market_rules_path,
    )
    document, _price_points, _refs = render_html(
        run=run,
        decision=decision,
        roles=roles,
        evidence=evidence,
        datapack_dir=datapack_dir,
        enabled_domains=enabled_domains,
        market_rules_path=market_rules_path,
    )
    data_gaps = decision.get("data_gaps", [])
    allow_trade = bool(run.get("capability_flags", {}).get("trade_instructions", False))
    audit = audit_report(
        document,
        decision=decision,
        enabled_domains=enabled_domains,
        role_outputs=roles,
        data_gaps=data_gaps,
        data_pack_dir=datapack_dir,
        risk_policy_path=risk_policy_path,
        allow_trade_instructions=allow_trade,
        trade_context=trade_context,
        market_rules_path=market_rules_path,
        asof=str(run.get("asof", "")),
    )
    (run_dir / "report.md").write_text(markdown, encoding="utf-8")
    (run_dir / "report.html").write_text(document, encoding="utf-8")
    (run_dir / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    run.update(
        {
            "status": "AUDITED",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "report": {
                "markdown": str(run_dir / "report.md"),
                "html": str(run_dir / "report.html"),
            },
            "audit": audit,
        }
    )
    (run_dir / "run.json").write_text(
        json.dumps(run, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return audit
