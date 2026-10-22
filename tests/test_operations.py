from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from trade_council.operations.runs import (
    RetentionPolicy,
    cleanup_runs,
    export_report,
    list_runs,
    run_summary,
    summarize_calls,
)


def _write_run(
    runs_root: Path,
    run_id: str,
    *,
    updated_at: str,
    status: str = "AUDITED",
    run_kind: str = "normal",
    data_pack_dir: str | None = None,
) -> Path:
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": status,
                "run_kind": run_kind,
                "symbol": "600519.SH",
                "stock_name": "贵州茅台",
                "asof": "2026-10-21",
                "mode": "full",
                "profile": "deep",
                "snapshot_id": f"snap-{run_id}",
                "started_at": updated_at,
                "updated_at": updated_at,
                "data_pack_dir": data_pack_dir,
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def test_cost_summary_uses_per_call_costs(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    run_dir = _write_run(
        runs_root,
        "run-cost",
        updated_at="2026-10-21T15:00:00",
    )
    calls = [
        {
            "phase": "phase1",
            "model": "deepseek-flash",
            "latency_ms": 100,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "call_cost": 0.01,
            "currency": "USD",
            "estimated_cost": 0.01,
        },
        {
            "phase": "phase4",
            "model": "deepseek-v4-pro",
            "latency_ms": 200,
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            "call_cost": 0.02,
            "currency": "USD",
            "estimated_cost": 0.03,
        },
    ]
    (run_dir / "model_calls.jsonl").write_text(
        "\n".join(json.dumps(item) for item in calls) + "\n",
        encoding="utf-8",
    )
    summary = summarize_calls(run_dir)
    assert summary["calls"] == 2
    assert summary["total_tokens"] == 45
    assert summary["known_cost"] == 0.03
    assert summary["estimated_cost"] == 0.03
    assert summary["by_phase"]["phase1"]["calls"] == 1
    assert summary["by_model"]["deepseek-v4-pro"]["tokens"] == 30


def test_run_listing_and_export_filename(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    first = _write_run(runs_root, "older", updated_at="2026-10-20T15:00:00")
    second = _write_run(runs_root, "newer", updated_at="2026-10-21T15:00:00")
    (first / "report.html").write_text("<html>old</html>", encoding="utf-8")
    (second / "report.html").write_text("<html>new</html>", encoding="utf-8")
    (second / "audit.json").write_text('{"verdict": "PASS"}', encoding="utf-8")
    summaries = list_runs(runs_root)
    assert [item["run_id"] for item in summaries] == ["newer", "older"]
    assert run_summary(second)["audit_verdict"] == "PASS"

    target = export_report(tmp_path, "newer")
    assert target.name == "贵州茅台-20261021.html"
    assert target.read_text(encoding="utf-8") == "<html>new</html>"
    state = json.loads((second / "run.json").read_text(encoding="utf-8"))
    assert state["exports"][0]["path"] == str(target)


def test_cleanup_preserves_golden_and_referenced_datapacks(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    old_time = datetime(2026, 1, 1, 12, 0).timestamp()
    new_time = datetime(2026, 10, 21, 12, 0).timestamp()
    datapack_keep = runs_root / "datapacks" / "keep"
    datapack_drop = runs_root / "datapacks" / "drop"
    datapack_keep.mkdir(parents=True)
    datapack_drop.mkdir(parents=True)
    (datapack_keep / "manifest.json").write_text("{}", encoding="utf-8")
    (datapack_drop / "manifest.json").write_text("{}", encoding="utf-8")
    golden = _write_run(
        runs_root,
        "golden",
        updated_at="2026-01-01T12:00:00",
        run_kind="golden",
        data_pack_dir=str(datapack_keep),
    )
    old = _write_run(runs_root, "old-normal", updated_at="2026-01-01T12:00:00")
    recent = _write_run(runs_root, "recent", updated_at="2026-10-21T12:00:00")
    export = tmp_path / "exports" / "old-export.html"
    export.parent.mkdir()
    export.write_text("old", encoding="utf-8")
    for path in (golden, old, datapack_keep, datapack_drop, export):
        os.utime(path, (old_time, old_time))
    os.utime(recent, (new_time, new_time))

    result = cleanup_runs(
        tmp_path,
        RetentionPolicy(
            normal_run_days=30,
            failed_run_days=14,
            datapack_days=7,
            export_days=90,
            keep_latest=1,
        ),
        apply=True,
        now=datetime(2026, 10, 22, 12, 0),
    )
    assert not old.exists()
    assert golden.exists()
    assert recent.exists()
    assert datapack_keep.exists()
    assert not datapack_drop.exists()
    assert not export.exists()
    assert {item["id"] for item in result["deleted"]} == {
        "old-normal",
        "drop",
        "old-export.html",
    }

