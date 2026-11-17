"""One structured view model for the report.

Markdown, HTML and JSON are rendered from this single object. If a number or a
verdict is not in here, it must not appear in any output format: that is what
stops the report from drifting away from the structured research result.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ..decision.risk_rules import TradeContext, board_rule_for
from ..research.gaps import GAP_LABELS
from ..research.propagation import role_claim_refs as _role_claim_refs
from .scoring import Scorecard

SCHEMA_VERSION = "report-view-1"

ASSESSMENT_LABELS = {
    "positive": "偏正面",
    "mixed": "多空交织",
    "negative": "偏负面",
    "unresolved": "未定",
    "attractive": "有吸引力",
    "neutral": "大致合理",
    "demanding": "偏贵",
    "ready": "可执行",
    "wait": "等待触发",
    "invalidated": "已失效",
    "continue": "继续研究",
    "watch": "保持观察",
    "reject": "放弃",
    "need_evidence": "需补证据",
    "not_assessed": "未评估",
    "blocked": "被条件阻塞",
    "conditional": "有条件的",
    "actionable": "可执行",
}

QUARTER_STATUS_LABELS = {
    "ok": "正常",
    "missing_predecessor": "缺上一季，单季不可得",
    "restated_incomparable": "重述不可比",
    "missing_value": "缺值",
    "missing_field": "缺字段",
}

COMPARABILITY_LABELS = {
    "confirmed": "已确认同版本",
    "changed": "存在重述版本",
    "unconfirmed": "单版本未确认",
}

CLAIM_STATUS_LABELS = {
    "intact": "成立",
    "weakened": "被削弱",
    "retracted": "已撤回",
    "unresolved": "待确认",
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _label(value: object) -> str:
    text = str(value or "").strip()
    return ASSESSMENT_LABELS.get(text, text or "未提供")


def _claim_line(claim: dict[str, Any]) -> str:
    """One claim, with its status, evidence and reason, on one line."""

    status = CLAIM_STATUS_LABELS.get(str(claim.get("status") or "intact"), "成立")
    evidence_ids = [str(item) for item in claim.get("supporting_evidence_ids") or []]
    counters = [str(item) for item in claim.get("counter_evidence_ids") or []]
    detail = f"证据：{'、'.join(evidence_ids) if evidence_ids else '未标注证据 ID'}"
    if counters:
        detail += f"；反证：{'、'.join(counters)}"
    if str(claim.get("status") or "intact") != "intact" and claim.get("status_reason"):
        detail += f"；状态说明：{claim.get('status_reason')}"
    return (
        f"{claim.get('claim_id')}（{claim.get('kind', 'inference')}／{status}）："
        f"{claim.get('statement', '')}｜{detail}"
    )


def _quarter_rows(datapack_dir: Path, limit: int = 8) -> list[dict[str, str]]:
    path = datapack_dir / "financial_quarters.csv"
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return rows[-limit:]


def _adjustment_rows(datapack_dir: Path, limit: int = 8) -> list[dict[str, Any]]:
    path = datapack_dir / "financial_adjustments.json"
    if not path.is_file():
        return []
    payload = _read_json(path)
    rows = [row for row in payload.get("periods", []) if row.get("reported_parent_net") is not None]
    return rows[-limit:]


def _bridge(datapack_dir: Path) -> dict[str, Any]:
    path = datapack_dir / "earnings_bridge.json"
    return _read_json(path) if path.is_file() else {}


def _adjustment_scenario(datapack_dir: Path) -> dict[str, Any]:
    path = datapack_dir / "financial_adjustments.json"
    if not path.is_file():
        return {}
    payload = _read_json(path)
    scenario = payload.get("scenario") or {}
    return {
        "tax_rate": scenario.get("tax_rate"),
        "tax_rate_source": scenario.get("tax_rate_source"),
        "scenario_id": scenario.get("scenario_id"),
        "minority_treatment": scenario.get("minority_treatment"),
    }


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def _yi(value: object) -> float | None:
    number = _as_float(value)
    return None if number is None else number / 1e8


def _financial_block(datapack_dir: Path) -> dict[str, Any]:
    rows = _quarter_rows(datapack_dir)
    body: list[list[dict[str, Any]]] = []
    for row in rows:
        body.append(
            [
                {"text": row.get("end_date", "")},
                {"text": _num(row.get("revenue_q")), "numeric": True, "source": "financial_quarters.csv:revenue_q"},
                {"text": _num(row.get("n_income_attr_p_q")), "numeric": True, "source": "financial_quarters.csv:n_income_attr_p_q"},
                {"text": QUARTER_STATUS_LABELS.get(str(row.get("quarter_status")), str(row.get("quarter_status") or "")), "muted": True},
                {
                    "text": COMPARABILITY_LABELS.get(
                        str(row.get("comparability")), str(row.get("comparability") or "未记录")
                    ),
                    "muted": True,
                },
            ]
        )
    return {
        "kind": "table",
        "caption": (
            "单季财务（亿元）：单季营收 = 本期累计 − 上一季累计，Q1 直接用累计数。"
            "可比性“单版本未确认”表示本地库每期只有一份披露，无法证明与当时披露一致"
        ),
        "columns": ["报告期", "单季营收", "单季归母", "季度状态", "可比性"],
        "rows": body,
    }


def _num(value: object) -> str:
    number = _yi(value)
    if number is None:
        return "缺失"
    return f"{number:,.2f}"


def _profit_lines_block(datapack_dir: Path) -> dict[str, Any]:
    rows = _adjustment_rows(datapack_dir)
    body: list[list[dict[str, Any]]] = []
    for row in rows:
        body.append(
            [
                {"text": str(row.get("end_date", ""))},
                {"text": _num(row.get("reported_parent_net")), "numeric": True,
                 "source": "financial_adjustments.json:reported_parent_net"},
                {"text": _num(row.get("official_deducted_parent_net_single")), "numeric": True,
                 "source": "financial_adjustments.json:official_deducted_parent_net_single"},
                {"text": _num(row.get("adjusted_parent_net_strip_asset")), "numeric": True,
                 "source": "financial_adjustments.json:adjusted_parent_net_strip_asset"},
                {"text": _num(row.get("adjusted_parent_net_strip_asset_and_credit")), "numeric": True,
                 "source": "financial_adjustments.json:adjusted_parent_net_strip_asset_and_credit"},
            ]
        )
    return {
        "kind": "table",
        "caption": (
            "三类盈利分列（亿元）：报表归母是原披露值；官方扣非来自公司公告；"
            "分析调整情景按统一税率重算，只是本次核对口径，不是公司真实可持续盈利"
        ),
        "columns": ["报告期", "报表归母", "官方扣非（单季）", "剔资产减值情景", "再剔信用减值情景"],
        "rows": body,
    }


def build_view_model(
    *,
    run: dict[str, Any],
    run_dir: Path,
    decision: dict[str, Any],
    roles: dict[str, dict[str, Any]],
    evidence: dict[str, Any],
    research: dict[str, Any],
    claims: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    revisions: list[dict[str, Any]],
    datapack_dir: Path,
    enabled_domains: list[str],
    market_rules_path: Path,
    asof: str,
    scorecard: Scorecard | None = None,
    finance_dir: Path | None = None,
) -> dict[str, Any]:
    """Assemble the six reader-facing sections plus the appendix."""

    # Financial numbers may come from a run-level recomputation; prices and
    # evidence always come from the frozen pack.
    fin = finance_dir or datapack_dir

    context = TradeContext(
        capital=(run.get("trade_context") or {}).get("capital"),
        position_state=(run.get("trade_context") or {}).get("position_state"),
        position_shares=(run.get("trade_context") or {}).get("position_shares"),
        position_opened_date=(run.get("trade_context") or {}).get("position_opened_date"),
        market=str((run.get("trade_context") or {}).get("market", "CN")),
        board=str((run.get("trade_context") or {}).get("board", "main")),
    )
    board_rule = board_rule_for(market_rules_path, market=context.market, board=context.board)
    execution = research.get("execution") or {}
    key_claims = [
        claim
        for claim in claims
        if str(claim.get("claim_id")) in {str(item) for item in research.get("key_claim_ids") or []}
    ]
    unresolved = [
        question
        for question in questions
        if str(question.get("status")) != "answered"
        or str(question.get("question_id")) in {str(i) for i in research.get("unresolved_question_ids") or []}
    ]
    bridge = _bridge(fin)
    ttm = bridge.get("ttm") or {}
    valuation = research.get("valuation_reference") or {}

    sections: list[dict[str, Any]] = []

    # 1. Research verdict, top reasons, decisive missing evidence.
    verdict_rows: list[list[dict[str, Any]]] = [
        [{"text": "基本面判断"}, {"text": _label(research.get("fundamental_assessment"))}],
        [{"text": "估值判断"}, {"text": _label(research.get("valuation_assessment"))}],
        [{"text": "技术时机"}, {"text": _label(research.get("timing_assessment"))}],
        [{"text": "研究状态"}, {"text": _label(research.get("research_status"))}],
        [{"text": "账户动作状态"}, {"text": _label(execution.get("status"))}],
    ]
    reasons = [
        f"{claim.get('claim_id')}（{CLAIM_STATUS_LABELS.get(str(claim.get('status') or 'intact'), '成立')}）："
        f"{claim.get('statement', '')}"
        for claim in key_claims[:3]
    ]
    missing = [
        f"{question.get('question_id')}：{question.get('question', '')}"
        for question in unresolved[:3]
    ]
    sections.append(
        {
            "id": "verdict",
            "title": "1. 当前研究判断",
            "blocks": [
                {"kind": "kv", "rows": verdict_rows},
                {
                    "kind": "bullets",
                    "title": "最重要的三条依据",
                    "items": reasons or ["本次运行没有登记关键论点，结论只能当作待验证的初稿。"],
                },
                {
                    "kind": "bullets",
                    "title": "还缺哪项决定性证据",
                    "items": missing
                    or ["没有登记未决问题；这不等于证据充分，只表示没有把不确定写下来。"],
                },
                {
                    "kind": "callout",
                    "text": str(decision.get("rationale") or research.get("rationale") or "未提供结论理由。"),
                },
            ],
        }
    )

    # 2. How the company makes money, and what the data covers.
    # This section is built from the claims filed under the business topic.
    # A role's free-form summary used to be printed here, which meant the same
    # question had two answers: the role text and the central claim list, and
    # nothing detected when they disagreed. A financial quarter-on-quarter
    # recap is also not an answer to "how does it earn money", so it is not
    # substituted when the business claims are missing.
    business_claims = [
        claim for claim in claims if str(claim.get("topic") or "other") == "business"
    ]
    superseded = [
        claim for claim in business_claims if str(claim.get("status") or "intact") != "intact"
    ]
    standing = [claim for claim in business_claims if claim not in superseded]
    business_blocks: list[dict[str, Any]] = []
    if business_claims:
        business_blocks.append(
            {
                "kind": "paragraph",
                "text": "本节只写登记为 business 维度的论点，每条都指向证据 ID；"
                "财务环比与季度桥接放在第 3 节，不在这里充当业务说明。"
                "已撤回或已降级的旧判断单列，不混在现行结论里。",
            }
        )
        if standing:
            business_blocks.append(
                {
                    "kind": "bullets",
                    "title": "业务结构论点",
                    "items": [_claim_line(claim) for claim in standing],
                }
            )
        if superseded:
            business_blocks.append(
                {
                    "kind": "bullets",
                    "title": "已撤回或已降级的旧判断（不作为结论）",
                    "items": [_claim_line(claim) for claim in superseded],
                }
            )
    else:
        business_blocks.append(
            {
                "kind": "paragraph",
                "text": "本次运行没有登记 business 维度的论点，因此“公司如何赚钱”这个问题没有被回答。"
                "财务数字的变化不构成业务说明，这里不补写；要补的是收入来源、成本结构与利润驱动，"
                "并各自给出证据。",
            }
        )
    coverage = [
        f"{gap.get('gap_id')}（{GAP_LABELS.get(str(gap.get('kind')), gap.get('kind'))}）："
        f"{gap.get('dataset_id')} / {gap.get('field') or '—'}，影响 {', '.join(gap.get('affects') or []) or '未标注'}"
        for gap in gaps
    ]
    business_blocks.append(
        {
            "kind": "bullets",
            "title": "资料覆盖边界",
            "items": coverage or ["没有登记缺口；只代表没有记录，不代表资料完整。"],
        }
    )
    sections.append({"id": "business", "title": "2. 公司如何赚钱", "blocks": business_blocks})

    # 3. Financials and the quarterly bridge.
    finance_blocks: list[dict[str, Any]] = [_financial_block(fin)]
    ttm_text = []
    for field, label in (("n_income_attr_p", "归母净利润"), ("revenue", "营业收入")):
        item = ttm.get(field) or {}
        if item.get("status") == "ok":
            value = _yi(item.get("value"))
            ttm_text.append(f"{label} {value:,.2f} 亿元" if value is not None else f"{label} 缺失")
        else:
            ttm_text.append(f"{label} 不可得（{item.get('reason') or '缺少上年同期累计'}）")
    finance_blocks.append(
        {
            "kind": "callout",
            "text": "过去 12 个月滚动口径 = 上年全年 + 本年累计 − 上年同期累计；"
            + ("；".join(ttm_text) if ttm_text else "本次没有可用的滚动数据"),
        }
    )
    finance_blocks.append(_profit_lines_block(fin))
    scenario = _adjustment_scenario(fin)
    tax_rate = scenario.get("tax_rate")
    rate_text = f"{float(tax_rate):.0%}" if isinstance(tax_rate, (int, float)) else "未记录"
    source_text = {
        "scenario": "本次运行输入的税率",
        "default": "模块默认税率，可被运行参数覆盖",
    }.get(str(scenario.get("tax_rate_source") or ""), "来源未记录")
    finance_blocks.append(
        {
            "kind": "paragraph",
            "text": f"调整情景的假设：统一按 {rate_text} 税率重算所得税（{source_text}）、"
            "少数股东损益沿用报表实际数；"
            "源表缺少利润总额、资产减值、少数股东损益或所得税任一项时，该季调整归母标为不可得，不做部分推算。"
            "调整后单季利润不是可持续年利润，不得乘以 4 年化。",
        }
    )
    sections.append(
        {"id": "financials", "title": "3. 财务与季度盈利桥接", "blocks": finance_blocks}
    )

    # 4. Valuation, technical state and signal availability.
    valuation_blocks: list[dict[str, Any]] = []
    if valuation:
        valuation_blocks.append(
            {
                "kind": "kv",
                "rows": [
                    [{"text": "估值方法"}, {"text": str(valuation.get("method") or "未指定")}],
                    [{"text": "参考价"}, {"text": _plain_number(valuation.get("reference_price"))}],
                    [{"text": "所用倍数"}, {"text": _plain_number(valuation.get("multiple"))}],
                    [{"text": "分位窗口"}, {"text": _window_text(valuation)}],
                    [{"text": "样本数"}, {"text": str(valuation.get("sample_size") or "未提供")}],
                ],
            }
        )
        if valuation.get("note"):
            valuation_blocks.append({"kind": "paragraph", "text": str(valuation["note"])})
    else:
        valuation_blocks.append(
            {
                "kind": "paragraph",
                "text": "本次没有提供结构化估值参照，因此不给参照价，也不写“合理价”。",
            }
        )
    price_points, alignment = _technical_state(datapack_dir)
    valuation_blocks.append(
        {
            "kind": "chart",
            "chart": "price",
            "points": price_points,
            "caption": "近 60 日收盘价（未复权）；虚线为 MA20/MA60。"
            "水平类指标用前复权价，真实委托价需单独换算。RSI 使用 Wilder 主口径，SMA RSI 仅作对照。",
        }
    )
    valuation_blocks.append(
        {
            "kind": "callout",
            "text": f"均线排列：{alignment}" if alignment else "均线数据不足，不做排列判断。",
        }
    )
    valuation_blocks.append(
        {
            "kind": "paragraph",
            "text": "技术信号在当日收盘后可得；本次没有做收益检验，"
            "因此只描述状态，不声称信号有效。",
        }
    )
    sections.append(
        {"id": "valuation", "title": "4. 估值与技术状态", "blocks": valuation_blocks}
    )

    # 5. Account constraints and execution state.
    execution_rows: list[list[dict[str, Any]]] = [
        [{"text": "账户资金"}, {"text": _plain_number(context.capital)}],
        [{"text": "持仓状态"}, {"text": str(context.position_state or "未提供")}],
        [{"text": "持仓股数"}, {"text": _plain_number(context.position_shares)}],
        [{"text": "板块 / 申报单位"}, {"text": f"{context.board} / lot={board_rule.lot_size}"}],
        [{"text": "执行状态"}, {"text": _label(execution.get("status"))}],
        [{"text": "目标账户权重"}, {"text": _pct(execution.get("target_account_weight"))}],
        [{"text": "减仓比例（占现持仓）"}, {"text": _pct(execution.get("sell_fraction_of_current"))}],
    ]
    execution_blocks: list[dict[str, Any]] = [
        {"kind": "kv", "rows": execution_rows},
        {
            "kind": "bullets",
            "title": "阻塞原因",
            "items": [str(item) for item in execution.get("block_reasons") or []]
            or ["无记录；若状态为可执行，则说明所需条件已齐备。"],
        },
        {
            "kind": "bullets",
            "title": "失效条件",
            "items": [str(item) for item in research.get("invalidation_conditions") or []]
            or ["没有登记失效条件，这是一个尚未闭合的环节。"],
        },
        {
            "kind": "bullets",
            "title": "风控规则命中",
            "items": [f"`{rule_id}`" for rule_id in decision.get("rule_checks") or []]
            or ["无规则命中"],
        },
    ]
    sections.append(
        {"id": "execution", "title": "5. 账户约束与执行状态", "blocks": execution_blocks}
    )

    # 6. What to check next.
    next_blocks = [
        {
            "kind": "bullets",
            "title": "下一次检查什么",
            "items": [str(item) for item in research.get("next_checks") or []]
            or ["没有登记下一步检查项。"],
        },
        {
            "kind": "bullets",
            "title": "什么事实会改变判断",
            "items": [str(item) for item in research.get("invalidation_conditions") or []]
            or ["没有登记；缺少这一项就无法判断结论何时应被推翻。"],
        },
    ]
    sections.append({"id": "next", "title": "6. 下一次检查", "blocks": next_blocks})

    known_claim_ids = {str(claim.get("claim_id")) for claim in claims}
    appendix = {
        "role_note": (
            "角色摘要保留为原始记录，不是结论来源：正文只引用中央论点表。"
            "每条角色摘要列出它引用的论点，凡引用不存在或已变更的论点都在此处标出。"
        ),
        "roles": [
            {
                "role_id": role_id,
                "stance": payload.get("stance", ""),
                "summary": payload.get("summary", ""),
                "key_findings": payload.get("key_findings") or [],
                "claim_refs": sorted(_role_claim_refs(payload)),
                "unknown_claim_refs": sorted(_role_claim_refs(payload) - known_claim_ids),
                "changed_claim_refs": sorted(
                    _role_claim_refs(payload)
                    & {
                        str(claim.get("claim_id"))
                        for claim in claims
                        if str(claim.get("status") or "intact") != "intact"
                    }
                ),
            }
            for role_id, payload in roles.items()
        ],
        "debate": {
            role_id: str((roles.get(role_id) or {}).get("summary") or "")
            for role_id in ("bull-researcher", "bear-researcher")
            if role_id in roles
        },
        "disagreements": [
            {
                "claim_id": claim.get("claim_id"),
                "status": claim.get("status") or "intact",
                "statement": claim.get("statement", ""),
                "reason": claim.get("status_reason", ""),
            }
            for claim in claims
            if str(claim.get("status") or "intact") != "intact"
        ],
        "claims": claims,
        "revisions": revisions,
        "questions": questions,
        "gaps": gaps,
        "evidence": evidence.get("items", []),
        "scorecard": scorecard.model_dump() if scorecard is not None else None,
        "enabled_domains": enabled_domains,
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "symbol": run.get("symbol", ""),
            "stock_name": run.get("stock_name") or run.get("symbol", ""),
            "asof": asof,
            "mode": run.get("mode", ""),
            "profile": run.get("profile", ""),
            "run_id": run.get("run_id", run_dir.name),
            "snapshot_id": evidence.get("snapshot_id", ""),
            "enabled_roles": sorted(roles),
            "enabled_domains": enabled_domains,
            "action": decision.get("action", "INSUFFICIENT_DATA"),
            "confidence": decision.get("confidence", "low"),
            "risk_level": decision.get("risk_level", "medium"),
            "position_shares": decision.get("position_shares"),
            "position_amount": decision.get("position_amount"),
            "position_opened_date": decision.get("position_opened_date"),
            "data_gaps": decision.get("data_gaps", []),
        },
        "sections": sections,
        "appendix": appendix,
    }


def _plain_number(value: object) -> str:
    number = _as_float(value)
    if number is None:
        return "未提供"
    return f"{number:,.2f}"


def _pct(value: object) -> str:
    number = _as_float(value)
    if number is None:
        return "未设定"
    return f"{number * 100:.2f}%"


def _window_text(valuation: dict[str, Any]) -> str:
    start = valuation.get("window_start") or "未提供"
    end = valuation.get("window_end") or "未提供"
    definition = valuation.get("percentile_definition") or "未定义"
    return f"{start} ~ {end}（{definition}）"


def _technical_state(datapack_dir: Path) -> tuple[int, str]:
    """Return (price points, alignment sentence) using the local price file."""

    path = datapack_dir / "price_daily.csv"
    if not path.is_file():
        return 0, ""
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return 0, ""
    tail = rows[-60:]
    numbers: list[float | None] = []
    for window in (5, 10, 20, 30, 60, 120, 250):
        column = f"ma{window}"
        numbers.append(_as_float(tail[-1].get(column)))
    from ..engineering.indicators import MA_WINDOWS, ma_alignment

    alignment = ma_alignment(numbers, MA_WINDOWS)
    return len(tail), f"{alignment.label}。{alignment.detail}"
