# Trade-Council 操作手册

## 1. 当前策略

`configs/app.yaml`：`analysis_backend: current_agent`，`default_profile: deep`，`batch_research_enabled: false`。

个股精读由当前聊天 agent 直接完成，不调用独立模型 API，不读取 DeepSeek 等服务的密钥。批量研究暂停。`configs/models.yaml` 中的服务配置用于历史 API 工作流兼容，不是当前模型路由。

## 2. 准备单股任务

仓库已有 Python 环境时：

```bash
PYTHONPATH=src .venv/bin/python -m trade_council analyze 600362.SH --asof 2026-11-13
```

默认 profile=deep、mode=full。准备命令返回 0 仅表示任务准备成功，`run.json` 的状态为 `AWAITING_CURRENT_AGENT`；它没有完成模型分析、没有生成交易结论，也没有调用 DeepSeek。

读取该运行目录下的 `CURRENT_AGENT_TASK.md`，由当前 agent 继续执行，冻结的角色提示词和 Schema 位于 `current_agent_request.json`。

只准备数据可用独立 `data` 命令或 `analyze --mode data`，后者状态为 `DATA_READY`。

```bash
PYTHONPATH=src .venv/bin/python -m trade_council data 600362.SH --asof 2026-11-13 --out /tmp/jiangxi-datapack
```

如需复用已有数据包并保留其修正记录，显式传入 `--datapack <目录> --resume`。续接已有运行须保持股票和 snapshot 相同；更换输入使用新 run ID。

## 3. 当前 agent 的分析职责

- 核对数据时点、单位、财务口径、数据缺口及数值与文字是否一致。
- 按阶段完成领域分析、多空论证、研究主管、交易方案、风险评议和最终裁决。
- 角色结果满足现有 Schema，事实能回指证据。需要补充核实时使用当前会话工具并记录来源。
- 没有资金与持仓时，不填写具体股数、金额或账户仓位。
- 保存角色 JSON/Markdown、decision.json、真实的运行与检查结果；不能把准备状态改名充当完成。
- 完成实际分析和确定性风控后，更新状态并运行现有报告构建与审计；未完成或审计失败不交付可执行结论。
- 不编造 API token、费用或当前 agent 不可确认的模型版本。

## 4. 查询与报告

```bash
PYTHONPATH=src .venv/bin/python -m trade_council models list
PYTHONPATH=src .venv/bin/python -m trade_council models check
PYTHONPATH=src .venv/bin/python -m trade_council models route --profile deep --role trader
PYTHONPATH=src .venv/bin/python -m trade_council runs list
PYTHONPATH=src .venv/bin/python -m trade_council runs inspect --run-id <run_id>
PYTHONPATH=src .venv/bin/python -m trade_council report --run-id <run_id> --datapack <目录>
PYTHONPATH=src .venv/bin/python -m trade_council export --run-id <run_id> --out-dir <交付目录>
```

`models check` 只确认当前 agent 模式，不连接外部模型。`models probe` 被禁用；`--allow-cloud` 不会覆盖当前策略。`report` 在待分析或仅数据就绪状态下拒绝执行。

当前会话的计费和 token 数不能由此 CLI 测得；历史 `runs costs` 统计的是项目 API 日志，不代表当前聊天的用量。

## 5. 历史工作流

原 batch/DeepSeek API 工作流及其参数保留用于兼容和测试。恢复它们需要另行明确修改执行策略；当前任务不得自动回退、索取密钥或安排批量调度。

复用历史报告前检查截止日期、运行后端和数据缺口。旧运行的审计通过不等于收益验证通过。
