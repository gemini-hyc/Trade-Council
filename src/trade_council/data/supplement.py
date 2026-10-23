"""Task-scoped data needs and external supplemental evidence."""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from zoneinfo import ZoneInfo

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


class CninfoAnnouncementProvider:
    """Task-scoped official announcement search through CNINFO."""

    endpoint = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
    search_endpoint = "https://www.cninfo.com.cn/new/information/topSearch/query"
    page_size = 30
    max_pages = 200
    user_agent = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
    _forecast_title = re.compile(
        r"业绩预告|业绩预增|业绩预减|业绩预亏|"
        r"预计.{0,12}(?:亏损|盈利|增长|下降)|"
        r"业绩(?:快报|情况)|主要经营数据|经营情况"
    )

    async def _post_json(self, endpoint: str, payload: dict[str, str]) -> Any:
        return await asyncio.to_thread(self._post_sync, endpoint, payload)

    def _post_sync(self, endpoint: str, payload: dict[str, str]) -> Any:
        request = urllib.request.Request(
            endpoint,
            data=urllib.parse.urlencode(payload).encode("utf-8"),
            headers={
                "User-Agent": self.user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://www.cninfo.com.cn/new/disclosure",
                "Origin": "https://www.cninfo.com.cn",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))

    async def _lookup_org_id(self, code6: str) -> str:
        parsed = await self._post_json(
            self.search_endpoint,
            {"keyWord": code6, "maxNum": "10"},
        )
        if not isinstance(parsed, list):
            raise ProviderUnavailableError("CNINFO stock search returned a non-list response")
        for item in parsed:
            if not isinstance(item, dict) or str(item.get("code") or "") != code6:
                continue
            org_id = str(item.get("orgId") or "").strip()
            if org_id:
                return org_id
        raise ProviderUnavailableError(f"CNINFO orgId not found for {code6}")

    async def _fetch_target_announcements(
        self,
        *,
        code6: str,
        org_id: str,
        column: str,
        start_date: date,
        end_date: date,
        query: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        total: int | None = None
        for page in range(1, self.max_pages + 1):
            response = await self._post_json(
                self.endpoint,
                {
                    "stock": f"{code6},{org_id}",
                    "tabName": "fulltext",
                    "pageSize": str(self.page_size),
                    "pageNum": str(page),
                    "column": column,
                    "category": "",
                    "plate": "",
                    "seDate": f"{start_date.isoformat()}~{end_date.isoformat()}",
                    "searchkey": query,
                    "secid": "",
                    "sortName": "",
                    "sortType": "",
                    "isHLtitle": "true",
                },
            )
            if not isinstance(response, dict):
                raise ProviderUnavailableError("CNINFO announcement query returned a non-object")
            if total is None:
                try:
                    total = int(response.get("totalAnnouncement") or 0)
                except (TypeError, ValueError) as exc:
                    raise ProviderUnavailableError(
                        "CNINFO announcement query returned an invalid total"
                    ) from exc
            batch = response.get("announcements") or []
            if not isinstance(batch, list):
                raise ProviderUnavailableError(
                    "CNINFO announcement query returned a non-list page"
                )
            if total == 0:
                if batch:
                    raise ProviderUnavailableError(
                        "CNINFO announcement query returned rows with total=0"
                    )
                return []
            for item in batch:
                if not isinstance(item, dict):
                    continue
                key = str(
                    item.get("announcementId")
                    or (
                        item.get("secCode"),
                        item.get("announcementTitle"),
                        item.get("announcementTime"),
                        item.get("adjunctUrl"),
                    )
                )
                if key not in seen:
                    seen.add(key)
                    rows.append(item)
            if len(rows) >= total:
                return rows
            if not batch:
                break
        raise ProviderUnavailableError(
            "CNINFO announcement query incomplete: "
            f"received {len(rows)} of {total or 'unknown'} rows"
        )

    async def search(
        self,
        need: DataNeed,
        *,
        symbol: str,
        stock_name: str,
        asof: str,
    ) -> list[WebSearchResult]:
        if need.dataset_id not in {"announcements.cninfo", "reports.forecast"}:
            raise ProviderUnavailableError(
                f"CNINFO does not provide dataset {need.dataset_id}"
            )
        code6 = symbol.split(".")[0]
        exchange = symbol.rsplit(".", 1)[-1].upper()
        end_date = date.fromisoformat(asof[:10])
        start_date = end_date - timedelta(days=14)
        org_id = await self._lookup_org_id(code6)
        rows = await self._fetch_target_announcements(
            code6=code6,
            org_id=org_id,
            column={"SH": "sse", "SZ": "szse", "BJ": "bj"}.get(exchange, "szse"),
            start_date=start_date,
            end_date=end_date,
            query="",
        )
        results: list[WebSearchResult] = []
        for row in rows:
            if str(row.get("secCode") or "") != code6:
                continue
            title = str(row.get("announcementTitle") or "")
            if (
                need.dataset_id == "reports.forecast"
                and not self._forecast_title.search(title)
            ):
                continue
            adjunct = str(row.get("adjunctUrl") or "")
            url = (
                f"https://static.cninfo.com.cn/{adjunct}"
                if adjunct
                else f"https://www.cninfo.com.cn/new/disclosure?stock={code6}"
            )
            published = self._format_time(row.get("announcementTime"))
            results.append(
                WebSearchResult(
                    title=title,
                    url=url,
                    published_at=published,
                    available_at=published,
                    summary=title,
                    fields={
                        "ann_date": published[:10] if published else None,
                        "type": (
                            "业绩预告/业绩情况"
                            if need.dataset_id == "reports.forecast"
                            else "公告"
                        ),
                        "summary": title,
                        "announcement_id": str(row.get("announcementId") or ""),
                        "source_title": title,
                    },
                    confidence="high",
                )
            )
        return results[:10]

    @staticmethod
    def _format_time(value: Any) -> str | None:
        try:
            timestamp = float(value) / 1000
            return (
                datetime.fromtimestamp(timestamp, tz=UTC)
                .astimezone(ZoneInfo("Asia/Shanghai"))
                .isoformat(sep=" ", timespec="seconds")
            )
        except (TypeError, ValueError, OSError, OverflowError):
            return None


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


def build_web_search_provider(config: Any) -> WebSearchProvider | None:
    endpoint = getattr(config, "web_supplement_endpoint", None)
    if getattr(config, "web_supplement_provider", "http_json") == "cninfo":
        return CninfoAnnouncementProvider()
    if not endpoint:
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
    allow_web: bool = False,
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
    if provider is None and allow_web:
        provider = build_web_search_provider(data_config)
    if provider is None:
        existing_path = run_dir / "supplemental_data.json"
        if existing_path.is_file():
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
            if (
                isinstance(existing, dict)
                and existing.get("status") not in {None, "", "unavailable"}
            ):
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
        satisfied_fields: set[str] = set()
        for hit in hits:
            if hit.available_at and str(hit.available_at)[:10] > asof[:10]:
                continue
            accepted.append(hit.model_dump())
            fields = hit.fields or {"summary": hit.summary}
            if hit.summary:
                satisfied_fields.add("summary")
            for field_name, value in fields.items():
                if value in (None, ""):
                    continue
                satisfied_fields.add(str(field_name))
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
        requested_fields = [
            item.strip() for item in need.field.split(",") if item.strip()
        ]
        missing_fields = [
            item for item in requested_fields if item not in satisfied_fields
        ]
        result_status = (
            "partial"
            if accepted and missing_fields
            else ("found" if accepted else "not_found")
        )
        results.append(
            {
                "need_id": need.need_id,
                "status": result_status,
                "sources": accepted,
                "missing_fields": missing_fields,
            }
        )

    statuses = [str(item.get("status") or "") for item in results]
    if statuses and all(status == "found" for status in statuses):
        status = "completed"
    elif statuses and all(status == "not_found" for status in statuses):
        status = "not_found"
    elif any(status in {"found", "partial"} for status in statuses):
        status = "partial"
    else:
        status = "error"
    payload = {
        "status": status,
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
