# Trade-Council 操作手册

## 1. 安装与配置

```bash
cd /Users/hyc/quant/Trade-Council
uv sync --dev
uv run trade-council config validate
uv run trade-council models check
```

主要配置：

| 文件 | 用途 |
|---|---|
| `configs/app.yaml` | 运行目录、默认模式和日志级别 |
| `configs/data.yaml` | 本地数据根目录和 as-of 时区 |
| `configs/models.yaml` | 本地/云模型、profile、预算和路由 |
| `configs/team.yaml` | 角色、阶段和方法挂载 |
| `configs/risk.yaml` | 资金、仓位和风险收益规则 |
| `configs/market-rules.yaml` | 板块申报单位和 T+1 |
| `configs/retention.yaml` | 运行、数据包和导出保留期 |

## 2. 密钥管理

密钥只从环境变量读取，不写入配置、Prompt 或报告。

```bash
export DEEPSEEK_API_KEY="..."
```

本地 Ollama 默认不需要真实密钥。`.env.example` 只是变量清单，
Trade-Council 不会自动加载 `.env`；使用前需在当前 shell 或密钥管理工具中
显式导出。

云模型只在显式传入 `--allow-cloud` 时调用：

```bash
uv run trade-council analyze 600519.SH \
  --mode full --profile deep --allow-cloud
```

任务级外部数据补充只在显式传入 `--allow-web`，并且配置了
`web_supplement_endpoint` 时启用：

```bash
uv run trade-council analyze 600519.SH \
  --mode full --profile deep --allow-cloud --allow-web
```

外部补充默认只写入本次 run：

```text
runs/<run_id>/data_needs.json
runs/<run_id>/supplemental_data.json
runs/<run_id>/supplemental_evidence.json
```

它不会自动修改 `~/quant-data`。数据库回填必须单独执行并重新校验。

不要将 `.env`、API Key、持仓文件或 `runs/` 提交到 Git。

## 3. 构建数据包

```bash
uv run trade-council data 600519.SH --asof 2026-10-21 --years 2
```

输出默认位于 `runs/datapacks/<代码>/`。

关键查询：

```bash
python -m json.tool runs/datapacks/600519/manifest.json
python -m json.tool runs/datapacks/600519/evidence.json
```

`manifest.json` 记录数据快照和模型运行所需上下文；
`evidence.json` 记录每个证据的值、可用时间和内容寻址 ID。

## 4. 运行分析

快速筛选：

```bash
uv run trade-council analyze 600519.SH \
  --mode quick --profile batch
```

本地批量分析：

```bash
uv run trade-council analyze 600519.SH \
  --mode full --profile batch
```

batch profile 仅用于批量研究，不允许可执行交易指令。若模型仍返回
BUY/SELL，系统会：

- 将最终 action 降级为 `INSUFFICIENT_DATA`。
- 清空入场、止损、目标价和仓位参数。
- 在 `degraded_from_action` 中保留模型原动作。
- 在 `data_gaps` 中记录 profile 拒绝原因。

云端个股精读：

```bash
uv run trade-council analyze 600519.SH \
  --mode full --profile deep --allow-cloud \
  --capital 100000 --position-state flat --board main
```

已有持仓：

```bash
uv run trade-council analyze 600519.SH \
  --mode full --profile deep --allow-cloud \
  --capital 100000 --position-state holding \
  --position-shares 500 --position-opened-date 2026-10-20 --board main
```

完整运行结束后会自动生成报告和审计：

```text
runs/<run_id>/
  run.json
  run.log
  model_calls.jsonl
  costs.json
  decision.json
  method_results.json
  resolved_methods.json
  report.md
  report.html
  audit.json
  agents/phase*/...
```

若最终报告审计为 FAIL，`analyze` 命令返回非零退出码，调度器应视为失败，
不得对外分发该报告。

## 5. 查询运行

```bash
uv run trade-council runs list
uv run trade-council runs inspect --run-id <run_id>
uv run trade-council runs costs --run-id <run_id>
uv run trade-council runs costs --json
```

`runs inspect` 会显示：

- 运行状态和失败阶段。
- 数据 snapshot ID。
- `data_pack_dir`。
- 模型 digest 列表。
- Prompt 哈希。
- 成本汇总和报告审计状态。

## 6. 失败续跑

如果运行在某个阶段失败：

1. 查看 `run.log` 和 `run.json` 中的 `failed_phase`、`error`。
2. 修复配置、数据或网络问题。
3. 使用同一运行 ID 续跑：

```bash
uv run trade-council analyze 600519.SH \
  --mode full --profile batch \
  --resume --run-id <run_id> \
  --datapack runs/datapacks/600519
```

已完成的角色产物和 `method_results.json` 会被复用，不会重复调用成功阶段。

## 7. 对外导出

```bash
uv run trade-council export --run-id <run_id>
```

默认文件名：

```text
<股票中文名>-<YYYYMMDD>.html
```

例如：

```text
贵州茅台-20261021.html
```

默认输出目录为 `exports/`，也可使用 `--out-dir`。

## 8. 保留策略

`configs/retention.yaml` 默认值：

```yaml
normal_run_days: 30
failed_run_days: 14
datapack_days: 7
export_days: 90
keep_latest: 10
```

- 最近 10 次普通运行始终保留。
- `--run-kind golden` 的运行永久保留。
- 失败运行只保留 14 天。
- 普通运行保留 30 天。
- 未被保留运行引用的数据包保留 7 天。
- 导出文件保留 90 天。

先预览，不删除：

```bash
uv run trade-council runs cleanup
```

确认后执行：

```bash
uv run trade-council runs cleanup --apply
```

黄金样例/回归基线：

```bash
uv run trade-council analyze 600519.SH \
  --mode full --profile batch --run-kind golden
```

## 9. 日更调度

v1.0 不内置 scheduler。可以在 macOS `launchd`、cron 或其他任务系统中
调用 CLI。示例：

```cron
30 18 * * 1-5 cd /Users/hyc/quant/Trade-Council && /path/to/uv run trade-council analyze 600519.SH --mode quick --profile batch
```

建议：

- 每个任务使用固定 `--run-id` 或带日期前缀。
- 云调用只在需要精读时开启。
- 日更先跑 data/quick，再决定是否跑 full/deep。
- 调度器只负责启动 CLI，不复制分析逻辑。

## 10. 隐私与云调用

- 本地数据默认不离开机器。
- DeepSeek 请求会发送数据摘要、必要证据、角色上下文和持仓输入。
- 不要把 API Key、完整账户信息或无关个人数据放入 Prompt。
- 新闻和公告正文视为不可信外部内容，不当作系统指令执行。
