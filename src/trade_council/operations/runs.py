"""Run inspection, cost summaries, retention, and report export."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import yaml

from ..errors import OperationError


@dataclass(frozen=True)
class RetentionPolicy:
    normal_run_days: int = 30
    failed_run_days: int = 14
    datapack_days: int = 7
    export_days: int = 90
    keep_latest: int = 10


def load_retention_policy(path: Path) -> RetentionPolicy:
    if not path.is_file():
        return RetentionPolicy()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return RetentionPolicy(
        normal_run_days=int(data.get("normal_run_days", 30)),
        failed_run_days=int(data.get("failed_run_days", 14)),
        datapack_days=int(data.get("datapack_days", 7)),
        export_days=int(data.get("export_days", 90)),
        keep_latest=int(data.get("keep_latest", 10)),
    )


def _read_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _timestamp(value: Any) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            pass
    return datetime.fromtimestamp(0)


def summarize_calls(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "model_calls.jsonl"
    if not path.is_file():
        return {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated_cost": 0.0,
            "known_cost": 0.0,
            "unknown_priced_calls": 0,
            "currencies": [],
            "latency_ms": 0,
            "by_phase": {},
            "by_model": {},
        }
    calls = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    usage = [call.get("usage", {}) for call in calls]
    currencies = sorted(
        {
            str(call["currency"])
            for call in calls
            if call.get("currency")
        }
    )
    uses_call_cost = any("call_cost" in call for call in calls)
    if uses_call_cost:
        known_cost = sum(
            float(call["call_cost"])
            for call in calls
            if call.get("call_cost") is not None
        )
        unknown = sum(1 for call in calls if call.get("call_cost") is None)
        estimated_cost: float | None = known_cost if unknown == 0 else None
    else:
        last_cost = calls[-1].get("estimated_cost") if calls else 0.0
        known_cost = float(last_cost) if last_cost is not None else 0.0
        unknown = sum(1 for call in calls if call.get("estimated_cost") is None)
        estimated_cost = float(last_cost) if last_cost is not None else None

    by_phase: dict[str, dict[str, int]] = {}
    by_model: dict[str, dict[str, int]] = {}
    for call in calls:
        call_usage = call.get("usage", {})
        for key, target in (
            (str(call.get("phase") or "unknown"), by_phase),
            (str(call.get("model") or "unknown"), by_model),
        ):
            bucket = target.setdefault(key, {"calls": 0, "tokens": 0})
            bucket["calls"] += 1
            bucket["tokens"] += int(call_usage.get("total_tokens", 0))
    return {
        "calls": len(calls),
        "prompt_tokens": sum(int(item.get("prompt_tokens", 0)) for item in usage),
        "completion_tokens": sum(int(item.get("completion_tokens", 0)) for item in usage),
        "total_tokens": sum(int(item.get("total_tokens", 0)) for item in usage),
        "estimated_cost": estimated_cost,
        "known_cost": known_cost,
        "unknown_priced_calls": unknown,
        "currencies": currencies,
        "latency_ms": sum(int(call.get("latency_ms", 0)) for call in calls),
        "by_phase": by_phase,
        "by_model": by_model,
    }


def run_summary(run_dir: Path) -> dict[str, Any]:
    state_path = run_dir / "run.json"
    if not state_path.is_file():
        raise OperationError(f"Run state not found: {state_path}")
    state = _read_json(state_path)
    audit_path = run_dir / "audit.json"
    audit = _read_json(audit_path) if audit_path.is_file() else {}
    cost = summarize_calls(run_dir)
    decision = state.get("final_decision") or {}
    return {
        "run_id": run_dir.name,
        "status": state.get("status"),
        "run_kind": state.get("run_kind", "normal"),
        "symbol": state.get("symbol"),
        "stock_name": state.get("stock_name"),
        "asof": state.get("asof"),
        "mode": state.get("mode"),
        "profile": state.get("profile"),
        "snapshot_id": state.get("snapshot_id"),
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "failed_phase": state.get("failed_phase"),
        "error": state.get("error"),
        "web_supplement_status": (state.get("web_supplement") or {}).get("status"),
        "data_need_count": len(state.get("data_needs", [])),
        "final_action": decision.get("action"),
        "degraded_from_action": decision.get("degraded_from_action"),
        "decision_data_gaps": decision.get("data_gaps", []),
        "audit_verdict": audit.get("verdict"),
        "cost": cost,
        "paths": {
            "run_dir": str(run_dir),
            "data_pack_dir": state.get("data_pack_dir"),
            "report": str(run_dir / "report.html") if (run_dir / "report.html").is_file() else None,
        },
    }


def list_runs(runs_root: Path) -> list[dict[str, Any]]:
    summaries = [
        run_summary(path)
        for path in runs_root.iterdir()
        if path.is_dir() and (path / "run.json").is_file()
    ] if runs_root.is_dir() else []
    return sorted(
        summaries,
        key=lambda item: str(item.get("updated_at") or item.get("started_at") or ""),
        reverse=True,
    )


def _older_than(path: Path, *, now: datetime, days: int) -> bool:
    modified = datetime.fromtimestamp(path.stat().st_mtime)
    return (now - modified).total_seconds() > days * 86400


def _delete_path(path: Path, *, apply: bool) -> None:
    if not apply:
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def cleanup_runs(
    project_root: Path,
    policy: RetentionPolicy,
    *,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    runs_root = project_root / "runs"
    summaries = list_runs(runs_root)
    keep_ids = {
        item["run_id"]
        for item in [
            summary for summary in summaries if summary["run_kind"] != "golden"
        ][: policy.keep_latest]
    }
    run_candidates: list[dict[str, Any]] = []
    kept_run_dirs: dict[str, Path] = {}
    for item in summaries:
        run_dir = Path(item["paths"]["run_dir"])
        if item["run_kind"] == "golden":
            kept_run_dirs[item["run_id"]] = run_dir
            continue
        age_days = policy.failed_run_days if item["status"] == "FAILED" else policy.normal_run_days
        if item["run_id"] in keep_ids or not _older_than(run_dir, now=now, days=age_days):
            kept_run_dirs[item["run_id"]] = run_dir
            continue
        size = sum(path.stat().st_size for path in run_dir.rglob("*") if path.is_file())
        run_candidates.append(
            {
                "kind": "run",
                "id": item["run_id"],
                "path": str(run_dir),
                "bytes": size,
            }
        )
        _delete_path(run_dir, apply=apply)

    referenced_datapacks: set[Path] = set()
    for run_dir in kept_run_dirs.values():
        state = _read_json(run_dir / "run.json")
        data_pack = state.get("data_pack_dir")
        if data_pack:
            referenced_datapacks.add(Path(data_pack).expanduser().resolve())

    datapack_candidates: list[dict[str, Any]] = []
    datapack_root = runs_root / "datapacks"
    if datapack_root.is_dir():
        for path in datapack_root.iterdir():
            if not path.is_dir() or path.resolve() in referenced_datapacks:
                continue
            if not _older_than(path, now=now, days=policy.datapack_days):
                continue
            size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
            datapack_candidates.append(
                {"kind": "datapack", "id": path.name, "path": str(path), "bytes": size}
            )
            _delete_path(path, apply=apply)

    export_candidates: list[dict[str, Any]] = []
    export_root = project_root / "exports"
    if export_root.is_dir():
        for path in export_root.iterdir():
            if not path.is_file() or not _older_than(path, now=now, days=policy.export_days):
                continue
            export_candidates.append(
                {
                    "kind": "export",
                    "id": path.name,
                    "path": str(path),
                    "bytes": path.stat().st_size,
                }
            )
            _delete_path(path, apply=apply)

    candidates = run_candidates + datapack_candidates + export_candidates
    return {
        "apply": apply,
        "policy": asdict(policy),
        "candidates": candidates,
        "deleted": candidates if apply else [],
        "bytes_reclaimable": sum(int(item["bytes"]) for item in candidates),
    }


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "-", value).strip(" .")
    return cleaned or "stock"


def export_report(
    project_root: Path,
    run_id: str,
    *,
    out_dir: Path | None = None,
) -> Path:
    run_dir = project_root / "runs" / run_id
    report = run_dir / "report.html"
    if not report.is_file():
        raise OperationError(f"Report not found: {report}")
    state = _read_json(run_dir / "run.json")
    stock_name = _safe_filename(str(state.get("stock_name") or state.get("symbol") or run_id))
    asof = str(state.get("asof") or "").replace("-", "")
    if len(asof) != 8:
        raise OperationError(f"Run has invalid asof: {state.get('asof')}")
    target_dir = (out_dir or project_root / "exports").expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{stock_name}-{asof}.html"
    shutil.copyfile(report, target)
    exports = state.setdefault("exports", [])
    exports.append({"path": str(target), "exported_at": datetime.now().isoformat(timespec="seconds")})
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    (run_dir / "run.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target
