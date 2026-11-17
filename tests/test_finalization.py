"""End-to-end checks for the single completion path (T02)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from trade_council import cli
from trade_council.config import load_project_config
from trade_council.data.snapshot import freeze
from trade_council.decision.risk_rules import TradeContext
from trade_council.orchestration.current_agent import prepare_current_agent_task
from trade_council.orchestration.finalization import finalize_run, validate_run

ROOT = Path(__file__).resolve().parents[1]
ASOF = "2026-11-13"


def _datapack(tmp_path: Path) -> Path:
    pack = tmp_path / "pack"
    pack.mkdir()
    dates = pd.bdate_range(end=ASOF, periods=10)
    pd.DataFrame(
        {
            "date": dates,
            "close": [100 + index for index in range(10)],
            "ma20": [99.0] * 10,
            "ma60": [98.0] * 10,
        }
    ).to_csv(pack / "price_daily.csv", index=False)
    # Minimal but internally consistent cumulative income statement: the
    # bridge checks (total profit / net income / parent net) must reconcile.
    pd.DataFrame(
        {
            "ts_code": ["600362.SH"] * 3,
            "ann_date": ["20260420", "20260820", "20261025"],
            "end_date": ["20260331", "20260630", "20260930"],
            "report_type": ["1", "1", "1"],
            "revenue": [1000.0, 2200.0, 3300.0],
            "oper_cost": [900.0, 1980.0, 3000.0],
            "biz_tax_surchg": [10.0, 22.0, 33.0],
            "sell_exp": [5.0, 11.0, 16.0],
            "admin_exp": [5.0, 11.0, 16.0],
            "rd_exp": [0.0, 0.0, 0.0],
            "fin_exp": [0.0, 0.0, 0.0],
            "fv_value_chg_gain": [0.0, 0.0, 0.0],
            "invest_income": [0.0, 0.0, 0.0],
            "assets_impair_loss": [-10.0, -25.0, -30.0],
            "operate_profit": [70.0, 151.0, 205.0],
            "non_oper_income": [0.0, 0.0, 0.0],
            "non_oper_exp": [0.0, 0.0, 0.0],
            "total_profit": [70.0, 151.0, 205.0],
            "income_tax": [17.5, 37.75, 51.25],
            "n_income": [52.5, 113.25, 153.75],
            "minority_gain": [2.5, 3.25, 3.75],
            "n_income_attr_p": [50.0, 110.0, 150.0],
        }
    ).to_csv(pack / "income.csv", index=False)
    pd.DataFrame(
        {
            "ts_code": ["600362.SH"] * 3,
            "end_date": ["20260331", "20260630", "20260930"],
            "total_assets": [2000.0, 2100.0, 2200.0],
            "total_liab": [800.0, 820.0, 840.0],
            "total_hldr_eqy_exc_min_int": [1200.0, 1280.0, 1360.0],
        }
    ).to_csv(pack / "balancesheet.csv", index=False)
    (pack / "evidence.json").write_text(
        json.dumps(
            {
                "snapshot_id": "snap-test",
                "items": [
                    {
                        "evidence_id": "F-market-daily-close",
                        "dataset_id": "market.daily",
                        "path": "market/daily",
                        "primary_key": "600362.SH",
                        "event_date": ASOF,
                        "available_at": f"{ASOF} 15:00:00",
                        "field": "close",
                        "value": 109,
                        "unit": "CNY",
                        "snapshot_id": "snap-test",
                        "value_type": "number",
                    },
                    {
                        "evidence_id": "F-event-announcement",
                        "dataset_id": "announcements.cninfo",
                        "path": "announcements/cninfo",
                        "primary_key": "600362.SH",
                        "event_date": "2026-11-10",
                        "available_at": "2026-11-10 20:00:00",
                        "field": "event",
                        "value": "三季度报告公告",
                        "unit": "",
                        "snapshot_id": "snap-test",
                        "value_type": "text",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (pack / "manifest.json").write_text(
        json.dumps(
            {
                "ts_code": "600362.SH",
                "code6": "600362",
                "stock_name": "江西铜业",
                "asof": ASOF,
                "snapshot_id": "snap-test",
                "calc_rules_version": "financials-1",
            }
        ),
        encoding="utf-8",
    )
    frozen = freeze(pack)
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    manifest["content_hash"] = frozen["content_hash"]
    manifest["hashes_file"] = "snapshot_files.json"
    (pack / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack


ROLE_PAYLOADS = {
    "research-manager": {
        "action": "HOLD",
        "confidence": "medium",
        "rationale": "研究判断为保留观察",
        "evidence_ids": ["F-market-daily-close"],
    },
    "trader": {"action": "HOLD", "confidence": "medium", "rationale": "不生成仓位"},
    "risk-manager": {
        "action": "HOLD",
        "confidence": "medium",
        "risk_level": "medium",
        "rationale": "账户未知，不生成仓位",
    },
}


def _make_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    context: TradeContext | None = None,
) -> tuple[object, Path, Path]:
    config = load_project_config(ROOT)
    config = replace(config, app=config.app.model_copy(update={"run_dir": str(tmp_path / "runs")}))
    monkeypatch.setattr(cli, "load_project_config", lambda _: config)
    pack = _datapack(tmp_path)
    run_dir = tmp_path / "runs" / "run-1"
    prepare_current_agent_task(
        config,
        pack,
        run_dir,
        context or TradeContext(),
        mode="full",
        include_news=False,
        allow_web=False,
        run_kind="normal",
    )
    request = json.loads((run_dir / "current_agent_request.json").read_text(encoding="utf-8"))
    for entry in request["roles"]:
        role_id = entry["role_id"]
        payload = ROLE_PAYLOADS.get(
            role_id,
            {
                "role_id": role_id,
                "stance": "neutral",
                "summary": f"{role_id} 的摘要",
                "key_findings": [
                    {
                        "claim_id": f"{role_id}-c1",
                        "claim": "收盘价 109 元",
                        "evidence_ids": ["F-market-daily-close"],
                        "kind": "fact",
                    }
                ],
            },
        )
        path = run_dir / entry["output"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (run_dir / "decision.json").write_text(
        json.dumps(ROLE_PAYLOADS["risk-manager"], ensure_ascii=False), encoding="utf-8"
    )
    # Every role finding must resolve to a claim in the central list: the two
    # texts are checked against each other, so the fixture files the claims the
    # roles actually cite.
    role_claims = [
        {
            "claim_id": f"{entry['role_id']}-c1",
            "role_id": entry["role_id"],
            "kind": "fact",
            "statement": "收盘价 109 元",
            "supporting_evidence_ids": ["F-market-daily-close"],
        }
        for entry in request["roles"]
        if entry["role_id"] not in ROLE_PAYLOADS
    ]
    (run_dir / "claims.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "claims": [
                    {
                        "claim_id": "c1",
                        "role_id": "fundamentals-analyst",
                        "kind": "inference",
                        "statement": "盈利下滑主要来自毛利",
                        "supporting_evidence_ids": ["F-market-daily-close"],
                    },
                    {
                        "claim_id": "c2",
                        "role_id": "bear-researcher",
                        "kind": "inference",
                        "statement": "旧目标价依赖已撤回的假设",
                        "status": "retracted",
                    },
                    *role_claims,
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "research_questions.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "questions": [
                    {
                        "question_id": "q1",
                        "question": "Q3 毛利下滑是否持续",
                        "why_it_matters": "决定盈利中枢",
                        "priority": "critical",
                        "status": "unresolved",
                        "attempted_paths": ["本地季报重算"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "research_decision.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "fundamental_assessment": "mixed",
                "valuation_assessment": "unresolved",
                "timing_assessment": "wait",
                "research_status": "watch",
                "key_claim_ids": ["c1"],
                "unresolved_question_ids": ["q1"],
                "next_checks": ["核对 Q4 毛利"],
                "invalidation_conditions": ["Q4 毛利继续下滑"],
                "execution": {
                    "status": "blocked",
                    "block_reasons": ["账户资金与持仓未提供"],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # The run keeps the state the preparation step wrote: no test may hand-edit
    # the completion flag, because that is exactly what a real user must not do.
    return config, run_dir, pack


def test_valid_run_passes_and_finalizes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert report.ok, report.to_dict()
    assert (run_dir / "validation.json").is_file()

    record = finalize_run(config, "run-1")  # type: ignore[arg-type]
    assert record["ok"] is True
    assert (run_dir / "report.html").is_file()
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "FINALIZED"

    # Idempotent: a second finalize keeps the same sealed report.
    first = (run_dir / "report.html").read_bytes()
    second = finalize_run(config, "run-1")  # type: ignore[arg-type]
    assert second["ok"] is True
    assert (run_dir / "report.html").read_bytes() == first
    assert second["report_html_sha256"] == record["report_html_sha256"]


def test_missing_role_fails_and_names_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    (run_dir / "agents" / "phase2" / "bear-researcher.json").unlink()
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    codes = {finding.code for finding in report.findings}
    assert "ROLE_MISSING" in codes
    assert any("bear-researcher" in finding.message for finding in report.findings)
    record = finalize_run(config, "run-1")  # type: ignore[arg-type]
    assert record["ok"] is False
    assert not (run_dir / "report.html").exists()


def test_unknown_role_field_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    path = run_dir / "agents" / "phase1" / "fundamentals-analyst.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["summarry"] = "拼错的字段"
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert any(finding.code == "ROLE_UNKNOWN_FIELD" for finding in report.findings)
    assert any("summarry" in finding.message for finding in report.findings)


def test_unknown_evidence_id_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    path = run_dir / "decision.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["evidence_ids"] = ["F-does-not-exist"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert any("F-does-not-exist" in finding.message for finding in report.findings)


def test_tampered_datapack_blocks_reuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, _run_dir, pack = _make_run(tmp_path, monkeypatch)
    price = pd.read_csv(pack / "price_daily.csv")
    price.loc[0, "close"] = 1.0
    price.to_csv(pack / "price_daily.csv", index=False)
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert any(finding.code == "INPUT_TAMPERED" for finding in report.findings)


def test_future_evidence_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, run_dir, pack = _make_run(tmp_path, monkeypatch)
    evidence = json.loads((pack / "evidence.json").read_text(encoding="utf-8"))
    evidence["items"][0]["available_at"] = "2026-12-31 15:00:00"
    (pack / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
    # Re-freeze so the tamper check does not mask the point-in-time check.
    from trade_council.data.snapshot import freeze

    frozen = freeze(pack)
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    manifest["content_hash"] = frozen["content_hash"]
    (pack / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    from trade_council.orchestration.current_agent import sha256_file

    state["input_binding"]["content_hash"] = frozen["content_hash"]
    state["input_binding"]["manifest_sha256"] = sha256_file(pack / "manifest.json")
    (run_dir / "run.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert any(finding.code == "EVIDENCE_FUTURE" for finding in report.findings)


def test_retracted_claim_used_by_decision_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    path = run_dir / "research_decision.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["key_claim_ids"] = ["c2"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert any(finding.code == "RETRACTED_CLAIM_IN_USE" for finding in report.findings)


def test_actionable_execution_without_size_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    path = run_dir / "research_decision.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["execution"] = {"status": "actionable"}
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert any(finding.code == "EXECUTION_WITHOUT_SIZE" for finding in report.findings)


def test_export_requires_finalized_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, _run_dir, _ = _make_run(tmp_path, monkeypatch)
    out = tmp_path / "exports"
    assert (
        cli.main(
            ["export", "--run-id", "run-1", "--out-dir", str(out), "--root", str(config.root)]
        )
        == 2
    )
    assert finalize_run(config, "run-1")["ok"] is True  # type: ignore[arg-type]
    assert (
        cli.main(
            ["export", "--run-id", "run-1", "--out-dir", str(out), "--root", str(config.root)]
        )
        == 0
    )
    exported = list(out.glob("*.html"))
    assert exported and exported[0].name.startswith("江西铜业-20261113")


def test_audit_failure_blocks_finalize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    path = run_dir / "decision.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(
        {
            "action": "BUY",
            "entry_zone": "100",
            "stop_loss": "90",
            "target_price": "120",
            "position_shares": 100,
            "position_amount": 10_000,
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    record = finalize_run(config, "run-1")  # type: ignore[arg-type]
    assert record["ok"] is False
    assert record["stage"] == "audit"
    assert not (run_dir / "report.html").exists()
    assert (run_dir / "report.draft.html").is_file()
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "AUDIT_FAILED"
