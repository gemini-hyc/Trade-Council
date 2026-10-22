# Trade-Council Architecture

当前为 Phase 9 试运行与验收阶段，包含配置、数据、模型、编排、决策规则、报告门禁、
运行查询、成本汇总、导出、保留策略和真实样本试运行记录。

## 主线与模型分支

主线的核心不是某个模型，而是可替换的模型接口、证据、状态机和门禁。

- DeepSeek deep：个股精读和可执行研究结论的主线 profile。
- 本地 batch：快速筛选、批量初筛、Schema 冒烟和离线演练分支。
- 本地 batch 不产生可执行交易指令；越权动作由确定性策略降级。
- 本地模型兼容性问题只在分支范围内处理，不作为主线功能扩张的理由。

## 已实现

```text
configs/      app + team config
data/         dataset registry + local provider
engineering/  datapack and deterministic indicators
llm/          providers, routing, retry, cost, JSON output
orchestration/ asyncio workflow, run state, role caching
decision/      risk rules and decision consistency
reporting/     Markdown/HTML rendering and audit gates
operations/    run inspection, cost summary, export and retention
methods/      method package discovery
src/          Pydantic schemas, loader, CLI
tests/        config and extension tests
```

## 已实现的数据约束

- as-of 截止时间
- 两融 T+1 发布滞后
- 北向字段级可用窗口
- 公告和新闻分离
- 财务按公告日过滤
- 新闻默认关闭
- 数据缺口状态
- 内容寻址证据 ID
- RSI 双口径
- 主力净额公式
- 价格双口径转换
- Ollama/DeepSeek Provider
- batch/deep 模型 profile
- DeepSeek auto/flash/pro 路由
- 模型 digest 和成本账本
- 12 角色 Prompt 与并行/串行编排
- quick 确定性筛选
- `DEGRADED` 和失败续跑
- 决策一致性、tradability 和 T+1 检查
- 资金、持仓状态、建仓日期和板块申报单位
- 确定性股数取整与单票金额上限校验
- Prompt/Code/Hybrid 方法包通用运行层
- 方法需求预检、Schema 校验和证据引用校验
- `method_results.json` 与方法版本内容哈希
- 自包含报告和 R0～R9 门禁

## 尚未实现

- Tushare 写入 provider
- 真实市场黄金样例和模型决策一致率基线
- 回测、组合分析和 Web UI

方法包运行流程：

```text
manifest 发现
  -> team.yaml 启停与角色挂载
  -> 数据需求预检
  -> Prompt 注入 / compute.py 子进程执行
  -> 输出 Schema 与证据引用校验
  -> MethodResult 标准化
  -> 角色输入、method_results.json 和审计
```

新增方法只修改 `methods/` 与 `configs/team.yaml`，不修改
`orchestration/`。

## 运行产物与运维

`run.json` 记录运行状态、snapshot、Prompt 哈希、模型 digest、失败阶段和成本；
`model_calls.jsonl` 记录每次模型调用；`run.log` 记录阶段日志；
`costs.json` 保存运行成本快照。

`runs list`、`runs inspect`、`runs costs` 提供本地查询；
`runs cleanup` 默认只预览，只有 `--apply` 才删除；
`export` 生成 `<股票中文名>-<YYYYMMDD>.html`。

后续阶段必须遵守主计划中的 as-of、DEGRADED、方法隔离和 gate 设计。
