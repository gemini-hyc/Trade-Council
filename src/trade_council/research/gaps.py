"""Data-gap classes and freshness rules.

Three different "we don't have it" cases must stay separate: we failed to
extract it, the source does not disclose it, or what we have is stale. A missing
record on its own only ever proves "not found", never "not disclosed".
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

GapKind = Literal["NOT_EXTRACTED", "NOT_DISCLOSED", "STALE", "NOT_APPLICABLE"]
Priority = Literal["critical", "material", "minor"]

GAP_LABELS: dict[str, str] = {
    "NOT_EXTRACTED": "我们没取成",
    "NOT_DISCLOSED": "源侧未披露（需写明检查范围）",
    "STALE": "已有但过期",
    "NOT_APPLICABLE": "不适用于本公司/本期",
}

#: 时效按数据类型分别设定，不用同一个“30 天过期”阈值套所有数据。
#: 阈值只是缺省提醒，不是合规期限；超阈值时必须写明下一步核实路径。
FRESHNESS_RULES: dict[str, tuple[int, str]] = {
    "price": (5, "对齐最近交易日（节假日顺延），不是自然日"),
    "daily_basic": (5, "对齐最近交易日"),
    "moneyflow": (5, "对齐最近交易日"),
    "financials": (200, "按最新应有披露期判断；超期先确认是否已有新报告"),
    "income": (200, "按最新应有披露期判断"),
    "cashflow": (200, "按最新应有披露期判断"),
    "balancesheet": (200, "按最新应有披露期判断"),
    "fina_indicator": (200, "按最新应有披露期判断"),
    "top10_holders": (200, "按最近报告期判断"),
    "holder_number": (200, "按最近报告期判断"),
    "pledge_stat": (400, "按覆盖窗口判断，不是单点日期"),
    "share_float": (400, "按覆盖窗口判断"),
    "stk_holdertrade": (180, "按公告覆盖窗口判断"),
    "forecast": (120, "距基准日超过 120 天必须先核实是否有新预告"),
    "news": (30, "新闻时效短，超过 30 天只能当背景，不能当当期驱动"),
}

_STATUS_TO_KIND: dict[str, GapKind] = {
    "not_found": "NOT_EXTRACTED",
    "unavailable": "NOT_EXTRACTED",
    "error": "NOT_EXTRACTED",
    "timeout": "NOT_EXTRACTED",
    "not_disclosed": "NOT_DISCLOSED",
    "absent_in_source": "NOT_DISCLOSED",
    "stale": "STALE",
    "not_applicable": "NOT_APPLICABLE",
}


def gap_kind_for_status(status: str) -> GapKind:
    """Map a supplement result status onto one of the four gap classes.

    Unknown statuses default to ``NOT_EXTRACTED``: claiming the source did not
    disclose something requires evidence of the checked scope.
    """

    return _STATUS_TO_KIND.get(status.strip().lower(), "NOT_EXTRACTED")


class GapRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    gap_id: str
    dataset_id: str
    field: str = ""
    kind: GapKind
    priority: Priority = "material"
    affects: list[str] = Field(default_factory=list)
    scope_of_check: str = ""
    last_checked: str = ""
    next_step: str = ""
    note: str = ""

    @model_validator(mode="after")
    def _needs_scope_for_not_disclosed(self) -> GapRecord:
        if self.kind == "NOT_DISCLOSED" and not self.scope_of_check.strip():
            raise ValueError(
                "NOT_DISCLOSED 必须填写 scope_of_check（查了哪些公告、期间、页码），"
                "否则只能算 NOT_EXTRACTED"
            )
        return self


def _parse_date(value: str) -> date | None:
    text = value.strip()[:10].replace("/", "-")
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def freshness_check(dataset_id: str, available_at: str, asof: str) -> dict[str, object]:
    """Compare an observation date against the rule for its data type.

    Returns ``status`` of ``ok`` / ``stale`` / ``unknown``. ``unknown`` means the
    rule needs a disclosure calendar we do not have offline; it must not be
    reported as fresh.
    """

    rule = FRESHNESS_RULES.get(dataset_id)
    if rule is None:
        return {
            "status": "unknown",
            "reason": f"{dataset_id} 没有配置时效规则，需人工判断，不得默认视为最新",
            "dataset_id": dataset_id,
        }
    max_age, anchor = rule
    observed = _parse_date(available_at)
    anchor_date = _parse_date(asof)
    if observed is None or anchor_date is None:
        return {
            "status": "unknown",
            "reason": f"{dataset_id} 日期不可解析（available_at={available_at!r}，asof={asof!r}）",
            "dataset_id": dataset_id,
        }
    age = (anchor_date - observed).days
    if age < 0:
        return {
            "status": "future",
            "reason": f"{dataset_id} 的可得时间晚于基准日 {-age} 天，属于未来数据",
            "dataset_id": dataset_id,
            "age_days": age,
        }
    status = "ok" if age <= max_age else "stale"
    return {
        "status": status,
        "reason": f"{dataset_id} 距基准日 {age} 天，阈值 {max_age} 天（{anchor}）",
        "dataset_id": dataset_id,
        "age_days": age,
        "max_age_days": max_age,
        "anchor": anchor,
    }
