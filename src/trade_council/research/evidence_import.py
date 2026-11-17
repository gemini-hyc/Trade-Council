"""Import supplementary evidence found by the current agent.

The importer is the only write path for evidence that did not come from the
local data pack. It refuses future-dated material, requires a locator and a
period for numbers, hashes any locally saved source file, and is idempotent:
re-importing the same finding never inflates the evidence count.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

EVIDENCE_FILE = "supplemental_evidence.json"
LOG_FILE = "evidence_imports.jsonl"
SOURCE_DIR = "supplement/sources"
SCHEMA_VERSION = "1.0"
VALID_TYPES = {"number", "text", "event", "boolean"}


class EvidenceImportError(ValueError):
    """Raised for a payload that cannot be imported at all."""


@dataclass
class ImportResult:
    added: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    batch_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "added": list(self.added),
            "duplicates": list(self.duplicates),
            "rejected": list(self.rejected),
            "added_count": len(self.added),
            "duplicate_count": len(self.duplicates),
            "rejected_count": len(self.rejected),
        }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_date(value: object) -> date | None:
    text = str(value or "").strip()[:10].replace("/", "-")
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _stable_id(item: dict[str, Any]) -> str:
    payload = {
        key: item.get(key)
        for key in ("dataset_id", "field", "period", "value", "unit", "source_url")
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return "S-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _fingerprint(item: dict[str, Any]) -> str:
    """What makes two imports the same evidence, ignoring bookkeeping fields."""

    payload = {
        key: item.get(key)
        for key in (
            "dataset_id",
            "field",
            "period",
            "value",
            "unit",
            "source_url",
            "source_file_sha256",
            "location",
            "available_at",
        )
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _store_source(run_dir: Path, path: Path) -> str:
    """Copy the source file under the run so the evidence stays verifiable."""

    digest = _sha256_file(path)
    target_dir = run_dir / SOURCE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{digest[:16]}{path.suffix.lower()}"
    if not target.is_file():
        target.write_bytes(path.read_bytes())
    return str(target.relative_to(run_dir))


def _check_item(
    item: dict[str, Any],
    *,
    symbol: str,
    asof: str,
    run_dir: Path,
    reserved_ids: set[str],
) -> tuple[dict[str, Any], str]:
    """Return ``(normalised_item, "")`` or ``({}, reason)``."""

    evidence = dict(item)
    evidence_id = str(evidence.get("evidence_id") or "").strip()
    if evidence_id and evidence_id in reserved_ids:
        return {}, f"证据 ID {evidence_id} 已被数据包基础证据占用，补充证据不能覆盖或冒充它"
    dataset_id = str(evidence.get("dataset_id") or "").strip()
    if not dataset_id:
        return {}, "缺少 dataset_id"
    evidence_symbol = str(evidence.get("symbol") or symbol).strip()
    if evidence_symbol and evidence_symbol != symbol:
        return {}, f"证据股票代码 {evidence_symbol} 与运行 {symbol} 不一致"
    value = evidence.get("value")
    value_type = str(evidence.get("value_type") or "").strip()
    if not value_type:
        if isinstance(value, bool):
            value_type = "boolean"
        elif isinstance(value, (int, float)):
            value_type = "number"
        elif isinstance(value, str):
            value_type = "text"
        else:
            return {}, "无法推断 value_type，需显式声明"
    if value_type not in VALID_TYPES:
        return {}, f"value_type 非法：{value_type}"
    if value_type == "number" and not isinstance(value, (int, float)):
        return {}, f"value_type=number 但值不是数字：{value!r}"
    if value_type == "number":
        for required in ("unit", "period"):
            if not str(evidence.get(required) or "").strip():
                return {}, f"数值证据缺少 {required}，口径不可复述"
    available_at = str(evidence.get("available_at") or "").strip()
    if not available_at:
        return {}, "缺少 available_at（该数据何时可得）"
    observed = _parse_date(available_at)
    if observed is None:
        return {}, f"available_at 不是合法日期：{available_at!r}"
    anchor = _parse_date(asof)
    if anchor is not None and observed > anchor:
        return {}, f"available_at {available_at} 晚于基准日 {asof}，属于未来数据"
    url = str(evidence.get("source_url") or "").strip()
    if url:
        host = urlparse(url).scheme
        if host not in {"http", "https"}:
            return {}, f"source_url 必须是 http(s) 链接：{url}"
    file_path = str(evidence.get("source_file") or "").strip()
    if not url and not file_path:
        return {}, "缺少 source_url 或 source_file，无法定位来源"
    if url and not str(evidence.get("location") or "").strip():
        return {}, f"URL 证据缺少 location（页码/表格/章节），无法复核：{url}"
    if file_path:
        path = Path(file_path).expanduser()
        if not path.is_file():
            return {}, f"source_file 不存在：{file_path}；不能保存没有来源定位的证据"
        try:
            path.read_bytes()[:1]
        except OSError as exc:
            return {}, f"source_file 不可读：{file_path}（{exc}）"
        evidence["source_file"] = str(path)
        evidence["source_file_sha256"] = _sha256_file(path)
        evidence["source_copy"] = _store_source(run_dir, path)
    material = str(evidence.get("material") or "").strip()
    if material:
        # Web page text is data, never an instruction to the agent.
        evidence["quoted_text"] = material[:2000]
        evidence["text_trust"] = "untrusted_source_text"
    evidence.pop("material", None)
    evidence["symbol"] = evidence_symbol or symbol
    evidence["value_type"] = value_type
    evidence["source_level"] = str(evidence.get("source_level") or "external")
    evidence["evidence_id"] = evidence_id or _stable_id(evidence)
    return evidence, ""


def import_evidence(
    run_dir: Path,
    payload: dict[str, Any] | list[Any],
    *,
    symbol: str,
    asof: str,
    source_label: str = "",
    reserved_ids: set[str] | None = None,
) -> ImportResult:
    """Validate and persist a batch of supplementary evidence.

    A repeated identical import is a duplicate; an id that already exists with
    different content is a conflict and is rejected, because silently keeping
    the first version would hide a corrected number.
    """

    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        raise EvidenceImportError("导入文件必须包含非空的 items 数组")

    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / EVIDENCE_FILE
    store: dict[str, Any] = (
        _read_json(path) if path.is_file() else {"schema_version": SCHEMA_VERSION, "items": []}
    )
    existing: dict[str, dict[str, Any]] = {}
    stored = store.get("items")
    for entry in stored if isinstance(stored, list) else []:
        if isinstance(entry, dict):
            existing[str(entry.get("evidence_id"))] = cast(dict[str, Any], entry)

    result = ImportResult()
    batch_blob = json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
    result.batch_id = "B-" + hashlib.sha256(batch_blob.encode("utf-8")).hexdigest()[:12]

    for raw in items:
        if not isinstance(raw, dict):
            result.rejected.append({"reason": "item 必须是对象", "value": str(raw)[:80]})
            continue
        evidence, reason = _check_item(
            raw,
            symbol=symbol,
            asof=asof,
            run_dir=run_dir,
            reserved_ids=reserved_ids or set(),
        )
        if reason:
            result.rejected.append({"reason": reason, "dataset_id": str(raw.get("dataset_id") or "")})
            continue
        evidence_id = str(evidence["evidence_id"])
        if evidence_id in existing:
            previous = existing[evidence_id]
            if _fingerprint(previous) == _fingerprint(evidence):
                result.duplicates.append(evidence_id)
            else:
                result.rejected.append(
                    {
                        "reason": (
                            f"证据 ID {evidence_id} 已存在但内容不同（来源或数值冲突），"
                            "请换一个明确的 evidence_id 并说明两者差异"
                        ),
                        "dataset_id": str(evidence.get("dataset_id") or ""),
                    }
                )
            continue
        evidence["import_batch_id"] = result.batch_id
        existing[evidence_id] = evidence
        result.added.append(evidence_id)

    if result.added or (path.is_file() and existing):
        store["items"] = list(existing.values())
        store["schema_version"] = SCHEMA_VERSION
        path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")

    log_entry = {
        "batch_id": result.batch_id,
        "source_label": source_label,
        "symbol": symbol,
        "asof": asof,
        "added": result.added,
        "duplicates": result.duplicates,
        "rejected": result.rejected,
        "stored_sources": [
            {
                "evidence_id": evidence_id,
                "source_file": existing[evidence_id].get("source_file"),
                "source_file_sha256": existing[evidence_id].get("source_file_sha256"),
                "source_copy": existing[evidence_id].get("source_copy"),
            }
            for evidence_id in result.added
            if existing.get(evidence_id, {}).get("source_copy")
        ],
    }
    with (run_dir / LOG_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    return result
