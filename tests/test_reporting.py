from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from trade_council.decision.risk_rules import (
    RiskPolicy,
    TradeContext,
    board_rule_for,
    infer_board,
    lot_size_for_board,
    shares_follow_board_rule,
    t_plus_one_blocked,
    validate_decision,
    validate_trade_context,
)
from trade_council.reporting.audit import audit_report
from trade_council.reporting.build import build_report

ROOT = Path(__file__).resolve().parents[1]


def _role(role_id: str, stance: str = "neutral") -> dict:
    return {
        "role_id": role_id,
        "stance": stance,
        "summary": "测试摘要",
        "key_findings": [],
        "risks": [],
        "data_gaps": [],
        "scores": {"score": 5.0},
    }


def _make_project_and_run(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "Trade-Council"
    (project / "configs").mkdir(parents=True)
    (project / "configs" / "risk.yaml").write_text(
        "min_reward_risk: 2.0\nmax_position_pct: 20\n",
        encoding="utf-8",
    )
    run_dir = project / "runs" / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "agents" / "phase1").mkdir(parents=True)
    (run_dir / "agents" / "phase2").mkdir(parents=True)
    (run_dir / "agents" / "phase4").mkdir(parents=True)
    for role_id in ("market-analyst", "fundamentals-analyst", "sentiment-analyst"):
        (run_dir / "agents" / "phase1" / f"{role_id}.json").write_text(
            json.dumps(_role(role_id)), encoding="utf-8"
        )
    for role_id in ("bull-researcher", "bear-researcher"):
        (run_dir / "agents" / "phase2" / f"{role_id}.json").write_text(
            json.dumps(_role(role_id, "bullish" if role_id.startswith("bull") else "bearish")),
            encoding="utf-8",
        )
    (run_dir / "agents" / "phase4" / "risk-manager.json").write_text(
        json.dumps(
            {
                "action": "HOLD",
                "confidence": "medium",
                "risk_level": "medium",
                "rationale": "测试裁决",
                "data_gaps": [],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "symbol": "600519.SH",
                "asof": "2026-10-21",
                "mode": "full",
                "profile": "deep",
                "status": "DECIDED",
                "capability_flags": {"trade_instructions": False},
            }
        ),
        encoding="utf-8",
    )
    decision = {
        "action": "HOLD",
        "confidence": "medium",
        "risk_level": "medium",
        "rationale": "测试裁决",
        "entry_zone": None,
        "target_price": None,
        "stop_loss": None,
        "position_pct": None,
        "evidence_ids": [],
        "capability_flags": {},
        "data_gaps": [],
    }
    (run_dir / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
    datapack = tmp_path / "pack"
    datapack.mkdir()
    dates = pd.bdate_range(end="2026-10-21", periods=10)
    pd.DataFrame(
        {
            "date": dates,
            "close": [100 + i for i in range(10)],
            "ma20": [99.0] * 10,
            "ma60": [98.0] * 10,
        }
    ).to_csv(datapack / "price_daily.csv", index=False)
    (datapack / "evidence.json").write_text(
        json.dumps(
            {
                "snapshot_id": "snap-test",
                "items": [
                    {
                        "evidence_id": "F-market-daily-close",
                        "dataset_id": "market.daily",
                        "path": "market/daily",
                        "primary_key": "600519.SH",
                        "event_date": "2026-10-21",
                        "available_at": "2026-10-21 15:00:00",
                        "field": "close",
                        "value": 109,
                        "unit": "CNY",
                        "formula": "raw close",
                        "snapshot_id": "snap-test",
                        "source_hash": "snap-test",
                        "note": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return project, run_dir, datapack


def test_report_builds_and_passes_gates(tmp_path: Path) -> None:
    _, run_dir, datapack = _make_project_and_run(tmp_path)
    audit = build_report(run_dir, datapack)
    assert audit["verdict"] == "PASS"
    assert (run_dir / "report.md").is_file()
    assert (run_dir / "report.html").is_file()
    assert (run_dir / "audit.json").is_file()
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "AUDITED"
    assert state["audit"]["verdict"] == "PASS"


def test_invalid_hold_trade_parameter_fails() -> None:
    datapack = Path("/tmp")
    result = validate_decision(
        {
            "action": "HOLD",
            "entry_zone": "100",
            "target_price": "120",
            "stop_loss": "90",
        },
        datapack,
        RiskPolicy(),
        allow_trade_instructions=True,
    )
    assert not result.ok
    assert "HOLD" in result.errors[0]


def test_market_rules_cover_board_lots_and_t_plus_one() -> None:
    rules = ROOT / "configs" / "market-rules.yaml"
    assert lot_size_for_board(rules, board="main") == 100
    assert lot_size_for_board(rules, board="star") == 200
    assert board_rule_for(rules, board="main").step == 100
    assert board_rule_for(rules, board="star").step == 1
    assert infer_board("600519.SH") == "main"
    assert infer_board("688981.SH") == "star"
    assert infer_board("830799.BJ") == "bj"
    assert t_plus_one_blocked("2026-10-21", "2026-10-21")
    assert not t_plus_one_blocked("2026-10-20", "2026-10-21")


def test_board_share_rules_are_not_hardcoded_to_100() -> None:
    rules = ROOT / "configs" / "market-rules.yaml"
    main_rule = board_rule_for(rules, board="main")
    star_rule = board_rule_for(rules, board="star")
    bj_rule = board_rule_for(rules, board="bj")
    assert shares_follow_board_rule(200, main_rule)
    assert not shares_follow_board_rule(250, main_rule)
    assert shares_follow_board_rule(201, star_rule)
    assert not shares_follow_board_rule(199, star_rule)
    assert shares_follow_board_rule(101, bj_rule)


def test_buy_checks_lot_and_capital_limit(tmp_path: Path) -> None:
    datapack = tmp_path / "pack"
    datapack.mkdir()
    pd.DataFrame({"date": ["2026-10-21"], "close": [100.0]}).to_csv(
        datapack / "price_daily.csv", index=False
    )
    context = TradeContext(
        capital=100_000,
        position_state="flat",
        position_shares=0,
        board="main",
    )
    decision = {
        "action": "BUY",
        "entry_zone": "100",
        "stop_loss": "90",
        "target_price": "120",
        "position_pct": 20,
        "position_shares": 250,
        "position_amount": 25_000,
        "position_opened_date": None,
    }
    failed = validate_decision(
        decision,
        datapack,
        RiskPolicy(),
        allow_trade_instructions=True,
        trade_context=context,
        market_rules_path=ROOT / "configs" / "market-rules.yaml",
        asof="2026-10-21",
    )
    assert not failed.ok
    assert "risk.lot.board" in failed.rule_checks
    assert "risk.position.max_amount" in failed.rule_checks

    decision["position_shares"] = 200
    decision["position_amount"] = 20_000
    passed = validate_decision(
        decision,
        datapack,
        RiskPolicy(),
        allow_trade_instructions=True,
        trade_context=context,
        market_rules_path=ROOT / "configs" / "market-rules.yaml",
        asof="2026-10-21",
    )
    assert passed.ok

    decision["entry_zone"] = "100-105"
    upper_entry_failed = validate_decision(
        decision,
        datapack,
        RiskPolicy(),
        allow_trade_instructions=True,
        trade_context=context,
        market_rules_path=ROOT / "configs" / "market-rules.yaml",
        asof="2026-10-21",
    )
    assert not upper_entry_failed.ok
    assert "risk.position.max_amount" in upper_entry_failed.rule_checks


def test_t_plus_one_blocks_same_day_sell(tmp_path: Path) -> None:
    datapack = tmp_path / "pack"
    datapack.mkdir()
    pd.DataFrame({"date": ["2026-10-21"], "close": [100.0]}).to_csv(
        datapack / "price_daily.csv", index=False
    )
    context = TradeContext(
        capital=100_000,
        position_state="holding",
        position_shares=1_000,
        position_opened_date="2026-10-21",
        board="main",
    )
    decision = {
        "action": "SELL",
        "entry_zone": "100",
        "position_pct": 20,
        "position_shares": 200,
        "position_amount": 20_000,
        "position_opened_date": "2026-10-21",
    }
    result = validate_decision(
        decision,
        datapack,
        RiskPolicy(),
        allow_trade_instructions=True,
        trade_context=context,
        market_rules_path=ROOT / "configs" / "market-rules.yaml",
        asof="2026-10-21",
    )
    assert not result.ok
    assert "risk.trade.t_plus_one" in result.rule_checks


def test_concrete_position_requires_capital_and_state(tmp_path: Path) -> None:
    datapack = tmp_path / "pack"
    datapack.mkdir()
    decision = {
        "action": "BUY",
        "entry_zone": "100",
        "stop_loss": "90",
        "target_price": "120",
        "position_pct": 20,
        "position_shares": 100,
        "position_amount": 10_000,
    }
    result = validate_decision(
        decision,
        datapack,
        RiskPolicy(),
        allow_trade_instructions=True,
        market_rules_path=ROOT / "configs" / "market-rules.yaml",
        asof="2026-10-21",
    )
    assert not result.ok
    assert "risk.capital.required" in result.rule_checks
    assert "risk.position.state" in result.rule_checks


def test_trade_context_validation() -> None:
    errors = validate_trade_context(
        TradeContext(
            capital=100_000,
            position_state="holding",
            position_shares=None,
            position_opened_date=None,
        ),
        "2026-10-21",
    )
    assert any("position-shares" in error for error in errors)
    assert any("position-opened-date" in error for error in errors)
    assert (
        validate_trade_context(
            TradeContext(
                position_state="holding",
                position_shares=1_000,
                position_opened_date="2026-10-20",
            ),
            "2026-10-21",
        )
        == []
    )


def test_report_gate_detects_empty_chart(tmp_path: Path) -> None:
    project, run_dir, datapack = _make_project_and_run(tmp_path)
    build_report(run_dir, datapack)
    document = (run_dir / "report.html").read_text(encoding="utf-8")
    mutated = document.replace('data-chart="risk" data-points="5"', 'data-chart="risk" data-points="0"')
    roles = {}
    for path in (run_dir / "agents").glob("phase*/*.json"):
        roles[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    result = audit_report(
        mutated,
        decision=json.loads((run_dir / "decision.json").read_text(encoding="utf-8")),
        enabled_domains=["technical", "fundamental", "sentiment", "risk"],
        role_outputs=roles,
        data_gaps=[],
        data_pack_dir=datapack,
        risk_policy_path=project / "configs" / "risk.yaml",
        allow_trade_instructions=False,
    )
    assert result["verdict"] == "FAIL"
    assert any("fewer than 5 points" in issue["message"] for issue in result["issues"])


def test_report_displays_trade_context_and_t_plus_one_failure(tmp_path: Path) -> None:
    _, run_dir, datapack = _make_project_and_run(tmp_path)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run["trade_context"] = {
        "capital": 100_000,
        "position_state": "holding",
        "position_shares": 1_000,
        "position_opened_date": "2026-10-21",
        "market": "CN",
        "board": "main",
    }
    run["capability_flags"]["trade_instructions"] = True
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    decision = {
        "action": "SELL",
        "confidence": "medium",
        "risk_level": "medium",
        "rationale": "测试当日卖出",
        "entry_zone": "109",
        "target_price": None,
        "stop_loss": None,
        "position_pct": 20,
        "position_shares": 200,
        "position_amount": 21_800,
        "position_opened_date": "2026-10-21",
        "evidence_ids": [],
        "capability_flags": {},
        "data_gaps": [],
    }
    (run_dir / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
    audit = build_report(run_dir, datapack)
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert 'data-decision-field="position_shares"' in html
    assert "T+1 状态" in html
    assert audit["verdict"] == "FAIL"
    assert any("T+1" in issue["message"] for issue in audit["issues"])


def test_report_gate_detects_decision_and_evidence_tampering(tmp_path: Path) -> None:
    project, run_dir, datapack = _make_project_and_run(tmp_path)
    build_report(run_dir, datapack)
    document = (run_dir / "report.html").read_text(encoding="utf-8")
    tampered = document.replace(
        'data-decision-field="action">HOLD</span>',
        'data-decision-field="action">BUY</span>',
    ).replace(
        'data-evidence-id="F-market-daily-close">109</span>',
        'data-evidence-id="F-market-daily-close">999</span>',
    )
    roles = {}
    for path in (run_dir / "agents").glob("phase*/*.json"):
        roles[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    result = audit_report(
        tampered,
        decision=json.loads((run_dir / "decision.json").read_text(encoding="utf-8")),
        enabled_domains=["technical", "fundamental", "sentiment", "risk"],
        role_outputs=roles,
        data_gaps=[],
        data_pack_dir=datapack,
        risk_policy_path=project / "configs" / "risk.yaml",
        allow_trade_instructions=False,
    )
    assert result["verdict"] == "FAIL"
    assert any("Decision field action" in issue["message"] for issue in result["issues"])
    assert any("evidence number was tampered" in issue["message"] for issue in result["issues"])


def test_report_displays_task_scoped_supplemental_evidence(tmp_path: Path) -> None:
    _, run_dir, datapack = _make_project_and_run(tmp_path)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run["data_needs"] = [
        {
            "need_id": "need-forecast",
            "dataset_id": "reports.forecast",
            "field": "net_profit_min",
            "priority": "critical",
        }
    ]
    run["web_supplement"] = {
        "status": "partial",
        "results": [
            {
                "need_id": "need-forecast",
                "status": "partial",
                "missing_fields": ["net_profit_max", "change_reason"],
                "sources": [
                    {
                        "title": "外部公告",
                        "url": "https://example.test/announcement",
                    }
                ],
            }
        ],
    }
    (run_dir / "run.json").write_text(json.dumps(run), encoding="utf-8")
    (run_dir / "supplemental_evidence.json").write_text(
        json.dumps(
            {
                "snapshot_id": "snap-test",
                "items": [
                    {
                        "evidence_id": "F-web-test",
                        "dataset_id": "reports.forecast",
                        "path": "web_supplement",
                        "primary_key": "600519.SH",
                        "event_date": "2026-10-16",
                        "available_at": "2026-10-16 20:00:00",
                        "field": "net_profit_min",
                        "value": 1.0,
                        "unit": "CNY",
                        "formula": "task-scoped web search",
                        "snapshot_id": "snap-test",
                        "source_hash": None,
                        "note": "test",
                        "source_level": "web",
                        "source_url": "https://example.test/announcement",
                        "retrieved_at": "2026-10-21T20:00:00",
                        "extraction_confidence": "high",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    audit = build_report(run_dir, datapack)
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert 'data-supplement="true"' in html
    assert "外部公告" in html
    assert "https://example.test/announcement" in html
    assert "net_profit_max" in html
    assert "change_reason" in html
    assert audit["verdict"] == "PASS"

    supplemental = json.loads(
        (run_dir / "supplemental_evidence.json").read_text(encoding="utf-8")
    )
    supplemental["items"][0]["source_url"] = None
    (run_dir / "supplemental_evidence.json").write_text(
        json.dumps(supplemental),
        encoding="utf-8",
    )
    audit_without_url = build_report(run_dir, datapack)
    assert audit_without_url["verdict"] == "FAIL"
    assert any(
        "Web evidence has no source_url" in issue["message"]
        for issue in audit_without_url["issues"]
    )
