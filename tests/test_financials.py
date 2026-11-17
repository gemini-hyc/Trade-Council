"""Offline regression for quarterly financial arithmetic (T03).

The 江西铜业 rows below are the frozen sample values recorded in
``复算数据.json``; they are test fixtures, not values the code knows about.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from trade_council.engineering.financials import (
    INCOME_FLOW_FIELDS,
    FinancialDataError,
    build_financial_artifacts,
    build_ttm,
    cash_conversion,
    quarterize,
    simple_annualized_roe,
)

YI = 1e8

# (end_date, revenue, oper_cost, biz_tax, sell, admin, rd, fin, fair_value,
#  invest, ass_invest, impairment, operate_profit, non_oper_income,
#  non_oper_exp, total_profit, income_tax, n_income, minority, parent)
ROWS: list[tuple[Any, ...]] = [
    (20250331, 111610579687, 106700158225, 405118839, 169493037, 593232260, 341128389, 252425912,
     -198469190, -772284823, -40562573, 171176990, 2423787128, 26837732, 5610678, 2445014182,
     413974480, 2031039702, 78608065, 1952431637),
    (20250630, 256958886885, 246874791966, 928627382, 186422467, 1244299012, 579338636, 562582887,
     -307000273, -120764312, 74624644, -782641135, 5411194849, 29068288, 18116208, 5422146929,
     1004755475, 4417391454, 242844979, 4174546475),
    (20250930, 396046714556, 381882509379, 1388088343, 548559030, 1770880737, 1073823563, 884945420,
     -155682092, -518169627, 113536631, -303724201, 7809237434, 42128629, 29227363, 7822138700,
     1341290611, 6480848089, 457455327, 6023392762),
    (20251231, 544623484060, 520686170283, 1919090355, 386937040, 3000943198, 1415439451, 909245735,
     -2514283623, -2412490438, 221593486, -1262658951, 10408112828, 58782913, 311705092,
     10155190649, 2687243539, 7467947110, 337755246, 7130191864),
    (20260331, 139124071797, 132070632121, 555171663, 188020041, 625317645, 244057699, 471922670,
     2297657758, -1819324121, 65905830.02, -1475654103, 4051412205, 14294248, 34814177,
     4030892276, 885638357, 3145253919, 327690917, 2817563002),
    (20260630, 307154938743, 289267608819, 1301242964, 224136114, 1402680324, 455437368, 985103250,
     3182537965, -1540299317, 423107512, -2881045213, 12415007690, 30134936, 62315958,
     12382826668, 2882053361, 9500773307, 868950699, 8631822608),
    (20260930, 442419883914, 419659611628, 1948522222, 477720123, 1981029714, 940519556, 575190753,
     1104709402, 377373303, 547028171, -1801350061, 16711387948, 41563054, 112412070,
     16640538932, 3454615368, 13185923564, 1499365558, 11686558006),
]

OFFICIAL = {
    "20260331": {
        "credit_impair": 12119213,
        "deduct_parent": 3126322051,
        "url": "https://static.cninfo.com.cn/finalpage/2026-04-29/1225226781.PDF",
        "location": "合并利润表第11页；主要指标第2页",
        "unit": "元",
        "ts_code": "600362.SH",
        "available_at": "2026-04-29",
    },
    "20260630": {
        "credit_impair": -23497011,
        "deduct_parent": 7556196885,
        "url": "https://static.cninfo.com.cn/finalpage/2026-08-26/1225505938.PDF",
        "location": "合并利润表；主要会计数据",
        "unit": "元",
        "ts_code": "600362.SH",
        "available_at": "2026-08-26",
    },
    "20260930": {
        "credit_impair": -24882862,
        "deduct_parent": 10239058822,
        "url": "https://static.cninfo.com.cn/finalpage/2026-10-29/1225636802.PDF",
        "location": "合并利润表第10页；主要指标第1页",
        "unit": "元",
        "ts_code": "600362.SH",
        "available_at": "2026-10-29",
    },
}


def _frame(rows: list[tuple[Any, ...]] = ROWS) -> pd.DataFrame:
    columns = [
        "end_date", "revenue", "oper_cost", "biz_tax_surchg", "sell_exp", "admin_exp",
        "rd_exp", "fin_exp", "fv_value_chg_gain", "invest_income", "ass_invest_income",
        "assets_impair_loss", "operate_profit", "non_oper_income", "non_oper_exp",
        "total_profit", "income_tax", "n_income", "minority_gain", "n_income_attr_p",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    frame["end_date"] = frame["end_date"].astype(str)
    frame["available_at"] = pd.to_datetime(frame["end_date"], format="%Y%m%d") + pd.Timedelta(days=29)
    frame["report_type"] = "1"
    frame["update_flag"] = "0"
    return frame


def _artifacts(tmp_path: Path) -> dict[str, Any]:
    return build_financial_artifacts(
        out_dir=tmp_path,
        symbol="600362.SH",
        asof="2026-11-13",
        income=_frame(),
        cashflow=None,
        balance=None,
        official_metrics={"periods": OFFICIAL},
    )


def test_jiangxi_quarterly_values_match_frozen_sample(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    series = artifacts["series"]
    expected = {
        "20260331": 28.17563002,
        "20260630": 58.14259606,
        "20260930": 30.54735398,
    }
    for end_date, value in expected.items():
        record = series.by_end_date(end_date)
        assert record is not None
        cell = record.values["n_income_attr_p"]
        assert cell.status == "ok"
        assert cell.single is not None
        assert round(cell.single / YI, 8) == pytest.approx(value, abs=1e-6)


def test_official_deducted_and_scenario_values_match_sample(tmp_path: Path) -> None:
    adjustments = _artifacts(tmp_path)["adjustments"]
    rows = {row["end_date"]: row for row in adjustments["periods"]}
    assert round(rows["20260331"]["official_deducted_parent_net_single"] / YI, 8) == pytest.approx(
        31.26322051, abs=1e-6
    )
    assert round(rows["20260630"]["official_deducted_parent_net_single"] / YI, 8) == pytest.approx(
        44.29874834, abs=1e-6
    )
    assert round(rows["20260930"]["official_deducted_parent_net_single"] / YI, 8) == pytest.approx(
        26.82861937, abs=1e-6
    )
    expected_asset = [39.1234979483, 69.7188085454, 18.1665831724]
    expected_credit = [39.0301800082, 69.9930534702, 18.1772542251]
    for end_date, asset, credit in zip(
        ("20260331", "20260630", "20260930"), expected_asset, expected_credit
    ):
        assert rows[end_date]["adjusted_parent_net_strip_asset"] / YI == pytest.approx(asset, abs=1e-6)
        assert rows[end_date]["adjusted_parent_net_strip_asset_and_credit"] / YI == pytest.approx(
            credit, abs=1e-6
        )


def test_qoq_changes_match_sample(tmp_path: Path) -> None:
    bridge = _artifacts(tmp_path)["bridge"]
    latest = next(item for item in bridge["qoq"] if item["to"] == "20260930")
    assert latest["parent_change_pct"] == pytest.approx(-0.4746131743, abs=1e-9)
    contributions = {item["line_id"]: item for item in latest["contributions"]}
    assert contributions["gross_profit"]["change"] / YI == pytest.approx(-59.60947886, abs=1e-6)
    assert contributions["assets_impair_loss"]["change"] / YI == pytest.approx(
        24.85086262, abs=1e-6
    )


def test_bridge_reconciles_to_source(tmp_path: Path) -> None:
    bridge = _artifacts(tmp_path)["bridge"]
    assert bridge["checks_summary"]["fail"] == 0
    latest = next(item for item in bridge["quarters"] if item["end_date"] == "20260930")
    checks = {check["check_id"]: check for check in latest["checks"]}
    assert all(check["status"] == "pass" for check in checks.values())
    assert latest["residual"] is not None


def test_missing_q2_does_not_produce_a_six_month_delta(tmp_path: Path) -> None:
    rows = [row for row in ROWS if row[0] != 20260630]
    series = quarterize(_frame(rows), symbol="600362.SH", flow_fields=INCOME_FLOW_FIELDS)
    q3 = series.by_end_date("20260930")
    assert q3 is not None
    assert q3.status == "missing_predecessor"
    assert q3.values["revenue"].single is None
    assert "不借用更早期间" in q3.reason


def test_balance_items_are_not_differenced(tmp_path: Path) -> None:
    income = _frame()
    balance = pd.DataFrame(
        {
            "end_date": ["20260331", "20260630", "20260930"],
            "total_hldr_eqy_exc_min_int": [1000.0, 1100.0, 1200.0],
            "total_assets": [5000.0, 5100.0, 5200.0],
        }
    )
    artifacts = build_financial_artifacts(
        out_dir=tmp_path,
        symbol="600362.SH",
        asof="2026-11-13",
        income=income,
        cashflow=None,
        balance=balance,
    )
    q3 = artifacts["series"].by_end_date("20260930")
    assert q3 is not None
    equity = q3.values["total_hldr_eqy_exc_min_int"]
    assert equity.status == "stock"
    assert equity.single is None
    frame = pd.read_csv(tmp_path / "financial_quarters.csv")
    assert "total_assets_q" not in frame.columns


def test_restated_period_is_marked_incomparable() -> None:
    """A second disclosure of the same period with different numbers is a restatement."""

    frame = _frame()
    original = frame[frame["end_date"] == "20260630"].copy()
    restated = original.copy()
    restated["end_date"] = "20260630"
    restated["ann_date"] = "20261110"
    restated["f_ann_date"] = "20261110"
    restated["revenue"] = restated["revenue"] + 500 * YI
    frame = pd.concat([frame, restated], ignore_index=True)

    series = quarterize(frame, symbol="600362.SH", flow_fields=INCOME_FLOW_FIELDS)
    q2 = series.by_end_date("20260630")
    q3 = series.by_end_date("20260930")
    assert q2 is not None and q3 is not None
    assert q2.comparability == "changed"
    # Q1 is a cumulative value, so it survives; the difference that spans the
    # restated period must not be computed as if nothing happened.
    assert q3.status == "restated_incomparable"
    assert q3.values["revenue"].single is None


def test_update_flag_alone_is_not_a_restatement_signal() -> None:
    """update_flag is per-disclosure; comparing it across periods proves nothing."""

    frame = _frame()
    frame.loc[frame["end_date"] == "20260930", "update_flag"] = "1"
    series = quarterize(frame, symbol="600362.SH", flow_fields=INCOME_FLOW_FIELDS)
    q3 = series.by_end_date("20260930")
    assert q3 is not None
    assert q3.status == "ok"
    # The honest state for a single visible version is "unconfirmed", not "ok".
    assert q3.comparability == "unconfirmed"


def test_period_end_versions_are_counted() -> None:
    frame = _frame()
    series = quarterize(frame, symbol="600362.SH", flow_fields=INCOME_FLOW_FIELDS)
    for record in series.records:
        assert record.comparability in {"confirmed", "changed", "unconfirmed"}
        assert record.comparability == "unconfirmed"


def test_ttm_inherits_comparability() -> None:
    series = quarterize(_frame(), symbol="600362.SH", flow_fields=INCOME_FLOW_FIELDS)
    ttm = build_ttm(series, "n_income_attr_p", end_date="20260930")
    assert ttm["comparability"] == "unconfirmed"
    assert ttm["comparability_note"]


def test_ttm_requires_all_three_legs() -> None:
    series = quarterize(_frame(), symbol="600362.SH", flow_fields=INCOME_FLOW_FIELDS)
    ttm = build_ttm(series, "n_income_attr_p", end_date="20260930")
    assert ttm["status"] == "ok"
    assert ttm["value"] == pytest.approx(
        7130191864 + 11686558006 - 6023392762, abs=1.0
    )
    short = quarterize(
        _frame([ROW for ROW in ROWS if ROW[0] != 20250930]),
        symbol="600362.SH",
        flow_fields=INCOME_FLOW_FIELDS,
    )
    missing = build_ttm(short, "n_income_attr_p", end_date="20260930")
    assert missing["status"] == "unavailable"
    assert "prior_year_same_ytd" in missing["reason"]


def test_negative_profit_and_missing_tax_are_not_disguised(tmp_path: Path) -> None:
    rows = [
        (20260331, 1000.0, 1200.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
         -200.0, 0.0, 0.0, -200.0, None, -200.0, 0.0, -200.0),
        (20260630, 2000.0, 2400.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
         -400.0, 0.0, 0.0, -400.0, 100.0, -500.0, 0.0, -500.0),
    ]
    artifacts = build_financial_artifacts(
        out_dir=tmp_path,
        symbol="600362.SH",
        asof="2026-11-13",
        income=_frame(rows),
        cashflow=None,
        balance=None,
        official_metrics={"periods": OFFICIAL},
    )
    q1 = artifacts["series"].by_end_date("20260331")
    assert q1 is not None
    assert q1.values["n_income_attr_p"].single == pytest.approx(-200.0)
    bridge = artifacts["bridge"]
    first = next(item for item in bridge["quarters"] if item["end_date"] == "20260331")
    net_check = next(check for check in first["checks"] if check["check_id"] == "n_income")
    assert net_check["status"] == "unavailable"
    rows_by_date = {row["end_date"]: row for row in artifacts["adjustments"]["periods"]}
    assert rows_by_date["20260331"]["adjusted_parent_net_strip_asset"] is None


def test_cash_conversion_uses_consolidated_profit(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    q3 = artifacts["series"].by_end_date("20260930")
    assert q3 is not None
    conversion = cash_conversion(q3)
    assert conversion["consistent_ratio"]["value"] is None
    assert "合并净利润" in conversion["consistent_ratio"]["denominator"]


def _record_with_equity(record, equity: float):  # type: ignore[no-untyped-def]
    from trade_council.engineering.financials import QuarterRecord, QuarterValue

    values = dict(record.values)
    values["total_hldr_eqy_exc_min_int"] = QuarterValue(
        end_date=record.end_date,
        field="total_hldr_eqy_exc_min_int",
        cumulative=equity,
        single=None,
        status="stock",
    )
    return QuarterRecord(
        end_date=record.end_date,
        year=record.year,
        quarter=record.quarter,
        available_at=record.available_at,
        status=record.status,
        reason=record.reason,
        values=values,
        comparability=record.comparability,
    )


def test_roe_uses_matching_numerator_and_annualisation() -> None:
    """YTD profit x 12/months and single-quarter profit x 4 are not interchangeable."""

    frame = pd.DataFrame(
        {
            "end_date": ["20260331", "20260630", "20260930"],
            "ann_date": ["20260401", "20260801", "20261001"],
            "revenue": [10.0, 30.0, 60.0],
            "n_income_attr_p": [10.0, 30.0, 60.0],
        }
    )
    series = quarterize(frame, symbol="600000.SH", flow_fields=("revenue", "n_income_attr_p"))
    equity = 100.0
    expected = {
        "20260331": (0.40, 0.40),   # 10/100*12/3 ; single 10/100*4
        "20260630": (0.60, 0.80),   # 30/100*12/6 ; single 20/100*4
        "20260930": (0.80, 1.20),   # 60/100*12/9 ; single 30/100*4
    }
    for end_date, (ytd_expected, single_expected) in expected.items():
        record = series.by_end_date(end_date)
        assert record is not None
        roe = simple_annualized_roe(_record_with_equity(record, equity), equity)
        assert roe["ytd_value"] == pytest.approx(ytd_expected)
        assert roe["single_quarter_value"] == pytest.approx(single_expected)
        assert roe["not_official"] is True
        assert "期末净资产口径简单年化" in roe["definition"]
        assert roe["ytd_formula"] and roe["single_quarter_formula"]
        # The two are not the same number except in Q1.
        if end_date != "20260331":
            assert roe["ytd_value"] != pytest.approx(roe["single_quarter_value"])


def test_adjustments_record_assumptions(tmp_path: Path) -> None:
    adjustments = _artifacts(tmp_path)["adjustments"]
    scenario = adjustments["scenario"]
    assert scenario["tax_rate"] == pytest.approx(0.23)
    assert scenario["not_sustainable_earnings"] is True
    assert any("不是可持续年利润" in item for item in scenario["assumptions"])
    assert "信用减值" in " ".join(adjustments["disclosures"])


def test_artifact_files_are_written(tmp_path: Path) -> None:
    _artifacts(tmp_path)
    assert (tmp_path / "financial_quarters.csv").is_file()
    assert (tmp_path / "earnings_bridge.json").is_file()
    assert (tmp_path / "financial_adjustments.json").is_file()
    payload = json.loads((tmp_path / "earnings_bridge.json").read_text(encoding="utf-8"))
    assert payload["calc_rules_version"] == "financials-1"


def test_build_artifacts_detects_a_restated_period(tmp_path: Path) -> None:
    """The production build path must not label a restated series as clean."""

    frame = _frame()
    restated = frame[frame["end_date"] == "20260630"].copy()
    restated["ann_date"] = "20261110"
    restated["revenue"] = restated["revenue"] + 400 * YI
    frame = pd.concat([frame, restated], ignore_index=True)
    (tmp_path / "income.csv").write_text("", encoding="utf-8")
    artifacts = build_financial_artifacts(
        out_dir=tmp_path,
        symbol="600362.SH",
        asof="2026-11-13",
        income=frame,
        cashflow=None,
        balance=None,
    )
    assert artifacts["status"] == "ok"
    csv = pd.read_csv(tmp_path / "financial_quarters.csv")
    row_q2 = csv[csv["end_date"] == 20260630].iloc[0]
    row_q3 = csv[csv["end_date"] == 20260930].iloc[0]
    assert row_q2["comparability"] == "changed"
    assert row_q3["quarter_status"] == "restated_incomparable"
    bridge = artifacts["bridge"]
    assert bridge["ttm"]["revenue"]["comparability"] in {"changed", "unavailable"}


# --------------------------------------------------------------- 官方补录核验


def _official_variant(**overrides: object) -> dict[str, object]:
    entry = dict(OFFICIAL["20260930"])
    entry.update(overrides)
    return {"periods": {"20260930": entry}}


def test_official_supplement_must_state_its_own_available_at(tmp_path: Path) -> None:
    entry = dict(OFFICIAL["20260930"])
    del entry["available_at"]
    with pytest.raises(FinancialDataError, match="available_at"):
        build_financial_artifacts(
            out_dir=tmp_path,
            symbol="600362.SH",
            asof="2026-11-13",
            income=_frame(),
            cashflow=None,
            balance=None,
            official_metrics={"periods": {"20260930": entry}},
        )
    assert not (tmp_path / "financial_adjustments.json").exists()


def test_official_supplement_cannot_be_known_after_the_asof(tmp_path: Path) -> None:
    with pytest.raises(FinancialDataError, match="晚于本次基准日"):
        build_financial_artifacts(
            out_dir=tmp_path,
            symbol="600362.SH",
            asof="2026-11-13",
            income=_frame(),
            cashflow=None,
            balance=None,
            official_metrics=_official_variant(available_at="2026-12-01"),
        )


def test_official_supplement_needs_a_unit_and_a_matching_company(tmp_path: Path) -> None:
    with pytest.raises(FinancialDataError, match="金额单位"):
        build_financial_artifacts(
            out_dir=tmp_path,
            symbol="600362.SH",
            asof="2026-11-13",
            income=_frame(),
            cashflow=None,
            balance=None,
            official_metrics=_official_variant(unit=None),
        )
    with pytest.raises(FinancialDataError, match="不一致"):
        build_financial_artifacts(
            out_dir=tmp_path,
            symbol="600362.SH",
            asof="2026-11-13",
            income=_frame(),
            cashflow=None,
            balance=None,
            official_metrics=_official_variant(ts_code="600486.SH"),
        )


def test_official_supplement_timestamp_overrides_the_statement_stamp(tmp_path: Path) -> None:
    """The supplement's own disclosure time wins over the statement record's.

    The fixture's statement record carries ``2026-10-29`` (its announcement
    time). The supplement says the PDF went up on ``2026-10-31``, so the
    evidence must carry the later date: the number was not knowable earlier.
    """

    artifacts = build_financial_artifacts(
        out_dir=tmp_path,
        symbol="600362.SH",
        asof="2026-11-13",
        income=_frame(),
        cashflow=None,
        balance=None,
        official_metrics=_official_variant(available_at="2026-10-31"),
    )
    rows = {row["end_date"]: row for row in artifacts["adjustments"]["periods"]}
    q3 = rows["20260930"]
    assert q3["official_available_at"] == "2026-10-31 00:00:00"
    record = artifacts["series"].by_end_date("20260930")
    assert record is not None
    assert record.available_at == "2026-10-29 00:00:00"
    assert q3["official_unit"] == "CNY"
    assert q3["official_ts_code"] == "600362.SH"
    assert (
        artifacts["adjustments"]["official_metrics_checked"]["checked_against_asof"]
        == "2026-11-13"
    )
