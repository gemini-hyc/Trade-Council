# 完善计划实施验收记录

日期：2026-11-17。计划来源：`Trade-Council完善实施计划.md`（T00–T07）。
基线：HEAD `bdb38ab`，86 项测试通过，ruff / mypy 通过。

## 0. 结论摘要

- 完成 T00–T07，共 144 项测试通过，ruff 与 mypy（61 源文件）通过。
- 两家公司的正式路径都已跑通：江西铜业（600362.SH）与扬农化工（600486.SH），
  均完成 `analyze → 当前 agent 分析 → validate PASS → finalize PASS`。
- 未做：接入 Chan 候选、宏观仓位接口、回测、批量选股（计划 M4，本轮范围外）。
- 未解决的真实研究问题已在两份报告里逐条登记（见 §5）。
- GitHub 同步状态见 §7。

## 1. 任务清单

| ID | 任务 | 状态 | 提交 |
|---|---|---|---|
| T00 | 基线、兼容与实施纪律 | 完成 | （工作区记录，见 `docs/implementation/baseline.md`） |
| T01 | 先修会误导研究的确定性错误 | 完成 | `71ea7f6` |
| T02 | 冻结输入与统一完成入口 | 完成 | `0192918` |
| T03 | 财务底座正式模块（单季/桥接/三类盈利） | 完成 | `5a3090c` |
| T04 | 问题驱动补证、缺口分类、论点修订传播 | 完成 | `b8aa60c` |
| T05 | 研究判断、技术时机与账户动作分离 | 完成 | `b8aa60c`、`9097d49` |
| T06 | 报告由同一份结构化结果生成 | 完成 | `14effa5` |
| T07 | 验收样例、错误用例、文档与同步 | 完成（同步见 §7） | `9097d49`、`e697417`、本次文档提交 |

补充修复（实施过程中发现）：`e697417` 修可用时点跨格式比较与数据包哈希自指。

## 2. T01 修掉的确定性错误

| 问题 | 修法 | 验证 |
|---|---|---|
| 均线升序被标“完全多头” | 逐对严格相邻比较；相等/缺值/方向翻转 → 混合或不可判；文案比较符随方向变化 | `tests/test_deterministic_fixes.py`、`test_indicators.py` |
| ROE 混称官方加权 ROE | 改名“期末净资产口径简单年化 ROE 估算”，写明不是官方值 | 同上 |
| 现金流/归母“>100% 即健康” | 分母口径分别说明，去掉健康结论 | 同上 |
| 价格口径未声明 | 摘要与报告都标“未复权/前复权”及用途 | 同上 |
| 固定风险雷达 + 无定义综合分 | 删除固定雷达；只有显式评分卡（权重/公式/缺失处理）才出分 | `test_deterministic_fixes.py::test_no_scorecard_no_radar` |
| 文本证据进 float 比对、缺失显示 0 | `value_type` 类型化，文本渲染为 `evidence-text` | `test_reporting.py`、审计层 |
| 审计 FAIL 仍标 AUDITED 并交付 | FAIL 只写 `report.draft.*`，状态 `AUDIT_FAILED` | `test_reporting.py` |

## 3. T02 冻结与完成入口

- `data/snapshot.py`：`snapshot_files.json` 逐文件 sha256 + `content_hash`；`snapshot.json`/
  `manifest.json`/`snapshot_files.json` 不进内容哈希（避免自指）。
- `orchestration/current_agent.py`：`input_binding`（snapshot/content_hash/manifest 哈希/
  prompt 哈希/calc_rules_version），`resume` 逐项比对。
- `cli.py`：`validate` / `finalize` 两个子命令；`report --datapack` 变可选；
  外部数据包按 `<snapshot>-adopted` 落盘，不再覆盖 `runs/datapacks/<code>`。
- 数据包生成后自校验：江西铜业与扬农化工均 `verify() == []`。

## 4. T03 财务模块

`engineering/financials.py`（`CALC_RULES_VERSION=financials-1`）：

- 单季差分仅在同年度相邻季进行；缺上一季 → `missing_predecessor`，不倒推；
  重述版本不一致 → `restated_incomparable`；存量科目不差分。
- 盈利桥接三条线（利润总额/净利润/归母）1 元绝对容差，未单列项目留 `unlisted_net` 残差。
- 三类盈利分列：报表归母、官方扣非（公告累计按季差分）、分析调整情景。
- 调整情景：23% 统一税率 + 固定实际少数股东；利润总额/资产减值/少数股东/所得税
  任一缺失 → 该季调整归母标不可得并记录 `adjusted_unavailable_reason`。

江西铜业锚点复核（亿元，全部一致）：

| 口径 | 2026Q1 | 2026Q2 | 2026Q3 |
|---|---|---|---|
| 报表归母 | 28.1756 | 58.1426 | 30.5474 |
| 官方扣非（单季） | 31.2632 | 44.2987 | 26.8286 |
| 剔资产减值情景 | 39.1235 | 69.7188 | 18.1666 |
| 再剔信用减值情景 | 39.0302 | 69.9931 | 18.1773 |

桥接对账 81 项通过、0 失败、0 不可得。

## 5. T04 / T05 研究工作流

- `research import-evidence`：校验股票/期间/单位/可得时点/来源定位，本地文件留 sha256，
  未来数据直接拒绝，重复导入去重不累加，网页正文落库为不可信文本。
- `research revise`：写 `revisions.jsonl`，依赖论点自动转 `unresolved`，
  受影响输出进入 `pending_review_outputs`。
- 缺口四分类 + 按数据类型时效规则（行情/财报/股东/质押/预告/新闻各不相同）。
  关键缺口未解决时，受影响维度必须写 `unresolved`。
- 执行语义：空仓不得输出卖出、比例必须在 0~1、未知可交易状态不得 `actionable`、
  未复核修订前不得 `actionable`；规则树同 `rule_id` 冲突、成交量/成交额混用、
  日线数据冒充 14:50 分时、减仓批次超 100% 全部失败。

## 6. T06 报告

`reporting/view_model.py` 生成唯一数据源，`report.md`、`report.html`、`report_view.json`
同源渲染。主文 6 段，附录保留全部启用角色（本次 11 个）、多空论点、失效论点、
更正记录、缺口与证据索引。无结构化估值参照时不给“合理价”；缺账户资金显示“未提供”。

## 7. 两家公司实跑

| 项目 | 江西铜业 600362.SH | 扬农化工 600486.SH |
|---|---|---|
| 运行目录 | `runs/jxc-20261113/` | `runs/yn-20261113/` |
| 快照 | `8ea685a82e90b684` | （见 run.json） |
| 校验 | `validate` PASS（1 WARN：q1 未决） | `validate` PASS（1 WARN：q1 未决） |
| 封存 | `finalize` FINALIZED，audit PASS | `finalize` FINALIZED，audit PASS |
| 交付物 | `reports/江西铜业-20261113.html` | `reports/扬农化工-20261113.html` |
| 结论 | HOLD／观察，执行 blocked | HOLD／观察，执行 blocked |

江西铜业验收事实（计划 11.1 B）：

- Q3 单季毛利率 3.60% < Q2 6.45%，归母环比 -47.46%，毛利额减少 59.61 亿元。✓
- Q3 资产减值 +10.80 亿元为转回，是缓冲项而非下滑原因。✓
- 报告明确写“调整后单季利润不是可持续年利润，不得乘以 4 年化”。✓
- 账户未知 → 无任何仓位/目标价数字，执行状态 blocked。✓

第二家公司验证的“无硬编码 + 局部降级”：

- 官方扣非数据集对该股零记录，报告如实显示“缺失”，其余分析照常完成。✓
- 该公司无历史业绩预告记录，缺口按 `NOT_EXTRACTED` 记录并写明检查范围。✓
- 同一套代码与流程，未出现股票代码特判。✓

## 8. 错误用例 A01–A18

| ID | 覆盖位置 | 断言要点 |
|---|---|---|
| A01 | `test_finalization.py::test_missing_role_fails_and_names_it`、`test_unknown_role_field_fails` | 报出角色名与拼错字段 |
| A02 | `test_tampered_datapack_blocks_reuse` | `INPUT_TAMPERED`，禁止复用旧结论 |
| A03 | `test_financials.py`（缺 Q2） | 单季不可得，不差出六个月 |
| A04 | `test_financials.py`（重述） | `restated_incomparable` |
| A05 | `test_finalization.py::test_future_evidence_fails` | `EVIDENCE_FUTURE` |
| A06 | `test_financials.py`（存量科目） | 不做差分 |
| A07 | `test_acceptance_cases.py::test_a07_*` | 不重复剔除；缺少数股东 → 不可得 |
| A08 | `test_research.py::test_revision_propagates_*`、`test_acceptance_cases.py::test_a08_*` | 撤回传播 + 旧目标价不得沿用 |
| A09 | `test_deterministic_fixes.py`（文本证据） | 文本按文本渲染 |
| A10 | `test_acceptance_cases.py::test_a10_*` | `expected_return` 被 Schema 拒绝 |
| A11 | `test_acceptance_cases.py::test_a11_*` | `RULE_CONFLICT` / `INDICATOR_MISMATCH` |
| A12 | `test_acceptance_cases.py::test_a12_*` | 40/25/25/10 通过，105% `TRANCHE_OVERFLOW` |
| A13 | `test_acceptance_cases.py::test_a13_*` | `RULE_NEEDS_INTRADAY`，不许 actionable |
| A14 | `test_acceptance_cases.py::test_a14_*` | 缺 capital 仍 PASS，仓位不生成 |
| A15 | `test_acceptance_cases.py::test_a15_*` | 股息假设 WARN，确定现金流字段被拒 |
| A16 | `test_finalization.py::test_export_requires_finalized_report` | 未 FINALIZED 时 export 返回 2 |
| A17 | `test_acceptance_cases.py::test_a17_*` | 不实例化 provider，无 `llm_calls.jsonl` |
| A18 | `test_acceptance_cases.py::test_a18_*` | 重复 finalize 哈希一致、无重复追加 |

## 9. 检查结果

```
144 passed
ruff check .        All checks passed!
mypy src            Success: no issues found in 61 source files
```

基线对照：86 → 144 项测试（新增 58 项）。

## 10. 实际缺口与后续事项

仍未解决（已在报告内登记，不是本轮缺陷）：

1. 江西铜业 q1（毛利率是否见底）、q2（分部毛利拆分）、q3（减值转回可持续性）未决；
   分部报告本地库无字段，需从三季报附注取。
2. 两家公司都缺最新业绩预告核实（本地数据到 2026-10-30，个股级记录陈旧或为零），
   本轮未联网，按要求标为 `NOT_EXTRACTED`，不是“源侧未披露”。
3. 江西铜业 2025 年及以前官方扣非未补录；扬农化工官方扣非完全未补录，
   因此“官方扣非”列显示缺失，相关分位比较只能停在报表口径。

后续（计划 M4，本轮不做）：

- T08A 接 Chan 候选（小接口，不扩成平台）。
- T08B 宏观/仓位上限先手动输入，不由新闻分数自动生成仓位。
- T09 轻量决策记录，不做回测平台。

## 11. Git 与同步

- 本地提交序列：`71ea7f6` → `0192918` → `5a3090c` → `b8aa60c` → `14effa5` → `9097d49` → `e697417` → 本次文档提交。
- 远端同步：已完成（见 §12）。

## 12. 同步结果

第一次失败原因（已定位，非仓库问题）：

```
git fetch origin
→ fatal: unable to access ...: Failed to connect to 127.0.0.1 port 58731
```

`~/.gitconfig` 里 `http.proxy` 指向已失效的 `127.0.0.1:58731`，而当前会话的代理是
`127.0.0.1:50013`；https 通道还缺少可非交互读取的凭证（osxkeychain 里没有该仓库条目）。

实际处理：改用 SSH 通道推送，未改 remote 配置、未强推、未改写历史：

```
git ls-remote git@github.com:gemini-hyc/Trade-Council.git   # 连通
git push git@github.com:gemini-hyc/Trade-Council.git main:main
→ bdb38ab..9da1b75  main -> main（快进）
```

`bdb38ab` 是本次实施前远端 HEAD，`9da1b75` 是本次全部代码与验收文档的提交。
本条同步记录所在的文档提交在同一分支上，随后以同样方式推送。

遗留配置问题（不属于本次代码范围）：建议把 `~/.gitconfig` 的 `http.proxy` 改为当前
代理端口或删除，并为 https 通道配置可用的凭证方式，否则后续 https 操作仍需改走 SSH。
