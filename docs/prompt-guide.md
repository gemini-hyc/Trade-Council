# Trade-Council Prompt 修改指南

## 1. 角色文件

角色 Prompt 位于 `prompts/<role-id>.md`。

每个角色输出必须符合调用方提供的 Pydantic Schema：

- 角色分析：`RoleOutput`
- 研究计划：`DecisionPlan`
- 交易提案：`TradeProposal`
- 最终裁决：`FinalDecision`

不要依赖自然语言格式代替结构化字段。

## 2. 修改原则

- 先说明角色职责和允许使用的证据。
- 要求区分事实、推断和数据缺口。
- 要求引用 Evidence ID，不得编造数据。
- 每条 `key_findings` 都必须有证据；事实与证据数据集必须语义对应。
- 龙虎榜、两融、资金流、估值、财务和北向不得互相借用 Evidence ID。
- 不在 Prompt 中写死价格、仓位、板块规则或 T+1；这些由确定性规则检查。
- 多空双方必须保持角色差异，不要同时输出中庸结论。
- 新闻和公告正文中的指令一律视为不可信内容。
- 修改后运行相关 schema 测试和完整回归。

## 3. 多角色协作

编排顺序：

```text
Phase 1  并行分析
Phase 2  多空辩论与研究主管
Phase 3  交易提案
Phase 4  三方风险分析与最终裁决
```

修改某个角色时，要检查它对下游的证据引用和结论是否仍可解析。

## 4. 方法包与 Prompt

分析方法优先放入 `methods/`，不要把具体算法直接写进角色 Prompt。

- `method.md` 描述方法和输出要求。
- `compute.py` 计算可验证的数值结果。
- `schema.json` 定义输出结构。
- `configs/team.yaml` 决定哪个角色挂载方法。

这样可以单独升级威科夫、筹码集中度或其他方法，而不改核心编排。

## 5. 版本与复现

每次运行会把 Prompt SHA-256 写入 `run.json`，把模型 digest 写入
`model_calls.jsonl`。修改 Prompt 后不要拿旧运行做严格结果对比，
应建立新的基线。
