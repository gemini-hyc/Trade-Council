# Trade-Council 当前执行约定

用户已明确决定（2026-11-16）：

- 暂停批量研究，不运行 batch/quick，不自动安排批量任务。
- 个股完整精读由正在服务用户的当前 agent，使用当前会话的大模型直接分析。
- 不为了精读另调 DeepSeek、Ollama 或其他模型 API，不索取或读取这类服务的 API Key。
- 多角色是同一 agent 的研究视角；不冒称独立研究者、独立模型共识或 API 调用。

## 开始一次精读

1. 核对股票、截止日期、原始数据时点，使用项目 CLI 准备本地数据和角色任务。
   `analyze <代码> --profile deep --mode full --asof <日期>` 默认生成
   `CURRENT_AGENT_TASK.md`、`current_agent_request.json`，状态为 `AWAITING_CURRENT_AGENT`。
   数据包在这一步被冻结（`snapshot_files.json` + `content_hash`），运行记录写入 `input_binding`。
2. 当前 agent 继续读取任务、`DATA_DIGEST.md`、manifest、evidence 与财务产物；按冻结的角色提示词
   分析、多空论证、提出方案并进行风控。终端进程不会调用聊天模型，不能只准备任务就宣称完成。
3. 有必要时使用当前会话的检索工具核实公告、行业与政策事实，保留来源、披露日期及不确定性。
   找到的新数字用 `research import-evidence` 落库，不要手改数据包。
4. 角色与最终结论按现有 Schema 保存；事实引用真实证据 ID，推断明确标注。
   还必须写三份研究结构：`research_questions.json`（最多 5 个决定性问题）、`claims.json`
   （带状态与依赖）、`research_decision.json`（研究判断与账户动作分开）。
5. 未提供资金、持仓及建仓日时，保留缺口，不代填账户信息；`execution.status` 写 `blocked`
   并列出 `block_reasons`，公司研究仍可完成。
6. 论点被推翻或证据不足时，用 `research revise` 记录修订。撤回会传播到依赖它的论点，
   并把受影响输出标为待复核。
7. 完成实际分析后依次运行 `validate` 与 `finalize`。`finalize` 通过才会写 `report.html`
   与 `finalize.json`；审计失败只留 `report.draft.*`。不要在终端之外手改 `run.json` 的完成状态。
8. 不伪造 token 用量、费用、模型版本或已完成阶段。

`configs/app.yaml` 是 CLI 执行策略来源。`configs/models.yaml` 保留历史 API 配置以便
理解旧运行；当前模式不会据此选择服务或发起模型请求。恢复 API/批量执行属于后续另行修改。

## 需要写进运行目录的东西

| 文件 | 作用 | 谁写 |
|---|---|---|
| `agents/phase*/<role>.json` | 角色产物，字段受 Schema 约束 | 当前 agent |
| `decision.json` | 最终裁决（FinalDecision） | 当前 agent |
| `research_questions.json` | 决定性问题、已尝试路径、状态 | 当前 agent |
| `claims.json` | 论点、类型、状态、依赖 | 当前 agent |
| `research_decision.json` | 基本面/估值/时机判断 + 执行状态 | 当前 agent |
| `data_gaps.json` | 缺口分类（未取成/未披露/过期/不适用） | 当前 agent |
| `trading_rules.json` | 结构化规则树与减仓批次（可选） | 当前 agent |
| `revisions.jsonl` | 修订与失效传播记录 | `research revise` |
| `supplemental_evidence.json` | 补证（含来源定位与文件哈希） | `research import-evidence` |
| `validation.json` / `finalize.json` | 机器校验与封存记录 | `validate` / `finalize` |
| `report_view.json` | 报告的唯一数据来源 | `finalize` |

## 开发检查

修改后运行 pytest、ruff、mypy；测试不得访问生产模型 API。保留用户未提交及未跟踪的工作。

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q --basetemp=.pytest-tmp
rm -rf .pytest-tmp            # 先删临时目录再跑 ruff，否则夹具代码会产生假 I001
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src
```
