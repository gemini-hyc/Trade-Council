"""Content-addressed evidence index and trace records."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..schemas import StrictModel


class EvidenceItem(StrictModel):
    evidence_id: str
    dataset_id: str
    path: str
    primary_key: str
    event_date: str | None = None
    available_at: str | None = None
    field: str | None = None
    value: Any = None
    unit: str = ""
    formula: str | None = None
    snapshot_id: str
    source_hash: str | None = None
    note: str = ""
    source_level: str = "local"
    source_url: str | None = None
    retrieved_at: str | None = None
    extraction_confidence: str | None = None


class EvidenceIndex:
    def __init__(self, snapshot_id: str) -> None:
        self.snapshot_id = snapshot_id
        self._items: dict[str, EvidenceItem] = {}

    @classmethod
    def load(cls, path: Path) -> EvidenceIndex:
        raw = json.loads(path.read_text(encoding="utf-8"))
        index = cls(snapshot_id=str(raw["snapshot_id"]))
        for item in raw.get("items", []):
            evidence = EvidenceItem.model_validate(item)
            index._items[evidence.evidence_id] = evidence
        return index

    @staticmethod
    def _digest(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def add(
        self,
        *,
        dataset_id: str,
        path: str,
        primary_key: str,
        event_date: str | None,
        available_at: str | None,
        field: str | None,
        value: Any,
        unit: str = "",
        formula: str | None = None,
        source_hash: str | None = None,
        note: str = "",
        source_level: str = "local",
        source_url: str | None = None,
        retrieved_at: str | None = None,
        extraction_confidence: str | None = None,
    ) -> EvidenceItem:
        payload = {
            "dataset_id": dataset_id,
            "path": path,
            "primary_key": primary_key,
            "event_date": event_date,
            "available_at": available_at,
            "field": field,
            "value": value,
            "unit": unit,
            "formula": formula,
            "snapshot_id": self.snapshot_id,
            "source_level": source_level,
            "source_url": source_url,
            "retrieved_at": retrieved_at,
            "extraction_confidence": extraction_confidence,
        }
        slug = dataset_id.replace(".", "-").replace("_", "-")
        evidence_id = f"F-{slug}-{self._digest(payload)}"
        item = EvidenceItem(
            evidence_id=evidence_id,
            dataset_id=dataset_id,
            path=path,
            primary_key=primary_key,
            event_date=event_date,
            available_at=available_at,
            field=field,
            value=value,
            unit=unit,
            formula=formula,
            snapshot_id=self.snapshot_id,
            source_hash=source_hash,
            note=note,
            source_level=source_level,
            source_url=source_url,
            retrieved_at=retrieved_at,
            extraction_confidence=extraction_confidence,
        )
        self._items[evidence_id] = item
        return item

    def validate_references(self, evidence_ids: list[str]) -> None:
        missing = sorted(set(evidence_ids) - set(self._items))
        if missing:
            raise ValueError(f"Unknown evidence ids: {', '.join(missing)}")

    def all(self) -> list[EvidenceItem]:
        return list(self._items.values())

    def add_item(self, item: EvidenceItem) -> None:
        self._items[item.evidence_id] = item

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "items": [item.model_dump() for item in self.all()],
        }

    def write(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "evidence.json").write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with (out_dir / "evidence_trace.jsonl").open("w", encoding="utf-8") as handle:
            for item in self.all():
                handle.write(json.dumps(item.model_dump(), ensure_ascii=False) + "\n")

    def append_digest(self, digest_path: Path) -> None:
        lines = [
            "",
            "## 14. 关键证据 ID",
            "",
            "| Evidence ID | 数据集 | 字段 | 值 | 可用时间 |",
            "|---|---|---|---|---|",
        ]
        for item in self.all():
            lines.append(
                f"| {item.evidence_id} | {item.dataset_id} | {item.field or '—'} | "
                f"{item.value} {item.unit} | {item.available_at or '—'} |"
            )
        with digest_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
