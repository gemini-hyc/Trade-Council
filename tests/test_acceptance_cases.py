"""A01–A18: every injected-error case in the implementation plan.

Each test pins the specific value, error code, or forbidden side effect — not
just "a file exists". Cases already covered elsewhere keep a pointer in
``docs/implementation/acceptance.md``; the ones below are the direct checks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_finalization import _make_run
from test_financials import _frame

from trade_council.decision.rule_tree import (
    RuleTree,
    SellTranche,
    TradingRule,
    validate_rule_tree,
)
from trade_council.engineering.financials import build_financial_artifacts
from trade_council.orchestration.finalization import validate_run
from trade_council.research_schemas import ResearchDecision

ASOF = "2026-11-13"


def _decision_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": "run-1",
        "fundamental_assessment": "mixed",
        "valuation_assessment": "unresolved",
        "timing_assessment": "wait",
        "research_status": "watch",
    }
    payload.update(overrides)
    return payload


def _rule(**overrides: object) -> TradingRule:
    payload: dict[str, object] = {
        "rule_id": "r-breakout",
        "indicator": "close",
        "metric": "close > ma20",
        "threshold": "close > ma20 * 1.02",
        "price_basis": "unadjusted",
        "action": "buy",
    }
    payload.update(overrides)
    return TradingRule.model_validate(payload)


# A03 / A04 / A06 are pinned by tests/test_financials.py.
def test_a07_impairment_is_stripped_once_not_twice(tmp_path: Path) -> None:
    artifacts = build_financial_artifacts(
        out_dir=tmp_path,
        symbol="600362.SH",
        asof=ASOF,
        income=_frame(),
        cashflow=None,
        balance=None,
        official_metrics=None,
    )
    q3 = next(
        row for row in artifacts["adjustments"]["periods"] if row["end_date"] == "20260930"
    )
    # Without an official credit-impairment note the second scenario is
    # unavailable instead of silently reusing the asset impairment.
    assert q3["adjusted_parent_net_strip_asset"] is not None
    assert q3["adjusted_parent_net_strip_asset_and_credit"] is None


def test_a07_missing_minority_leaves_adjusted_profit_unavailable(tmp_path: Path) -> None:
    frame = _frame()
    frame.loc[frame["end_date"] == "20260930", "minority_gain"] = None
    artifacts = build_financial_artifacts(
        out_dir=tmp_path,
        symbol="600362.SH",
        asof=ASOF,
        income=frame,
        cashflow=None,
        balance=None,
        official_metrics=None,
    )
    q3 = next(
        row for row in artifacts["adjustments"]["periods"] if row["end_date"] == "20260930"
    )
    assert q3["adjusted_parent_net_strip_asset"] is None
    assert q3["adjusted_unavailable_reason"] == "missing_minority_gain"


# A08 is pinned by tests/test_research.py::test_revision_propagates_and_blocks_actionable
# and tests/test_finalization.py::test_retracted_claim_used_by_decision_fails.
def test_a08_retraction_keeps_old_target_price_out_of_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    path = run_dir / "research_decision.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["key_claim_ids"] = ["c2"]
    payload["valuation_reference"] = {"method": "scenario_earnings", "reference_price": 42.0}
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    codes = {finding.code for finding in report.findings}
    assert "RETRACTED_CLAIM_IN_USE" in codes


# A09 is pinned by tests/test_deterministic_fixes.py (text evidence rendering).
def test_a10_expected_return_without_probabilities_is_rejected() -> None:
    with pytest.raises(ValidationError) as error:
        ResearchDecision.model_validate(_decision_payload(expected_return=0.18))
    assert "expected_return" in str(error.value)


def test_a15_dividend_assumption_cannot_enter_the_main_return() -> None:
    with pytest.raises(ValidationError):
        ResearchDecision.model_validate(_decision_payload(dividend_cash_flow=1234.0))
    decision = ResearchDecision.model_validate(_decision_payload(dividend_treatment="assumption"))
    assert decision.dividend_treatment == "assumption"


def test_a11_same_rule_id_with_different_meaning_conflicts() -> None:
    tree = RuleTree(
        rules=[
            _rule(),
            _rule(threshold="close > ma20 * 1.05", action="exit"),
        ]
    )
    errors, _ = validate_rule_tree(tree)
    assert any("RULE_CONFLICT" in message for message in errors)


def test_a11_volume_and_amount_indicators_are_not_interchangeable() -> None:
    tree = RuleTree(rules=[_rule(indicator="volume", metric="成交额 > 5亿", action="watch")])
    errors, _ = validate_rule_tree(tree)
    assert any("INDICATOR_MISMATCH" in message for message in errors)


def test_a12_tranche_sum_over_one_hundred_percent_fails() -> None:
    valid = RuleTree(
        sell_tranches=[
            SellTranche(tranche_id="t1", fraction=0.40),
            SellTranche(tranche_id="t2", fraction=0.25),
            SellTranche(tranche_id="t3", fraction=0.25),
            SellTranche(tranche_id="t4", fraction=0.10),
        ]
    )
    errors, _ = validate_rule_tree(valid)
    assert not errors

    overflow = RuleTree(
        sell_tranches=[
            SellTranche(tranche_id="t1", fraction=0.40),
            SellTranche(tranche_id="t2", fraction=0.25),
            SellTranche(tranche_id="t3", fraction=0.25),
            SellTranche(tranche_id="t4", fraction=0.15),
        ]
    )
    errors, _ = validate_rule_tree(overflow)
    assert any("TRANCHE_OVERFLOW" in message for message in errors)


def test_a13_intraday_trigger_cannot_be_verified_with_daily_bars() -> None:
    tree = RuleTree(
        rules=[
            _rule(
                rule_id="r-1450",
                metric="14:50 放量突破",
                observation_frequency="intraday_14_50",
                data_frequency="intraday",
            )
        ]
    )
    errors, notes = validate_rule_tree(tree, available_data="daily")
    assert any("RULE_NEEDS_INTRADAY" in message for message in errors)
    assert not any("RULE_NEEDS_INTRADAY" in note for note in notes)


def test_a13_rule_tree_blocks_actionable_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    (run_dir / "trading_rules.json").write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "rule_id": "r-1450",
                        "indicator": "close",
                        "metric": "14:50 突破",
                        "threshold": "close > ma20",
                        "price_basis": "unadjusted",
                        "action": "buy",
                        "observation_frequency": "intraday_14_50",
                        "data_frequency": "intraday",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    research_path = run_dir / "research_decision.json"
    research = json.loads(research_path.read_text(encoding="utf-8"))
    research["execution"] = {"status": "actionable", "target_account_weight": 0.05}
    research_path.write_text(json.dumps(research, ensure_ascii=False), encoding="utf-8")
    decision_path = run_dir / "decision.json"
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    decision["action"] = "BUY"
    decision_path.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    codes = {finding.code for finding in report.findings}
    assert "RULE_NEEDS_INTRADAY" in codes
    assert "RULE_TREE_BLOCKS_ACTION" in codes


def test_a14_missing_capital_keeps_the_research_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    research_path = run_dir / "research_decision.json"
    research = json.loads(research_path.read_text(encoding="utf-8"))
    research["execution"] = {
        "status": "blocked",
        "block_reasons": ["账户资金与持仓未提供"],
    }
    research_path.write_text(json.dumps(research, ensure_ascii=False), encoding="utf-8")
    report = validate_run(config, "run-1")  # type: ignore[arg-type]
    assert report.ok, report.to_dict()
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert (state.get("trade_context") or {}).get("capital") is None
    assert state["capability_flags"]["position_size"] is False


def test_a17_current_agent_mode_never_builds_a_model_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trade_council.orchestration import current_agent

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("current_agent 模式不得实例化外部模型 provider")

    monkeypatch.setattr("trade_council.llm.providers.build_provider", explode)
    _, run_dir, _ = _make_run(tmp_path, monkeypatch)
    assert (run_dir / "current_agent_request.json").is_file()
    assert not (run_dir / "llm_calls.jsonl").exists()
    assert current_agent.RUN_SCHEMA_VERSION.startswith("run-")


def test_a18_repeated_finalize_is_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from trade_council.orchestration.finalization import finalize_run

    config, run_dir, _ = _make_run(tmp_path, monkeypatch)
    first = finalize_run(config, "run-1")  # type: ignore[arg-type]
    assert first["ok"] is True
    sealed = (run_dir / "report.html").read_bytes()
    revisions_before = (run_dir / "revisions.jsonl").exists()
    second = finalize_run(config, "run-1")  # type: ignore[arg-type]
    assert second["report_html_sha256"] == first["report_html_sha256"]
    assert (run_dir / "report.html").read_bytes() == sealed
    assert (run_dir / "revisions.jsonl").exists() is revisions_before
