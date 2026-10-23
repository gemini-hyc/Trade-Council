"""Task-scoped data needs and external supplemental evidence."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import Field

from ..engineering.evidence import EvidenceIndex
from ..errors import ProviderUnavailableError
from ..llm.http import request_json
from ..schemas import StrictModel


class DataNeed(StrictModel):
    need_id: str
    dataset_id: str
    field: str
    reason: str
    priority: Literal["critical", "normal", "low"] = "normal"
    required_for: str
    query: str


class WebSearchResult(StrictModel):
    title: str
    url: str
    published_at: str | None = None
    available_at: str | None = None
    summary: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    confidence: Literal["high", "medium", "low"] = "medium"


class WebSearchProvider(Protocol):
    async def search(
        self,
        need: DataNeed,
        *,
        symbol: str,
        stock_name: str,
        asof: str,
    ) -> list[WebSearchResult]: ...


class HttpJsonSearchProvider:
    """Adapter for a local/team search service with a small JSON contract."""

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def search(
        self,
        need: DataNeed,
        *,
        symbol: str,
        stock_name: str,
        asof: str,
    ) -> list[WebSearchResult]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = await request_json(
            self.endpoint,
            payload={
                "query": need.query,
                "symbol": symbol,
                "stock_name": stock_name,
                "asof": asof,
                "dataset_id": need.dataset_id,
                "field": need.field,
            },
            headers=headers,
            timeout=self.timeout_seconds,
        )
        raw_results = response.get("results", [])
        if not isinstance(raw_results, list):
            raise ProviderUnavailableError("Search endpoint returned non-list results")
        return [
            WebSearchResult.model_validate(item)
            for item in raw_results
            if isinstance(item, dict)
        ]


def _dataset_status(manifest: dict[str, Any], key: str) -> str:
    for item in manifest.get("datasets", []):
        if item.get("key") == key:
            return str(item.get("status") or "")
    return ""


def detect_data_needs(
    manifest: dict[str, Any],
    *,
    include_news: bool,
) -> list[DataNeed]:
    """Derive analysis-task data needs from DataPack diagnostics."""

    needs: list[DataNeed] = []
    forecast_status = _dataset_status(manifest, "forecast")
    forecast_single = _dataset_status(manifest, "forecast_single")
    if forecast_status in {"NO_RECORD", "WARN", "ERROR"} or forecast_single in {
        "NO_RECORD",
        "WARN",
        "ERROR",
    }:
        needs.append(
            DataNeed(
                need_id="need-forecast",
                dataset_id="reports.forecast",
                field="ann_date,type,net_profit_min,net_profit_max,summary,change_reason",
                reason="本地业绩预告缺失、滞后或未核实",
                priority="critical",
                required_for="事件催化、盈利预期和交易判断",
                query="业绩预告 公告 净利润 同比 变动原因",
            )
        )
    if _dataset_status(manifest, "daily_basic_quality") == "ERROR":
        needs.append(
            DataNeed(
                need_id="need-valuation",
                dataset_id="market.daily_basic",
                field="pe_ttm,pb,ps_ttm",
                reason="最新交易日估值字段整列为空",
                priority="normal",
                required_for="估值分位和风险收益分析",
                query="最新估值 PE PB PS 历史分位",
            )
        )
    if _dataset_status(manifest, "announcements_cninfo") == "NO_RECORD":
        needs.append(
            DataNeed(
                need_id="need-announcements",
                dataset_id="announcements.cninfo",
                field="title,event_tag,ann_time,full_url",
                reason="本次快照没有匹配到公告事件",
                priority="normal",
                required_for="事件时间线和重大事项分析",
                query="公司公告 重大事项 业绩预告 问询函",
            )
        )
    if include_news and _dataset_status(manifest, "news_em_fast") in {
        "NO_RECORD",
        "DISABLED",
        "ERROR",
    }:
        needs.append(
            DataNeed(
                need_id="need-news",
                dataset_id="news.em_fast",
                field="title,summary,show_time",
                reason="本次快照没有匹配到新闻快讯",
                priority="normal",
                required_for="消息面和事件驱动分析",
                query="个股 最新新闻 公告 催化",
            )
        )
    return needs


def _default_provider(config: Any) -> WebSearchProvider | None:
    endpoint = getattr(config, "web_supplement_endpoint", None)
    if not getattr(config, "web_supplement_enabled", False) or not endpoint:
        return None
    api_key = os.environ.get(
        getattr(config, "web_supplement_api_key_env", "TRADE_COUNCIL_WEB_SEARCH_KEY")
    )
    return HttpJsonSearchProvider(
        str(endpoint),
        api_key=api_key,
        timeout_seconds=float(getattr(config, "web_supplement_timeout_seconds", 20.0)),
    )


async def supplement_data_needs(
    *,
    needs: list[DataNeed],
    provider: WebSearchProvider | None,
    data_config: Any,
    evidence: EvidenceIndex,
    asof: str,
    symbol: str,
    stock_name: str,
    run_dir: Path,
) -> dict[str, Any]:
    """Search only for the current run and add accepted external facts to Evidence."""

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "data_needs.json").write_text(
        json.dumps([item.model_dump() for item in needs], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    results: list[dict[str, Any]] = []
    if not needs:
        (run_dir / "supplemental_data.json").write_text("[]", encoding="utf-8")
        return {"status": "not_needed", "needs": [], "results": []}
    if provider is None:
        provider = _default_provider(data_config)
    if provider is None:
        existing_path = run_dir / "supplemental_data.json"
        if existing_path.is_file():
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
            if existing.get("status") == "completed":
                return cast(dict[str, Any], existing)
        payload = {
            "status": "unavailable",
            "needs": [item.model_dump() for item in needs],
            "results": [],
            "message": "web supplement provider is not configured",
        }
        (run_dir / "supplemental_data.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return payload

    for need in needs:
        try:
            hits = await provider.search(
                need,
                symbol=symbol,
                stock_name=stock_name,
                asof=asof,
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "need_id": need.need_id,
                    "status": "error",
                    "error": str(exc),
                    "sources": [],
                }
            )
            continue
        accepted: list[dict[str, Any]] = []
        for hit in hits:
            if hit.available_at and str(hit.available_at)[:10] > asof[:10]:
                continue
            accepted.append(hit.model_dump())
            fields = hit.fields or {"summary": hit.summary}
            for field_name, value in fields.items():
                if value in (None, ""):
                    continue
                evidence.add(
                    dataset_id=need.dataset_id,
                    path="web_supplement",
                    primary_key=symbol,
                    event_date=str(hit.published_at or hit.available_at or asof)[:10],
                    available_at=hit.available_at or hit.published_at,
                    field=str(field_name),
                    value=value,
                    unit="",
                    formula="task-scoped web search",
                    source_hash=None,
                    note=f"{hit.title} | {hit.url} | need={need.need_id}",
                    source_level="web",
                    source_url=hit.url,
                    retrieved_at=datetime.now().isoformat(timespec="seconds"),
                    extraction_confidence=hit.confidence,
                )
        results.append(
            {
                "need_id": need.need_id,
                "status": "found" if accepted else "not_found",
                "sources": accepted,
            }
        )

    payload = {
        "status": "completed",
        "needs": [item.model_dump() for item in needs],
        "results": results,
    }
    (run_dir / "supplemental_data.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    supplemental_items = [
        item.model_dump()
        for item in evidence.all()
        if item.source_level == "web"
    ]
    (run_dir / "supplemental_evidence.json").write_text(
        json.dumps(
            {"snapshot_id": evidence.snapshot_id, "items": supplemental_items},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return payload
