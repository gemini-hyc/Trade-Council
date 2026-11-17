"""Recompute the financial artifacts at run level, without touching the frozen pack.

A research loop often learns something after the data pack was frozen: an
official deducted-profit figure, a credit-impairment number, a different tax
assumption. Editing the pack would break the input binding, so the corrected
calculation is written under ``recompute/<scenario_id>/`` and the run records
which scenario the report must read.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from ..data.snapshot import sha256_file
from ..engineering.financials import build_financial_artifacts

RECOMPUTE_DIR = "recompute"
STATE_FILE = "run.json"
MANIFEST_FILES = (
    "financial_quarters.csv",
    "earnings_bridge.json",
    "financial_adjustments.json",
)


class RecomputeError(ValueError):
    """Raised when a run-level recomputation cannot be performed."""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load_frame(datapack: Path, name: str) -> pd.DataFrame | None:
    path = datapack / name
    if not path.is_file():
        return None
    frame = pd.read_csv(path)
    if "available_at" not in frame.columns and "ann_date" in frame.columns:
        frame["available_at"] = pd.to_datetime(frame["ann_date"], errors="coerce")
    return frame


def recompute_financials(
    run_dir: Path,
    payload: dict[str, Any],
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Write a new financial scenario derived from the frozen pack.

    ``payload`` keys: ``revision_id``/``scenario_label``, ``adjustment_tax_rate``,
    ``official_metrics_file`` (path relative to the run dir or absolute) or
    ``official_metrics`` (inline object), ``note``.
    """

    state_path = run_dir / STATE_FILE
    if not state_path.is_file():
        raise RecomputeError("缺少 run.json，无法确定数据包")
    run = _read_json(state_path)
    datapack = Path(str(run.get("data_pack_dir") or ""))
    if not datapack.is_dir():
        raise RecomputeError(f"数据包目录不存在：{datapack}")

    tax_rate = payload.get("adjustment_tax_rate")
    if tax_rate is not None:
        if not isinstance(tax_rate, (int, float)) or isinstance(tax_rate, bool):
            raise RecomputeError(f"adjustment_tax_rate 必须是数字：{tax_rate!r}")
        if not 0 < float(tax_rate) < 1:
            raise RecomputeError("adjustment_tax_rate 必须在 0~1 之间（小数，不是百分数）")

    official_metrics: dict[str, Any] | None = None
    official_source = ""
    if payload.get("official_metrics"):
        official_metrics = dict(payload["official_metrics"])
        official_source = "inline"
    elif payload.get("official_metrics_file"):
        raw = str(payload["official_metrics_file"])
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = run_dir / path
        if not path.is_file():
            raise RecomputeError(f"official_metrics_file 不存在：{path}")
        official_metrics = _read_json(path)
        official_source = str(path)

    income = _load_frame(datapack, "income.csv")
    cashflow = _load_frame(datapack, "cashflow.csv")
    balance = _load_frame(datapack, "balancesheet.csv")
    if income is None and cashflow is None:
        raise RecomputeError("数据包缺少 income.csv / cashflow.csv，无法重算")

    label = str(payload.get("scenario_label") or payload.get("revision_id") or "").strip()
    scenario_seed = {
        "datapack_content_hash": (run.get("input_binding") or {}).get("content_hash"),
        "adjustment_tax_rate": tax_rate,
        "official_metrics_sha256": _sha256(official_metrics) if official_metrics else None,
        "label": label,
    }
    scenario_id = "rc-" + _sha256(scenario_seed)[:12]
    out_dir = run_dir / RECOMPUTE_DIR / scenario_id
    out_dir.mkdir(parents=True, exist_ok=True)

    result = build_financial_artifacts(
        out_dir=out_dir,
        symbol=str(run.get("symbol") or ""),
        asof=str(run.get("asof") or ""),
        income=income,
        cashflow=cashflow,
        balance=balance,
        official_metrics=official_metrics,
        adjustment_tax_rate=tax_rate,
    )
    if result.get("status") != "ok":
        raise RecomputeError(f"重算失败：{result.get('reason')}")

    record = {
        "schema_version": "1.0",
        "scenario_id": scenario_id,
        "label": label,
        "note": str(payload.get("note") or ""),
        "created_at": created_at or date.today().isoformat(),
        "datapack_dir": str(datapack),
        "datapack_content_hash": (run.get("input_binding") or {}).get("content_hash"),
        "inputs": {
            name: sha256_file(datapack / name)
            for name in ("income.csv", "cashflow.csv", "balancesheet.csv")
            if (datapack / name).is_file()
        },
        "official_metrics_source": official_source,
        "adjustment_tax_rate": result["adjustments"]["scenario"].get("tax_rate"),
        "tax_rate_source": result["adjustments"]["scenario"].get("tax_rate_source"),
        "outputs": {
            name: sha256_file(out_dir / name)
            for name in MANIFEST_FILES
            if (out_dir / name).is_file()
        },
        "checks_summary": result["checks_summary"],
    }
    (out_dir / "recompute.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    run["financial_override"] = {
        "scenario_id": scenario_id,
        "dir": str(out_dir),
        "created_at": record["created_at"],
        "label": label,
        "adjustment_tax_rate": record["adjustment_tax_rate"],
    }
    state_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def finance_dir(run: dict[str, Any], datapack: Path) -> Path:
    """Which directory the report and the checker must read financials from."""

    override = run.get("financial_override") or {}
    path = Path(str(override.get("dir") or ""))
    if str(override.get("dir") or "") and path.is_dir():
        return path
    return datapack
