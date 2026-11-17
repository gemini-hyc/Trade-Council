"""Rework acceptance (R1-R6): the completion path must judge real products.

Each test here reproduces a finding from the independent review: the old
behaviour passed, and the new behaviour must fail loudly instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from test_finalization import ASOF, _make_run

from trade_council import cli
from trade_council.decision.risk_rules import TradeContext
from trade_council.orchestration.finalization import (
    finalize_run,
    submit_analysis,
    validate_run,
    verify_seal,
)
from trade_council.research import record_revision, review_output


def _codes(report) -> set[str]:  # type: ignore[no-untyped-def]
    return {finding.code for finding in report.findings}


def _research_path(run_dir: Path) -> Path:
    return run_dir / "research_decision.json"


def _edit_json(path: Path, **changes: object) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- R1


def test_prepared_run_completes_without_hand_editing_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "AWAITING_CURRENT_AGENT"

    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert report.ok, report.to_dict()
    assert report.input_ok
    assert "ANALYSIS_NOT_READY" not in _codes(report)

    submitted = submit_analysis(config, "run-1")  # type: ignore[arg-type]
    assert submitted["ok"] is True
    assert submitted["status"] == "DECIDED"

    record = finalize_run(config, "run-1")  # type: ignore[arg-type]
    assert record["ok"] is True
    assert record["seal"]["digest"]


def test_prepared_run_without_products_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    for entry in json.loads((run_dir / "current_agent_request.json").read_text(encoding="utf-8"))[
        "roles"
    ]:
        (run_dir / entry["output"]).unlink()
    (run_dir / "decision.json").unlink()
    for name in ("research_questions.json", "claims.json", "research_decision.json"):
        (run_dir / name).unlink()

    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    codes = _codes(report)
    assert "ANALYSIS_NOT_READY" in codes
    assert "ROLE_MISSING" in codes
    assert "DECISION_MISSING" in codes
    assert submit_analysis(config, "run-1")["ok"] is False  # type: ignore[arg-type]


def test_submit_names_every_missing_piece(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    (run_dir / "agents" / "phase2" / "bear-researcher.json").unlink()
    record = submit_analysis(config, "run-1")  # type: ignore[arg-type]
    assert record["ok"] is False
    assert any("ROLE_MISSING" == item["code"] for item in record["failures"])
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "AWAITING_CURRENT_AGENT"


# --------------------------------------------------------------------------- R2


def test_export_refuses_when_frozen_input_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _run_dir, pack = _make_run(tmp_path, monkeypatch)
    assert finalize_run(config, "run-1")["ok"] is True  # type: ignore[arg-type]
    out = tmp_path / "exports"
    assert (
        cli.main(
            ["export", "--run-id", "run-1", "--out-dir", str(out), "--root", str(config.root)]
        )
        == 0
    )

    price = pd.read_csv(pack / "price_daily.csv")
    price.loc[0, "close"] = 1.0
    price.to_csv(pack / "price_daily.csv", index=False)
    assert verify_seal(config, "run-1")  # type: ignore[arg-type]
    assert (
        cli.main(
            ["export", "--run-id", "run-1", "--out-dir", str(out), "--root", str(config.root)]
        )
        == 2
    )


@pytest.mark.parametrize(
    "relative, mutate",
    [
        ("decision.json", lambda payload: payload.update({"confidence": "low"})),
        (
            "claims.json",
            lambda payload: payload["claims"][0].update({"statement": "改写后的陈述"}),
        ),
        (
            "research_decision.json",
            lambda payload: payload.update({"next_checks": ["新增检查项"]}),
        ),
    ],
)
def test_export_refuses_when_a_conclusion_or_claim_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
    mutate,
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    assert finalize_run(config, "run-1")["ok"] is True  # type: ignore[arg-type]
    payload = json.loads((run_dir / relative).read_text(encoding="utf-8"))
    mutate(payload)
    (run_dir / relative).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    problems = verify_seal(config, "run-1")  # type: ignore[arg-type]
    assert any(relative in problem for problem in problems)
    out = tmp_path / "exports"
    assert (
        cli.main(
            ["export", "--run-id", "run-1", "--out-dir", str(out), "--root", str(config.root)]
        )
        == 2
    )
    # The old bytes stay available, but only as an explicitly labelled history.
    assert (
        cli.main(
            [
                "export",
                "--run-id",
                "run-1",
                "--out-dir",
                str(out),
                "--root",
                str(config.root),
                "--historical",
            ]
        )
        == 0
    )
    assert any("已封存历史版本" in path.name for path in out.glob("*.html"))


def test_export_refuses_when_account_context_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    assert finalize_run(config, "run-1")["ok"] is True  # type: ignore[arg-type]
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state["trade_context"] = {**state.get("trade_context", {}), "capital": 500000}
    (run_dir / "run.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    assert any("trade_context" in item for item in verify_seal(config, "run-1"))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- R3


def test_missing_research_file_blocks_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    (run_dir / "claims.json").unlink()
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert "RESEARCH_FILE_MISSING" in _codes(report)
    assert any("claims.json" in finding.message for finding in report.findings)
    assert finalize_run(config, "run-1")["ok"] is False  # type: ignore[arg-type]
    assert not (run_dir / "report.html").exists()


def test_typo_in_question_and_claim_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    questions = json.loads((run_dir / "research_questions.json").read_text(encoding="utf-8"))
    questions["questions"][0]["statuz"] = questions["questions"][0].pop("status")
    questions["questions"][0].pop("why_it_matters")
    (run_dir / "research_questions.json").write_text(
        json.dumps(questions, ensure_ascii=False), encoding="utf-8"
    )
    claims = json.loads((run_dir / "claims.json").read_text(encoding="utf-8"))
    claims["claims"][0]["statuus"] = "intact"
    (run_dir / "claims.json").write_text(json.dumps(claims, ensure_ascii=False), encoding="utf-8")

    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert "RESEARCH_SCHEMA_INVALID" in _codes(report)
    messages = " ".join(finding.message for finding in report.findings)
    assert "statuz" in messages or "why_it_matters" in messages
    assert "statuus" in messages


def test_wrong_run_id_and_duplicate_ids_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    _edit_json(run_dir / "claims.json", run_id="another-run")
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert "RESEARCH_RUN_MISMATCH" in _codes(report)

    claims = json.loads((run_dir / "claims.json").read_text(encoding="utf-8"))
    claims["run_id"] = "run-1"
    claims["claims"].append(dict(claims["claims"][0]))
    (run_dir / "claims.json").write_text(json.dumps(claims, ensure_ascii=False), encoding="utf-8")
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert "RESEARCH_DUPLICATE_ID" in _codes(report)


def test_legacy_run_reads_with_explicit_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state.pop("analysis_schema", None)
    (run_dir / "run.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    (run_dir / "claims.json").unlink()
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    codes = _codes(report)
    # Legacy absence is a warning, but it is named as legacy rather than hidden.
    assert "RESEARCH_FILE_MISSING_LEGACY" in codes
    assert "RESEARCH_FILE_MISSING" not in codes


# --------------------------------------------------------------------------- R4


def _buy_decision(run_dir: Path, **overrides: object) -> None:
    payload = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
    payload.update(
        {
            "action": "BUY",
            "entry_zone": "100",
            "stop_loss": "90",
            "target_price": "120",
            "position_opened_date": None,
        }
    )
    payload.update(overrides)
    (run_dir / "decision.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _execution(run_dir: Path, execution: dict[str, object], **decision_overrides: object) -> None:
    research = json.loads(_research_path(run_dir).read_text(encoding="utf-8"))
    research["execution"] = execution
    _research_path(run_dir).write_text(
        json.dumps(research, ensure_ascii=False), encoding="utf-8"
    )
    _buy_decision(run_dir, **decision_overrides)


def test_unknown_account_cannot_be_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    _execution(
        run_dir,
        {"status": "actionable", "target_account_weight": 0.1},
        position_pct=10,
    )
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert "EXECUTION_CONTEXT_MISSING" in _codes(report)
    assert finalize_run(config, "run-1")["ok"] is False  # type: ignore[arg-type]


def test_actionable_with_full_context_and_consistent_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(
        tmp_path, monkeypatch, TradeContext(capital=1_000_000, position_state="flat")
    )
    monkeypatch.setattr(
        "trade_council.orchestration.finalization.tradability_status", lambda _: "tradable"
    )
    _execution(
        run_dir,
        {
            "status": "actionable",
            "target_account_weight": 0.1,
            "requires": ["capital", "position_state"],
        },
        position_pct=10,
    )
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert report.ok, report.to_dict()
    assert report.capabilities["capital"] is True


def test_cash_amount_only_plan_uses_one_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(
        tmp_path, monkeypatch, TradeContext(capital=1_000_000, position_state="flat")
    )
    monkeypatch.setattr(
        "trade_council.orchestration.finalization.tradability_status", lambda _: "tradable"
    )
    _execution(
        run_dir,
        {"status": "actionable", "cash_amount": 100_000, "requires": ["capital"]},
        position_pct=10,
        position_amount=100_000,
        position_shares=1000,
    )
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert report.ok, report.to_dict()


def test_sell_must_not_reuse_account_weight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(
        tmp_path,
        monkeypatch,
        TradeContext(capital=1_000_000, position_state="holding", position_shares=1000),
    )
    _execution(
        run_dir,
        {"status": "actionable", "target_account_weight": 0.1, "requires": ["capital"]},
        position_pct=10,
    )
    decision = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
    decision["action"] = "SELL"
    (run_dir / "decision.json").write_text(
        json.dumps(decision, ensure_ascii=False), encoding="utf-8"
    )
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert "EXECUTION_DIRECTION_MISMATCH" in _codes(report)


def test_new_and_legacy_position_numbers_must_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(
        tmp_path, monkeypatch, TradeContext(capital=1_000_000, position_state="flat")
    )
    monkeypatch.setattr(
        "trade_council.orchestration.finalization.tradability_status", lambda _: "tradable"
    )
    _execution(
        run_dir,
        {"status": "actionable", "target_account_weight": 0.1, "requires": ["capital"]},
        position_pct=25,
    )
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert "POSITION_SOURCE_CONFLICT" in _codes(report)


def test_unknown_requirement_key_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(
        tmp_path, monkeypatch, TradeContext(capital=1_000_000, position_state="flat")
    )
    monkeypatch.setattr(
        "trade_council.orchestration.finalization.tradability_status", lambda _: "tradable"
    )
    _execution(
        run_dir,
        {
            "status": "actionable",
            "target_account_weight": 0.1,
            "requires": ["capital", "margin_account"],
        },
        position_pct=10,
    )
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert "EXECUTION_REQUIREMENT_UNKNOWN" in _codes(report)


def test_execution_requires_is_a_hard_constraint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(
        tmp_path, monkeypatch, TradeContext(capital=1_000_000, position_state="flat")
    )
    _execution(
        run_dir,
        {
            "status": "blocked",
            "target_account_weight": 0.1,
            "requires": ["capital", "position_shares"],
        },
        position_pct=10,
    )
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    # blocked may keep the scenario, but it must be reported as unsatisfied.
    assert any(
        finding.code == "EXECUTION_CONTEXT_MISSING" for finding in report.findings
    )


# --------------------------------------------------------------------------- R5


def _pending_revision(run_dir: Path) -> None:
    record_revision(
        run_dir,
        {
            "revision_id": "r2",
            "before": "估值可用",
            "after": "估值依赖的论点被撤回",
            "changed_claims": [{"claim_id": "c1", "status": "retracted"}],
            "affected_outputs": ["valuation", "execution"],
        },
    )


def test_unreviewed_valuation_cannot_be_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    _edit_json(
        _research_path(run_dir),
        valuation_assessment="attractive",
        valuation_reference={
            "method": "historical_median_multiple",
            "reference_price": 123.0,
            "multiple": 9.0,
            "window_start": "2020-01-02",
            "window_end": ASOF,
            "sample_size": 1600,
            "percentile_definition": "严格小于当前倍数的交易日占比",
        },
    )
    # Refresh the key claim so it is not itself a retraction problem.
    claims = json.loads((run_dir / "claims.json").read_text(encoding="utf-8"))
    claims["claims"].append(
        {
            "claim_id": "c-dependent",
            "role_id": "bull-researcher",
            "kind": "inference",
            "statement": "旧估值 123 元",
            "depends_on_claim_ids": ["c1"],
        }
    )
    (run_dir / "claims.json").write_text(json.dumps(claims, ensure_ascii=False), encoding="utf-8")
    _edit_json(_research_path(run_dir), key_claim_ids=["c-dependent"])

    _pending_revision(run_dir)
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert "UNREVIEWED_OUTPUT" in _codes(report)

    # Downgrading the dimension makes the report honest, but the role that
    # asserted the retracted claim is still carrying its old text, so the run
    # stays blocked until that role has been re-read.
    _edit_json(
        _research_path(run_dir),
        valuation_assessment="unresolved",
        valuation_reference=None,
    )
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert "ROLE_REVIEW_PENDING" in _codes(report)
    # c1's own role and the role that filed the dependent claim both have text
    # that no longer matches the claim list; each is closed one at a time.
    pending_roles = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))[
        "revision_state"
    ]["pending_role_reviews"]
    assert {item["role"] for item in pending_roles} == {
        "fundamentals-analyst",
        "bull-researcher",
    }
    for item in pending_roles:
        review_output(
            run_dir,
            {
                "role": item["role"],
                "revision_id": item["revision_id"],
                "note": "重读该角色摘要：相关论点已撤回，摘要里不再保留那些结论",
            },
        )
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert report.ok, report.to_dict()


def test_one_review_cannot_clear_another_pending_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    _pending_revision(run_dir)
    outcome = review_output(
        run_dir,
        {"output": "valuation", "revision_id": "r2", "note": "按新证据重算分位"},
    )
    assert [item["output"] for item in outcome["pending_reviews"]] == ["execution"]

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["revision_state"]["pending_review_outputs"] == ["execution"]

    research = json.loads(_research_path(run_dir).read_text(encoding="utf-8"))
    research["execution"] = {"status": "actionable", "target_account_weight": 0.05}
    _research_path(run_dir).write_text(
        json.dumps(research, ensure_ascii=False), encoding="utf-8"
    )
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    codes = _codes(report)
    assert "EXECUTION_BEFORE_REVIEW" in codes
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["revision_state"]["pending_review_outputs"] == ["execution"]
    assert [item["output"] for item in state["revision_state"]["reviews"]] == ["valuation"]


def test_review_must_name_an_existing_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    _pending_revision(run_dir)
    with pytest.raises(ValueError, match="没有待复核"):
        review_output(run_dir, {"output": "valuation", "revision_id": "r9", "note": "随手写"})
    with pytest.raises(ValueError, match="复核内容"):
        review_output(run_dir, {"output": "valuation", "revision_id": "r2", "note": ""})


def test_revision_requires_affected_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="affected_outputs"):
        record_revision(
            run_dir,
            {
                "before": "旧判断",
                "after": "新判断",
                "changed_claims": [{"claim_id": "c1", "status": "weakened"}],
            },
        )


# --------------------------------------------------------------------------- R8


def _recompute(config, run_id: str, payload: dict[str, object], tmp_path: Path) -> int:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return cli.main(
        [
            "research",
            "recompute",
            "--run-id",
            run_id,
            "--file",
            str(path),
            "--root",
            str(config.root),
        ]
    )


def test_recompute_changes_adjusted_numbers_but_not_reported_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    assert _recompute(config, "run-1", {"adjustment_tax_rate": 0.25}, tmp_path) == 0

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    override = state["financial_override"]
    assert override["adjustment_tax_rate"] == 0.25
    out_dir = Path(override["dir"])
    adjustments = json.loads((out_dir / "financial_adjustments.json").read_text(encoding="utf-8"))
    assert adjustments["scenario"]["tax_rate"] == 0.25
    assert adjustments["scenario"]["tax_rate_source"] == "scenario"
    q3 = next(row for row in adjustments["periods"] if row["end_date"] == "20260930")
    # Reported values are the source numbers: 150 - 110 = 40, untouched.
    assert q3["reported_parent_net"] == pytest.approx(40.0)
    # (54 - (-5)) * (1 - 0.25) - 0.5 = 43.75
    assert q3["adjusted_parent_net_strip_asset"] == pytest.approx(43.75)

    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert report.ok, report.to_dict()
    assert any(
        finding.code == "FINANCIAL_RECOMPUTED_AT_RUN" for finding in report.findings
    )
    assert finalize_run(config, "run-1")["ok"] is True  # type: ignore[arg-type]
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "25%" in html
    assert "23%" not in html
    assert "本次运行输入的税率" in html


def test_default_scenario_stays_23_percent_and_is_labelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    assert _recompute(config, "run-1", {}, tmp_path) == 0
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    adjustments = json.loads(
        (Path(state["financial_override"]["dir"]) / "financial_adjustments.json").read_text(
            encoding="utf-8"
        )
    )
    assert adjustments["scenario"]["tax_rate"] == pytest.approx(0.23)
    assert adjustments["scenario"]["tax_rate_source"] == "default"
    assert finalize_run(config, "run-1")["ok"] is True  # type: ignore[arg-type]
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "23%" in html
    assert "模块默认税率" in html


def test_recompute_after_finalize_invalidates_the_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _run_dir, _ = _make_run(tmp_path, monkeypatch)
    assert finalize_run(config, "run-1")["ok"] is True  # type: ignore[arg-type]
    assert _recompute(config, "run-1", {"adjustment_tax_rate": 0.25}, tmp_path) == 0
    problems = verify_seal(config, "run-1")  # type: ignore[arg-type]
    assert any("financial_override" in item or "recompute" in item for item in problems)
    out = tmp_path / "exports"
    assert (
        cli.main(
            ["export", "--run-id", "run-1", "--out-dir", str(out), "--root", str(config.root)]
        )
        == 2
    )


def test_recompute_rejects_a_ratio_outside_zero_to_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _run_dir, _ = _make_run(tmp_path, monkeypatch)
    assert _recompute(config, "run-1", {"adjustment_tax_rate": 23}, tmp_path) == 2


# ------------------------------------------- 角色摘要与中央论点双源一致性


def test_role_summary_cannot_cite_a_claim_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A role finding must point at a central claim, or the two texts diverge."""

    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    payload = json.loads((run_dir / "agents/phase1/market-analyst.json").read_text(encoding="utf-8"))
    payload["key_findings"] = [
        {
            "claim_id": "c-does-not-exist",
            "claim": "收盘价 109 元",
            "evidence_ids": ["F-market-daily-close"],
            "kind": "fact",
        }
    ]
    (run_dir / "agents/phase1/market-analyst.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert not report.ok
    assert "ROLE_CLAIM_UNKNOWN" in _codes(report)


def test_role_review_must_name_a_pending_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="没有待重审的角色"):
        review_output(
            run_dir,
            {"role": "market-analyst", "revision_id": "r2", "note": "随便写一句"},
        )


def test_revision_puts_the_claim_roles_into_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    _pending_revision(run_dir)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    pending = state["revision_state"]["pending_role_reviews"]
    assert {item["role"] for item in pending} == {"fundamentals-analyst"}
    assert pending[0]["claim_ids"] == ["c1"]


def test_revision_counts_as_reviewed_only_after_every_queue_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report prints the revision log, so its flags must match reality."""

    _config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    _pending_revision(run_dir)

    def flag() -> bool:
        lines = (run_dir / "revisions.jsonl").read_text(encoding="utf-8").splitlines()
        return json.loads(lines[-1])["reviewed"]

    assert flag() is False
    review_output(run_dir, {"output": "valuation", "revision_id": "r2", "note": "估值已重算"})
    assert flag() is False
    review_output(run_dir, {"output": "execution", "revision_id": "r2", "note": "执行已重算"})
    # The role that asserted the changed claim is still open.
    assert flag() is False
    review_output(
        run_dir,
        {"role": "fundamentals-analyst", "revision_id": "r2", "note": "角色摘要已重写"},
    )
    assert flag() is True
    record = json.loads((run_dir / "revisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert "角色摘要已重写" in record["review_note"]


def test_blocked_plan_may_declare_its_missing_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Declaring ``requires`` on a blocked plan is honest, not a failure.

    Only claiming a size, or claiming to be actionable, while the inputs behind
    that size are missing is a failure.
    """

    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    _execution(run_dir, {"status": "blocked", "requires": ["capital", "position_shares"]})
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert report.ok, report.to_dict()
    levels = {
        finding.level
        for finding in report.findings
        if finding.code == "EXECUTION_CONTEXT_MISSING"
    }
    assert levels == {"WARN"}
