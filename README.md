# Trade-Council

本地数据与证据驱动的多角色投研工作台。当前主线：准备本地数据 → 当前聊天 agent 直接精读 → 多空论证 → 风险评议 → 证据核查与报告。

## 当前执行方式（2026-11-16）

- 暂停批量研究；不运行 batch/quick。
- 个股完整精读使用当前 agent 所在会话的大模型。用户正在和哪个 agent 工作，就由该 agent 完成分析。
- 不另行调用 DeepSeek、Ollama 或其他模型 API，不要求这些服务的密钥。
- 角色提示词、数据包、证据 ID、确定性交易规则和报告审计继续复用。
- 当前 agent 的多个研究角色是不同视角，不宣称是独立模型的共识。

执行策略在 `configs/app.yaml`：

```yaml
default_profile: deep
analysis_backend: current_agent
batch_research_enabled: false
```

`configs/models.yaml` 保留历史 API 参数；当前主线不会使用这些服务。即使传入旧的 `--allow-cloud` 参数，也不会覆盖当前执行策略。`models check` 不连接供应商，`models probe` 在当前模式下拒绝执行。

## 使用

在项目 Python 环境中安装后运行 `trade-council`；已有虚拟环境尚未安装包时，可在仓库内使用：

```bash
PYTHONPATH=src .venv/bin/python -m trade_council config validate
PYTHONPATH=src .venv/bin/python -m trade_council analyze 600362.SH --asof 2026-11-13
PYTHONPATH=src .venv/bin/python -m trade_council validate --run-id <run_id>
PYTHONPATH=src .venv/bin/python -m trade_council finalize --run-id <run_id>
```

`analyze` 准备本地数据，生成：

```text
runs/<run_id>/run.json                    状态 AWAITING_CURRENT_AGENT
runs/<run_id>/CURRENT_AGENT_TASK.md       当前 agent 的执行任务
runs/<run_id>/current_agent_request.json  冻结的角色提示词、Schema、输出路径
runs/datapacks/<代码>/                    数据摘要、manifest、evidence、明细
```

数据包在这一步被冻结（`snapshot_files.json` + `content_hash`），运行记录写入 `input_binding`；之后改动数据包会让运行失效。

**终端程序无法自行调用当前聊天会话的大模型。** 在聊天中让当前 agent 继续执行生成的任务。它需要实际读取数据，完成各角色分析及核查，保存产物并交付报告。仅生成上述文件不表示分析完成，尚未完成时 `report` 命令会拒绝生成最终报告。

完成分析后必须写 `research_questions.json`、`claims.json`、`research_decision.json`（可选 `data_gaps.json`、`trading_rules.json`），再依次跑 `validate` 与 `finalize`。`finalize` 通过才写正式报告，审计失败只留草稿。补证与修订用 `research import-evidence` / `research revise`，字段见 `docs/implementation/schemas.md`。

常用参数：`--datapack <路径>`、`--run-id <ID>`、`--resume`、`--include-news`、`--capital`、`--position-state`、`--position-shares`、`--position-opened-date`。不填写账户信息也能做研究，具体股数与账户仓位保留为未知。

`--allow-web` 在当前模式下记录联网核实需求，查询由当前 agent 通过会话工具完成，不启动项目 API 工作流。当前 agent 仍遵守会话权限与用户约定。

## 验证与历史能力

```bash
PYTHONPATH=src .venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/mypy
```

项目保留历史 API 工作流代码、数据时点过滤、Schema、方法包机制、风控规则及报告审计，用于兼容和回归；当前主线无需启动 API 工作流。黄金样例与历史审计成绩验证软件行为，不代表选股收益。

实施验收记录见 `docs/implementation/acceptance.md`，操作说明见 `docs/operations.md`，agent 执行约定见 `AGENTS.md`，历史评估见 `docs/evaluation.md`。
