"""Render reports from completed workflow artifacts."""

from __future__ import annotations

import html as html_module
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..decision.risk_rules import BoardRule, TradeContext, board_rule_for, t_plus_one_blocked
from ..engineering.evidence import infer_value_type
from .audit import audit_report
from .scoring import Scorecard, load_scorecard
from .view_model import build_view_model

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


def _evidence_value_html(item: dict[str, Any]) -> str:
    """Numbers, text and events are rendered differently and never coerced to 0."""

    value = item.get("value")
    value_type = str(item.get("value_type") or infer_value_type(value))
    if value is None or value == "":
        return "<em>未提供</em>"
    if value_type == "number":
        return _format_number(value, str(item.get("evidence_id")))
    text = html_module.escape(str(value))
    return f'<span class="evidence-text" data-value-type="text">{text}</span>'


def _scorecard_svg(scorecard: Any) -> str:
    """Render a radar only from explicit, weighted dimensions."""

    computed = scorecard.computed()
    if computed["status"] == "unavailable":
        return ""
    dimensions = [item for item in scorecard.dimensions if item.score is not None]
    if len(dimensions) < 3:
        return ""
    points = []
    for index, item in enumerate(dimensions):
        angle = -1.5708 + index * 2 * 3.14159 / len(dimensions)
        radius = 20 + (float(item.score) / max(scorecard.scale_max, 1e-9)) * 70
        points.append(
            f"{100 + radius * math.cos(angle):.1f},{100 + radius * math.sin(angle):.1f}"
        )
    return (
        f'<svg data-chart="scorecard" data-points="{len(dimensions)}" viewBox="0 0 200 200" '
        'role="img" aria-label="评分卡维度">'
        '<circle cx="100" cy="100" r="90" fill="#f8fafc" stroke="#cbd5e1"/>'
        f'<polygon points="{" ".join(points)}" fill="#93c5fd" fill-opacity="0.6" stroke="#2563eb"/>'
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


def _load_research_artifacts(run_dir: Path) -> dict[str, Any]:
    def read(name: str) -> Any:
        path = run_dir / name
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    research = read("research_decision.json") or {}
    claims_payload = read("claims.json") or {}
    claims = claims_payload.get("claims") if isinstance(claims_payload, dict) else claims_payload
    questions_payload = read("research_questions.json") or {}
    questions = (
        questions_payload.get("questions")
        if isinstance(questions_payload, dict)
        else questions_payload
    )
    gaps_payload = read("data_gaps.json") or {}
    gaps = gaps_payload.get("gaps") if isinstance(gaps_payload, dict) else gaps_payload
    revisions: list[dict[str, Any]] = []
    revision_path = run_dir / "revisions.jsonl"
    if revision_path.is_file():
        for line in revision_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                revisions.append(json.loads(line))
    return {
        "research": research if isinstance(research, dict) else {},
        "claims": claims if isinstance(claims, list) else [],
        "questions": questions if isinstance(questions, list) else [],
        "gaps": gaps if isinstance(gaps, list) else [],
        "revisions": revisions,
    }


def build_view(
    *,
    run: dict[str, Any],
    run_dir: Path,
    decision: dict[str, Any],
    roles: dict[str, dict[str, Any]],
    evidence: dict[str, Any],
    enabled_domains: list[str],
    market_rules_path: Path,
    datapack_dir: Path,
    scorecard: Scorecard | None = None,
) -> dict[str, Any]:
    """Single source for every output format."""

    artifacts = _load_research_artifacts(run_dir)
    return build_view_model(
        run=run,
        run_dir=run_dir,
        decision=decision,
        roles=roles,
        evidence=evidence,
        research=artifacts["research"],
        claims=artifacts["claims"],
        questions=artifacts["questions"],
        gaps=artifacts["gaps"],
        revisions=artifacts["revisions"],
        datapack_dir=datapack_dir,
        enabled_domains=enabled_domains,
        market_rules_path=market_rules_path,
        asof=str(run.get("asof", ""))[:10],
        scorecard=scorecard,
    )


def _md_block(block: dict[str, Any]) -> list[str]:
    kind = block.get("kind")
    lines: list[str] = []
    if kind == "paragraph":
        lines.extend([str(block.get("text", "")), ""])
    elif kind == "callout":
        lines.extend([f"> {block.get('text', '')}", ""])
    elif kind == "kv":
        for label, value in block.get("rows", []):
            lines.append(f"- {label['text']}：{value['text']}")
        lines.append("")
    elif kind == "bullets":
        if block.get("title"):
            lines.extend([f"**{block['title']}**", ""])
        items = block.get("items") or ["无"]
        lines.extend(f"- {item}" for item in items)
        lines.append("")
    elif kind == "table":
        if block.get("caption"):
            lines.extend([f"*{block['caption']}*", ""])
        header = "| " + " | ".join(block.get("columns", [])) + " |"
        divider = "|" + "---|" * len(block.get("columns", []))
        lines.extend([header, divider])
        for row in block.get("rows", []):
            lines.append("| " + " | ".join(cell.get("text", "") for cell in row) + " |")
        lines.append("")
    elif kind == "chart":
        caption = str(block.get("caption") or "图表").rstrip("。；")
        lines.extend([f"（{caption}。图形见 HTML 版本）", ""])
    return lines


def render_markdown(view: dict[str, Any]) -> str:
    meta = view["meta"]
    lines = [
        f"# {meta.get('stock_name') or meta.get('symbol', '')}（{meta.get('symbol', '')}）决策研究 · {meta.get('asof', '')}",
        "",
        f"- 运行：`{meta.get('run_id', '')}`（{meta.get('mode', '')} / {meta.get('profile', '')}）",
        (
            f"- 动作：**{meta.get('action', '')}**，信心 {meta.get('confidence', '')}，"
            f"风险 {meta.get('risk_level', '')}"
        ),
        (
            f"- 启用角色：{len(meta.get('enabled_roles') or [])} 个；"
            f"启用分析域：{', '.join(meta.get('enabled_domains') or [])}"
        ),
        f"- 证据快照：`{meta.get('snapshot_id', '')}`",
        "",
    ]
    for section in view["sections"]:
        lines.extend([f"## {section['title']}", ""])
        for block in section["blocks"]:
            lines.extend(_md_block(block))
    appendix = view["appendix"]
    lines.extend(["## 附录", ""])
    lines.extend(["### 实际启用角色", ""])
    for role in appendix["roles"]:
        lines.append(f"- `{role['role_id']}`（{role.get('stance') or '未标注立场'}）：{role.get('summary', '')}")
    lines.append("")
    if appendix["debate"]:
        lines.extend(["### 多空最强论点", ""])
        for role_id, summary in appendix["debate"].items():
            lines.append(f"- **{role_id}**：{summary}")
        lines.append("")
    if appendix["disagreements"]:
        lines.extend(["### 重大分歧与已失效论点", ""])
        for item in appendix["disagreements"]:
            lines.append(
                f"- `{item['claim_id']}`（{item['status']}）：{item['statement']}"
                + (f"；原因：{item['reason']}" if item.get("reason") else "")
            )
        lines.append("")
    if appendix["revisions"]:
        lines.extend(["### 更正记录", ""])
        for record in appendix["revisions"]:
            lines.append(
                f"- {record.get('revision_id')}（{record.get('created_at', '')}）："
                f"{record.get('before', '')} → {record.get('after', '')}"
                f"；复核={'已完成' if record.get('reviewed') else '未完成'}"
            )
        lines.append("")
    if appendix["gaps"]:
        lines.extend(["### 数据缺口", ""])
        for gap in appendix["gaps"]:
            lines.append(
                f"- `{gap.get('gap_id')}`（{gap.get('kind')}，{gap.get('priority')}）："
                f"{gap.get('dataset_id')} / {gap.get('field') or '—'}；"
                f"检查范围：{gap.get('scope_of_check') or '未填写'}；"
                f"下一步：{gap.get('next_step') or '未填写'}"
            )
        lines.append("")
    lines.extend(
        [
            "### 证据索引",
            "",
            (
                f"共 {len(appendix['evidence'])} 条证据，字段级可追溯；"
                "HTML 版本含来源链接与可用时点。"
            ),
            "",
            "> 本报告由结构化结果生成，仅供研究参考，不构成投资建议。",
        ]
    )
    return "\n".join(lines)


def _supplement_html(run: dict[str, Any]) -> str:
    """Task-scoped supplement trail: what was asked for and what came back."""

    data_needs = run.get("data_needs") or []
    web_supplement = run.get("web_supplement") or {}
    if not data_needs and not web_supplement:
        return ""
    need_rows = "".join(
        f"<li><code>{html_module.escape(str(need.get('need_id')))}</code>："
        f"{html_module.escape(str(need.get('dataset_id')))} / "
        f"{html_module.escape(str(need.get('field')))} "
        f"<strong>{html_module.escape(str(need.get('priority')))}</strong></li>"
        for need in data_needs
    )
    result_rows: list[str] = []
    for result in web_supplement.get("results") or []:
        result_rows.append(
            f"<li><code>{html_module.escape(str(result.get('need_id')))}</code>："
            f"{html_module.escape(str(result.get('status')))}</li>"
        )
        if result.get("missing_fields"):
            result_rows.append(
                "<li>未提取字段："
                + html_module.escape("、".join(str(f) for f in result["missing_fields"]))
                + "</li>"
            )
        if result.get("error"):
            result_rows.append(f"<li>错误：{html_module.escape(str(result['error']))}</li>")
        for source in result.get("sources") or []:
            url = str(source.get("url") or "")
            title = str(source.get("title") or url)
            result_rows.append(
                f'<li><a href="{html_module.escape(url)}" rel="noreferrer">'
                f"{html_module.escape(title)}</a></li>"
                if url
                else "<li>无外部来源链接</li>"
            )
    return (
        '<section class="card" data-supplement="true"><h2>任务级数据补充</h2>'
        f"<p>状态：{html_module.escape(str(web_supplement.get('status', 'not_run')))}</p>"
        f"<ul>{need_rows or '<li>无缺口</li>'}</ul>"
        f"<ul>{''.join(result_rows) or '<li>无外部来源</li>'}</ul></section>"
    )


def _html_kv(block: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><th>{html_module.escape(label['text'])}</th>"
        f"<td>{html_module.escape(value['text'])}</td></tr>"
        for label, value in block.get("rows", [])
    )
    return f"<table><tbody>{rows}</tbody></table>"


def _html_bullets(block: dict[str, Any], *, gap_marker: bool = False) -> str:
    attributes = ' data-gap="true"' if gap_marker else ""
    items = block.get("items") or ["无"]
    body = "".join(f"<li{attributes}>{html_module.escape(str(item))}</li>" for item in items)
    title = (
        f"<h3>{html_module.escape(str(block['title']))}</h3>" if block.get("title") else ""
    )
    return f"{title}<ul>{body}</ul>"


def _html_table(block: dict[str, Any]) -> str:
    header = "".join(f"<th>{html_module.escape(str(name))}</th>" for name in block.get("columns", []))
    rows = []
    for row in block.get("rows", []):
        cells = []
        for cell in row:
            text = str(cell.get("text", ""))
            if cell.get("numeric"):
                source = str(cell.get("source") or "structured")
                rendered = _format_number(text, source)
            else:
                rendered = html_module.escape(text)
            cells.append(f'<td class="{"muted" if cell.get("muted") else ""}">{rendered}</td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")
    caption = (
        f'<p class="muted">{html_module.escape(str(block["caption"]))}</p>'
        if block.get("caption")
        else ""
    )
    return (
        f"{caption}<table><thead><tr>{header}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def render_html(
    view: dict[str, Any],
    *,
    datapack_dir: Path,
    scorecard: Scorecard | None = None,
    run: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    market_rules_path: Path | None = None,
) -> tuple[str, int, int]:
    """Render the view model to one HTML document."""

    meta = view["meta"]
    run = run or {}
    decision = decision or {}
    rules_path = market_rules_path or Path("__missing_market_rules__.yaml")
    context = _trade_context_from_run(run)
    board_rule = board_rule_for(rules_path, market=context.market, board=context.board)
    t_plus_status = _t_plus_status(decision, context, board_rule, str(meta.get("asof", "")))
    price_svg, price_points, refs = _price_svg(datapack_dir)
    scorecard_html, _svg = _render_scorecard(scorecard)
    action = html_module.escape(str(meta.get("action", "INSUFFICIENT_DATA")))
    action_color = {
        "BUY": "#15803d",
        "SELL": "#b91c1c",
        "HOLD": "#a16207",
        "INSUFFICIENT_DATA": "#475569",
    }.get(str(meta.get("action")), "#475569")
    domain_tags = " ".join(
        f'<span class="domain-pill" data-domain="{domain}">'
        f"{DOMAIN_LABELS.get(domain, domain)}</span>"
        for domain in meta.get("enabled_domains") or []
    )
    sections_html: list[str] = []
    for section in view["sections"]:
        blocks = []
        for block in section["blocks"]:
            kind = block.get("kind")
            if kind == "paragraph":
                blocks.append(f"<p>{html_module.escape(str(block.get('text', '')))}</p>")
            elif kind == "callout":
                blocks.append(f"<p class=\"callout\">{html_module.escape(str(block.get('text', '')))}</p>")
            elif kind == "kv":
                blocks.append(_html_kv(block))
            elif kind == "bullets":
                blocks.append(_html_bullets(block))
            elif kind == "table":
                blocks.append(_html_table(block))
            elif kind == "chart":
                blocks.append(
                    f'{price_svg}<p class="muted">{html_module.escape(str(block.get("caption", "")))}</p>'
                )
        sections_html.append(
            f'<section class="card" data-section="{section["id"]}">'
            f"<h2>{html_module.escape(section['title'])}</h2>{''.join(blocks)}</section>"
        )

    appendix = view["appendix"]
    role_html = "".join(
        f'<details data-role="{role["role_id"]}"><summary>{html_module.escape(role["role_id"])}'
        f"（{html_module.escape(str(role.get('stance') or '未标注立场'))}）</summary>"
        f"<p>{html_module.escape(str(role.get('summary') or ''))}</p>"
        "<ul>"
        + "".join(
            f"<li><strong>{html_module.escape(str(item.get('claim_id', 'claim')))}</strong>："
            f"{html_module.escape(str(item.get('claim', '')))} "
            f"<code>{html_module.escape(', '.join(item.get('evidence_ids') or []))}</code></li>"
            for item in role.get("key_findings") or []
        )
        + "</ul></details>"
        for role in appendix["roles"]
    )
    debate_html = "".join(
        f'<section data-role="{role_id}"><h3>{html_module.escape(role_id)}</h3>'
        f"<p>{html_module.escape(summary)}</p></section>"
        for role_id, summary in appendix["debate"].items()
    )
    revision_html = "".join(
        f"<li>{html_module.escape(str(record.get('revision_id')))}"
        f"（{html_module.escape(str(record.get('created_at', '')))}）："
        f"{html_module.escape(str(record.get('before', '')))} → "
        f"{html_module.escape(str(record.get('after', '')))}；"
        f"复核={'已完成' if record.get('reviewed') else '未完成'}</li>"
        for record in appendix["revisions"]
    )
    gap_html = "".join(
        f'<li data-gap-kind="{html_module.escape(str(gap.get("kind")))}">'
        f"<code>{html_module.escape(str(gap.get('gap_id')))}</code>"
        f"（{html_module.escape(str(gap.get('kind')))}/{html_module.escape(str(gap.get('priority')))}）："
        f"{html_module.escape(str(gap.get('dataset_id')))} / "
        f"{html_module.escape(str(gap.get('field') or '—'))}；"
        f"检查范围：{html_module.escape(str(gap.get('scope_of_check') or '未填写'))}</li>"
        for gap in appendix["gaps"]
    )
    decision_gap_html = "".join(
        f'<li data-gap="true">{html_module.escape(str(gap))}</li>' for gap in meta.get("data_gaps") or []
    )
    evidence_rows = "".join(
        "<tr>"
        f"<td><code>{html_module.escape(str(item.get('evidence_id')))}</code></td>"
        f"<td>{html_module.escape(str(item.get('dataset_id')))}</td>"
        f"<td>{html_module.escape(str(item.get('field', '—')))}</td>"
        f"<td>{_evidence_value_html(item)} "
        f"{html_module.escape(str(item.get('unit', '')))}</td>"
        f"<td>{html_module.escape(str(item.get('value_type') or 'text'))}</td>"
        f"<td>{html_module.escape(str(item.get('available_at') or '—'))}</td>"
        f"<td>{html_module.escape(str(item.get('source_level', 'local')))}</td>"
        f"<td>{html_module.escape(str(item.get('source_url') or '—'))}</td>"
        "</tr>"
        for item in appendix["evidence"]
    )
    trade_fields = ""
    if meta.get("action") in {"BUY", "SELL"}:
        trade_fields = f"""
<tr><th>拟交易股数</th><td><span data-decision-field="position_shares">{_format_number(meta.get('position_shares'), 'decision.position_shares')}</span></td></tr>
<tr><th>拟交易金额</th><td><span data-decision-field="position_amount">{_format_number(meta.get('position_amount'), 'decision.position_amount')}</span></td></tr>
<tr><th>建仓日期</th><td><span data-decision-field="position_opened_date">{html_module.escape(str(meta.get('position_opened_date') or '未提供'))}</span></td></tr>
<tr><th>T+1 状态</th><td><span data-decision-field="t_plus_one_status">{html_module.escape(t_plus_status)}</span></td></tr>
"""
    supplement_html = _supplement_html(run)
    document = f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trade-Council · {html_module.escape(str(meta.get('stock_name') or meta.get('symbol', '')))}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;background:#f1f5f9;color:#0f172a;margin:0;padding:24px}}
main{{max-width:1080px;margin:auto}} .card{{background:white;border-radius:14px;padding:20px;margin:16px 0;box-shadow:0 2px 10px #0f172a12}}
.decision{{background:{action_color};color:white;padding:28px}} .decision h1{{font-size:46px;margin:0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}} .metric{{background:#ffffff22;padding:12px;border-radius:8px}}
.domain-pill{{display:inline-block;background:#dbeafe;color:#1e40af;padding:4px 9px;border-radius:999px;margin:3px}}
table{{width:100%;border-collapse:collapse;font-size:13px}} td,th{{border-bottom:1px solid #e2e8f0;padding:7px;text-align:left}}
code{{font-size:11px;overflow-wrap:anywhere}} svg{{max-width:100%;height:auto}} .muted{{color:#64748b}}
.callout{{background:#f8fafc;border-left:4px solid #94a3b8;padding:10px 12px}}
details{{border-top:1px solid #e2e8f0;padding:8px 0}}
</style></head>
<body><main>
<section class="card decision">
<div class="muted">Trade-Council · {html_module.escape(str(meta.get('stock_name') or meta.get('symbol', '')))} · {html_module.escape(str(meta.get('asof', '')))}</div>
<h1><span data-decision-field="action">{action}</span></h1>
<div class="grid">
<div class="metric">信心：<span data-decision-field="confidence">{html_module.escape(str(meta.get('confidence', 'low')))}</span></div>
<div class="metric">风险：<span data-decision-field="risk_level">{html_module.escape(str(meta.get('risk_level', 'medium')))}</span></div>
<div class="metric">模式：{html_module.escape(str(meta.get('profile', '')))}</div>
</div></section>
<section class="card"><h2>已启用分析域</h2>{domain_tags}</section>
{f'<section class="card" data-trade-fields="true"><h2>账户动作与交易制度</h2><table><tbody>{trade_fields}</tbody></table></section>' if trade_fields else ''}
{supplement_html}
{''.join(sections_html)}
{scorecard_html}
<section class="card" data-appendix="true"><h2>附录</h2>
<h3>实际启用角色（{len(appendix['roles'])} 个）</h3>{role_html or '<p>无角色产物</p>'}
{debate_html}
<h3>数据缺口</h3><ul>{gap_html or '<li>未登记缺口</li>'}</ul>
<h3>决策层缺口</h3><ul>{decision_gap_html or '<li>无已记录缺口</li>'}</ul>
<h3>更正记录</h3><ul>{revision_html or '<li>无更正</li>'}</ul>
<h3>证据索引</h3><table><thead><tr><th>Evidence ID</th><th>数据集</th><th>字段</th><th>值</th><th>类型</th><th>可用时间</th><th>来源层级</th><th>来源链接</th></tr></thead><tbody>{evidence_rows}</tbody></table>
</section>
<p class="muted">本报告由同一份结构化结果生成，仅供研究参考，不构成投资建议。</p>
</main></body></html>"""
    return document, price_points, refs


def _render_scorecard(scorecard: Scorecard | None) -> tuple[str, str]:
    """Build the scorecard section; absence of a definition is stated, not faked."""

    if scorecard is None:
        return (
            (
                '<section class="card" data-scorecard="absent"><h2>评分卡</h2>'
                "<p>本次运行没有提供带权重、公式与缺失处理的评分卡，"
                "因此不生成综合评分，也不绘制雷达图。研究结论以结构化判断与证据为准。</p></section>"
            ),
            "",
        )
    computed = scorecard.computed()
    rows = "".join(
        f"<tr><td>{html_module.escape(item.label)}</td>"
        f"<td>{item.weight:g}</td>"
        f"<td>{'缺失' if item.score is None else f'{item.score:g}'}</td>"
        f"<td>{html_module.escape(item.basis)}</td></tr>"
        for item in scorecard.dimensions
    )
    total = (
        f"{computed['total']:.2f} / {scorecard.scale_max:g}"
        if computed["total"] is not None
        else "不可得"
    )
    missing = "、".join(computed.get("missing") or []) or "无"
    svg = _scorecard_svg(scorecard)
    section = (
        '<section class="card" data-scorecard="present">'
        "<h2>评分卡</h2>"
        f"<p>公式：{html_module.escape(scorecard.formula)}；缺失处理："
        f"{html_module.escape(scorecard.missing_handling)}；缺失维度：{html_module.escape(missing)}</p>"
        f"<p>综合分：<strong data-scorecard-total>{html_module.escape(total)}</strong></p>"
        f"<table><thead><tr><th>维度</th><th>权重</th><th>得分</th><th>依据</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>{svg}</section>"
    )
    return section, svg


def build_report(run_dir: Path, datapack_dir: Path) -> dict[str, Any]:
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    decision = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
    evidence = json.loads((datapack_dir / "evidence.json").read_text(encoding="utf-8"))
    supplemental_path = run_dir / "supplemental_evidence.json"
    if supplemental_path.is_file():
        supplemental = json.loads(supplemental_path.read_text(encoding="utf-8"))
        evidence["items"].extend(supplemental.get("items", []))
    roles = _read_roles(run_dir)
    enabled_domains = _enabled_domains(roles)
    risk_policy_path = run_dir.parent.parent / "configs" / "risk.yaml"
    market_rules_path = run_dir.parent.parent / "configs" / "market-rules.yaml"
    trade_context = _trade_context_from_run(run)
    scorecard = load_scorecard(run_dir / "risk_scorecard.json")
    view = build_view(
        run=run,
        run_dir=run_dir,
        decision=decision,
        roles=roles,
        evidence=evidence,
        enabled_domains=enabled_domains,
        market_rules_path=market_rules_path,
        datapack_dir=datapack_dir,
        scorecard=scorecard,
    )
    (run_dir / "report_view.json").write_text(
        json.dumps(view, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown = render_markdown(view)
    document, _price_points, _refs = render_html(
        view,
        datapack_dir=datapack_dir,
        scorecard=scorecard,
        run=run,
        decision=decision,
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
        evidence_items=evidence.get("items", []),
    )
    passed = audit["verdict"] != "FAIL"
    # A failing audit may only produce a diagnostic draft. It must never
    # overwrite a previously passing deliverable or be mistaken for one.
    suffix = "" if passed else ".draft"
    (run_dir / f"report{suffix}.md").write_text(markdown, encoding="utf-8")
    (run_dir / f"report{suffix}.html").write_text(document, encoding="utf-8")
    (run_dir / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    run.update(
        {
            "status": "AUDITED" if passed else "AUDIT_FAILED",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "report": (
                {
                    "markdown": str(run_dir / "report.md"),
                    "html": str(run_dir / "report.html"),
                }
                if passed
                else {
                    "markdown_draft": str(run_dir / "report.draft.md"),
                    "html_draft": str(run_dir / "report.draft.html"),
                }
            ),
            "audit": audit,
        }
    )
    (run_dir / "run.json").write_text(
        json.dumps(run, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return audit
