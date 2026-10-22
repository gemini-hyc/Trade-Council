# Trade-Council 方法包开发说明

方法包是项目的开放扩展点。威科夫量价、筹码集中度、事件驱动或其他方法
都应通过同一契约接入，而不是把领域逻辑写进编排器。

## 1. 目录结构

```text
methods/<method-id>/
  manifest.yaml
  method.md
  compute.py       # code/hybrid 方法需要
  schema.json
```

## 2. manifest.yaml

```yaml
id: wyckoff-volume-price
name: Wyckoff Volume Price
version: 0.1.0
domain: technical
type: hybrid
enabled: false
requires:
  datasets: [market.daily]
  fields: [close, volume]
outputs:
  schema: schema.json
  evidence_policy: required
parameters:
  lookback: 120
```

- `type=prompt`：`method.md` 作为分析方法说明注入角色。
- `type=code`：执行 `compute.py`，结果进入角色输入。
- `type=hybrid`：先执行代码，再将方法说明和计算结果一起注入角色。
- `enabled` 是默认值，最终启停由 `configs/team.yaml` 覆盖。
- `requires` 在运行前按证据中的 dataset ID 和 field 检查；不满足时方法被
  `skipped`，不会生成伪结果。

## 3. 角色挂载

```yaml
methods:
  wyckoff-volume-price:
    enabled: true

roles:
  - id: market-analyst
    stage: phase1
    domains: [technical]
    methods: [wyckoff-volume-price]
```

一个角色可以挂载多个同域方法。方法结果按 ID 分别保留，并在
`method_context.conflict_groups` 中标记同域并存情况。

## 4. compute.py 契约

输入为标准输入 JSON：

```json
{
  "method_id": "wyckoff-volume-price",
  "parameters": {"lookback": 120},
  "symbol": "600519.SH",
  "asof": "2026-10-16",
  "snapshot_id": "...",
  "data_pack_dir": "/path/to/datapack",
  "evidence": []
}
```

输出必须是 JSON 对象：

```json
{
  "signal": "positive",
  "summary": "放量突破交易区间",
  "evidence_ids": ["F-market-daily-..."]
}
```

约束：

- 不得访问网络。
- 只读取 `data_pack_dir` 和传入证据。
- 输出必须满足 `schema.json`。
- `evidence_policy=required` 时至少引用一个真实 Evidence ID。
- 未知 Evidence ID 会使方法失败，不会进入角色结果。

当前内置 Schema 校验支持常用子集：`type`、`required`、`properties`、
`additionalProperties=false`、`enum`、`minLength`、`minimum`、`maximum`、
`items`。复杂 JSON Schema 关键字应在后续版本补全。

## 5. 方法与证据

方法不应只返回自然语言。建议结构化输出包含：

- `signal`
- `summary`
- `evidence_ids`
- 方法特有的 `zones`、`scores`、`signals` 或 `warnings`

自然语言解释可以保留，但数值、阈值和结论必须能回指证据或方法参数。

## 6. 安全边界

`compute.py` 通过独立子进程运行，并有超时和最小环境变量。这是故障与
依赖隔离，不等同于操作系统级安全沙箱。只应运行本机可信方法包，不要执行
来源不明的代码。

