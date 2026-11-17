# 实施验收记录（T00–T07 返工版）

日期：2026-11-17（返工）。首次验收记录见 git 历史（提交 `9da1b75` 版本的本文件）。
计划来源：`Trade-Council完善实施计划.md`（T00–T07）。
返工依据：`验收复核与返工清单.md`（R1–R8 + 第 4 节 5 项补充验收）。

本轮原则（来自返工清单）：每一项先加入能在返工前 HEAD 失败的回归用例，再修实现；
不删除旧测试；不把 FAIL 改 WARN 通过；不扩展到 M4（Chan、宏观接口）。

## 0. 结论摘要

- R1–R8 全部修复，逐条用例与结果见 §1；第 4 节 5 项补充验收见 §2。
- 测试 144 → 191 项（新增 47 项，其中 `tests/test_rework_acceptance.py` 32 项为 R1–R8 的专用回归）。
  `ruff check .` 全通过，`mypy src` 62 个源文件全通过。
- 两家公司重做了一轮完整循环（新运行目录，旧运行保留为历史）：
  江西铜业 `runs/jxc-20261117`、扬农化工 `runs/yn-20261117`，
  各完成「正式分析 → 补证导入 → 运行级重算 → 论点修订 → 逐条复核 → 重新封存 → 重新导出」。
- 两份新报告：`reports/江西铜业-20261113.html`、`reports/扬农化工-20261113.html`。
- 仍然未解的真实问题留在报告与缺口清单里（§4），没有为了让报告好看而消掉。

## 1. R1–R8 逐条结论

| 编号 | 复核指出的问题 | 返工做法 | 回归用例 | 结果 |
|---|---|---|---|---|
| R1 | 正常准备状态没有合法完成入口 | 完成判定改为看产物；新增 `submit` 子命令（校验角色产物 + decision + 三份研究文件后推进 `DECIDED`） | `test_rework_acceptance.py::test_prepared_run_completes_without_hand_editing_status`、`test_submit_names_what_is_missing` | 通过 |
| R2 | 数据或结论变化后旧报告仍能正式导出 | 新增 `seal-1` 封存（37 项依赖：输入绑定、角色产物、研究文件、revisions、规则树、账户上下文、重算产物）；`export` 前 `verify_seal`，失败拒绝；`--historical` 显式导出并标注文件名 | `test_export_refuses_when_frozen_input_changed`、`test_export_refuses_when_a_conclusion_or_claim_changed`（4 例）、`test_export_refuses_when_account_context_changed` | 通过 |
| R3 | 三份研究文件可全缺，严格 Schema 未接入 | 三份文件改为必需（`RESEARCH_FILE_MISSING`），真正调用 `QuestionSet`/`ClaimSet`/`ResearchDecision`；查 run_id/revision_id 一致、重复 ID、未知依赖/问题；旧运行走 legacy 路径并 WARN | `test_missing_role_fails_and_names_it`、`test_research_file_is_required`、`test_misspelled_claim_field_fails`、`test_legacy_run_warns_instead_of_failing` | 通过 |
| R4 | 账户未知仍可 actionable | `Field`→依赖映射（`target_account_weight`→资本/持仓等），能力位由 `TradeContext` 推导；买卖方向字段不混用；新旧仓位一致性；`cash_amount` 有限正数；未知 `requires` 直接失败 | `test_unknown_account_cannot_be_actionable`、`test_sell_fraction_cannot_use_target_weight`、`test_position_source_conflict`、`test_unknown_requires_is_rejected`、`test_blocked_plan_may_declare_its_missing_inputs` | 通过 |
| R5 | 未复核输出与失效论点仍可作为有效结论 | 逐项 `pending_reviews`（`output@revision`），一次复核只销一条；未复核输出对应维度必须降级为 `unresolved` 并移除 `valuation_reference` | `test_unreviewed_valuation_cannot_be_published`、`test_one_review_cannot_clear_another_pending_output`、`test_review_must_name_an_existing_revision`、`test_revision_requires_affected_outputs` | 通过 |
| R6 | 不存在的来源也能入库，路径被丢弃 | 来源文件必须存在可读；复制到 `supplement/sources/<sha16>.<ext>` 并记哈希；URL 证据必须给 `location`；同 ID 不同内容判冲突；不得占用基础证据 ID | `test_missing_source_file_is_rejected`、`test_existing_source_file_is_copied_and_hashed`、`test_url_evidence_needs_a_location`、`test_same_id_with_different_content_is_a_conflict`、`test_supplement_cannot_take_over_a_pack_evidence_id` | 通过 |
| R7 | 简单年化 ROE 混用单季利润与累计月份 | 两口径分列：累计 `YTD归母 ÷ 期末净资产 × 12/月数`，单季 `单季归母 ÷ 期末净资产 × 4`；摘要与报告同步改名 | `test_roe_uses_matching_numerator_and_annualisation`（Q1/Q2/Q3 断言 0.40/0.40、0.60/0.80、0.80/1.20） | 通过 |
| R8 | 重述保护未贯通真实入口，23% 为常量 | `update_flag` 进入数据包；同期多版本统计与 `comparability`（confirmed/changed/unconfirmed）；TTM 继承窗口内最弱可比性；税率由 `--adjustment-tax-rate` / `research recompute` 输入并写入情景、manifest 与报告 | `test_build_artifacts_detects_a_restated_period`、`test_period_end_versions_are_counted`、`test_ttm_inherits_comparability`、`test_recompute_changes_adjusted_numbers_but_not_reported_ones`、`test_default_scenario_stays_23_percent_and_is_labelled`、`test_recompute_after_finalize_invalidates_the_seal`、`test_recompute_rejects_a_ratio_outside_zero_to_one` | 通过 |

返工过程中另外修掉的两个真实缺陷（不是清单条目，但会直接影响结论）：

1. `blocked` 执行方案只要声明了 `requires` 就判 FAIL，等于惩罚「诚实声明缺什么」。
   改为：只有在已经给出仓位数字、或状态写成 `actionable` 时才 FAIL，否则 WARN。
2. 修订明细一直显示「复核=未完成」，即使所有输出与角色都已复核。改为按待审队列
   重新推导 `reviewed`，并在 `finalize` 写报告前校正（`reconcile_revisions`）。

## 2. 第 4 节补充验收

| 缺口 | 本轮做法 | 证据 |
|---|---|---|
| 1. 两个真实 run 没有补证/修订记录 | 两家公司各跑一轮完整循环，命令与产物见 §3 | `runs/*/evidence_imports.jsonl`、`revisions.jsonl`、`supplement/sources/` |
| 2. 缺运行级重算入口 | 新增 `research recompute` → `recompute/<scenario_id>/`，冻结包不改，写 `run.financial_override`，报告与校验从新目录读数 | 江西铜业税率 23%→25%、扬农化工 23%→15%，调整后归母均发生变化（§3） |
| 3. 官方补录缺公司/期间/单位/可得时间校验 | 新增 `validate_official_metrics`：`ts_code` 必须等于运行标的、`unit` 必须是元/CNY、`available_at` 必填且不得早于期末、不得晚于基准日、必须给 `url` 或 `source_file`；数据包的官方证据改用补录自带的披露时间，不再沿用财报记录 | `test_official_supplement_must_state_its_own_available_at`、`test_official_supplement_cannot_be_known_after_the_asof`、`test_official_supplement_needs_a_unit_and_a_matching_company`、`test_official_supplement_timestamp_overrides_the_statement_stamp` |
| 4. 报告「公司如何赚钱」仍是财务复述 | 该章节改由 `topic=business` 的论点生成；没登记业务论点时明确写「没有被回答」，不再用财务环比顶替。两份报告各补了一个真能补齐的问题 | 江西铜业补回购预案金额（3 亿~5 亿元等）；扬农化工补原药/制剂拆分与原料价格，关掉 `product_mix` 缺口 |
| 5. 角色摘要与中央 claims 双源 | 报告正文只引用中央论点表；角色摘要降为附录原始记录并标注引用关系；角色引用的 claim 必须存在（`ROLE_CLAIM_UNKNOWN`）；修订后受影响角色进入 `pending_role_reviews`，未重审则 `finalize` 不通过；`research review --role` 逐条关闭 | `test_business_section_is_built_from_business_claims`、`test_business_section_admits_when_no_business_claim_exists`、`test_role_summary_cannot_cite_a_claim_that_does_not_exist`、`test_revision_puts_the_claim_roles_into_review`、`test_revision_counts_as_reviewed_only_after_every_queue_is_closed` |

官方补录校验的一个限定说明：江西铜业样例里补录自带的 `available_at`（2026-04-29 /
08-26 / 10-29）与该期财报记录的公告日恰好相同，所以在真实数据上「是否沿用」看不出差别；
字段独立性由单元测试用不同日期证伪（`test_official_supplement_timestamp_overrides_the_statement_stamp`）。

## 3. 两家公司的真实循环

### 3.1 江西铜业 600362.SH（`runs/jxc-20261117`，snapshot `faa74adab93b5b41`）

| 步骤 | 命令 | 结果 |
|---|---|---|
| 建包（新补录 Schema） | `data 600362.SH --asof 2026-11-13 --out datapacks/jxc-20261117 --official-metrics configs/supplements/jxc-official.json` | 56 条证据，官方补录 3 期通过校验 |
| 正式分析 | `analyze … --run-id jxc-20261117` → 写 11 个角色 + 4 份研究文件 | `submit` → `DECIDED` |
| 首版交付 | `validate` → `finalize` → `export` | PASS（2 WARN）／FINALIZED／`reports/江西铜业-20261113.html` |
| 补证 | `research import-evidence`（巨潮 2026-11-12 回购预案 PDF） | 新增 4 条，PDF 复制为 `supplement/sources/5ea9d0c94a181118.pdf` 并留 sha256 |
| 封存失效 | 补证后立刻 `export` | 被拒：`封存后新增了依赖：supplemental_evidence.json` |
| 运行级重算 | `research recompute`，税率 23%→25% | Q3 调整后归母 18.17 → 17.53 亿元；数据包未改 |
| 修订 r2 / r3 | `research revise`：`c-odds` 降级、新增并降级 `c-buyback-signal` | 待复核输出 valuation/timing + 角色重审队列 |
| 复核 | `research review`（valuation、timing、aggressive-risk-analyst、bull-researcher） | 全部关闭，修订明细显示「复核=已完成」 |
| 重新交付 | `validate` → `finalize` → `export` | PASS／FINALIZED，seal `8ecf6b7c9fc7…`（37 项依赖） |

本轮补上的问题：q4「回购预案金额/价格上限/股数」从「未知」变为已答——
3 亿~5 亿元、价格上限 56.34 元/股、532.48 万~887.47 万股（占总股本 0.15%~0.26%）、
期限 3 个月、自有资金。据此把「回购提供赔率优势」降级：上限只占市值 0.46%。

### 3.2 扬农化工 600486.SH（`runs/yn-20261117`，snapshot `96ac30b902e04652`）

| 步骤 | 命令 | 结果 |
|---|---|---|
| 建包 | `data 600486.SH --asof 2026-11-13 --out datapacks/yn-20261117` | 50 条证据（该股无官方补录，报告如实显示缺失） |
| 正式分析 | `analyze … --run-id yn-20261117` | `submit` → `DECIDED` |
| 首版交付 | `validate` → `finalize` → `export` | PASS／FINALIZED／`reports/扬农化工-20261113.html`；第 2 节写明「毛利率下滑是价格还是成本无法判断」 |
| 补证 | `research import-evidence`（2026-10-27 主要经营数据公告） | 新增 6 条：原药/制剂收入、销量、均价 + 两种主要原料价格 |
| 重算 | `research recompute`，税率 23%→15%（实际有效税率 14.87%/15.47%） | Q3 调整后归母 2.273 → 2.509 亿元 |
| 修订 r2 | `research revise`：撤回「无法判断」论点 | 待复核输出 fundamental + 角色重审 |
| 复核 | `research review`（fundamental、fundamentals-analyst） | 全部关闭 |
| 重新交付 | `validate` → `finalize` → `export` | PASS／FINALIZED，seal `1f6460c91911…` |

本轮补上的问题：q1「毛利率下滑是价格还是成本」有了直接证据——原药均价同比
+2.77%、制剂 +10.62%（售价在涨），氯化亚砜 +117.27%、氢溴酸 +38.92%、
异戊二烯 +12.82%（成本在涨），液碱 -20.22%、一氯甲烷 -10.50% 部分对冲；
`product_mix` 缺口随之关闭。

历史版本导出在隔离副本上验证（不改生产 run）：改动 `claims.json` 一行后，
`export` 返回 2 并指出「封存后内容已变化：claims.json」；加 `--historical` 后
导出为 `江西铜业-20261113-已封存历史版本.html` 并给出 warning。

## 4. 仍未解决的事（不是本轮缺陷）

1. 江西铜业分部毛利（矿山/冶炼/副产品）本地库没有字段，只能从三季报附注取，
   仍是 `NOT_EXTRACTED`；报告第 2 节明确写了「这是本节最主要的未知」。
2. 江西铜业 2025 年及以前的官方扣非未补录；扬农化工官方扣非完全未补录，
   因此「官方扣非」列对该股显示缺失。
3. 两家公司的最新业绩预告未联网核实（本地最新记录停在 2026-07-09），按
   `NOT_EXTRACTED` 记录并写明检查范围，未写成「源侧未披露」。
4. 江西铜业 `TTM comparability=unconfirmed`：本地库每期只有一个披露版本，
   无法证明与当时披露一致，报告随文标注了这一点。

## 5. 保留的 T00–T07 结论

首次验收中已确认落实、本轮未回退的部分：数据包冻结与逐文件哈希、输入绑定与
`resume` 比对、单季差分与三类盈利分列、缺口四分类与时效规则、执行语义约束、
报告单一数据源（`report.md`/`report.html`/`report_view.json` 同源）、A01–A18 错误用例。
其中被本轮推翻或改写的三处已在 §1 说明：完成入口（R1）、导出有效性（R2）、
调整情景税率（R8，不再是固定 23%）。

## 6. 检查结果

```
pytest -q            191 passed
ruff check .         All checks passed!
mypy src             Success: no issues found in 62 source files
```

测试分布：`test_rework_acceptance.py` 32 项（R1–R8 与第 4 节专项）、
`test_financials.py` 21 项、`test_research.py` 19 项、`test_reporting.py` 17 项，
其余为既有用例（未删除、未下调等级）。

## 7. Git 与同步

- 基准 HEAD（返工前）：`0e819a8`。
- 推送沿用 SSH 通道（`~/.gitconfig` 的 `http.proxy` 仍指向失效端口 58731，
  属于本机配置问题，未改仓库配置、未强推、未改写历史）。

## 8. 同步结果

```
git ls-remote git@github.com:gemini-hyc/Trade-Council.git main   # 返工前 0e819a8
git push   git@github.com:gemini-hyc/Trade-Council.git main:main
→ 0e819a8..7278084  main -> main（快进）
```

- `7278084`：R1–R8 与第 4 节补充验收的全部代码、测试、文档、两份报告。
- `b6b7d2f`：本条同步记录。
- `e3202f6`：报告文案修正（无已撤回论点时不显示「旧判断单列」那句）。
- 以上提交在同一分支上依次快进推送，远端 HEAD 见仓库；本轮最后一个功能提交为 `e3202f6`。
- `runs/`、`datapacks/`、`.workbuddy/` 不进版本库：运行产物与冻结数据包可重建，
  本次新增 `configs/supplements/jxc-official.json` 作为手工维护的官方补录输入留档。
