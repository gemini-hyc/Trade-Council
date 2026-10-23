from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pandas as pd

from trade_council.data.supplement import (
    DataNeed,
    WebSearchResult,
    detect_data_needs,
    supplement_data_needs,
)
from trade_council.engineering.events import load_events, write_event_artifacts
from trade_council.engineering.evidence import EvidenceIndex


def test_event_normalization_filters_issuer_news_and_asof(tmp_path: Path) -> None:
    root = tmp_path / "quant-data"
    announcement_dir = root / "announcements" / "cninfo"
    news_dir = root / "news" / "em_fast"
    forecast_dir = root / "reports" / "forecast" / "2026"
    announcement_dir.mkdir(parents=True)
    news_dir.mkdir(parents=True)
    forecast_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-10-21"),
                "trade_date": "20261021",
                "sec_code": "600519",
                "sec_name": "贵州茅台",
                "title": "关于业绩预告的公告",
                "ann_time": pd.Timestamp("2026-10-21"),
                "event_tag": "业绩预告",
                "event_conf": "high",
                "event_all": "业绩预告",
                "full_url": "https://example.test/issuer",
                "ann_id": "ann-1",
                "doc_role": "issuer",
            },
            {
                "date": pd.Timestamp("2026-10-21"),
                "trade_date": "20261021",
                "sec_code": "600519",
                "sec_name": "贵州茅台",
                "title": "法律意见书",
                "ann_time": pd.Timestamp("2026-10-21"),
                "event_tag": "业绩预告",
                "event_conf": "low",
                "event_all": "业绩预告",
                "full_url": "https://example.test/intermediary",
                "ann_id": "ann-2",
                "doc_role": "intermediary",
            },
            {
                "date": pd.Timestamp("2026-10-24"),
                "trade_date": "20261024",
                "sec_code": "600519",
                "sec_name": "贵州茅台",
                "title": "未来公告",
                "ann_time": pd.Timestamp("2026-10-24"),
                "event_tag": "重大合同",
                "event_conf": "high",
                "event_all": "重大合同",
                "full_url": "https://example.test/future",
                "ann_id": "ann-3",
                "doc_role": "issuer",
            },
        ]
    ).to_parquet(announcement_dir / "2026-10-21.parquet", index=False)
    pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-10-21"),
                "trade_date": "20261021",
                "code": "news-1",
                "show_time": pd.Timestamp("2026-10-21 09:30:00"),
                "title": "公司发布重要合同",
                "summary": "合同摘要",
                "a_codes": "600519.SH",
            },
            {
                "date": pd.Timestamp("2026-10-21"),
                "trade_date": "20261021",
                "code": "news-2",
                "show_time": pd.Timestamp("2026-10-21 09:31:00"),
                "title": "无关新闻",
                "summary": "无关",
                "a_codes": "000001.SZ",
            },
        ]
    ).to_parquet(news_dir / "2026-10-21.parquet", index=False)
    pd.DataFrame(
        [
            {
                "ts_code": "600519.SH",
                "ann_date": "20261021",
                "end_date": "20260930",
                "type": "预增",
                "summary": "预告摘要",
                "change_reason": "业务增长",
            }
        ]
    ).to_parquet(forecast_dir / "2026-10-21.parquet", index=False)

    payload = load_events(
        root,
        code6="600519",
        ts_code="600519.SH",
        asof="2026-10-21",
        include_news=True,
    )
    event_types = {item["event_type"] for item in payload["events"]}
    assert event_types == {"业绩预告", "快讯"}
    assert len(payload["announcements"]) == 2
    assert all(item["summary"].lower() != "nan" for item in payload["events"])
    assert not any("未来公告" in item["title"] for item in payload["events"])
    assert not any("无关新闻" in item["title"] for item in payload["events"])

    (tmp_path / "pack").mkdir()
    write_event_artifacts(tmp_path / "pack", payload)
    assert (tmp_path / "pack" / "events.csv").is_file()
    assert (tmp_path / "pack" / "event_timeline.json").is_file()


class FakeWebProvider:
    async def search(
        self,
        need: DataNeed,
        *,
        symbol: str,
        stock_name: str,
        asof: str,
    ) -> list[WebSearchResult]:
        assert symbol == "600519.SH"
        return [
            WebSearchResult(
                title="贵州茅台 2026Q3 业绩预告",
                url="https://example.test/forecast",
                published_at="2026-10-21",
                available_at="2026-10-21 18:00:00",
                summary="公司公告业绩预增",
                fields={"net_profit_min": 100.0, "net_profit_max": 110.0},
                confidence="high",
            )
        ]


def test_task_scoped_supplement_adds_web_evidence(tmp_path: Path) -> None:
    manifest = {
        "datasets": [
            {"key": "forecast_single", "status": "WARN"},
            {"key": "daily_basic_quality", "status": "ERROR"},
        ]
    }
    needs = detect_data_needs(manifest, include_news=False)
    assert {need.need_id for need in needs} == {"need-forecast", "need-valuation"}
    evidence = EvidenceIndex("snap-test")
    result = asyncio.run(
        supplement_data_needs(
            needs=needs,
            provider=FakeWebProvider(),
            data_config=object(),
            evidence=evidence,
            asof="2026-10-21",
            symbol="600519.SH",
            stock_name="贵州茅台",
            run_dir=tmp_path / "run",
        )
    )
    assert result["status"] == "completed"
    assert any(item.source_level == "web" for item in evidence.all())
    assert (tmp_path / "run" / "data_needs.json").is_file()
    assert (tmp_path / "run" / "supplemental_data.json").is_file()
    assert (tmp_path / "run" / "supplemental_evidence.json").is_file()


def test_supplement_marks_missing_provider_as_unavailable(tmp_path: Path) -> None:
    need = DataNeed(
        need_id="need-forecast",
        dataset_id="reports.forecast",
        field="net_profit_min",
        reason="local missing",
        priority="critical",
        required_for="catalyst",
        query="forecast",
    )
    result = asyncio.run(
        supplement_data_needs(
            needs=[need],
            provider=None,
            data_config=object(),
            evidence=EvidenceIndex("snap-test"),
            asof="2026-10-21",
            symbol="600519.SH",
            stock_name="贵州茅台",
            run_dir=tmp_path / "run",
        )
    )
    assert result["status"] == "unavailable"
    state = json.loads(
        (tmp_path / "run" / "supplemental_data.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "unavailable"
