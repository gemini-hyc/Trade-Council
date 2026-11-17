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
from datetime import date
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
MMDD_BY_QUARTER = {value: key for key, value in QUARTER_BY_MMDD.items()}


def _quarter_end(quarter: int) -> str:
    return MMDD_BY_QUARTER[quarter]

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
    comparability: str = "unconfirmed"


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


def _version_metadata(
    frame: pd.DataFrame, flow_fields: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Describe how many disclosure versions each period has in this frame.

    One row per period is normal but proves nothing about restatements: the
    local store keeps only the latest version, so the honest state is
    ``unconfirmed``. Two rows with different values for the same period is a
    restatement that must not be differenced across.
    """

    stamp_column = None
    for candidate in ("f_ann_date", "ann_date", "available_at"):
        if candidate in frame.columns:
            stamp_column = candidate
            break
    meta: dict[str, dict[str, Any]] = {}
    for end_date, group in frame.groupby("end_date", sort=False):
        key = str(end_date)
        ordered = group
        if stamp_column is not None:
            ordered = group.assign(
                _stamp=pd.to_datetime(group[stamp_column], errors="coerce")
            ).sort_values("_stamp")
        first = ordered.iloc[0]
        last = ordered.iloc[-1]
        changed = False
        if len(ordered) > 1:
            for field_name in flow_fields:
                if field_name not in ordered.columns:
                    continue
                left, right = _number(first.get(field_name)), _number(last.get(field_name))
                if left != right:
                    changed = True
                    break
        meta[key] = {
            "versions": len(ordered),
            "changed": changed,
            "first_announce": (
                str(first.get(stamp_column)) if stamp_column is not None else None
            ),
            "last_announce": (
                str(last.get(stamp_column)) if stamp_column is not None else None
            ),
            "update_flags": sorted(
                {
                    str(value)
                    for value in group.get("update_flag", pd.Series(dtype=object)).tolist()
                    if value is not None and str(value) not in {"nan", "None"}
                }
            ),
        }
    return meta


def _comparability_issue(current: pd.Series, previous: pd.Series) -> str:
    """Return a reason when two cumulative rows cannot be subtracted.

    Only the statement type is compared across periods. ``update_flag`` is a
    per-disclosure marker, so a difference between two different periods says
    nothing about comparability; treating it as a restatement signal produced
    both false positives and false confidence.
    """

    if "report_type" in current.index and "report_type" in previous.index:
        left, right = current.get("report_type"), previous.get("report_type")
        if pd.notna(left) and pd.notna(right) and str(left) != str(right):
            return f"报表类型不一致（{left} vs {right}），不做无条件差分"
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
    # Keep every disclosure version long enough to see whether the same period
    # was restated, then difference only within the latest version.
    version_meta = _version_metadata(frame, flow_fields)
    announce_column = next(
        (name for name in ("f_ann_date", "ann_date", "available_at") if name in frame.columns),
        None,
    )
    if announce_column is not None:
        frame = frame.assign(
            _announce_order=pd.to_datetime(frame[announce_column], errors="coerce")
        ).sort_values(["end_date", "_announce_order"])
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
        own_meta = version_meta.get(end_date) or {}
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
                else:
                    previous_meta = version_meta.get(str(predecessor["end_date"])) or {}
                    previous_stamp = (
                        _timestamp(predecessor.get(available_column))
                        if available_column
                        else None
                    )
                    if own_meta.get("changed"):
                        record_status = "restated_incomparable"
                        record_reason = (
                            "本季同一报告期存在多份披露版本且数值不同，"
                            "与上一季相减会混用版本，故单季不可得"
                        )
                    elif previous_meta.get("changed"):
                        record_status = "restated_incomparable"
                        record_reason = (
                            f"{predecessor['end_date']} 存在重述版本，"
                            "与其相减会混用重述前后数据，故单季不可得"
                        )
                    elif (
                        previous_stamp is not None
                        and stamp is not None
                        and previous_stamp > stamp
                    ):
                        record_status = "restated_incomparable"
                        record_reason = (
                            f"上一季累计数在本季披露之后才更新（{previous_stamp.date()} > "
                            f"{stamp.date()}），说明存在事后重述，不做无条件差分"
                        )
        if own_meta.get("changed"):
            comparability = "changed"
        elif own_meta.get("versions", 1) > 1:
            comparability = "confirmed"
        else:
            comparability = "unconfirmed"
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
                comparability=comparability,
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
                comparability=record.comparability,
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
    """Simple annualisation on period-end equity, in two explicit versions.

    The numerator and the annualisation factor must describe the same period:
    a year-to-date profit is scaled by ``12 / months_reported``, while a single
    quarter is scaled by 4. Mixing a single-quarter numerator with the YTD
    factor understates ROE by roughly the number of quarters elapsed, which is
    why both are reported separately instead of one blended number.
    """

    ytd_parent = record.values.get("n_income_attr_p")
    ytd_value = ytd_parent.cumulative if ytd_parent is not None else None
    single_value = _single(record, "n_income_attr_p")
    months = record.quarter * 3
    ytd_roe: float | None = None
    single_roe: float | None = None
    if equity is not None and equity != 0:
        if ytd_value is not None:
            ytd_roe = ytd_value / equity * (12 / months)
        if single_value is not None:
            single_roe = single_value / equity * 4
    return {
        "ytd_value": ytd_roe,
        "single_quarter_value": single_roe,
        "definition": "期末净资产口径简单年化 ROE 估算（两种口径分列）",
        "ytd_formula": "年初至本期累计归母净利润 ÷ 期末归母净资产 × (12 ÷ 累计报告月数)",
        "single_quarter_formula": "单季归母净利润 ÷ 期末归母净资产 × 4",
        "period": f"YTD to Q{record.quarter}（{months} 个月）",
        "not_official": True,
        "note": (
            "这不是公司披露的加权平均净资产收益率。两种口径不能互相替代："
            "累计口径适合与年度 ROE 比较，单季口径反映当期盈利能力但不代表全年。"
            "官方加权 ROE 需单列来源与期间，不得用 PB/PE 隐含比值代替。"
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
    # TTM mixes three disclosure versions, so it inherits the weakest
    # comparability of its legs instead of silently calling the result clean.
    # Checking the legs alone is not enough: the current leg is a year-to-date
    # figure that spans every quarter of the fiscal year, so a restatement of
    # any of those quarters means the cumulative number may predate the restated
    # values. The window is therefore (prior FY end, current end date], plus the
    # two prior-year legs.
    fiscal_end = f"{year - 1}1231"
    window = [
        item for item in series.records if fiscal_end < item.end_date <= record.end_date
    ]
    involved = {item.end_date: item for item in [*window, record, prior, fiscal] if item}
    restated = sorted(
        end_date_
        for end_date_, item in involved.items()
        if item.comparability == "changed" or item.status == "restated_incomparable"
    )
    leg_states = [
        item.comparability for item in (record, prior, fiscal) if item is not None
    ]
    if restated:
        comparability = "changed"
        comparability_note = (
            "TTM 窗口内存在重述期次（" + "、".join(restated) + "）："
            "本地库保留了同一报告期的多份披露且数值不同，三条累计腿可能取自重述前版本，"
            "跨期比较需注明口径"
        )
    elif leg_states and all(state == "confirmed" for state in leg_states):
        comparability = "confirmed"
        comparability_note = ""
    else:
        comparability = "unconfirmed"
        comparability_note = (
            "三条腿来自三次不同披露；本地库只保留每期最新版本，"
            "无法证明与当时披露一致（unconfirmed），跨期比较需注明"
        )
    return {
        "status": "ok",
        "value": value,
        "unit": UNIT,
        "formula": "上年全年 + 本年累计 − 上年同期累计",
        "legs": {name: value for name, (value, _) in legs.items()},
        "available_at": available,
        "comparability": comparability,
        "comparability_note": comparability_note,
        "comparability_window": sorted(involved),
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
            "comparability": record.comparability,
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
        roe = simple_annualized_roe(record, _single_equity(record))
        row["roe_simple_annualised_ytd"] = roe["ytd_value"]
        row["roe_simple_annualised_single_q"] = roe["single_quarter_value"]
        rows.append(row)
    frame = pd.DataFrame(rows)
    if not frame.empty:
        ordered = [
            "end_date",
            "year",
            "quarter",
            "available_at",
            "quarter_status",
            "quarter_reason",
            "comparability",
        ]
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
    adjustment_tax_rate: float | None = None,
) -> dict[str, Any]:
    """Write ``financial_quarters.csv``, ``earnings_bridge.json`` and adjustments."""

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
            comparability=record.comparability,
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
            (
                "comparability=unconfirmed 表示本地库每期只有一个披露版本，"
                "无法证明与当时披露一致；changed 表示同一期间出现数值不同的多份披露，"
                "跨期相减会混用版本。"
            ),
        ],
    }
    (out_dir / "earnings_bridge.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    adjustments = build_adjustments(
        series=series,
        official_metrics=official_metrics,
        tax_rate=adjustment_tax_rate,
        tax_rate_source="scenario" if adjustment_tax_rate is not None else "default",
        asof=asof,
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

OFFICIAL_METRIC_FIELDS = ("deduct_parent", "credit_impair")
OFFICIAL_UNITS = {"CNY": "CNY", "RMB": "CNY", "元": "CNY", "人民币元": "CNY", "人民币": "CNY"}


def _parse_calendar_date(value: Any) -> date | None:
    text = str(value).strip()[:10].replace("/", "-")
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if len(text) != 10:
        return None
    try:
        return date(int(text[:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


def validate_official_metrics(
    official_metrics: dict[str, Any] | None,
    *,
    symbol: str,
    asof: str = "",
) -> dict[str, Any]:
    """Check a hand-entered official supplement before it is treated as official.

    An official number is only as good as its four anchors: which company, which
    period, which unit, and when the *source itself* became public. The last one
    is the trap. Borrowing the announcement time of the financial-statement
    record would let a restatement published later — or a figure typed in after
    the fact — claim it was knowable on the as-of date, which is exactly the
    failure mode a point-in-time audit exists to catch. Each period must
    therefore carry its own ``available_at``, and this function refuses to fill
    the field in from anything else.
    """

    if official_metrics is None:
        return {"periods": {}}
    if not isinstance(official_metrics, dict):
        raise FinancialDataError(
            '官方补录必须是 JSON 对象，形如 {"periods": {"20260930": {...}}}'
        )
    if "periods" not in official_metrics:
        raise FinancialDataError(
            '官方补录缺少 periods 字段；本次没有官方数字请写成 {"periods": {}}'
        )
    periods = official_metrics["periods"]
    if not isinstance(periods, dict):
        raise FinancialDataError("官方补录的 periods 必须是「期末日期 → 指标」的对象")
    asof_date = _parse_calendar_date(asof) if asof else None

    checked: dict[str, Any] = {}
    for raw_end, entry in periods.items():
        end_date = str(raw_end).strip()
        period_end = _parse_calendar_date(end_date) if len(end_date) == 8 else None
        if period_end is None:
            raise FinancialDataError(
                f"官方补录的期间键必须是 8 位期末日期（如 20260930），收到 {raw_end!r}"
            )
        if not isinstance(entry, dict):
            raise FinancialDataError(f"{end_date} 的补录内容必须是对象")
        values: dict[str, float] = {}
        for field_name in OFFICIAL_METRIC_FIELDS:
            if entry.get(field_name) is None:
                continue
            raw_value = entry[field_name]
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                raise FinancialDataError(
                    f"{end_date} 的 {field_name} 必须是数字（单位为元），收到 {raw_value!r}"
                )
            values[field_name] = float(raw_value)
        if not values:
            raise FinancialDataError(f"{end_date} 没有给出任何官方指标值")

        unit = str(entry.get("unit") or "").strip()
        if unit not in OFFICIAL_UNITS:
            raise FinancialDataError(
                f"{end_date} 必须写明金额单位（元 或 CNY），收到 {entry.get('unit')!r}；"
                "单位不明时数字无法与其他口径对账"
            )
        code = str(entry.get("ts_code") or "").strip()
        if not code:
            raise FinancialDataError(
                f"{end_date} 必须写明所属公司代码 ts_code；"
                "同一份补录文件里混入别家公司是最难事后发现的错"
            )
        if code != symbol:
            raise FinancialDataError(
                f"{end_date} 的 ts_code={code} 与本次运行标的 {symbol} 不一致"
            )
        stamp_text = str(entry.get("available_at") or "").strip()
        if not stamp_text:
            raise FinancialDataError(
                f"{end_date} 必须写明补录来源自身的披露时间 available_at；"
                "不得沿用财报记录的公告时间，否则事后重述会冒充当时可得"
            )
        stamp = _parse_calendar_date(stamp_text)
        if stamp is None:
            raise FinancialDataError(
                f"{end_date} 的 available_at 无法解析为日期：{stamp_text!r}"
            )
        if stamp < period_end:
            raise FinancialDataError(
                f"{end_date} 的 available_at={stamp} 早于报告期末 {period_end}，不可能成立"
            )
        if asof_date is not None and stamp > asof_date:
            raise FinancialDataError(
                f"{end_date} 的 available_at={stamp} 晚于本次基准日 {asof_date}；"
                "基准日之后的披露不能算作当时可得"
            )
        if not (entry.get("url") or entry.get("source_file")):
            raise FinancialDataError(
                f"{end_date} 必须给出 url 或 source_file，说明数字抄自哪份公告"
            )
        checked[end_date] = {
            "values": values,
            "unit": OFFICIAL_UNITS[unit],
            "ts_code": code,
            "available_at": f"{stamp.isoformat()} 00:00:00",
            "available_at_raw": stamp_text,
            "url": entry.get("url"),
            "source_file": entry.get("source_file"),
            "location": entry.get("location"),
        }
    return {"periods": checked}


def build_adjustments(
    *,
    series: QuarterSeries,
    official_metrics: dict[str, Any] | None = None,
    tax_rate: float | None = None,
    tax_rate_source: str = "default",
    asof: str = "",
) -> dict[str, Any]:
    """Three separate earnings lines: reported, official deducted, adjusted.

    The adjusted scenario recomputes income tax at a single flat rate on the
    adjusted pretax profit and keeps the reported minority interest. Both are
    assumptions of this scenario, not facts about the company. The rate is a
    scenario input, recorded with the result, so changing it changes the
    numbers and the report text through the same path.
    """

    if tax_rate is None:
        tax_rate = DEFAULT_ADJUSTMENT_TAX_RATE
    if not 0 < tax_rate < 1:
        raise FinancialDataError(f"调整情景税率必须是 0~1 之间的小数，收到 {tax_rate!r}")
    scenario_version = f"strip-impairment-flat-tax@{tax_rate:.4f}"
    official = validate_official_metrics(
        official_metrics, symbol=series.symbol, asof=asof
    )["periods"]

    def _official_single(field_name: str) -> dict[str, float | None]:
        """Difference an official cumulative figure inside the same fiscal year."""

        cumulative: dict[str, float | None] = {
            end_date: _number(((official.get(end_date) or {}).get("values") or {}).get(field_name))
            for end_date in official
        }
        single: dict[str, float | None] = {}
        for record in series.records:
            value = cumulative.get(record.end_date)
            if value is None:
                single[record.end_date] = None
                continue
            if record.quarter == 1:
                single[record.end_date] = value
                continue
            previous = series.by_end_date(f"{record.year}{_quarter_end(record.quarter - 1)}")
            previous_value = (
                cumulative.get(previous.end_date) if previous is not None else None
            )
            single[record.end_date] = (
                value - previous_value if previous_value is not None else None
            )
        return single

    official_deduct_single = _official_single("deduct_parent")
    credit_single = _official_single("credit_impair")

    rows: list[dict[str, Any]] = []
    for record in series.records:
        total_profit = _single(record, "total_profit")
        impairment = _single(record, "assets_impair_loss")
        minority = _single(record, "minority_gain")
        tax = _single(record, "income_tax")
        parent = _single(record, "n_income_attr_p")
        official_row = official.get(record.end_date) or {}
        official_values = official_row.get("values") or {}
        credit = credit_single.get(record.end_date)
        credit_cumulative = _number(official_values.get("credit_impair"))
        adjusted_asset = adjusted_asset_credit = None
        adjusted_reason: str | None = None
        # 统一税率情景重算所得税，但源表所得税缺失时无法核对“税前−所得税=净利润”
        # 这条链是否在源数据里成立，重算结果就只是一段无法对账的算术，故标不可得。
        if total_profit is None:
            adjusted_reason = "missing_total_profit"
        elif impairment is None:
            adjusted_reason = "missing_asset_impairment"
        elif minority is None:
            adjusted_reason = "missing_minority_gain"
        elif tax is None:
            adjusted_reason = "missing_income_tax"
        else:
            adjusted_pretax = total_profit - impairment
            adjusted_asset = adjusted_pretax * (1 - tax_rate) - minority
            if credit is not None:
                adjusted_asset_credit = (adjusted_pretax - credit) * (1 - tax_rate) - minority
        rows.append(
            {
                "end_date": record.end_date,
                "quarter_status": record.status,
                "reported_parent_net": parent,
                "official_deducted_parent_net_single": official_deduct_single.get(record.end_date),
                "official_deducted_parent_net_cumulative": (
                    _number(official_values.get("deduct_parent"))
                ),
                "official_source_url": official_row.get("url"),
                "official_source_file": official_row.get("source_file"),
                "official_location": official_row.get("location"),
                # The supplement's own disclosure time, never the statement
                # record's announcement time.
                "official_available_at": official_row.get("available_at"),
                "official_unit": official_row.get("unit"),
                "official_ts_code": official_row.get("ts_code"),
                "credit_impairment_single": credit,
                "credit_impairment_cumulative": credit_cumulative,
                "asset_impairment": impairment,
                "adjusted_parent_net_strip_asset": adjusted_asset,
                "adjusted_parent_net_strip_asset_and_credit": adjusted_asset_credit,
                "adjusted_unavailable_reason": adjusted_reason,
                "unit": UNIT,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "calc_rules_version": CALC_RULES_VERSION,
        "scenario_version": scenario_version,
        "symbol": series.symbol,
        "scenario": {
            "scenario_id": scenario_version,
            "label": "本次核对情景：剔除资产减值（可再剔除信用减值）",
            "tax_rate": tax_rate,
            "tax_rate_source": tax_rate_source,
            "tax_treatment": "flat_rate_recompute",
            "minority_treatment": "fixed_reported_actual",
            "formula": "(利润总额 − 剔除项) × (1 − 税率) − 当季实际少数股东损益",
            "assumptions": [
                f"统一按 {tax_rate:.0%} 重算所得税；这不是公司实际税率，也不是默认真值。",
                "少数股东损益沿用报表实际数并保持不变，属于简化。",
                "剔除项按报表符号处理：计提为负、转回为正，不能只加回损失而保留转回收益。",
                "调整后单季利润不是可持续年利润，不得乘以 4 直接年化；预测必须另有假设。",
                (
                    "源表利润总额、资产减值、少数股东损益、所得税费用任一缺失，"
                    "该季调整归母标为不可得，不做部分推算"
                    "（见每期 adjusted_unavailable_reason）。"
                ),
            ],
            "not_sustainable_earnings": True,
        },
        "periods": rows,
        "official_metrics_checked": {
            "periods": sorted(official),
            "fields": list(OFFICIAL_METRIC_FIELDS),
            "available_at_source": "补录条目自带的来源披露时间",
            "checked_against_asof": asof or None,
        },
        "line_types": {
            "reported_parent_net": "报表归母：原披露值",
            "official_deducted_parent_net_single": (
                "官方扣非归母（单季）：公告累计数按同年度相邻季差分，缺失即标缺失"
            ),
            "official_deducted_parent_net_cumulative": "官方扣非归母（累计）：公告原值",
            "adjusted_parent_net_strip_asset": "分析调整情景：仅剔资产减值",
            "adjusted_parent_net_strip_asset_and_credit": "分析调整情景：再剔信用减值",
        },
        "disclosures": [
            "信用减值来自公司公告补录；本地利润表无该列时不假设其存在。",
            "资产减值与信用减值分列，避免同一损失被加回两次。",
            "公允价值变动与投资收益合计不等于套保损益；联营合营投资收益需单独列出。",
            (
                "官方补录每期自带 available_at（来源自身披露时点）、单位与公司代码，"
                "已逐条核验且不得晚于本次基准日；财报记录的公告时间不会被沿用。"
            ),
        ],
    }
