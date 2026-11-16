"""Offline regression for the deterministic fixes in T01.

Each test pins a concrete error that previously produced a misleading
conclusion: swapped moving-average alignment, an ROE label that implied the
official weighted ratio, text evidence parsed as a number, and a default risk
radar with a hard-coded verdict dimension.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from test_reporting import _make_project_and_run

from trade_council.engineering.datapack import write_digest
from trade_council.engineering.financials import build_financial_artifacts
from trade_council.engineering.indicators import add_indicators, ma_alignment
from trade_council.reporting.build import build_report
from trade_council.reporting.scoring import Scorecard


def test_ma_alignment_requires_strict_adjacent_order() -> None:
    bull = ma_alignment([10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0])
    assert bull.state == "bull"
    bear = ma_alignment([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
    assert bear.state == "bear"

    equal = ma_alignment([10.0, 9.0, 9.0, 7.0, 6.0, 5.0, 4.0])
    assert equal.state == "mixed"
    assert any("MA10=MA20" in violation for violation in equal.violations)

    missing = ma_alignment([10.0, 9.0, 8.0, 7.0, 6.0, None, None])
    assert missing.state == "undetermined"
    assert "MA120" in missing.detail and "MA250" in missing.detail

    mixed = ma_alignment([10.0, 11.0, 9.0, 8.0, 7.0, 6.0, 5.0])
    assert mixed.state == "mixed"
    assert mixed.violations


def _price_frame(close: list[float]) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-11-13", periods=len(close))
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": [value * 1.01 for value in close],
            "low": [value * 0.99 for value in close],
            "close": close,
            "volume": 1_000_000.0,
            "amount": [value * 1_000_000 for value in close],
            "turn": 1.0,
            "pctChg": 0.0,
            "adj_factor": 1.0,
        }
    )
    for column in ("adj_open", "adj_high", "adj_low", "adj_close"):
        frame[column] = frame[column.replace("adj_", "")]
    return add_indicators(frame)


def _cumulative_income(tmp_path: Path) -> pd.DataFrame:
    rows = []
    totals = {
        "20260331": (1000.0, 800.0, 200.0, 50.0, 20.0, 30.0),
        "20260630": (2200.0, 1800.0, 400.0, 90.0, 35.0, 70.0),
        "20260930": (3300.0, 2700.0, 600.0, 140.0, 50.0, 105.0),
    }
    for end_date, (revenue, cost, profit, tax, non_oper, minority) in totals.items():
        rows.append(
            {
                "end_date": end_date,
                "available_at": pd.Timestamp(f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}") + pd.Timedelta(days=30),
                "revenue": revenue,
                "oper_cost": cost,
                "operate_profit": profit,
                "total_profit": profit + non_oper,
                "n_income": profit * 0.77,
                "n_income_attr_p": profit * 0.75,
                "income_tax": profit * 0.23,
                "non_oper_income": non_oper,
                "non_oper_exp": 0.0,
                "minority_gain": minority,
                "biz_tax_surchg": 0.0,
                "sell_exp": 0.0,
                "admin_exp": 0.0,
                "rd_exp": 0.0,
                "fin_exp": 0.0,
                "fv_value_chg_gain": 0.0,
                "invest_income": 0.0,
                "assets_impair_loss": 0.0,
            }
        )
    return pd.DataFrame(rows)


def test_digest_declares_price_basis_and_roe_definition(tmp_path: Path) -> None:
    close = list(np.linspace(200.0, 120.0, 300))
    price = _price_frame(close)
    income = _cumulative_income(tmp_path)
    artifacts = build_financial_artifacts(
        out_dir=tmp_path,
        symbol="600000.SH",
        asof="2026-11-13",
        income=income,
        cashflow=None,
        balance=pd.DataFrame(
            [
                {"end_date": "20260930", "total_hldr_eqy_exc_min_int": 1000.0},
                {"end_date": "20260331", "total_hldr_eqy_exc_min_int": 980.0},
                {"end_date": "20260630", "total_hldr_eqy_exc_min_int": 990.0},
            ]
        ),
    )
    write_digest(
        tmp_path,
        "600000.SH",
        "600000",
        "样例",
        "2026-11-13",
        price,
        price.iloc[-1],
        None,
        None,
        None,
        None,
        False,
        None,
        None,
        fin_artifacts=artifacts,
    )
    digest = (tmp_path / "DATA_DIGEST.md").read_text(encoding="utf-8")
    assert "完全空头排列" in digest
    assert "完全多头排列" not in digest
    assert "期末净资产口径简单年化 ROE 估算" in digest
    assert "不是公司披露的加权平均净资产收益率" in digest
    assert "未复权" in digest and "前复权" in digest
    assert ">100% 为健康" not in digest


def test_text_evidence_is_not_compared_as_a_number(tmp_path: Path) -> None:
    _, run_dir, datapack = _make_project_and_run(tmp_path)
    evidence = json.loads((datapack / "evidence.json").read_text(encoding="utf-8"))
    evidence["items"].append(
        {
            "evidence_id": "F-event-announcement",
            "dataset_id": "announcements.cninfo",
            "path": "announcements/cninfo",
            "primary_key": "600519.SH",
            "event_date": "2026-10-20",
            "available_at": "2026-10-20 20:00:00",
            "field": "event",
            "value": "关于2026年第三季度报告的公告",
            "unit": "",
            "formula": "event_type=announcement",
            "snapshot_id": "snap-test",
            "value_type": "text",
        }
    )
    (datapack / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
    audit = build_report(run_dir, datapack)
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "关于2026年第三季度报告的公告" in html
    assert 'class="evidence-text"' in html
    assert audit["verdict"] != "FAIL"
    assert audit["text_evidence"] == 1


def test_no_scorecard_means_no_radar(tmp_path: Path) -> None:
    _, run_dir, datapack = _make_project_and_run(tmp_path)
    audit = build_report(run_dir, datapack)
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "评分卡" in html
    assert 'data-scorecard="absent"' in html
    assert 'data-chart="scorecard"' not in html
    assert audit["verdict"] == "PASS"
    assert "score" not in audit


def test_scorecard_total_comes_from_declared_formula(tmp_path: Path) -> None:
    _, run_dir, datapack = _make_project_and_run(tmp_path)
    scorecard = Scorecard.model_validate(
        {
            "formula": "加权平均：Σ(得分×权重)/Σ权重",
            "scale_max": 5,
            "missing_handling": "exclude_and_renormalise",
            "dimensions": [
                {"id": "quality", "label": "盈利质量", "weight": 2, "score": 4, "basis": "经营现金流/合并净利"},
                {"id": "valuation", "label": "估值", "weight": 1, "score": 2, "basis": "PE 分位"},
                {"id": "growth", "label": "增长", "weight": 1, "score": 3, "basis": "单季营收同比"},
                {"id": "timing", "label": "时机", "weight": 1, "score": None, "basis": "缺分时数据"},
            ],
        }
    )
    (run_dir / "risk_scorecard.json").write_text(
        json.dumps(scorecard.model_dump(), ensure_ascii=False), encoding="utf-8"
    )
    audit = build_report(run_dir, datapack)
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert 'data-chart="scorecard"' in html
    assert "3.25" in html
    assert "缺分时数据" in html
    assert audit["verdict"] == "PASS"
