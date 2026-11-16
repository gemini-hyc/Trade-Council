"""Deterministic quarterly financial calculations, bridge, and TTM.

This module is the single source of the financial arithmetic used by the data
pack, the evidence index, and the report. It never calls a model and never
evaluates free-form formula strings.

Design rules enforced here:

1. Cumulative (YTD) statements stay untouched; single quarters are derived only
   from an explicit same-year predecessor (Q1=YTD, Q2=H1-Q1, Q3=9M-H1,
   Q4=FY-9M).
2. A missing predecessor never borrows an earlier period: the quarter is
   reported as unavailable instead of silently producing a multi-quarter delta.
3. Balance-sheet and share-count items are period-end stocks and are never
   differenced into a "flow".
4. A derived metric is available only at ``max(available_at of all inputs)``.
5. Every check has an explicit tolerance; values stay in source units (CNY).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

CALC_RULES_VERSION = "financials-1"
SCHEMA_VERSION = "1.0"
UNIT = "CNY"

#: Cumulative income-statement flows that may be differenced into quarters.
INCOME_FLOW_FIELDS: tuple[str, ...] = (
    "revenue",
    "oper_cost",
    "biz_tax_surchg",
    "sell_exp",
    "admin_exp",
    "rd_exp",
    "fin_exp",
    "fv_value_chg_gain",
    "invest_income",
    "ass_invest_income",
    "assets_impair_loss",
    "operate_profit",
    "non_oper_income",
    "non_oper_exp",
    "total_profit",
    "income_tax",
    "n_income",
    "n_income_attr_p",
    "minority_gain",
)

#: Cumulative cash-flow items that may be differenced into quarters.
CASH_FLOW_FIELDS: tuple[str, ...] = (
    "n_cashflow_act",
    "n_cashflow_inv_act",
    "n_cash_flows_fnc_act",
    "c_pay_acq_const_fiolta",
)

#: Period-end stocks: never differenced.
BALANCE_STOCK_FIELDS: tuple[str, ...] = (
    "total_assets",
    "total_liab",
    "total_hldr_eqy_exc_min_int",
    "minority_int",
    "money_cap",
    "inventories",
    "accounts_receiv",
    "st_borr",
    "lt_borr",
    "total_share",
)

QUARTER_BY_MMDD = {"0331": 1, "0630": 2, "0930": 3, "1231": 4}

#: Absolute tolerance in CNY. Local statements are reported to the yuan, so a
#: one-yuan difference is a real difference; display rounding must not hide it.
ABSOLUTE_TOLERANCE = 1.0

LABELS: dict[str, str] = {
    "revenue": "营业收入",
    "oper_cost": "营业成本",
    "gross_profit": "毛利",
    "biz_tax_surchg": "税金及附加",
    "sell_exp": "销售费用",
    "admin_exp": "管理费用",
    "rd_exp": "研发费用",
    "fin_exp": "财务费用",
    "fv_value_chg_gain": "公允价值变动收益",
    "invest_income": "投资收益",
    "ass_invest_income": "其中：对联营合营企业的投资收益",
    "assets_impair_loss": "资产减值损失（负数为计提，正数为转回）",
    "unlisted_net": "未单列项目净额（含其他收益、信用减值、资产处置等）",
    "operate_profit": "营业利润",
    "non_oper_income": "营业外收入",
    "non_oper_exp": "营业外支出",
    "total_profit": "利润总额",
    "income_tax": "所得税费用",
    "n_income": "净利润（合并）",
    "minority_gain": "少数股东损益",
    "n_income_attr_p": "归属于母公司股东的净利润",
    "n_cashflow_act": "经营活动产生的现金流量净额",
    "n_cashflow_inv_act": "投资活动产生的现金流量净额",
    "n_cash_flows_fnc_act": "筹资活动产生的现金流量净额",
}


class FinancialDataError(ValueError):
    """Raised when the source frames cannot support a requested calculation."""


def _clean_end_date(value: Any) -> str:
    raw = str(value).strip()
    digits = "".join(character for character in raw if character.isdigit())
    return digits[:8]


def quarter_of(end_date: str) -> int:
    quarter = QUARTER_BY_MMDD.get(end_date[4:8])
    if quarter is None:
        raise FinancialDataError(f"unsupported period end (not a quarter end): {end_date}")
    return quarter


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _timestamp(value: Any) -> pd.Timestamp | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp):
        return None
    return stamp


@dataclass(frozen=True)
class QuarterValue:
    """One metric for one quarter, with the derivation status kept explicit."""

    end_date: str
    field: str
    cumulative: float | None
    single: float | None
    status: str
    reason: str = ""
    predecessor: str | None = None

    @property
    def available(self) -> bool:
        return self.status == "ok" and self.single is not None


@dataclass(frozen=True)
class QuarterRecord:
    end_date: str
    year: int
    quarter: int
    available_at: str | None
    status: str
    reason: str
    values: dict[str, QuarterValue] = field(default_factory=dict)


@dataclass(frozen=True)
class QuarterSeries:
    symbol: str
    records: list[QuarterRecord]
    missing_fields: tuple[str, ...] = ()

    def by_end_date(self, end_date: str) -> QuarterRecord | None:
        for record in self.records:
            if record.end_date == end_date:
                return record
        return None

    def latest(self) -> QuarterRecord | None:
        return self.records[-1] if self.records else None

    def usable(self) -> list[QuarterRecord]:
        return [record for record in self.records if record.status == "ok"]


def _comparability_issue(current: pd.Series, previous: pd.Series) -> str:
    """Return a reason when two cumulative rows cannot be subtracted."""

    for column, label in (("report_type", "报表类型"), ("update_flag", "更新标记")):
        if column in current.index and column in previous.index:
            left, right = current.get(column), previous.get(column)
            if pd.notna(left) and pd.notna(right) and str(left) != str(right):
                return f"{label}不一致（{left} vs {right}），视为重述或口径不同，不做无条件差分"
    return ""


def quarterize(
    cumulative: pd.DataFrame,
    *,
    symbol: str,
    flow_fields: Sequence[str] = INCOME_FLOW_FIELDS,
) -> QuarterSeries:
    """Turn cumulative rows into explicit single-quarter values.

    A row qualifies for differencing only when the immediately preceding
    quarter of the same fiscal year exists and is comparable. Otherwise the
    single-quarter value stays ``None`` with a stated reason.
    """

    if cumulative is None or cumulative.empty:
        return QuarterSeries(symbol=symbol, records=[])
    frame = cumulative.copy()
    if "end_date" not in frame.columns:
        raise FinancialDataError("cumulative frame requires end_date")
    frame["end_date"] = frame["end_date"].map(_clean_end_date)
    frame = frame[frame["end_date"].str.len() == 8]
    frame = frame.drop_duplicates(subset=["end_date"], keep="last")
    frame = frame.sort_values("end_date").reset_index(drop=True)
    available_column = "available_at" if "available_at" in frame.columns else None
    missing = tuple(field for field in flow_fields if field not in frame.columns)

    by_key: dict[tuple[int, int], pd.Series] = {}
    for _, row in frame.iterrows():
        try:
            quarter = quarter_of(str(row["end_date"]))
        except FinancialDataError:
            continue
        by_key[(int(str(row["end_date"])[:4]), quarter)] = row

    records: list[QuarterRecord] = []
    for (year, quarter), row in sorted(by_key.items()):
        end_date = str(row["end_date"])
        stamp = _timestamp(row.get(available_column)) if available_column else None
        record_status = "ok"
        record_reason = ""
        predecessor_key = (year, quarter - 1)
        predecessor: pd.Series | None = None
        if quarter > 1:
            predecessor = by_key.get(predecessor_key)
            if predecessor is None:
                record_status = "missing_predecessor"
                record_reason = (
                    f"缺 {year}Q{quarter - 1} 累计数，无法拆出单季；"
                    "不借用更早期间，避免把半年或九个月数当单季"
                )
            else:
                issue = _comparability_issue(row, predecessor)
                if issue:
                    record_status = "restated_incomparable"
                    record_reason = issue
        values: dict[str, QuarterValue] = {}
        for field_name in flow_fields:
            cumulative_value = _number(row.get(field_name))
            if field_name not in frame.columns:
                values[field_name] = QuarterValue(
                    end_date=end_date,
                    field=field_name,
                    cumulative=None,
                    single=None,
                    status="missing_field",
                    reason="原始库无该科目",
                )
                continue
            if quarter == 1:
                single = cumulative_value
                status = "ok" if single is not None else "missing_value"
                reason = "" if status == "ok" else "源表该科目为空"
            elif record_status != "ok":
                single = None
                status = record_status
                reason = record_reason
            else:
                assert predecessor is not None
                previous_value = _number(predecessor.get(field_name))
                if cumulative_value is None or previous_value is None:
                    single = None
                    status = "missing_value"
                    reason = "本期或上期该科目为空"
                else:
                    single = cumulative_value - previous_value
                    status = "ok"
                    reason = ""
            values[field_name] = QuarterValue(
                end_date=end_date,
                field=field_name,
                cumulative=cumulative_value,
                single=single,
                status=status,
                reason=reason,
                predecessor=str(predecessor["end_date"]) if predecessor is not None else None,
            )
        records.append(
            QuarterRecord(
                end_date=end_date,
                year=year,
                quarter=quarter,
                available_at=stamp.isoformat(sep=" ") if stamp is not None else None,
                status=record_status,
                reason=record_reason,
                values=values,
            )
        )
    return QuarterSeries(symbol=symbol, records=records, missing_fields=missing)


def merge_availability(
    series: QuarterSeries,
    frames: Iterable[pd.DataFrame | None],
) -> QuarterSeries:
    """Re-derive ``available_at`` as the max of every input frame's timestamp."""

    extra: dict[str, pd.Timestamp] = {}
    for frame in frames:
        if frame is None or frame.empty or "end_date" not in frame.columns:
            continue
        column = "available_at" if "available_at" in frame.columns else None
        if column is None:
            continue
        for _, row in frame.iterrows():
            end_date = _clean_end_date(row["end_date"])
            stamp = _timestamp(row.get(column))
            if stamp is None:
                continue
            current = extra.get(end_date)
            if current is None or stamp > current:
                extra[end_date] = stamp
    updated: list[QuarterRecord] = []
    for record in series.records:
        stamp = extra.get(record.end_date)
        current = _timestamp(record.available_at)
        if stamp is not None and (current is None or stamp > current):
            current = stamp
        updated.append(
            QuarterRecord(
                end_date=record.end_date,
                year=record.year,
                quarter=record.quarter,
                available_at=current.isoformat(sep=" ") if current is not None else None,
                status=record.status,
                reason=record.reason,
                values=record.values,
            )
        )
    return QuarterSeries(symbol=series.symbol, records=updated, missing_fields=series.missing_fields)


def _single(record: QuarterRecord, field_name: str) -> float | None:
    value = record.values.get(field_name)
    if value is None or value.status != "ok":
        return None
    return value.single


def _sum_or_none(values: Sequence[float | None]) -> float | None:
    if any(value is None for value in values):
        return None
    total = 0.0
    for value in values:
        assert value is not None
        total += value
    return total


def gross_profit(record: QuarterRecord) -> float | None:
    revenue = _single(record, "revenue")
    cost = _single(record, "oper_cost")
    if revenue is None or cost is None:
        return None
    return revenue - cost


def _check(
    check_id: str,
    label: str,
    reported: float | None,
    computed: float | None,
    *,
    formula: str,
    tolerance: float = ABSOLUTE_TOLERANCE,
) -> dict[str, Any]:
    if reported is None or computed is None:
        return {
            "check_id": check_id,
            "label": label,
            "status": "unavailable",
            "reported": reported,
            "computed": computed,
            "difference": None,
            "tolerance": tolerance,
            "formula": formula,
        }
    difference = computed - reported
    ok = abs(difference) <= tolerance
    return {
        "check_id": check_id,
        "label": label,
        "status": "pass" if ok else "fail",
        "reported": reported,
        "computed": computed,
        "difference": difference,
        "tolerance": tolerance,
        "formula": formula,
    }


def bridge_for(record: QuarterRecord) -> dict[str, Any]:
    """Build the income bridge for one quarter and reconcile it to the source."""

    revenue = _single(record, "revenue")
    cost = _single(record, "oper_cost")
    gross = gross_profit(record)
    biz_tax = _single(record, "biz_tax_surchg")
    sell = _single(record, "sell_exp")
    admin = _single(record, "admin_exp")
    rd = _single(record, "rd_exp")
    fin = _single(record, "fin_exp")
    fair_value = _single(record, "fv_value_chg_gain")
    invest = _single(record, "invest_income")
    impairment = _single(record, "assets_impair_loss")
    operate_profit = _single(record, "operate_profit")
    non_oper_income = _single(record, "non_oper_income")
    non_oper_exp = _single(record, "non_oper_exp")
    total_profit = _single(record, "total_profit")
    tax = _single(record, "income_tax")
    net = _single(record, "n_income")
    minority = _single(record, "minority_gain")
    parent_net = _single(record, "n_income_attr_p")

    known_cost = _sum_or_none([biz_tax, sell, admin, rd, fin])
    known_gain = _sum_or_none([fair_value, invest, impairment])
    unlisted: float | None = None
    if (
        gross is not None
        and known_cost is not None
        and known_gain is not None
        and operate_profit is not None
    ):
        unlisted = operate_profit - (gross - known_cost + known_gain)

    lines = [
        {"line_id": "revenue", "label": LABELS["revenue"], "amount": revenue, "sign": 1},
        {"line_id": "oper_cost", "label": LABELS["oper_cost"], "amount": cost, "sign": -1},
        {"line_id": "gross_profit", "label": LABELS["gross_profit"], "amount": gross, "sign": 1},
        {"line_id": "biz_tax_surchg", "label": LABELS["biz_tax_surchg"], "amount": biz_tax, "sign": -1},
        {"line_id": "sell_exp", "label": LABELS["sell_exp"], "amount": sell, "sign": -1},
        {"line_id": "admin_exp", "label": LABELS["admin_exp"], "amount": admin, "sign": -1},
        {"line_id": "rd_exp", "label": LABELS["rd_exp"], "amount": rd, "sign": -1},
        {"line_id": "fin_exp", "label": LABELS["fin_exp"], "amount": fin, "sign": -1},
        {"line_id": "fv_value_chg_gain", "label": LABELS["fv_value_chg_gain"], "amount": fair_value, "sign": 1},
        {"line_id": "invest_income", "label": LABELS["invest_income"], "amount": invest, "sign": 1},
        {"line_id": "assets_impair_loss", "label": LABELS["assets_impair_loss"], "amount": impairment, "sign": 1},
        {
            "line_id": "unlisted_net",
            "label": LABELS["unlisted_net"],
            "amount": unlisted,
            "sign": 1,
            "is_residual": True,
            "note": "残差项。它只是让营业利润闭合，不能据此把差额归因到套保、汇兑或主营。",
        },
        {"line_id": "operate_profit", "label": LABELS["operate_profit"], "amount": operate_profit, "sign": 1},
        {"line_id": "non_oper_income", "label": LABELS["non_oper_income"], "amount": non_oper_income, "sign": 1},
        {"line_id": "non_oper_exp", "label": LABELS["non_oper_exp"], "amount": non_oper_exp, "sign": -1},
        {"line_id": "total_profit", "label": LABELS["total_profit"], "amount": total_profit, "sign": 1},
        {"line_id": "income_tax", "label": LABELS["income_tax"], "amount": tax, "sign": -1},
        {"line_id": "n_income", "label": LABELS["n_income"], "amount": net, "sign": 1},
        {"line_id": "minority_gain", "label": LABELS["minority_gain"], "amount": minority, "sign": -1},
        {"line_id": "n_income_attr_p", "label": LABELS["n_income_attr_p"], "amount": parent_net, "sign": 1},
    ]

    computed_total_profit = (
        operate_profit + non_oper_income - non_oper_exp
        if operate_profit is not None
        and non_oper_income is not None
        and non_oper_exp is not None
        else None
    )
    computed_net = (
        total_profit - tax if total_profit is not None and tax is not None else None
    )
    computed_parent = net - minority if net is not None and minority is not None else None
    checks = [
        _check(
            "total_profit",
            "利润总额 = 营业利润 + 营业外收入 − 营业外支出",
            total_profit,
            computed_total_profit,
            formula="operate_profit + non_oper_income - non_oper_exp",
        ),
        _check(
            "n_income",
            "合并净利润 = 利润总额 − 所得税费用",
            net,
            computed_net,
            formula="total_profit - income_tax",
        ),
        _check(
            "n_income_attr_p",
            "归母净利润 = 合并净利润 − 少数股东损益",
            parent_net,
            computed_parent,
            formula="n_income - minority_gain",
        ),
    ]
    return {
        "end_date": record.end_date,
        "status": record.status,
        "reason": record.reason,
        "available_at": record.available_at,
        "unit": UNIT,
        "lines": lines,
        "checks": checks,
        "residual": unlisted,
        "residual_note": (
            "未单列项目净额是残差，包含其他收益、信用减值损失、资产处置收益等未进入"
            "本模块明细的科目；不得把残差整体解释为某一业务或套保损益。"
        ),
    }


def qoq_contributions(
    previous: QuarterRecord,
    current: QuarterRecord,
) -> dict[str, Any]:
    """Explain the accounting-line change between two adjacent quarters."""

    line_fields = (
        "revenue",
        "oper_cost",
        "gross_profit",
        "biz_tax_surchg",
        "sell_exp",
        "admin_exp",
        "rd_exp",
        "fin_exp",
        "fv_value_chg_gain",
        "invest_income",
        "assets_impair_loss",
        "operate_profit",
        "total_profit",
        "income_tax",
        "n_income",
        "minority_gain",
        "n_income_attr_p",
    )
    contributions: list[dict[str, Any]] = []
    for field_name in line_fields:
        if field_name == "gross_profit":
            before, after = gross_profit(previous), gross_profit(current)
        else:
            before, after = _single(previous, field_name), _single(current, field_name)
        change = after - before if before is not None and after is not None else None
        contributions.append(
            {
                "line_id": field_name,
                "label": LABELS.get(field_name, field_name),
                "previous": before,
                "current": after,
                "change": change,
                "unit": UNIT,
            }
        )
    parent_before = _single(previous, "n_income_attr_p")
    parent_after = _single(current, "n_income_attr_p")
    parent_change = (
        parent_after - parent_before
        if parent_before is not None and parent_after is not None
        else None
    )
    return {
        "from": previous.end_date,
        "to": current.end_date,
        "parent_change": parent_change,
        "parent_change_pct": (
            parent_change / parent_before
            if parent_change is not None and parent_before
            else None
        ),
        "contributions": contributions,
        "interpretation": (
            "这里解释的是会计科目的变化，不自动认定为业务因果；"
            "环比也未剔除季节性，不能声称“环比天然排除季节因素”。"
        ),
    }


def ratios(record: QuarterRecord) -> dict[str, Any]:
    """Quarterly ratios with explicit numerator and denominator."""

    revenue = _single(record, "revenue")
    parent_net = _single(record, "n_income_attr_p")
    net = _single(record, "n_income")
    gross = gross_profit(record)
    return {
        "end_date": record.end_date,
        "gross_margin": {
            "value": (gross / revenue) if gross is not None and revenue else None,
            "numerator": "单季毛利（营业收入 − 营业成本）",
            "denominator": "单季营业收入",
            "period": "single_quarter",
        },
        "parent_net_margin": {
            "value": (parent_net / revenue) if parent_net is not None and revenue else None,
            "numerator": "单季归母净利润",
            "denominator": "单季营业收入",
            "period": "single_quarter",
        },
        "consolidated_net_margin": {
            "value": (net / revenue) if net is not None and revenue else None,
            "numerator": "单季合并净利润（含少数股东）",
            "denominator": "单季营业收入",
            "period": "single_quarter",
        },
        "cash_conversion": cash_conversion(record),
    }


def cash_conversion(record: QuarterRecord) -> dict[str, Any]:
    """Operating cash flow against consolidated net profit, not parent net."""

    operating = _single(record, "n_cashflow_act")
    net = _single(record, "n_income")
    parent = _single(record, "n_income_attr_p")
    consistent = operating / net if operating is not None and net else None
    parent_based = operating / parent if operating is not None and parent else None
    return {
        "consistent_ratio": {
            "value": consistent,
            "numerator": "单季经营活动现金流量净额",
            "denominator": "单季合并净利润（含少数股东）",
            "note": "分子分母同为合并口径，可作为一致性指标。",
        },
        "parent_based_ratio": {
            "value": parent_based,
            "numerator": "单季经营活动现金流量净额",
            "denominator": "单季归母净利润",
            "note": (
                "分母含少数股东损益时口径不匹配，仅作参考；"
                "大于 100% 并不等于“健康”，也不构成结论。"
            ),
        },
    }


def simple_annualized_roe(record: QuarterRecord, equity: float | None) -> dict[str, Any]:
    """Period-end-equity simple annualisation, explicitly not official ROE."""

    parent_net = _single(record, "n_income_attr_p")
    value: float | None = None
    if parent_net is not None and equity not in (None, 0):
        assert equity is not None
        value = parent_net / equity * (12 / (record.quarter * 3))
    return {
        "value": value,
        "definition": "期末净资产口径简单年化 ROE 估算",
        "formula": "单季归母净利润 ÷ 期末归母净资产 × (12 ÷ 报告期月数)",
        "period": f"YTD to Q{record.quarter}, annualised by 12/{record.quarter * 3}",
        "not_official": True,
        "note": (
            "这不是公司披露的加权平均净资产收益率。官方加权 ROE 需单列来源与期间，"
            "不得用 PB/PE 隐含比值代替。"
        ),
    }


def build_ttm(
    series: QuarterSeries,
    field_name: str,
    *,
    end_date: str,
) -> dict[str, Any]:
    """TTM = latest complete FY + current YTD − prior-year same YTD."""

    record = series.by_end_date(end_date)
    if record is None:
        return {"status": "unavailable", "reason": f"{end_date} 不在季度序列内", "value": None}
    if field_name not in record.values:
        return {"status": "unavailable", "reason": f"缺科目 {field_name}", "value": None}
    year = record.year
    prior = series.by_end_date(f"{year - 1}{end_date[4:]}")
    fiscal = series.by_end_date(f"{year - 1}1231")
    legs = {
        "current_ytd": (
            record.values[field_name].cumulative,
            record.available_at,
        ),
        "prior_year_same_ytd": (
            prior.values[field_name].cumulative if prior is not None else None,
            prior.available_at if prior is not None else None,
        ),
        "prior_complete_fy": (
            fiscal.values[field_name].cumulative if fiscal is not None else None,
            fiscal.available_at if fiscal is not None else None,
        ),
    }
    missing = [name for name, (value, _) in legs.items() if value is None]
    if missing:
        return {
            "status": "unavailable",
            "reason": "缺少 TTM 的一条腿：" + "、".join(missing),
            "value": None,
            "legs": {name: value for name, (value, _) in legs.items()},
        }
    current_ytd = legs["current_ytd"][0]
    prior_ytd = legs["prior_year_same_ytd"][0]
    fiscal_fy = legs["prior_complete_fy"][0]
    assert current_ytd is not None and prior_ytd is not None and fiscal_fy is not None
    value = fiscal_fy + current_ytd - prior_ytd
    stamps = [stamp for _, stamp in legs.values() if stamp is not None]
    available = max(stamps) if stamps else None
    return {
        "status": "ok",
        "value": value,
        "unit": UNIT,
        "formula": "上年全年 + 本年累计 − 上年同期累计",
        "legs": {name: value for name, (value, _) in legs.items()},
        "available_at": available,
    }


def quarters_frame(series: QuarterSeries, extra: dict[str, dict[str, float | None]] | None = None) -> pd.DataFrame:
    """Wide, inspection-friendly table of cumulative and single-quarter values."""

    fields = sorted({field for record in series.records for field in record.values})
    rows: list[dict[str, Any]] = []
    for record in series.records:
        row: dict[str, Any] = {
            "end_date": record.end_date,
            "year": record.year,
            "quarter": record.quarter,
            "available_at": record.available_at,
            "quarter_status": record.status,
            "quarter_reason": record.reason,
        }
        cumulative_values: list[float | None] = []
        single_values: list[float | None] = []
        for field_name in fields:
            value = record.values.get(field_name)
            if value is None:
                continue
            row[field_name] = value.cumulative
            row[f"{field_name}_q"] = value.single
            cumulative_values.append(value.cumulative)
            single_values.append(value.single)
        if extra and record.end_date in extra:
            for key, extra_value in extra[record.end_date].items():
                row[key] = extra_value
        row["gross_profit_q"] = gross_profit(record)
        row["gross_margin_q"] = ratios(record)["gross_margin"]["value"]
        row["roe_simple_annualised_estimate"] = simple_annualized_roe(
            record, _single_equity(record)
        )["value"]
        rows.append(row)
    frame = pd.DataFrame(rows)
    if not frame.empty:
        ordered = ["end_date", "year", "quarter", "available_at", "quarter_status", "quarter_reason"]
        frame = frame[[column for column in ordered if column in frame.columns] +
                      [column for column in frame.columns if column not in ordered]]
    return frame


def _single_equity(record: QuarterRecord) -> float | None:
    """Equity is a stock; the datapack attaches it as a pseudo cumulative value."""

    value = record.values.get("total_hldr_eqy_exc_min_int")
    if value is None:
        return None
    return value.cumulative


def build_financial_artifacts(
    *,
    out_dir: Path,
    symbol: str,
    asof: str,
    income: pd.DataFrame | None,
    cashflow: pd.DataFrame | None,
    balance: pd.DataFrame | None,
    official_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write ``financial_quarters.csv`` and ``earnings_bridge.json``."""

    flow_fields = list(INCOME_FLOW_FIELDS) + [
        field_name for field_name in CASH_FLOW_FIELDS if cashflow is not None
        and field_name in cashflow.columns
    ]
    combined_parts: list[pd.DataFrame] = []
    if income is not None and not income.empty:
        combined_parts.append(income)
    if cashflow is not None and not cashflow.empty:
        keep = [column for column in ("end_date", "available_at", *CASH_FLOW_FIELDS) if column in cashflow.columns]
        combined_parts.append(cashflow[keep])
    if not combined_parts:
        return {"status": "unavailable", "reason": "无财务三表输入"}

    base = combined_parts[0].copy()
    for part in combined_parts[1:]:
        base = base.merge(part, on="end_date", how="outer", suffixes=("", "_cash"))
    base = base.sort_values("end_date").reset_index(drop=True)

    series = quarterize(base, symbol=symbol, flow_fields=flow_fields)
    series = merge_availability(series, [income, cashflow, balance])

    equity_by_end: dict[str, float | None] = {}
    if balance is not None and not balance.empty and "end_date" in balance.columns:
        equity_column = "total_hldr_eqy_exc_min_int"
        if equity_column in balance.columns:
            for _, row in balance.iterrows():
                equity_by_end[_clean_end_date(row["end_date"])] = _number(row.get(equity_column))

    # Attach equity as a pseudo cumulative value so the report can show the
    # period-end stock without differencing it.
    for index, record in enumerate(series.records):
        equity = equity_by_end.get(record.end_date)
        if equity is None:
            continue
        values = dict(record.values)
        values["total_hldr_eqy_exc_min_int"] = QuarterValue(
            end_date=record.end_date,
            field="total_hldr_eqy_exc_min_int",
            cumulative=equity,
            single=None,
            status="stock",
            reason="期末存量，不做单季差分",
        )
        series.records[index] = QuarterRecord(
            end_date=record.end_date,
            year=record.year,
            quarter=record.quarter,
            available_at=record.available_at,
            status=record.status,
            reason=record.reason,
            values=values,
        )

    frame = quarters_frame(series)
    out_dir.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out_dir / "financial_quarters.csv", index=False, encoding="utf-8")

    bridges = [bridge_for(record) for record in series.records if record.status == "ok"]
    checks = [check for bridge in bridges for check in bridge["checks"]]
    qoq = []
    for previous, current in zip(series.records, series.records[1:]):
        if previous.status == "ok" and current.status == "ok":
            qoq.append(qoq_contributions(previous, current))
    latest = series.latest()
    ttm_parent = build_ttm(series, "n_income_attr_p", end_date=latest.end_date) if latest else {"status": "unavailable"}
    ttm_revenue = build_ttm(series, "revenue", end_date=latest.end_date) if latest else {"status": "unavailable"}

    payload = {
        "schema_version": SCHEMA_VERSION,
        "calc_rules_version": CALC_RULES_VERSION,
        "symbol": symbol,
        "asof": asof,
        "unit": UNIT,
        "generated_from": "local cumulative statements (report_type=1)",
        "periods": [record.end_date for record in series.records],
        "quarters": bridges,
        "qoq": qoq,
        "ttm": {
            "n_income_attr_p": ttm_parent,
            "revenue": ttm_revenue,
        },
        "checks": checks,
        "checks_summary": {
            "pass": sum(1 for check in checks if check["status"] == "pass"),
            "fail": sum(1 for check in checks if check["status"] == "fail"),
            "unavailable": sum(1 for check in checks if check["status"] == "unavailable"),
        },
        "notes": [
            "单季值只按同年度相邻季度差分；缺上一季则标不可得，不倒推。",
            "存量科目（资产负债、股数）不做单季差分。",
            "未单列项目净额是残差，不能据此把差额归因到套保、汇兑或主营业务。",
            "容差 1 元，按源表元级数据对账，不用展示层四舍五入掩盖差异。",
        ],
    }
    (out_dir / "earnings_bridge.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    adjustments = build_adjustments(
        series=series,
        official_metrics=official_metrics,
    )
    (out_dir / "financial_adjustments.json").write_text(
        json.dumps(adjustments, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return {
        "status": "ok",
        "series": series,
        "frame": frame,
        "bridge": payload,
        "adjustments": adjustments,
        "checks_summary": payload["checks_summary"],
    }


DEFAULT_ADJUSTMENT_TAX_RATE = 0.23


def build_adjustments(
    *,
    series: QuarterSeries,
    official_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Three separate earnings lines: reported, official deducted, adjusted.

    The adjusted scenario recomputes income tax at a single flat rate on the
    adjusted pretax profit and keeps the reported minority interest. Both are
    assumptions of this scenario, not facts about the company.
    """

    tax_rate = DEFAULT_ADJUSTMENT_TAX_RATE
    official = (official_metrics or {}).get("periods", {})
    rows: list[dict[str, Any]] = []
    for record in series.records:
        total_profit = _single(record, "total_profit")
        impairment = _single(record, "assets_impair_loss")
        minority = _single(record, "minority_gain")
        parent = _single(record, "n_income_attr_p")
        official_row = official.get(record.end_date) or {}
        credit = _number(official_row.get("credit_impair"))
        adjusted_asset = adjusted_asset_credit = None
        if (
            total_profit is not None
            and impairment is not None
            and minority is not None
        ):
            adjusted_pretax = total_profit - impairment
            adjusted_asset = adjusted_pretax * (1 - tax_rate) - minority
            if credit is not None:
                adjusted_asset_credit = (adjusted_pretax - credit) * (1 - tax_rate) - minority
        rows.append(
            {
                "end_date": record.end_date,
                "quarter_status": record.status,
                "reported_parent_net": parent,
                "official_deducted_parent_net": (
                    _number(official_row.get("deduct_parent"))
                ),
                "official_source_url": official_row.get("url"),
                "official_location": official_row.get("location"),
                "credit_impairment": credit,
                "asset_impairment": impairment,
                "adjusted_parent_net_strip_asset": adjusted_asset,
                "adjusted_parent_net_strip_asset_and_credit": adjusted_asset_credit,
                "unit": UNIT,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "calc_rules_version": CALC_RULES_VERSION,
        "symbol": series.symbol,
        "scenario": {
            "scenario_id": "strip-impairment-flat-tax",
            "label": "本次核对情景：剔除资产减值（可再剔除信用减值）",
            "tax_rate": tax_rate,
            "tax_treatment": "flat_rate_recompute",
            "minority_treatment": "fixed_reported_actual",
            "formula": "(利润总额 − 剔除项) × (1 − 税率) − 当季实际少数股东损益",
            "assumptions": [
                f"统一按 {tax_rate:.0%} 重算所得税；这不是公司实际税率，也不是默认真值。",
                "少数股东损益沿用报表实际数并保持不变，属于简化。",
                "剔除项按报表符号处理：计提为负、转回为正，不能只加回损失而保留转回收益。",
                "调整后单季利润不得乘以 4 命名为可持续年利润；预测必须另有假设。",
            ],
            "not_sustainable_earnings": True,
        },
        "periods": rows,
        "line_types": {
            "reported_parent_net": "报表归母：原披露值",
            "official_deducted_parent_net": "官方扣非归母：公告来源，缺失即标缺失",
            "adjusted_parent_net_strip_asset": "分析调整情景：仅剔资产减值",
            "adjusted_parent_net_strip_asset_and_credit": "分析调整情景：再剔信用减值",
        },
        "disclosures": [
            "信用减值来自公司公告补录；本地利润表无该列时不假设其存在。",
            "资产减值与信用减值分列，避免同一损失被加回两次。",
            "公允价值变动与投资收益合计不等于套保损益；联营合营投资收益需单独列出。",
        ],
    }
