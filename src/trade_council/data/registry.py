"""Dataset capability registry."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from pydantic import ValidationError

from ..errors import ConfigError
from .models import DatasetRegistryConfig, DatasetSpec


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    compact = value.replace("-", "")
    return date(int(compact[:4]), int(compact[4:6]), int(compact[6:8]))


class DatasetRegistry:
    def __init__(self, config: DatasetRegistryConfig) -> None:
        self.config = config
        self._by_id: dict[str, DatasetSpec] = {}
        for spec in config.datasets:
            if spec.id in self._by_id:
                raise ConfigError(f"Duplicate dataset id: {spec.id}")
            self._by_id[spec.id] = spec

    @classmethod
    def load(cls, path: Path) -> DatasetRegistry:
        if not path.is_file():
            raise ConfigError(f"Dataset registry not found: {path}")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            config = DatasetRegistryConfig.model_validate(raw)
        except (yaml.YAMLError, ValidationError) as exc:
            raise ConfigError(f"Invalid dataset registry {path}: {exc}") from exc
        return cls(config)

    def get(self, dataset_id: str) -> DatasetSpec:
        try:
            return self._by_id[dataset_id]
        except KeyError as exc:
            raise ConfigError(f"Unknown dataset id: {dataset_id}") from exc

    def all(self) -> list[DatasetSpec]:
        return list(self._by_id.values())

    def enabled(self) -> list[DatasetSpec]:
        return [spec for spec in self._by_id.values() if spec.enabled]

    def field_available(self, dataset_id: str, field_name: str, asof: date) -> bool:
        spec = self.get(dataset_id)
        for item in spec.field_availability:
            if item.field != field_name:
                continue
            start = _parse_date(item.available_from)
            end = _parse_date(item.available_to)
            return (start is None or asof >= start) and (end is None or asof <= end)
        return field_name in spec.fields

    def semantic_available(self, dataset_id: str, name: str, asof: date) -> bool:
        spec = self.get(dataset_id)
        for item in spec.semantic_fields:
            if item.name != name:
                continue
            start = _parse_date(item.available_from)
            end = _parse_date(item.available_to)
            return (start is None or asof >= start) and (end is None or asof <= end)
        return True

