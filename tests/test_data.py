from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from trade_council.data.local_quant import LocalQuantProvider
from trade_council.data.registry import DatasetRegistry
from trade_council.engineering.datapack import build

ROOT = Path(__file__).resolve().parents[1]


def _write_parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def _make_quant_root(tmp_path: Path) -> Path:
    root = tmp_path / "quant-data"
    dates = pd.bdate_range(end="2026-10-16", periods=25)
    code = "sh.600519"

    prices = pd.DataFrame(
        {
            "date": dates,
            "code": code,
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
            "preclose": 100.5,
            "volume": 1_000_000,
            "amount": 100_000_000,
            "turn": 1.2,
            "pctChg": 0.5,
            "stock_name": "测试股票",
        }
    )
    _write_parquet(root / "market" / "daily" / "2026-10-16.parquet", prices)
    _write_parquet(
        root / "adj_factor" / "2026-10-16.parquet",
        pd.DataFrame({"date": dates, "code": code, "ts_code": "600519.SH", "adj_factor": 1.0}),
    )

    _write_parquet(
        root / "daily_basic" / "2026-10-16.parquet",
        pd.DataFrame(
            {
                "ts_code": ["600519.SH"],
                "trade_date": ["20261016"],
                "close": [101.0],
                "pe_ttm": [20.0],
                "pb": [5.0],
                "ps_ttm": [6.0],
                "total_mv": [1_000_000.0],
                "circ_mv": [900_000.0],
            }
        ),
    )
    _write_parquet(
        root / "moneyflow" / "2026-10-16.parquet",
        pd.DataFrame(
            {
                "ts_code": ["600519.SH"],
                "trade_date": ["20261016"],
                "buy_lg_amount": [100.0],
                "sell_lg_amount": [80.0],
                "buy_elg_amount": [50.0],
                "sell_elg_amount": [30.0],
                "net_mf_amount": [40.0],
            }
        ),
    )
    _write_parquet(
        root / "sentiment" / "margin_detail" / "2026-10-15.parquet",
        pd.DataFrame(
            {
                "date": [pd.Timestamp("2026-10-15")],
                "code": ["600519.SH"],
                "ts_code": ["600519.SH"],
                "rzye": [100_000_000.0],
                "rqye": [20_000_000.0],
                "rzmre": [1_000_000.0],
                "rqyl": [2_000.0],
                "rzche": [500_000.0],
                "rqchl": [0.0],
                "rqmcl": [0.0],
                "rzrqye": [None],
            }
        ),
    )
    _write_parquet(
        root / "sentiment" / "margin_detail" / "2026-10-16.parquet",
        pd.DataFrame(
            {
                "date": [pd.Timestamp("2026-10-16")],
                "code": ["600519.SH"],
                "ts_code": ["600519.SH"],
                "rzye": [300_000_000.0],
                "rqye": [30_000_000.0],
                "rzmre": [3_000_000.0],
                "rqyl": [3_000.0],
                "rzche": [1_500_000.0],
                "rqchl": [0.0],
                "rqmcl": [0.0],
                "rzrqye": [330_000_000.0],
            }
        ),
    )

    financial = pd.DataFrame(
        {
            "ts_code": ["600519.SH", "600519.SH"],
            "ann_date": ["20260830", "20261020"],
            "f_ann_date": ["20260830", "20261020"],
            "end_date": ["20260630", "20260930"],
            "report_type": ["1", "1"],
            "revenue": [100.0, 200.0],
            "n_income_attr_p": [10.0, 20.0],
        }
    )
    _write_parquet(root / "financial_statements" / "income" / "income_20260930.parquet", financial)
    _write_parquet(
        root / "financial_statements" / "balancesheet" / "balancesheet_20260630.parquet",
        pd.DataFrame(
            {
                "ts_code": ["600519.SH"],
                "ann_date": ["20260830"],
                "f_ann_date": ["20260830"],
                "end_date": ["20260630"],
                "report_type": ["1"],
                "total_assets": [1000.0],
                "total_liab": [300.0],
            }
        ),
    )
    _write_parquet(
        root / "financial_statements" / "cashflow" / "cashflow_20260630.parquet",
        pd.DataFrame(
            {
                "ts_code": ["600519.SH"],
                "ann_date": ["20260830"],
                "f_ann_date": ["20260830"],
                "end_date": ["20260630"],
                "report_type": ["1"],
                "n_cashflow_act": [90.0],
                "free_cashflow": [70.0],
            }
        ),
    )

    announcement = pd.DataFrame(
        {
            "title": ["测试股票公告-历史", "测试股票公告-未来"],
            "summary": ["历史公告", "未来公告"],
            "publish_time": [pd.Timestamp("2026-10-16 10:00"), pd.Timestamp("2026-10-17 10:00")],
            "publish_date": ["20261016", "20261017"],
        }
    )
    _write_parquet(root / "market" / "announcement_20261016.parquet", announcement)

    news = pd.DataFrame(
        {
            "title": ["测试股票新闻-历史", "测试股票新闻-未来"],
            "publish_time": [pd.Timestamp("2026-06-17 10:00"), pd.Timestamp("2026-10-17 10:00")],
        }
    )
    _write_parquet(root / "news" / "raw_major_news_20260617.parquet", news)

    limit = pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-10-16")],
            "ts_code": ["600519.SH"],
            "up_limit": [111.1],
            "down_limit": [90.9],
        }
    )
    _write_parquet(root / "stk_limit" / "2026-10-16.parquet", limit)
    (root / "suspend_d").mkdir(parents=True, exist_ok=True)
    return root


def test_registry_encodes_publish_lag_and_northbound_windows() -> None:
    registry = DatasetRegistry.load(ROOT / "configs" / "datasets.yaml")
    assert registry.get("announcements.cninfo").partition == "daily"
    assert registry.get("news.em_fast").time_field == "show_time"
    assert registry.get("industry.sw_official").enabled is False
    margin = registry.get("sentiment.margin_detail")
    assert margin.publish_lag_days == 1
    assert registry.field_available(
        "sentiment.hsgt_top10", "net_amount", date(2024, 8, 18)
    )
    assert not registry.field_available(
        "sentiment.hsgt_top10", "net_amount", date(2024, 8, 19)
    )
    assert not registry.semantic_available(
        "sentiment.hk_hold", "northbound_holdings", date(2024, 8, 19)
    )


def test_local_provider_adds_available_at(tmp_path: Path) -> None:
    root = _make_quant_root(tmp_path)
    registry = DatasetRegistry.load(ROOT / "configs" / "datasets.yaml")
    provider = LocalQuantProvider(root)
    result = provider.scan(
        registry.get("sentiment.margin_detail"),
        match=("ts_code", "600519.SH"),
    )
    assert result.status == "OK"
    assert "available_at" in result.frame.columns
    assert result.frame["available_at"].min() >= pd.Timestamp("2026-10-16")


def test_datapack_asof_and_news_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _make_quant_root(tmp_path)
    out = tmp_path / "pack-default"
    manifest = build(
        "600519.SH", "2026-10-16", out, 1, include_news=False, data_root=root
    )
    assert manifest["news_included"] is False
    assert manifest["snapshot_id"]
    assert (out / "snapshot.json").exists()
    assert (out / "evidence.json").exists()
    assert (out / "evidence_trace.jsonl").exists()
    assert manifest["evidence_count"] > 0
    assert (out / "announcements.csv").exists()
    assert not (out / "news.csv").exists()

    margin = pd.read_csv(out / "sent_margin_detail.csv")
    assert pd.to_datetime(margin["date"]).max() <= pd.Timestamp("2026-10-15")
    income = pd.read_csv(out / "income.csv")
    assert income["ann_date"].astype(str).tolist() == ["20260830"]
    assert (out / "financials.csv").exists()
    announcements = pd.read_csv(out / "announcements.csv")
    assert announcements["title"].tolist() == ["测试股票公告-历史"]
    assert any(item["key"] == "news" and item["status"] == "DISABLED" for item in manifest["datasets"])

    out_news = tmp_path / "pack-news"
    build("600519.SH", "2026-10-16", out_news, 1, include_news=True, data_root=root)
    news = pd.read_csv(out_news / "news.csv")
    assert news["title"].tolist() == ["测试股票新闻-历史"]


def test_local_provider_status_distinguishes_absent_and_no_record(tmp_path: Path) -> None:
    root = _make_quant_root(tmp_path)
    registry = DatasetRegistry.load(ROOT / "configs" / "datasets.yaml")
    provider = LocalQuantProvider(root)

    absent = provider.scan(registry.get("reports.forecast"))
    assert absent.status == "DATASET_ABSENT"

    no_record = provider.scan(
        registry.get("market.daily"),
        match=("code", "sh.999999"),
    )
    assert no_record.status == "NO_RECORD"
