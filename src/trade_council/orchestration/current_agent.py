"""Prepare an honest handoff to the current conversational agent, without model API calls."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import ProjectConfig
from ..data.snapshot import sha256_file
from ..data.snapshot import verify as verify_snapshot
from ..decision.risk_rules import TradeContext
from ..schemas import DecisionPlan, FinalDecision, RoleOutput, StrictModel, TradeProposal

RUN_SCHEMA_VERSION = "run-1"
#: Marks a run whose research files are mandatory and strictly schema-checked.
ANALYSIS_SCHEMA_VERSION = "research-1"


def _sha256(path: Path) -> str:
    return sha256_file(path) if path.is_file() else ""


def config_fingerprint(config: ProjectConfig) -> dict[str, str]:
    """Hash the deterministic rule files a run depends on."""

    targets = {
        "risk": config.root / "configs" / "risk.yaml",
        "market_rules": config.root / "configs" / "market-rules.yaml",
        "team": config.root / "configs" / "team.yaml",
    }
    return {name: _sha256(path) for name, path in targets.items()}


def build_input_binding(
    *,
    config: ProjectConfig,
    datapack: Path,
    manifest: dict[str, Any],
    prompt_hashes: dict[str, str],
    trade_context: TradeContext,
    mode: str,
    include_news: bool,
) -> dict[str, Any]:
    """Bind a run to the exact bytes, rules, prompts and account context it used."""

    content_hash = manifest.get("content_hash")
    if not content_hash:
        raise ValueError(
            "数据包缺少 content_hash（未冻结）；请用 analyze 重新生成，或先导入为冻结数据包"
        )
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "snapshot_id": manifest.get("snapshot_id"),
        "content_hash": content_hash,
        "manifest_sha256": _sha256(datapack / "manifest.json"),
        "prompt_hashes": prompt_hashes,
        "schema_versions": {
            "run": RUN_SCHEMA_VERSION,
            "role": "RoleOutput",
            "decision": "FinalDecision",
        },
        "calc_rules_version": manifest.get("calc_rules_version"),
        "price_basis": manifest.get("price_basis"),
        "rule_fingerprints": config_fingerprint(config),
        "account_context": asdict(trade_context),
        "mode": mode,
        "news_enabled": include_news,
        "symbol": manifest.get("ts_code"),
        "asof": manifest.get("asof"),
        "datapack_dir": str(datapack),
    }


def verify_input_binding(run: dict[str, Any], root: Path | None = None) -> list[str]:
    """Check that the frozen inputs still match the recorded binding."""

    binding = run.get("input_binding") or {}
    datapack = Path(binding.get("datapack_dir") or run.get("data_pack_dir") or "")
    if not binding:
        return ["运行记录没有 input_binding，无法证明输入与规则未变"]
    problems = list(verify_snapshot(datapack, binding.get("content_hash")))
    manifest_path = datapack / "manifest.json"
    if (
        manifest_path.is_file()
        and binding.get("manifest_sha256")
        and sha256_file(manifest_path) != binding["manifest_sha256"]
    ):
        problems.append("manifest.json 与冻结时不一致")
    if root is not None:
        for name, expected in (binding.get("rule_fingerprints") or {}).items():
            path = {
                "risk": root / "configs" / "risk.yaml",
                "market_rules": root / "configs" / "market-rules.yaml",
                "team": root / "configs" / "team.yaml",
            }.get(name)
            if path is None:
                continue
            actual = _sha256(path)
            if expected and actual != expected:
                problems.append(f"规则文件在运行后被修改：configs/{path.name}")
    return problems


def _role_plan(config: ProjectConfig, include_news: bool) -> list[dict[str, Any]]:
    schema_types: dict[str, type[StrictModel]] = {
        "research-manager": DecisionPlan,
        "trader": TradeProposal,
        "risk-manager": FinalDecision,
    }
    plan: list[dict[str, Any]] = []
    for role in config.team.roles:
        if role.id == "news-analyst" and not include_news:
            continue
        if not role.enabled and role.id != "news-analyst":
            continue
        schema = schema_types.get(role.id, RoleOutput)
        prompt_path = config.root / "prompts" / f"{role.id}.md"
        prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""
        plan.append(
            {
                "role_id": role.id,
                "phase": role.stage,
                "prompt": prompt,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "schema": schema.model_json_schema(),
                "output": f"agents/{role.stage}/{role.id}.json",
            }
        )
    return plan


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
    plan = _role_plan(config, include_news)
    prompt_hashes = {item["role_id"]: item["prompt_sha256"] for item in plan}
    binding = build_input_binding(
        config=config,
        datapack=datapack,
        manifest=manifest,
        prompt_hashes=prompt_hashes,
        trade_context=trade_context,
        mode=mode,
        include_news=include_news,
    )
    if state_path.is_file():
        existing: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8"))
        if existing.get("execution_backend") != "current_agent":
            raise ValueError("Run ID belongs to a different backend; use a new run ID")
        previous = existing.get("input_binding") or {}
        for key, label in (
            ("snapshot_id", "snapshot"),
            ("symbol", "symbol"),
            ("asof", "asof"),
            ("content_hash", "输入文件内容"),
            ("mode", "mode"),
            ("news_enabled", "news flag"),
            ("account_context", "账户上下文"),
            ("prompt_hashes", "角色提示词"),
            ("calc_rules_version", "计算规则版本"),
        ):
            if previous.get(key) != binding.get(key):
                raise ValueError(
                    f"Run inputs changed ({label}); use a new run ID instead of silently "
                    "replacing prior work"
                )
        problems = verify_input_binding(existing, config.root)
        if problems:
            raise ValueError(
                "冻结输入已变化，旧结论不得复用：" + "；".join(problems)
            )
        # Resume returns existing work without overwriting conclusions or changing inputs.
        return existing

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
        "prompt_hashes": prompt_hashes,
        "input_binding": binding,
        "capability_flags": {"news": include_news, "position_size": False,
                             "trade_instructions": False},
        "external_model_calls": 0,
        "schema_version": RUN_SCHEMA_VERSION,
        "analysis_schema": ANALYSIS_SCHEMA_VERSION,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    request = {
        "run": state,
        "roles": plan,
        "web_verification_requested": allow_web,
        "input_binding": binding,
    }
    (run_dir / "current_agent_request.json").write_text(
        json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    task = f"""# 当前 agent 个股精读任务

标的：{state['stock_name']}（{state['symbol']}），数据截止 {state['asof']}。
状态：{state['status']}。准备任务成功不等于分析完成。

使用当前聊天会话实际采用的大模型直接分析；不调用 DeepSeek、Ollama 或其他模型 API，
不读取它们的密钥，不自动退回批量研究。终端不能直接调用当前会话模型；请当前 agent
继续执行本任务。多个角色是分析视角，不代表多名独立研究者或独立模型投票。

## 输入（已冻结）

- 数据目录：`{datapack}`
- 内容哈希：`{binding['content_hash'][:16]}…`，manifest 哈希：`{binding['manifest_sha256'][:16]}…`
- 先读 `manifest.json`、`DATA_DIGEST.md`、`evidence.json`、`financial_quarters.csv`、
  `earnings_bridge.json`、`financial_adjustments.json`，交叉核对数值与文字。
- 本目录 `current_agent_request.json` 已冻结启用角色的提示词、Schema、输出路径和交易上下文。
- 补证写入本运行目录的 `supplement/`，不得修改数据包；改动数据包会使本次运行失效。
- 联网核实请求：{allow_web}。必要核实使用当前 agent 可用工具，保留来源和披露时点；
  需要新权限时按当前会话权限处理，不将数据另发给模型 API。

## 执行与交付

1. 按 phase1 领域分析 → phase2 多空观点与研究主管 → phase3 交易方案 → phase4 风险评议与裁决推进。
2. 保存各角色 JSON/Markdown。事实必须引用真实证据 ID；推断标明依据、假设及反例。
3. 研究问题写入 `research_questions.json`（最多 5 个决定性问题），论点写入 `claims.json`，
   结论写入 `research_decision.json`；三者格式见 `docs/implementation/schemas.md`。
4. 缺失不当作零，过期不当作最新；不能验证的问题保留为数据缺口。识别到输入错误应留痕更正。
5. 资金与持仓未提供时不虚构金额、股数和账户仓位。不宣称识别或使用了无法确认的模型版本。
   执行状态为 actionable 时，target_account_weight / sell_fraction_of_current / cash_amount
   各自依赖的账户输入必须真实存在（见 `execution.requires`），否则只能写 blocked。
6. 补齐产物后运行 `trade-council submit --run-id {run_dir.name}` 推进状态（不要手改
   run.json），再运行 `trade-council validate --run-id {run_dir.name}`，通过后运行
   `trade-council finalize --run-id {run_dir.name}`。缺少任一角色或三份研究文件都会失败。
7. 补证用 `research import-evidence`，修订用 `research revise`（必须写 affected_outputs），
   复核用 `research review`（一次只销一条 output@revision 或 role@revision）。修订未复核时，
   受影响维度必须降级为 unresolved，写过相关论点的角色也要重审，否则 validate 失败。
   要改财务口径（税率、官方补录）用 `research recompute`，不要动数据包。
   报告第 2 节「公司如何赚钱」只认 claims.json 里 topic=business 的论点，
   没有登记就如实写没回答，不要用财务环比代替。
8. 不伪造 API 调用记录、token 计数或费用；当前会话用量不可得时注明不可得。
"""
    (run_dir / "CURRENT_AGENT_TASK.md").write_text(task, encoding="utf-8")
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state
