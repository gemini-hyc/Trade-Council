from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from trade_council.config import load_project_config
from trade_council.decision.risk_rules import TradeContext
from trade_council.errors import InvalidResponseError, ProviderUnavailableError
from trade_council.llm.base import (
    CompletionRequest,
    CompletionResult,
    ModelInfo,
    ProviderStatus,
    Usage,
)
from trade_council.orchestration.workflow import WorkflowEngine
from trade_council.schemas import FinalDecision, KeyFinding, RoleOutput

ROOT = Path(__file__).resolve().parents[1]


class FakeWorkflowProvider:
    name = "fake"
    model_specs: ClassVar[dict[str, Any]] = {}

    def __init__(self, decision_action: str = "HOLD") -> None:
        self.calls = 0
        self.decision_action = decision_action
        self.payloads: list[dict[str, Any]] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls += 1
        user = request.messages[-1].content
        payload = json.loads(user)
        self.payloads.append(payload)
        role = payload["role_id"]
        if role == "research-manager":
            content = {
                "action": "HOLD",
                "confidence": "medium",
                "rationale": "Synthetic workflow test",
                "key_points": [],
                "evidence_ids": [],
                "data_gaps": [],
            }
        elif role == "trader":
            content = {
                "action": self.decision_action,
                "confidence": "medium",
                "entry_zone": "100" if self.decision_action == "BUY" else None,
                "target_price": "120" if self.decision_action == "BUY" else None,
                "stop_loss": "90" if self.decision_action == "BUY" else None,
                "position_pct": 20 if self.decision_action == "BUY" else None,
                "position_shares": None,
                "position_amount": None,
                "position_opened_date": None,
                "position_size_blocked_reason": "test",
                "rationale": "Synthetic workflow test",
                "evidence_ids": [],
                "data_gaps": [],
            }
        elif role == "risk-manager":
            content = {
                "action": self.decision_action,
                "confidence": "medium",
                "risk_level": "medium",
                "rationale": "Synthetic workflow test",
                "entry_zone": "100" if self.decision_action == "BUY" else None,
                "target_price": "120" if self.decision_action == "BUY" else None,
                "stop_loss": "90" if self.decision_action == "BUY" else None,
                "position_pct": 20 if self.decision_action == "BUY" else None,
                "position_shares": None,
                "position_amount": None,
                "position_opened_date": None,
                "evidence_ids": [],
                "capability_flags": {"position_size": False},
                "data_gaps": [],
            }
        else:
            content = {
                "role_id": role,
                "stance": "neutral",
                "summary": "Synthetic workflow test",
                "key_findings": [],
                "risks": [],
                "data_gaps": ["synthetic gap"] if role == "market-analyst" else [],
                "scores": {},
            }
        return CompletionResult(
            provider="fake",
            model=request.model,
            content=json.dumps(content),
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def list_models(self) -> list[ModelInfo]:
        return []

    async def preflight(self) -> ProviderStatus:
        return ProviderStatus(self.name, True, [])


class Phase2FailingProvider(FakeWorkflowProvider):
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        payload = json.loads(request.messages[-1].content)
        if payload["role_id"] == "bull-researcher":
            raise ProviderUnavailableError("phase2 transport failure")
        return await super().complete(request)


def _make_datapack(tmp_path: Path) -> Path:
    datapack = tmp_path / "pack"
    datapack.mkdir()
    (datapack / "manifest.json").write_text(
        json.dumps(
            {
                "ts_code": "600519.SH",
                "asof": "2026-10-16",
                "snapshot_id": "snap-test",
            }
        ),
        encoding="utf-8",
    )
    (datapack / "DATA_DIGEST.md").write_text("# test digest\n", encoding="utf-8")
    (datapack / "evidence.json").write_text(
        json.dumps({"snapshot_id": "snap-test", "items": []}),
        encoding="utf-8",
    )
    return datapack


def test_full_workflow_and_resume(tmp_path: Path) -> None:
    config = load_project_config(ROOT)
    datapack = _make_datapack(tmp_path)
    fake = FakeWorkflowProvider()
    providers = {"ollama": fake, "deepseek": fake}

    engine = WorkflowEngine(
        config,
        data_pack_dir=datapack,
        run_dir=tmp_path / "run",
        profile="batch",
        mode="full",
        deep_tier=None,
        include_news=False,
        providers=providers,
    )
    state = asyncio.run(engine.run())
    assert state["status"] == "DECIDED"
    assert state["degraded"] is True
    assert state["final_decision"]["action"] == "HOLD"
    assert state["final_decision"]["capability_flags"] == {
        "news": False,
        "position_size": False,
        "trade_instructions": False,
    }
    assert state["completed_phases"] == ["phase1", "phase2", "phase3", "phase4"]
    assert state["trade_context"]["board"] == "main"
    assert fake.calls == 11
    assert (tmp_path / "run" / "decision.json").is_file()
    assert (tmp_path / "run" / "resolved_methods.json").is_file()
    assert (tmp_path / "run" / "method_results.json").is_file()
    assert (tmp_path / "run" / "model_calls.jsonl").is_file()
    assert len((tmp_path / "run" / "model_calls.jsonl").read_text().splitlines()) == 11
    assert (tmp_path / "run" / "agents" / "phase4" / "risk-manager.json").is_file()
    method_result = json.loads(
        (tmp_path / "run" / "method_results.json").read_text(encoding="utf-8")
    )[0]
    assert method_result["id"] == "example-method"
    assert method_result["status"] == "ready"
    assert fake.payloads[0]["method_context"]["methods"][0]["id"] == "example-method"
    assert fake.payloads[0]["trade_instructions_allowed"] is False
    resolved = json.loads(
        (tmp_path / "run" / "resolved_methods.json").read_text(encoding="utf-8")
    )[0]
    assert len(resolved["manifest_sha256"]) == 64
    assert len(resolved["method_md_sha256"]) == 64
    method_results_text = (
        tmp_path / "run" / "method_results.json"
    ).read_text(encoding="utf-8")

    resumed = WorkflowEngine(
        config,
        data_pack_dir=datapack,
        run_dir=tmp_path / "run",
        profile="batch",
        mode="full",
        deep_tier=None,
        include_news=False,
        resume=True,
        providers={"ollama": fake, "deepseek": fake},
    )
    resumed_state = asyncio.run(resumed.run())
    assert resumed_state["status"] == "DECIDED"
    assert resumed_state["completed_phases"] == ["phase1", "phase2", "phase3", "phase4"]
    assert resumed_state["cost"]["calls"] == 11
    assert resumed_state["cost"]["total_tokens"] == 165
    assert fake.calls == 11
    assert len((tmp_path / "run" / "model_calls.jsonl").read_text().splitlines()) == 11
    assert (
        tmp_path / "run" / "method_results.json"
    ).read_text(encoding="utf-8") == method_results_text


def test_quick_mode_is_deterministic(tmp_path: Path) -> None:
    config = load_project_config(ROOT)
    datapack = _make_datapack(tmp_path)
    fake = FakeWorkflowProvider()
    engine = WorkflowEngine(
        config,
        data_pack_dir=datapack,
        run_dir=tmp_path / "quick-run",
        profile="batch",
        mode="quick",
        deep_tier=None,
        include_news=False,
        providers={"ollama": fake, "deepseek": fake},
    )
    state = asyncio.run(engine.run())
    assert state["status"] == "COMPLETED_NO_DECISION"
    assert state["screening_result"]["direction_bias"] == "insufficient"
    assert fake.calls == 0


def test_failed_phase_is_recorded_and_resume_continues(tmp_path: Path) -> None:
    config = load_project_config(ROOT)
    datapack = _make_datapack(tmp_path)
    failing = Phase2FailingProvider()
    engine = WorkflowEngine(
        config,
        data_pack_dir=datapack,
        run_dir=tmp_path / "failed-run",
        profile="batch",
        mode="full",
        deep_tier=None,
        include_news=False,
        providers={"ollama": failing, "deepseek": failing},
    )
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(engine.run())
    failed_state = json.loads(
        (tmp_path / "failed-run" / "run.json").read_text(encoding="utf-8")
    )
    assert failed_state["status"] == "FAILED"
    assert failed_state["failed_phase"] == "phase2"
    assert failed_state["error"]["type"] == "ProviderUnavailableError"
    assert (tmp_path / "failed-run" / "costs.json").is_file()

    resumed_provider = FakeWorkflowProvider()
    resumed = WorkflowEngine(
        config,
        data_pack_dir=datapack,
        run_dir=tmp_path / "failed-run",
        profile="batch",
        mode="full",
        deep_tier=None,
        include_news=False,
        resume=True,
        providers={"ollama": resumed_provider, "deepseek": resumed_provider},
    )
    resumed_state = asyncio.run(resumed.run())
    assert resumed_state["status"] == "DECIDED"
    assert resumed_provider.calls == 8
    assert resumed_state["cost"]["calls"] == 11
    assert "failed_phase" not in resumed_state
    assert resumed_state["failure_history"][0]["phase"] == "phase2"


def test_role_fact_claim_requires_matching_evidence_dataset(tmp_path: Path) -> None:
    config = load_project_config(ROOT)
    datapack = _make_datapack(tmp_path)
    (datapack / "evidence.json").write_text(
        json.dumps(
            {
                "snapshot_id": "snap-test",
                "items": [
                    {
                        "evidence_id": "F-margin-fixture",
                        "dataset_id": "sentiment.margin_detail",
                        "path": "sentiment/margin_detail",
                        "primary_key": "600519.SH",
                        "event_date": "2026-10-15",
                        "available_at": "2026-10-16",
                        "field": "rzrqye",
                        "value": 100.0,
                        "unit": "CNY",
                        "formula": None,
                        "snapshot_id": "snap-test",
                        "source_hash": "snap-test",
                        "note": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    engine = WorkflowEngine(
        config,
        data_pack_dir=datapack,
        run_dir=tmp_path / "evidence-rule-run",
        profile="batch",
        mode="full",
        deep_tier=None,
        include_news=False,
        providers={"ollama": FakeWorkflowProvider(), "deepseek": FakeWorkflowProvider()},
    )
    output = RoleOutput(
        role_id="sentiment-analyst",
        stance="neutral",
        summary="fixture",
        key_findings=[
            KeyFinding(
                claim_id="top-list",
                claim="龙虎榜数据显示累计净买入。",
                evidence_ids=["F-margin-fixture"],
                kind="fact",
            )
        ],
    )
    with pytest.raises(InvalidResponseError, match="unrelated evidence"):
        engine._validate_evidence(output)


def test_role_retries_once_after_evidence_gate_failure(tmp_path: Path) -> None:
    class RetryEvidenceProvider(FakeWorkflowProvider):
        def __init__(self) -> None:
            super().__init__()
            self.bull_calls = 0

        async def complete(self, request: CompletionRequest) -> CompletionResult:
            first_user = next(
                message.content for message in request.messages if message.role == "user"
            )
            payload = json.loads(first_user)
            if payload["role_id"] != "bull-researcher":
                return await super().complete(request)
            self.bull_calls += 1
            evidence_id = (
                "F-margin-fixture"
                if self.bull_calls == 1
                else "F-daily-fixture"
            )
            content = {
                "role_id": "bull-researcher",
                "stance": "bullish",
                "summary": "fixture",
                "key_findings": [
                    {
                        "claim_id": "macd-claim",
                        "claim": "MACD 显示价格动能改善。",
                        "evidence_ids": [evidence_id],
                        "self_reported_confidence": "medium",
                        "kind": "fact",
                        "status": "intact",
                    }
                ],
                "risks": [],
                "data_gaps": [],
                "scores": {},
            }
            return CompletionResult(
                provider="fake",
                model=request.model,
                content=json.dumps(content),
                usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )

    config = load_project_config(ROOT)
    datapack = _make_datapack(tmp_path)
    (datapack / "evidence.json").write_text(
        json.dumps(
            {
                "snapshot_id": "snap-test",
                "items": [
                    {
                        "evidence_id": "F-margin-fixture",
                        "dataset_id": "sentiment.margin_detail",
                        "path": "sentiment/margin_detail",
                        "primary_key": "600519.SH",
                        "event_date": "2026-10-15",
                        "available_at": "2026-10-16",
                        "field": "rzrqye",
                        "value": 100.0,
                        "unit": "CNY",
                        "formula": None,
                        "snapshot_id": "snap-test",
                        "source_hash": "snap-test",
                        "note": "",
                    },
                    {
                        "evidence_id": "F-daily-fixture",
                        "dataset_id": "market.daily",
                        "path": "market/daily",
                        "primary_key": "600519.SH",
                        "event_date": "2026-10-16",
                        "available_at": "2026-10-16 15:00:00",
                        "field": "MACD",
                        "value": 1.0,
                        "unit": "index",
                        "formula": None,
                        "snapshot_id": "snap-test",
                        "source_hash": "snap-test",
                        "note": "",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    provider = RetryEvidenceProvider()
    engine = WorkflowEngine(
        config,
        data_pack_dir=datapack,
        run_dir=tmp_path / "retry-run",
        profile="batch",
        mode="full",
        deep_tier=None,
        include_news=False,
        providers={"ollama": provider, "deepseek": provider},
    )
    result = asyncio.run(
        engine._run_role(
            phase="phase2",
            role_id="bull-researcher",
            schema=RoleOutput,
            prior={},
        )
    )
    assert provider.bull_calls == 2
    assert result["key_findings"][0]["evidence_ids"] == ["F-daily-fixture"]


def test_role_retries_after_unparseable_provider_response(tmp_path: Path) -> None:
    class UnparseableThenValidProvider(FakeWorkflowProvider):
        async def complete(self, request: CompletionRequest) -> CompletionResult:
            self.calls += 1
            if self.calls <= 2:
                content = "not json"
            else:
                content = json.dumps(
                    {
                        "role_id": "bull-researcher",
                        "stance": "bullish",
                        "summary": "fixture",
                        "key_findings": [],
                        "risks": [],
                        "data_gaps": [],
                        "scores": {},
                    }
                )
            return CompletionResult(
                provider="fake",
                model=request.model,
                content=content,
                usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )

    config = load_project_config(ROOT)
    datapack = _make_datapack(tmp_path)
    provider = UnparseableThenValidProvider()
    engine = WorkflowEngine(
        config,
        data_pack_dir=datapack,
        run_dir=tmp_path / "parse-retry-run",
        profile="batch",
        mode="full",
        deep_tier=None,
        include_news=False,
        providers={"ollama": provider, "deepseek": provider},
    )
    result = asyncio.run(
        engine._run_role(
            phase="phase2",
            role_id="bull-researcher",
            schema=RoleOutput,
            prior={},
        )
    )
    assert provider.calls == 3
    assert result["role_id"] == "bull-researcher"


def test_workflow_passes_context_and_sizes_buy_deterministically(tmp_path: Path) -> None:
    config = load_project_config(ROOT)
    datapack = _make_datapack(tmp_path)
    fake = FakeWorkflowProvider(decision_action="BUY")
    context = TradeContext(
        capital=100_000,
        position_state="flat",
        position_shares=0,
        board="main",
    )
    engine = WorkflowEngine(
        config,
        data_pack_dir=datapack,
        run_dir=tmp_path / "buy-run",
        profile="deep",
        mode="full",
        deep_tier="flash",
        include_news=False,
        trade_context=context,
        providers={"ollama": fake, "deepseek": fake},
    )
    state = asyncio.run(engine.run())
    assert all(payload["trade_context"]["capital"] == 100_000 for payload in fake.payloads)
    assert state["final_decision"]["position_shares"] == 200
    assert state["final_decision"]["position_amount"] == 20_000
    assert state["final_decision"]["degraded_from_action"] is None
    assert state["capability_flags"]["position_size"] is True
    assert state["decision_check"]["ok"] is True


def test_batch_profile_downgrades_actionable_decision(tmp_path: Path) -> None:
    config = load_project_config(ROOT)
    datapack = _make_datapack(tmp_path)
    fake = FakeWorkflowProvider(decision_action="BUY")
    context = TradeContext(
        capital=100_000,
        position_state="flat",
        position_shares=0,
        board="main",
    )
    engine = WorkflowEngine(
        config,
        data_pack_dir=datapack,
        run_dir=tmp_path / "batch-downgrade-run",
        profile="batch",
        mode="full",
        deep_tier=None,
        include_news=False,
        trade_context=context,
        providers={"ollama": fake, "deepseek": fake},
    )
    state = asyncio.run(engine.run())
    final = state["final_decision"]
    assert final["action"] == "INSUFFICIENT_DATA"
    assert final["degraded_from_action"] == "BUY"
    assert final["position_shares"] is None
    assert final["position_amount"] is None
    assert any("profile rejected executable action" in gap for gap in final["data_gaps"])
    assert state["capability_flags"]["trade_instructions"] is False


def test_non_actionable_decision_has_all_trade_fields_cleared(tmp_path: Path) -> None:
    config = load_project_config(ROOT)
    datapack = _make_datapack(tmp_path)
    engine = WorkflowEngine(
        config,
        data_pack_dir=datapack,
        run_dir=tmp_path / "hold-clean-run",
        profile="deep",
        mode="full",
        deep_tier="auto",
        include_news=False,
        providers={"ollama": FakeWorkflowProvider(), "deepseek": FakeWorkflowProvider()},
    )
    decision = FinalDecision(
        action="HOLD",
        confidence="medium",
        risk_level="medium",
        rationale="资金不足一手，保持观望",
        position_shares=0,
        position_amount=0.0,
    )
    cleaned = engine._apply_profile_action_policy(decision)
    assert cleaned.position_shares is None
    assert cleaned.position_amount is None
    assert cleaned.degraded_from_action is None
