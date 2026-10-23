from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trade_council.data.supplement import (
    CninfoAnnouncementProvider,
    DataNeed,
    WebSearchResult,
    build_web_search_provider,
    detect_data_needs,
    supplement_data_needs,
)
from trade_council.engineering.events import load_events, write_event_artifacts
from trade_council.engineering.evidence import EvidenceIndex
from trade_council.errors import ProviderUnavailableError


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
        fields = (
            {
                "ann_date": "2026-10-21",
                "type": "预增",
                "net_profit_min": 100.0,
                "net_profit_max": 110.0,
                "summary": "公司公告业绩预增",
                "change_reason": "业务增长",
            }
            if need.dataset_id == "reports.forecast"
            else {"pe_ttm": 20.0, "pb": 8.0, "ps_ttm": 6.0}
        )
        return [
            WebSearchResult(
                title="贵州茅台 2026Q3 业绩预告",
                url="https://example.test/forecast",
                published_at="2026-10-21",
                available_at="2026-10-21 18:00:00",
                summary="公司公告业绩预增",
                fields=fields,
                confidence="high",
            )
        ]


def test_cninfo_provider_filters_stock_and_forecast_title() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    class FakeCninfo(CninfoAnnouncementProvider):
        async def _post_json(self, endpoint: str, payload: dict[str, str]) -> object:
            calls.append((endpoint, payload))
            if endpoint == self.search_endpoint:
                return [{"code": "600519", "orgId": "gssh0600519"}]
            return {
                "totalAnnouncement": 3,
                "announcements": [
                    {
                        "secCode": "600519",
                        "announcementTitle": "2026年前三季度业绩预告",
                        "announcementId": "a1",
                        "announcementTime": 1792080000000,
                        "adjunctUrl": "finalpage/2026-10-16/a1.PDF",
                    },
                    {
                        "secCode": "600519",
                        "announcementTitle": "董事会决议公告",
                        "announcementId": "a2",
                        "announcementTime": 1792080000000,
                        "adjunctUrl": "finalpage/2026-10-16/a2.PDF",
                    },
                    {
                        "secCode": "000001",
                        "announcementTitle": "业绩预告",
                        "announcementId": "a3",
                        "announcementTime": 1792080000000,
                        "adjunctUrl": "finalpage/2026-10-16/a3.PDF",
                    },
                ],
            }

    need = DataNeed(
        need_id="need-forecast",
        dataset_id="reports.forecast",
        field="net_profit_min",
        reason="missing",
        priority="critical",
        required_for="catalyst",
        query="业绩预告",
    )
    hits = asyncio.run(
        FakeCninfo().search(
            need,
            symbol="600519.SH",
            stock_name="贵州茅台",
            asof="2026-10-19",
        )
    )
    assert len(hits) == 1
    assert hits[0].title == "2026年前三季度业绩预告"
    assert hits[0].url.endswith("a1.PDF")
    assert hits[0].fields["ann_date"] == "2026-10-16"
    assert calls[0][1]["keyWord"] == "600519"
    assert calls[1][0] == CninfoAnnouncementProvider.endpoint
    assert calls[1][1]["stock"] == "600519,gssh0600519"
    assert calls[1][1]["column"] == "sse"


def test_cninfo_provider_rejects_incomplete_pagination() -> None:
    class IncompleteCninfo(CninfoAnnouncementProvider):
        async def _lookup_org_id(self, code6: str) -> str:
            return "gssh0600519"

        async def _post_json(self, endpoint: str, payload: dict[str, str]) -> object:
            return {
                "totalAnnouncement": 31,
                "announcements": [],
            }

    need = DataNeed(
        need_id="need-forecast",
        dataset_id="reports.forecast",
        field="ann_date",
        reason="missing",
        priority="critical",
        required_for="catalyst",
        query="业绩预告",
    )
    with pytest.raises(ProviderUnavailableError, match="incomplete"):
        asyncio.run(
            IncompleteCninfo().search(
                need,
                symbol="600519.SH",
                stock_name="贵州茅台",
                asof="2026-10-19",
            )
        )


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


def test_supplement_distinguishes_partial_extraction_from_not_found(
    tmp_path: Path,
) -> None:
    class MetadataOnlyProvider:
        async def search(
            self,
            need: DataNeed,
            *,
            symbol: str,
            stock_name: str,
            asof: str,
        ) -> list[WebSearchResult]:
            return [
                WebSearchResult(
                    title="业绩情况自愿性披露",
                    url="https://example.test/announcement",
                    published_at="2026-10-17",
                    available_at="2026-10-17 00:00:00",
                    fields={"ann_date": "2026-10-17", "net_profit_min": None},
                    confidence="high",
                )
            ]

    need = DataNeed(
        need_id="need-forecast",
        dataset_id="reports.forecast",
        field="ann_date,net_profit_min,net_profit_max",
        reason="local missing",
        priority="critical",
        required_for="catalyst",
        query="forecast",
    )
    result = asyncio.run(
        supplement_data_needs(
            needs=[need],
            provider=MetadataOnlyProvider(),
            data_config=object(),
            evidence=EvidenceIndex("snap-test"),
            asof="2026-10-19",
            symbol="600487.SH",
            stock_name="亨通光电",
            run_dir=tmp_path / "partial-run",
        )
    )
    assert result["status"] == "partial"
    assert result["results"][0]["status"] == "partial"
    assert result["results"][0]["missing_fields"] == [
        "net_profit_min",
        "net_profit_max",
    ]


def test_supplement_marks_provider_failure_as_error(tmp_path: Path) -> None:
    class FailingProvider:
        async def search(
            self,
            need: DataNeed,
            *,
            symbol: str,
            stock_name: str,
            asof: str,
        ) -> list[WebSearchResult]:
            raise ProviderUnavailableError("CNINFO pagination incomplete")

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
            provider=FailingProvider(),
            data_config=object(),
            evidence=EvidenceIndex("snap-test"),
            asof="2026-10-19",
            symbol="600487.SH",
            stock_name="亨通光电",
            run_dir=tmp_path / "failed-run",
        )
    )
    assert result["status"] == "error"
    assert result["results"][0]["status"] == "error"
    assert "not_found" not in {item["status"] for item in result["results"]}


def test_web_provider_is_built_only_with_explicit_consent(tmp_path: Path) -> None:
    config = SimpleNamespace(
        web_supplement_provider="cninfo",
        web_supplement_endpoint=None,
        web_supplement_api_key_env="TRADE_COUNCIL_WEB_SEARCH_KEY",
        web_supplement_timeout_seconds=20,
    )
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
            data_config=config,
            evidence=EvidenceIndex("snap-test"),
            asof="2026-10-19",
            symbol="600487.SH",
            stock_name="亨通光电",
            run_dir=tmp_path / "no-consent",
            allow_web=False,
        )
    )
    assert result["status"] == "unavailable"
    assert isinstance(build_web_search_provider(config), CninfoAnnouncementProvider)
