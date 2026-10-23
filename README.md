# Trade-Council

Trade-Council 是一个本地优先、可审计、可扩展的多角色投研工作台。

当前状态：Phase 9 真实样本试运行与 v1.0 候选验收。

## 主线与分支

- **主线**：本地数据与证据包 → 显式状态机 → DeepSeek deep 个股精读 →
  确定性风控 → 报告审计。v1.0 的产品质量与验收以这条链路为准。
- **本地 batch 分支**：qwen2.5 等本地模型只用于快速筛选、批量初筛、
  Schema 冒烟和离线演练，结论默认不可执行。
- 本地模型输出不合格时，确定性门禁会拒绝或降级，不让分支问题反向污染主线。
- 后续不再以本地模型反复调参作为项目推进主线。

## 已实现能力

- CLI 启动与版本命令
- `app.yaml` 和 `team.yaml` 配置校验
- `data.yaml` 和 dataset capability registry
- Pydantic 配置与方法 Schema
- 方法包发现、启停和引用校验
- 纯 Prompt 示例方法包
- 风控规则表初版
- 本地 `~/quant-data` 只读 provider
- as-of 过滤和 `snapshot_id`
- 公告/新闻分流，新闻默认关闭
- 北向字段级可用窗口
- 两融 T+1 发布滞后
- 财务按公告日过滤
- 个股数据包、`DATA_DIGEST.md` 和 `manifest.json`
- 内容寻址证据 ID、`evidence.json` 和 `evidence_trace.jsonl`
- 指标模块与 Wilder/SMA 双口径 RSI
- 主力净额公式
- 复权分析价与未复权交易价换算
- 角色 JSON 与 Markdown 分文件写出
- Ollama 本地 Provider
- DeepSeek 云端 Provider
- `batch` / `deep` 两套模型 profile
- DeepSeek `auto/flash/pro` 路由
- 模型 digest、token、延迟和成本账本
- JSON 输出解析、修复和重试
- 云端付费调用显式开关
- 12 个角色 Prompt
- asyncio 显式五阶段状态机
- Phase 1 并行分析
- 多空辩论与研究主管裁决
- 交易提案和风险裁决
- `quick` 确定性筛选
- run 状态、角色产物和失败续跑
- 方法子进程沙箱
- `DEGRADED` 和能力位
- `configs/risk.yaml` 风控规则
- `configs/market-rules.yaml` 交易制度规则
- 决策一致性检查
- 可交易性、T+1 与申报单位门禁
- 账户资金、持仓状态和建仓日期输入
- 确定性股数取整、金额上限与 T+1 校验
- 报告展示资金、持仓和交易制度上下文
- 通用 Prompt/Code/Hybrid 方法运行层
- 方法需求预检、输出 Schema 校验和证据引用校验
- 方法版本与内容哈希快照
- 三只合成股票黄金样例和十次重复 Schema 基线
- 数据故障注入与 R9 数字篡改门禁
- Markdown 摘要
- 自包含 HTML 报告
- R0～R9 报告门禁
- 门禁变异测试
- 基础单元测试

后续计划：Tushare 写入 provider、回测、组合分析和 Web UI。

## 快速开始

```bash
uv sync --dev
uv run trade-council --help
uv run trade-council config validate
uv run trade-council methods list
uv run trade-council data 600519.SH --asof 2026-10-16 --years 2
uv run trade-council models list
uv run trade-council models check
uv run trade-council models route --profile deep --role risk-manager --deep-tier auto
uv run trade-council models probe --profile batch --role market-analyst --json
uv run trade-council analyze 600519.SH --mode quick --profile batch
uv run trade-council analyze 600519.SH --mode full --profile batch
uv run trade-council analyze 600519.SH --mode full --profile deep --allow-cloud
uv run trade-council analyze 600519.SH --mode full --profile deep --allow-cloud --allow-web
uv run trade-council analyze 600519.SH --mode full --profile deep --allow-cloud \
  --capital 100000 --position-state flat --board main
uv run trade-council analyze 600519.SH --mode full --profile deep --allow-cloud \
  --capital 100000 --position-state holding --position-shares 500 \
  --position-opened-date 2026-10-20 --board main
uv run trade-council report --run-id <run_id> --datapack <datapack_dir>
uv run trade-council runs list
uv run trade-council runs inspect --run-id <run_id>
uv run trade-council runs costs --run-id <run_id>
uv run trade-council runs cleanup
uv run trade-council export --run-id <run_id>
uv run pytest
uv run ruff check .
uv run mypy
```

## 目录

```text
configs/      应用与团队配置
configs/risk.yaml       确定性风控参数
configs/market-rules.yaml 交易制度参数
methods/      可插拔分析方法包
prompts/      角色提示词（后续阶段填充）
src/trade_council/data/         数据契约与本地 provider
src/trade_council/engineering/  数据包、指标、证据和价格换算
src/trade_council/llm/          Provider、路由、重试、成本和 JSON 解析
src/trade_council/orchestration/ asyncio 状态机、运行状态和角色缓存
src/trade_council/decision/      决策一致性与可交易性规则
src/trade_council/reporting/     Markdown/HTML 报告与门禁
src/trade_council/              其余核心代码
tests/        测试
docs/         架构和规则文档
runs/         本地运行产物（不进入 Git）
```

扩展方法说明见 `docs/method-packages.md`。
日常操作见 `docs/operations.md`，Prompt 修改见 `docs/prompt-guide.md`。
数据事件、任务级补数和质量门禁见 `docs/data-layer.md`。
