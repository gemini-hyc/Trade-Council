from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pandas as pd
import pytest

from trade_council.config import load_project_config
from trade_council.engineering.datapack import build
from trade_council.llm.base import (
    CompletionRequest,
    CompletionResult,
    ModelInfo,
    ProviderStatus,
    Usage,
)
from trade_council.orchestration.workflow import WorkflowEngine
from trade_council.schemas import MethodSelection

ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads(
    (Path(__file__).with_name("golden_baseline.json")).read_text(encoding="utf-8")
)

STOCKS = {
    "600519.SH": ("sh.600519", "蓝筹样例", 100.0, 0.2),
    "300750.SZ": ("sz.300750", "成长样例", 80.0, 4.0),
    "601857.SH": ("sh.601857", "周期样例", 50.0, 2.0),
}


def _write_parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def _make_quant_root(tmp_path: Path) -> Path:
    root = tmp_path / "quant-data"
    dates = pd.bdate_range(end="2026-10-16", periods=60)
    price_frames = []
    adj_frames = []
    basic_frames = []
    flow_frames = []
    limit_frames = []
    for ts_code, (dot_code, name, base, volatility) in STOCKS.items():
        close = base + np.arange(len(dates)) * 0.2 + np.sin(np.arange(len(dates))) * volatility
        price_frames.append(
            pd.DataFrame(
                {
                    "date": dates,
                    "code": dot_code,
                    "open": close - 0.5,
                    "high": close + 1.0,
                    "low": close - 1.0,
                    "close": close,
                    "preclose": np.r_[close[0], close[:-1]],
                    "volume": 1_000_000.0,
                    "amount": close * 1_000_000.0,
                    "turn": 1.0,
                    "pctChg": 0.2,
                    "stock_name": name,
                }
            )
        )
        adj_frames.append(
            pd.DataFrame(
                {
                    "date": dates,
                    "code": dot_code,
                    "ts_code": ts_code,
                    "adj_factor": 1.0,
                }
            )
        )
        basic_frames.append(
            pd.DataFrame(
                {
                    "ts_code": [ts_code],
                    "trade_date": ["20261016"],
                    "close": [float(close[-1])],
                    "pe_ttm": [20.0],
                    "pb": [2.0],
                    "ps_ttm": [3.0],
                    "total_mv": [1_000_000.0],
                    "circ_mv": [900_000.0],
                }
            )
        )
        flow_frames.append(
            pd.DataFrame(
                {
                    "ts_code": [ts_code],
                    "trade_date": ["20261016"],
                    "buy_lg_amount": [100.0],
                    "sell_lg_amount": [80.0],
                    "buy_elg_amount": [50.0],
                    "sell_elg_amount": [30.0],
                }
            )
        )
        limit_frames.append(
            pd.DataFrame(
                {
                    "date": [pd.Timestamp("2026-10-16")],
                    "ts_code": [ts_code],
                    "up_limit": [float(close[-1] * 1.1)],
                    "down_limit": [float(close[-1] * 0.9)],
                }
            )
        )
    prices = pd.concat(price_frames, ignore_index=True)
    future = prices[prices["code"] == "sh.600519"].tail(1).copy()
    future["date"] = pd.Timestamp("2026-10-19")
    future["close"] = 9999.0
    prices = pd.concat([prices, future], ignore_index=True)
    _write_parquet(root / "market" / "daily" / "2026-10-16.parquet", prices)
    _write_parquet(
        root / "adj_factor" / "2026-10-16.parquet",
        pd.concat(adj_frames, ignore_index=True),
    )
    _write_parquet(
        root / "daily_basic" / "2026-10-16.parquet",
        pd.concat(basic_frames, ignore_index=True),
    )
    _write_parquet(
        root / "moneyflow" / "2026-10-16.parquet",
        pd.concat(flow_frames, ignore_index=True),
    )
    _write_parquet(
        root / "stk_limit" / "2026-10-16.parquet",
        pd.concat(limit_frames, ignore_index=True),
    )
    (root / "suspend_d").mkdir(parents=True, exist_ok=True)
    return root


@pytest.mark.parametrize(
    "sample",
    BASELINE["samples"],
    ids=[sample["id"] for sample in BASELINE["samples"]],
)
def test_synthetic_golden_samples_are_structural_and_asof_safe(
    tmp_path: Path,
    sample: dict[str, Any],
) -> None:
    ts_code = sample["ts_code"]
    expected_asof = BASELINE["asof"]
    root = _make_quant_root(tmp_path)
    output = tmp_path / "golden" / ts_code.replace(".", "-")
    manifest = build(
        ts_code,
        expected_asof,
        output,
        1,
        include_news=False,
        data_root=root,
    )
    assert manifest["ts_code"] == ts_code
    assert manifest["asof"] == expected_asof
    assert manifest["evidence_count"] > 0
    assert manifest["snapshot_id"]
    for name in sample["expected_files"]:
        assert (output / name).is_file()

    price = pd.read_csv(output / "price_daily.csv")
    assert pd.to_datetime(price["date"]).max() <= pd.Timestamp(expected_asof)
    assert float(price["close"].iloc[-1]) < 9999.0
    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["snapshot_id"] == manifest["snapshot_id"]
    for item in evidence["items"]:
        if item.get("available_at"):
            assert pd.to_datetime(item["available_at"]) <= pd.Timestamp(
                f'{manifest["asof"]} 23:59:59'
            )


def test_snapshot_is_reproducible_for_same_source(tmp_path: Path) -> None:
    root = _make_quant_root(tmp_path)
    first = build("600519.SH", "2026-10-16", tmp_path / "first", 1, data_root=root)
    second = build("600519.SH", "2026-10-16", tmp_path / "second", 1, data_root=root)
    assert first["snapshot_id"] == second["snapshot_id"]
    first_trace = (tmp_path / "first" / "evidence_trace.jsonl").read_text(encoding="utf-8")
    second_trace = (tmp_path / "second" / "evidence_trace.jsonl").read_text(encoding="utf-8")
    assert first_trace == second_trace


def test_non_trading_asof_falls_back_to_prior_trade_date(tmp_path: Path) -> None:
    root = _make_quant_root(tmp_path)
    manifest = build("600519.SH", "2026-10-17", tmp_path / "pack", 1, data_root=root)
    assert manifest["asof"] == "2026-10-16"


def test_missing_daily_data_fails_with_clear_error(tmp_path: Path) -> None:
    root = _make_quant_root(tmp_path)
    shutil.rmtree(root / "market" / "daily")
    with pytest.raises(SystemExit, match="未找到交易日数据"):
        build("600519.SH", "2026-10-16", tmp_path / "pack", 1, data_root=root)


def test_bad_parquet_fails_without_partial_pack(tmp_path: Path) -> None:
    root = _make_quant_root(tmp_path)
    daily = root / "market" / "daily"
    shutil.rmtree(daily)
    daily.mkdir(parents=True)
    (daily / "2026-10-16.parquet").write_bytes(b"not-a-parquet")
    with pytest.raises(SystemExit, match="无行情数据"):
        build("600519.SH", "2026-10-16", tmp_path / "pack", 1, data_root=root)


def test_all_null_close_fails_quality_gate(tmp_path: Path) -> None:
    root = _make_quant_root(tmp_path)
    price_path = root / "market" / "daily" / "2026-10-16.parquet"
    price = pd.read_parquet(price_path)
    price.loc[price["code"] == "sh.600519", "close"] = np.nan
    price.to_parquet(price_path, index=False)
    with pytest.raises(SystemExit, match="关键列整列为空"):
        build("600519.SH", "2026-10-16", tmp_path / "pack", 1, data_root=root)


def test_missing_adjustment_factor_is_explicitly_degraded(tmp_path: Path) -> None:
    root = _make_quant_root(tmp_path)
    shutil.rmtree(root / "adj_factor")
    manifest = build("600519.SH", "2026-10-16", tmp_path / "pack", 1, data_root=root)
    adj = next(item for item in manifest["datasets"] if item["key"] == "adj_applied")
    assert adj["status"] == "ERROR"


def test_all_null_valuation_columns_are_flagged(tmp_path: Path) -> None:
    root = _make_quant_root(tmp_path)
    path = root / "daily_basic" / "2026-10-16.parquet"
    frame = pd.read_parquet(path)
    frame[["pe_ttm", "pb", "ps_ttm"]] = np.nan
    frame.to_parquet(path, index=False)
    manifest = build("600519.SH", "2026-10-16", tmp_path / "pack", 1, data_root=root)
    quality = next(
        item for item in manifest["datasets"] if item["key"] == "daily_basic_quality"
    )
    assert quality["status"] == "ERROR"


class _WorkflowFake:
    name = "fake"
    model_specs: ClassVar[dict[str, Any]] = {}

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        payload = json.loads(request.messages[-1].content)
        self.payloads.append(payload)
        role = payload["role_id"]
        if role == "research-manager":
            content: dict[str, Any] = {
                "action": "HOLD",
                "confidence": "medium",
                "rationale": "repeatability fixture",
                "key_points": [],
                "evidence_ids": [],
                "data_gaps": [],
            }
        elif role == "trader":
            content = {
                "action": "HOLD",
                "confidence": "medium",
                "entry_zone": None,
                "target_price": None,
                "stop_loss": None,
                "position_pct": None,
                "position_shares": None,
                "position_amount": None,
                "position_opened_date": None,
                "position_size_blocked_reason": "fixture",
                "rationale": "repeatability fixture",
                "evidence_ids": [],
                "data_gaps": [],
            }
        elif role == "risk-manager":
            content = {
                "action": "HOLD",
                "confidence": "medium",
                "risk_level": "medium",
                "rationale": "repeatability fixture",
                "entry_zone": None,
                "target_price": None,
                "stop_loss": None,
                "position_pct": None,
                "position_shares": None,
                "position_amount": None,
                "position_opened_date": None,
                "evidence_ids": [],
                "capability_flags": {},
                "data_gaps": [],
            }
        else:
            content = {
                "role_id": role,
                "stance": "neutral",
                "summary": "repeatability fixture",
                "key_findings": [],
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

    async def list_models(self) -> list[ModelInfo]:
        return []

    async def preflight(self) -> ProviderStatus:
        return ProviderStatus(self.name, True, [])


def _workflow_datapack(tmp_path: Path) -> Path:
    datapack = tmp_path / "workflow-pack"
    datapack.mkdir()
    (datapack / "manifest.json").write_text(
        json.dumps(
            {
                "ts_code": "600519.SH",
                "asof": "2026-10-16",
                "snapshot_id": "snap-repeat",
            }
        ),
        encoding="utf-8",
    )
    (datapack / "DATA_DIGEST.md").write_text("# repeatability fixture\n", encoding="utf-8")
    (datapack / "evidence.json").write_text(
        json.dumps({"snapshot_id": "snap-repeat", "items": []}),
        encoding="utf-8",
    )
    return datapack


def test_workflow_schema_passes_ten_repeated_runs(tmp_path: Path) -> None:
    config = load_project_config(ROOT)
    datapack = _workflow_datapack(tmp_path)
    successes = 0
    repeat = BASELINE["repeatability"]
    for index in range(int(repeat["runs"])):
        fake = _WorkflowFake()
        engine = WorkflowEngine(
            config,
            data_pack_dir=datapack,
            run_dir=tmp_path / f"repeat-{index}",
            profile="batch",
            mode="full",
            deep_tier=None,
            include_news=False,
            providers={"ollama": fake, "deepseek": fake},
        )
        state = asyncio.run(engine.run())
        if state["status"] == "DECIDED" and state["final_decision"]["action"] == "HOLD":
            successes += 1
    assert successes / int(repeat["runs"]) == repeat["schema_pass_rate"]
    assert BASELINE["decision_consistency"]["status"] == "not_established"


def test_new_code_method_is_discovered_and_executed_without_orchestrator_change(
    tmp_path: Path,
) -> None:
    config = load_project_config(ROOT)
    shutil.copytree(ROOT / "prompts", tmp_path / "prompts")
    shutil.copytree(ROOT / "methods", tmp_path / "methods")
    method_dir = tmp_path / "methods" / "golden-code"
    method_dir.mkdir()
    (method_dir / "manifest.yaml").write_text(
        """
id: golden-code
name: Golden Code Method
version: 9.1.0
domain: technical
type: code
enabled: true
requires:
  datasets: [market.daily]
  fields: [close]
outputs:
  schema: schema.json
  evidence_policy: required
parameters: {}
""",
        encoding="utf-8",
    )
    (method_dir / "method.md").write_text("# Golden Code Method\n", encoding="utf-8")
    (method_dir / "compute.py").write_text(
        "import json, sys\n"
        "ctx = json.load(sys.stdin)\n"
        "eid = ctx['evidence'][0]['evidence_id']\n"
        "print(json.dumps({'signal': 'positive', 'summary': 'ok', 'evidence_ids': [eid]}))\n",
        encoding="utf-8",
    )
    (method_dir / "schema.json").write_text(
        """
{
  "type": "object",
  "required": ["signal", "summary", "evidence_ids"],
  "properties": {
    "signal": {"type": "string"},
    "summary": {"type": "string"},
    "evidence_ids": {"type": "array", "items": {"type": "string"}}
  }
}
""",
        encoding="utf-8",
    )
    team = config.team.model_copy(deep=True)
    team.methods["golden-code"] = MethodSelection(enabled=True)
    market_role = next(role for role in team.roles if role.id == "market-analyst")
    market_role.methods.append("golden-code")
    project_config = replace(config, root=tmp_path, team=team)

    datapack = _workflow_datapack(tmp_path)
    (datapack / "evidence.json").write_text(
        json.dumps(
            {
                "snapshot_id": "snap-repeat",
                "items": [
                    {
                        "evidence_id": "F-market-daily-fixture",
                        "dataset_id": "market.daily",
                        "path": "market/daily",
                        "primary_key": "600519.SH",
                        "event_date": "2026-10-16",
                        "available_at": "2026-10-16 15:00:00",
                        "field": "close",
                        "value": 100.0,
                        "unit": "CNY",
                        "formula": "raw close",
                        "snapshot_id": "snap-repeat",
                        "source_hash": "snap-repeat",
                        "note": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    fake = _WorkflowFake()
    engine = WorkflowEngine(
        project_config,
        data_pack_dir=datapack,
        run_dir=tmp_path / "custom-method-run",
        profile="batch",
        mode="full",
        deep_tier=None,
        include_news=False,
        providers={"ollama": fake, "deepseek": fake},
    )
    state = asyncio.run(engine.run())
    results = json.loads(
        (tmp_path / "custom-method-run" / "method_results.json").read_text(
            encoding="utf-8"
        )
    )
    golden = next(item for item in results if item["id"] == "golden-code")
    assert golden["status"] == "computed"
    assert golden["result"]["version"] == "9.1.0"
    market_payload = next(
        payload for payload in fake.payloads if payload["role_id"] == "market-analyst"
    )
    golden_payload = next(
        item
        for item in market_payload["method_context"]["methods"]
        if item["id"] == "golden-code"
    )
    assert golden_payload["result"]["structured_output"]["signal"] == "positive"
    assert state["status"] == "DECIDED"
