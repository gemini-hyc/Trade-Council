"""Prepare an honest handoff to the current conversational agent, without model API calls."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import ProjectConfig
from ..decision.risk_rules import TradeContext
from ..schemas import DecisionPlan, FinalDecision, RoleOutput, StrictModel, TradeProposal


def prepare_current_agent_task(
    config: ProjectConfig,
    datapack: Path,
    run_dir: Path,
    trade_context: TradeContext,
    *,
    mode: str,
    include_news: bool,
    allow_web: bool,
    run_kind: str,
) -> dict[str, Any]:
    """Record inputs and roles, never pretend preparation is completed research."""
    manifest = json.loads((datapack / "manifest.json").read_text(encoding="utf-8"))
    state_path = run_dir / "run.json"
    if state_path.is_file():
        existing: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
        if (existing.get("execution_backend") != "current_agent"
                or existing.get("snapshot_id") != manifest.get("snapshot_id")
                or existing.get("symbol") != manifest["ts_code"]):
            raise ValueError("Run ID belongs to a different backend, symbol or snapshot; use a new run ID")
        if (existing.get("trade_context") != asdict(trade_context)
                or existing.get("mode") != mode
                or existing.get("capability_flags", {}).get("news") != include_news):
            raise ValueError("Run inputs changed; use a new run ID instead of silently replacing prior work")
        # Resume returns existing work without overwriting conclusions or changing its inputs.
        return existing

    plan = []
    schema_types: dict[str, type[StrictModel]] = {
        "research-manager": DecisionPlan, "trader": TradeProposal, "risk-manager": FinalDecision,
    }
    for role in config.team.roles:
        if not role.enabled and not (role.id == "news-analyst" and include_news):
            continue
        if role.id == "news-analyst" and not include_news:
            continue
        schema = schema_types.get(role.id, RoleOutput)
        prompt_path = config.root / "prompts" / f"{role.id}.md"
        prompt = prompt_path.read_text(encoding="utf-8")
        plan.append({"role_id": role.id, "phase": role.stage,
                     "prompt": prompt, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                     "schema": schema.model_json_schema(),
                     "output": f"agents/{role.stage}/{role.id}.json"})

    now = datetime.now().isoformat(timespec="seconds")
    state: dict[str, Any] = {
        "run_id": run_dir.name, "symbol": manifest["ts_code"],
        "stock_name": manifest.get("stock_name", manifest["ts_code"]),
        "asof": manifest["asof"], "snapshot_id": manifest.get("snapshot_id"),
        "data_pack_dir": str(datapack), "profile": "deep", "mode": mode,
        "execution_backend": "current_agent", "model": "current_session",
        "status": "DATA_READY" if mode == "data" else "AWAITING_CURRENT_AGENT",
        "started_at": now, "updated_at": now, "run_kind": run_kind,
        "trade_context": asdict(trade_context), "completed_phases": [],
        "prompt_hashes": {item["role_id"]: item["prompt_sha256"] for item in plan},
        "capability_flags": {"news": include_news, "position_size": False,
                             "trade_instructions": False},
        "external_model_calls": 0,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    request = {"run": state, "roles": plan, "web_verification_requested": allow_web}
    (run_dir / "current_agent_request.json").write_text(
        json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    task = f"""# 当前 agent 个股精读任务

标的：{state['stock_name']}（{state['symbol']}），数据截止 {state['asof']}。
状态：{state['status']}。准备任务成功不等于分析完成。

使用当前聊天会话实际采用的大模型直接分析；不调用 DeepSeek、Ollama 或其他模型 API，
不读取它们的密钥，不自动退回批量研究。终端不能直接调用当前会话模型；请当前 agent
继续执行本任务。多个角色是分析视角，不代表多名独立研究者或独立模型投票。

## 输入

- 数据目录：`{datapack}`
- 先读 `manifest.json`、`DATA_DIGEST.md`、`evidence.json`，交叉核对数值与文字。
- 本目录 `current_agent_request.json` 已冻结启用角色的提示词、Schema、输出路径和交易上下文。
- 联网核实请求：{allow_web}。必要核实使用当前 agent 可用工具，保留来源和披露时点；
  需要新权限时按当前会话权限处理，不将数据另发给模型 API。

## 执行与交付

1. 按 phase1 领域分析 → phase2 多空观点与研究主管 → phase3 交易方案 → phase4 风险评议与裁决推进。
2. 保存各角色 JSON/Markdown。事实必须引用真实证据 ID；推断标明依据、假设及反例。
3. 缺失不当作零，过期不当作最新；不能验证的问题保留为数据缺口。识别到输入错误应留痕更正。
4. 资金与持仓未提供时不虚构金额、股数和账户仓位。不宣称识别或使用了无法确认的模型版本。
5. 完成后用 FinalDecision 校验并保存 decision.json；运行现有确定性风控校验和报告审计，
   记录真实执行后端。未完成角色或审计失败不得标记为完成或交付可执行交易结论。
6. CLI report 在状态为 AWAITING_CURRENT_AGENT / DATA_READY 时会拒绝渲染。
   只有上述产物和检查真实完成后，才更新 run.json 状态及 completed_phases，生成最终报告。
7. 不伪造 API 调用记录、token 计数或费用；当前会话用量不可得时注明不可得。
"""
    (run_dir / "CURRENT_AGENT_TASK.md").write_text(task, encoding="utf-8")
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state
