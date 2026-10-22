"""Models for dataset capability metadata."""

from __future__ import annotations

from pydantic import Field

from ..schemas import StrictModel


class FieldAvailability(StrictModel):
    field: str
    available_from: str | None = None
    available_to: str | None = None
    quality: str = "usable"
    note: str = ""


class SemanticFieldAvailability(StrictModel):
    name: str
    available_from: str | None = None
    available_to: str | None = None
    quality: str = "usable"
    note: str = ""


class CaliberBreakpoint(StrictModel):
    date: str
    description: str


class DatasetSpec(StrictModel):
    id: str
    label: str
    path: str
    file_glob: str | None = None
    time_field: str
    available_at_field: str
    alternate_available_at_field: str | None = None
    publish_lag_days: int = Field(default=0, ge=0)
    publish_time_local: str = "23:59:59"
    timezone: str = "Asia/Shanghai"
    asof_cutoff: str = "23:59:59"
    required: bool = False
    enabled: bool = True
    fields: list[str] = Field(default_factory=list)
    known_traps: list[str] = Field(default_factory=list)
    rebuild_rules: list[str] = Field(default_factory=list)
    caliber_breakpoints: list[CaliberBreakpoint] = Field(default_factory=list)
    field_availability: list[FieldAvailability] = Field(default_factory=list)
    semantic_fields: list[SemanticFieldAvailability] = Field(default_factory=list)
    verification_status: str = "verified"


class DatasetRegistryConfig(StrictModel):
    version: str
    datasets: list[DatasetSpec]

