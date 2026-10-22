"""Read-only local quant-data provider."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd
import pyarrow.dataset as pads

from .models import DatasetSpec

ScanStatus = Literal["OK", "NO_RECORD", "DATASET_ABSENT", "DATASET_EMPTY", "ERROR"]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanResult:
    dataset_id: str
    status: ScanStatus
    frame: pd.DataFrame
    files: list[Path]
    message: str = ""


class LocalQuantProvider:
    """Read-only access to parquet datasets rooted at QUANT_DATA."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def trade_dates(self) -> list[str]:
        daily = self.root / "market" / "daily"
        return sorted(path.stem for path in daily.glob("*.parquet"))

    def _dataset_files(self, spec: DatasetSpec) -> list[Path]:
        path = self.root / spec.path
        if path.is_file():
            return [path]
        if not path.is_dir():
            return []
        pattern = spec.file_glob or "*.parquet"
        return sorted(path.rglob(pattern))

    def dataset_files(self, spec: DatasetSpec) -> list[Path]:
        """Return the current files backing a dataset."""

        return self._dataset_files(spec)

    def _add_available_at(self, frame: pd.DataFrame, spec: DatasetSpec) -> pd.DataFrame:
        if frame.empty:
            return frame
        field = spec.available_at_field
        if field not in frame.columns and spec.alternate_available_at_field:
            field = spec.alternate_available_at_field
        if field not in frame.columns:
            return frame
        available = pd.to_datetime(frame[field], errors="coerce")
        if spec.publish_lag_days:
            available = available + pd.to_timedelta(spec.publish_lag_days, unit="D")
        frame = frame.copy()
        frame["available_at"] = available
        return frame

    def scan(
        self,
        spec: DatasetSpec,
        *,
        match: tuple[str, str] | None = None,
        columns: list[str] | None = None,
        since_year: int | None = None,
    ) -> ScanResult:
        files = self._dataset_files(spec)
        if not files:
            path = self.root / spec.path
            status: ScanStatus = "DATASET_ABSENT" if not path.exists() else "DATASET_EMPTY"
            return ScanResult(spec.id, status, pd.DataFrame(), [])

        if spec.id == "industry.members" and files:
            files = [path for path in files if path.stem[:4].isdigit() or path == self.root / spec.path]

        try:
            dataset = pads.dataset(str(self.root / spec.path), format="parquet")
            filter_expr = pads.field(match[0]) == match[1] if match else None
            available_columns = set(dataset.schema.names)
            selected = [column for column in (columns or []) if column in available_columns]
            table = dataset.to_table(filter=filter_expr, columns=selected or None)
            frame = table.to_pandas()
        except Exception as exc:
            logger.debug("Dataset scan failed, falling back to file mode", exc_info=exc)
            frames: list[pd.DataFrame] = []
            for path in files:
                if since_year is not None and path.stem[:4].isdigit() and int(path.stem[:4]) < since_year:
                    continue
                try:
                    part = pd.read_parquet(path)
                except Exception as exc:
                    logger.debug("Skipping unreadable parquet file %s", path, exc_info=exc)
                    continue
                if match is not None:
                    if match[0] not in part.columns:
                        continue
                    part = part[part[match[0]].astype(str) == match[1]]
                if columns:
                    keep = [column for column in columns if column in part.columns]
                    part = part[keep]
                if not part.empty:
                    frames.append(part)
            frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

        if frame.empty:
            return ScanResult(
                spec.id,
                "NO_RECORD",
                frame,
                files,
                "Dataset exists but no matching record; this is not DATASET_ABSENT.",
            )
        frame = self._add_available_at(frame, spec)
        return ScanResult(spec.id, "OK", frame, files)

    @staticmethod
    def lag_cutoff(asof: pd.Timestamp, lag_days: int) -> pd.Timestamp:
        return asof - pd.to_timedelta(lag_days, unit="D")
