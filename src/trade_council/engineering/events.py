"""Normalize company announcements and market news for a single analysis run."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, cast

import pandas as pd

logger = logging.getLogger(__name__)
EVENT_COLUMNS = [
    "event_id",
    "event_time",
    "available_at",
    "event_date",
    "event_type",
    "event_subtype",
    "security_ids",
    "direction",
    "severity",
    "confidence",
    "title",
    "summary",
    "source_level",
    "source_url",
    "source_dataset",
    "source_record_id",
    "doc_role",
    "is_issuer_event",
    "dedupe_key",
    "snapshot_id",
    "quality_status",
]


def _event_id(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return "E-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _safe_date(value: Any) -> pd.Timestamp | None:
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else cast(pd.Timestamp, parsed)


def _text(value: Any) -> str:
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return ""
    return str(value)


def _conservative_available_at(value: Any, date_value: Any) -> str:
    parsed = _safe_date(value)
    if parsed is not None and (parsed.hour or parsed.minute or parsed.second):
        return cast(str, parsed.isoformat(sep=" ", timespec="seconds"))
    day = _safe_date(date_value)
    if day is None:
        return ""
    return f"{day.date().isoformat()} 23:59:59"


def _event_record(
    *,
    event_id: str,
    event_time: Any,
    available_at: str,
    event_date: Any,
    event_type: str,
    event_subtype: str,
    security_ids: list[str],
    direction: str,
    severity: str,
    confidence: str,
    title: str,
    summary: str,
    source_level: str,
    source_url: str | None,
    source_dataset: str,
    source_record_id: str,
    doc_role: str,
    is_issuer_event: bool,
    dedupe_key: str,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_time": str(event_time or ""),
        "available_at": available_at,
        "event_date": str(event_date or ""),
        "event_type": event_type,
        "event_subtype": event_subtype,
        "security_ids": security_ids,
        "direction": direction,
        "severity": severity,
        "confidence": confidence,
        "title": title,
        "summary": summary,
        "source_level": source_level,
        "source_url": source_url,
        "source_dataset": source_dataset,
        "source_record_id": source_record_id,
        "doc_role": doc_role,
        "is_issuer_event": is_issuer_event,
        "dedupe_key": dedupe_key,
        "snapshot_id": "",
        "quality_status": "usable",
    }


def _code_matches(value: Any, code6: str) -> bool:
    text = str(value or "")
    return bool(re.search(rf"(?<!\d){re.escape(code6)}(?!\d)", text))


def _load_announcements(
    data_root: Path,
    code6: str,
    ts_code: str,
    asof: str,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    path = data_root / "announcements" / "cninfo"
    if not path.is_dir():
        return pd.DataFrame(), []
    frames: list[pd.DataFrame] = []
    for file_path in sorted(path.glob("*.parquet")):
        try:
            frame = pd.read_parquet(file_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skip unreadable announcement partition %s: %s", file_path, exc)
            continue
        if "sec_code" not in frame or "date" not in frame:
            continue
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        mask = frame["sec_code"].map(lambda item: _code_matches(item, code6))
        mask &= frame["date"] <= pd.Timestamp(asof)
        if mask.any():
            selected = frame.loc[mask].copy()
            selected["source_file"] = file_path.name
            frames.append(selected)
    if not frames:
        return pd.DataFrame(), []
    announcements = pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["ann_id"] if "ann_id" in frames[0].columns else None
    )
    events: list[dict[str, Any]] = []
    for _, row in announcements.iterrows():
        event_tag = str(row.get("event_tag") or "").strip()
        doc_role = str(row.get("doc_role") or "").strip()
        if not event_tag or doc_role != "issuer":
            continue
        event_date = _safe_date(row.get("date"))
        title = _text(row.get("title"))
        ann_id = _text(row.get("ann_id"))
        dedupe_key = f"{event_date.date() if event_date else ''}|{code6}|{event_tag}"
        events.append(
            _event_record(
                event_id=_event_id("announcements.cninfo", ann_id, event_tag, title),
                event_time=row.get("ann_time"),
                available_at=_conservative_available_at(row.get("ann_time"), row.get("date")),
                event_date=event_date.date() if event_date else "",
                event_type=event_tag,
                event_subtype=_text(row.get("event_all")),
                security_ids=[ts_code],
                direction="neutral",
                severity="high" if event_tag in {"退市风险", "诉讼仲裁", "问询函"} else "medium",
                confidence=str(row.get("event_conf") or "medium"),
                title=title,
                summary=_text(row.get("event_all")),
                source_level="local",
                source_url=_text(row.get("full_url")) or None,
                source_dataset="announcements.cninfo",
                source_record_id=ann_id,
                doc_role=doc_role,
                is_issuer_event=True,
                dedupe_key=dedupe_key,
            )
        )
    return announcements, events


def _load_news(
    data_root: Path,
    code6: str,
    ts_code: str,
    asof: str,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    path = data_root / "news" / "em_fast"
    if not path.is_dir():
        return pd.DataFrame(), []
    frames: list[pd.DataFrame] = []
    for file_path in sorted(path.glob("*.parquet")):
        try:
            frame = pd.read_parquet(file_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skip unreadable news partition %s: %s", file_path, exc)
            continue
        if "show_time" not in frame or "a_codes" not in frame:
            continue
        frame["show_time"] = pd.to_datetime(frame["show_time"], errors="coerce")
        mask = frame["a_codes"].map(lambda item: _code_matches(item, code6))
        mask &= frame["show_time"] <= pd.Timestamp(f"{asof} 23:59:59")
        if mask.any():
            selected = frame.loc[mask].copy()
            selected["source_file"] = file_path.name
            frames.append(selected)
    if not frames:
        return pd.DataFrame(), []
    news = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["code"])
    events: list[dict[str, Any]] = []
    for _, row in news.iterrows():
        show_time = _safe_date(row.get("show_time"))
        if show_time is None:
            continue
        title = _text(row.get("title"))
        news_code = _text(row.get("code"))
        events.append(
            _event_record(
                event_id=_event_id("news.em_fast", news_code, title),
                event_time=show_time.isoformat(sep=" ", timespec="seconds"),
                available_at=show_time.isoformat(sep=" ", timespec="seconds"),
                event_date=show_time.date(),
                event_type="快讯",
                event_subtype="市场新闻",
                security_ids=[ts_code],
                direction="neutral",
                severity="low",
                confidence="medium",
                title=title,
                summary=_text(row.get("summary")),
                source_level="local",
                source_url=None,
                source_dataset="news.em_fast",
                source_record_id=news_code,
                doc_role="news",
                is_issuer_event=False,
                dedupe_key=_event_id("news.em_fast", news_code),
            )
        )
    return news, events


def _load_forecast_events(
    data_root: Path,
    code6: str,
    asof: str,
) -> list[dict[str, Any]]:
    path = data_root / "reports" / "forecast"
    if not path.is_dir():
        return []
    frames: list[pd.DataFrame] = []
    for file_path in sorted(path.rglob("*.parquet")):
        try:
            frame = pd.read_parquet(file_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skip unreadable forecast partition %s: %s", file_path, exc)
            continue
        if "ts_code" not in frame or "ann_date" not in frame:
            continue
        mask = frame["ts_code"].map(lambda item: _code_matches(item, code6))
        mask &= frame["ann_date"].astype(str) <= asof.replace("-", "")
        if mask.any():
            frames.append(frame.loc[mask].copy())
    if not frames:
        return []
    forecast = pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["ts_code", "ann_date", "end_date"]
    )
    events: list[dict[str, Any]] = []
    for _, row in forecast.iterrows():
        ann_date = _safe_date(row.get("ann_date"))
        if ann_date is None:
            continue
        summary = _text(row.get("summary"))
        event_key = f"{row.get('ts_code')}|{row.get('ann_date')}|{row.get('end_date')}"
        events.append(
            _event_record(
                event_id=_event_id("reports.forecast", event_key),
                event_time=ann_date.isoformat(sep=" ", timespec="seconds"),
                available_at=f"{ann_date.date().isoformat()} 23:59:59",
                event_date=ann_date.date(),
                event_type="业绩预告",
                event_subtype=_text(row.get("type")),
                security_ids=[_text(row.get("ts_code"))],
                direction="neutral",
                severity="high",
                confidence="high",
                title=summary,
                summary=_text(row.get("change_reason")),
                source_level="local",
                source_url=None,
                source_dataset="reports.forecast",
                source_record_id=event_key,
                doc_role="issuer",
                is_issuer_event=True,
                dedupe_key=event_key,
            )
        )
    return events


def load_events(
    data_root: Path,
    *,
    code6: str,
    ts_code: str,
    asof: str,
    include_news: bool,
) -> dict[str, Any]:
    """Load normalized event data for one stock and one as-of date."""

    announcements, announcement_events = _load_announcements(data_root, code6, ts_code, asof)
    news, news_events = (
        _load_news(data_root, code6, ts_code, asof)
        if include_news
        else (pd.DataFrame(), [])
    )
    events = [
        *announcement_events,
        *news_events,
        *_load_forecast_events(data_root, code6, asof),
    ]
    return {
        "announcements": announcements,
        "news": news,
        "events": events,
    }


def write_event_artifacts(out_dir: Path, payload: dict[str, Any]) -> None:
    announcements = payload["announcements"]
    news = payload["news"]
    events = payload["events"]
    if not announcements.empty:
        announcements.to_csv(out_dir / "announcements_cninfo.csv", index=False, encoding="utf-8")
    if not news.empty:
        news.to_csv(out_dir / "news_em_fast.csv", index=False, encoding="utf-8")
    event_frame = pd.DataFrame(events)
    if not event_frame.empty:
        event_frame = event_frame[EVENT_COLUMNS]
        event_frame.to_csv(out_dir / "events.csv", index=False, encoding="utf-8")
        (out_dir / "events.jsonl").write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n",
            encoding="utf-8",
        )
    else:
        pd.DataFrame(columns=EVENT_COLUMNS).to_csv(
            out_dir / "events.csv", index=False, encoding="utf-8"
        )
        (out_dir / "events.jsonl").write_text("", encoding="utf-8")
    timeline = sorted(events, key=lambda item: str(item["available_at"]), reverse=True)
    (out_dir / "event_timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_event_digest(out_dir: Path, payload: dict[str, Any]) -> None:
    events = payload["events"]
    digest = out_dir / "DATA_DIGEST.md"
    lines = [
        "",
        "## 13. 事件时间线",
        "",
        f"- 事件数量：{len(events)}",
        f"- 公告原始记录：{len(payload['announcements'])}",
        f"- 快讯原始记录：{len(payload['news'])}",
        "",
    ]
    for event in sorted(events, key=lambda item: str(item["available_at"]), reverse=True)[:20]:
        lines.append(
            f"- `{event['available_at']}` **{event['event_type']}** "
            f"{event['title']} {event['source_url'] or ''}"
        )
    with digest.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
