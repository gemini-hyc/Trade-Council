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
PYTHONPATH=src .venv/bin/python -m trade_council data 600362.SH --asof 2026-11-13 \
  --out /tmp/jiangxi-datapack --official-metrics official.json   # 官方扣非/信用减值补录
```

如需复用已有数据包并保留其修正记录，显式传入 `--datapack <目录> --resume`。续接已有运行须保持股票和 snapshot 相同；更换输入使用新 run ID。

## 3. 当前 agent 的分析职责

- 核对数据时点、单位、财务口径、数据缺口及数值与文字是否一致。
- 按阶段完成领域分析、多空论证、研究主管、交易方案、风险评议和最终裁决。
- 角色结果满足现有 Schema，事实能回指证据。需要补充核实时使用当前会话工具并记录来源。
- 没有资金与持仓时，不填写具体股数、金额或账户仓位。
- 保存角色 JSON/Markdown、decision.json、真实运行与检查结果；不能把准备状态改名充当完成。
- 完成实际分析后运行 `validate`，再运行 `finalize`；未完成或审计失败不交付可执行结论。
- 不编造 API token、费用或当前 agent 不可确认的模型版本。

## 4. 研究补证与论点修订

```bash
# 补证：结构化 JSON，校验股票/期间/单位/可得时点/来源定位，重复导入自动去重
PYTHONPATH=src .venv/bin/python -m trade_council research import-evidence \
  --run-id <run_id> --file supplement.json

# 修订：记录修订前后、触发证据、受影响输出；依赖论点自动转 unresolved
PYTHONPATH=src .venv/bin/python -m trade_council research revise \
  --run-id <run_id> --file revision.json

# 复核：一次只销一条（output@revision 或 role@revision）
PYTHONPATH=src .venv/bin/python -m trade_council research review \
  --run-id <run_id> --file review.json

# 运行级重算：换税率或补录官方数字，不动冻结数据包
PYTHONPATH=src .venv/bin/python -m trade_council research recompute \
  --run-id <run_id> --file scenario.json
```

补证文件的字段与示例见 `docs/implementation/schemas.md`。补证写入运行目录的
`supplemental_evidence.json`，来源文件复制到 `supplement/sources/` 并留 sha256，
同时追加 `evidence_imports.jsonl`；不得修改数据包。

补证与修订只解决「证据与判断」，不自动改数字：要改财务口径就再跑 `research recompute`
（新目录 `recompute/<scenario_id>/`，`run.financial_override` 指向它）。这两类改动都会让
已有的封存失效，必须重新 `validate` + `finalize` 才能正式导出。

角色摘要与中央论点表必须一致：角色引用的 `claim_id` 不存在会失败
（`ROLE_CLAIM_UNKNOWN`）；论点状态变化后，写过这些论点的角色进入待重审队列，
没关闭就 `ROLE_REVIEW_PENDING`。关闭方式：重写角色摘要，或 `research review --role`。

### 4.1 复核口径清单（交付前逐条回答）

机器校验只能证明「有没有登记」「有没有复核」，证明不了口径用错。以下三问必须在
`research review` 的 note 里写明答案，写不出来就说明还没复核：

1. **累计还是单季。** 引用的价格、销量、收入是「1-9 月累计」还是「Q3 单季」？
   公告标题与正文表头可能不一致（2026-10-27 扬农化工的公告标题写「第三季度」，
   价格表口径是「1-9 月」）。用累计数回答单季问题，必须先把累计差成单季：
   收入与销量各自相减，再用「单季收入 ÷ 单季销量」算均价；**不能相减均价**。
   差分后要用同一个方法回算三个累计期，与公告披露值对上才算可用。
2. **同比还是环比。** 同比是本届对上年同期，环比是本届对上季。用「累计均价同比上涨」
   回答「Q3 相对 Q2 是否跌价」是无效论证：累计同比为正，单季环比仍可能为负。
   问题用哪个方向，证据就必须是哪个方向。
3. **价格变化还是成本贡献。** 售价涨跌是收入端，毛利是收入减成本。没有原料采购量、
   单位耗用与库存结转，就不能把售价涨跌或某种原料的涨幅写成毛利贡献；也不能把
   几种原料的涨幅相加当作总体单位成本涨幅。方向可辨、幅度不可辨时，就只写方向，
   并把「无法分配贡献」登记为未决论点与缺口。

三类口径错用都不会被程序拦住，只会被下一次复核拦住；所以清单放在人工这一步，
`review` 的 note 是它的落点，不写 note 的复核视为没做。

命名规则：`source_level=official` 只表示来源是公司公告，不表示口径可比。
跨公告拼接（Q1 + H1 → Q2）必须在 `calc_refs` 里写清两次减法与除法的分子分母。


## 5. 完成与交付

```bash
PYTHONPATH=src .venv/bin/python -m trade_council submit --run-id <run_id>        # 产物齐了才推进
PYTHONPATH=src .venv/bin/python -m trade_council validate --run-id <run_id>      # 机器校验
PYTHONPATH=src .venv/bin/python -m trade_council finalize --run-id <run_id>      # 校验+审计+封存
PYTHONPATH=src .venv/bin/python -m trade_council export --run-id <run_id> --out-dir <交付目录>
```

`submit` 检查角色产物、`decision.json` 与三份研究文件是否齐全；齐全才把状态推进到
`DECIDED`。这一步不需要、也不允许手改 `run.json` 的状态。

`validate` 检查：输入绑定未变、启用角色产物齐全且符合 Schema、证据存在且不晚于基准日、
类型一致、研究结构合规（含三份研究文件必需）、角色论点引用一致、关键缺口已反映到判断、
规则树内部一致、执行语义不越界。

`finalize` 在 `validate` 通过后生成报告、跑审计并写 `seal-1` 封存（输入绑定、角色产物、
研究文件、revisions、规则树、账户上下文、重算产物共 37 项依赖）。审计 FAIL 只写
`report.draft.*` 并置 `AUDIT_FAILED`，不得当成交付物。

`export` 要求 `finalize.json.ok`、`report.html` 哈希匹配，并且逐项核对封存依赖；
任何依赖在封存后变化都会被拒绝。确需保留旧版本时用 `--historical`，
文件名会带「已封存历史版本」，并给出 warning。

成功后会得到 `report.md`、`report.html` 与 `report_view.json`（三者同一数据源）。
报告结构：6 段主文（判断 / 业务 / 财务桥接 / 估值与技术 / 账户与执行 / 下次检查）+
附录（全部启用角色、多空论点、失效论点、更正记录、缺口、证据索引）。
第 2 节只由 `topic=business` 的论点生成，没有就明说没回答。

## 6. 查询

```bash
PYTHONPATH=src .venv/bin/python -m trade_council models list
PYTHONPATH=src .venv/bin/python -m trade_council models check
PYTHONPATH=src .venv/bin/python -m trade_council runs list
PYTHONPATH=src .venv/bin/python -m trade_council runs inspect --run-id <run_id>
PYTHONPATH=src .venv/bin/python -m trade_council report --run-id <run_id>
```

`models check` 只确认当前 agent 模式，不连接外部模型。`models probe` 被禁用；`--allow-cloud` 不会覆盖当前策略。

当前会话的计费和 token 数不能由此 CLI 测得；历史 `runs costs` 统计的是项目 API 日志，不代表当前聊天的用量。

## 7. 历史工作流与兼容

原 batch/DeepSeek API 工作流及其参数保留用于兼容和测试。恢复它们需要另行明确修改执行策略；当前任务不得自动回退、索取密钥或安排批量调度。

旧产物兼容：本轮新增 `schema_version` 字段。旧 run 的 `agents/*.json` 仍可读取，但缺少
`research_questions.json` / `claims.json` / `research_decision.json` 的旧运行会在 `validate`
得到 WARN，且不能被视为通过新流程验收；需要按新结构补齐后重新 `validate`。

复用历史报告前检查截止日期、运行后端和数据缺口。旧运行的审计通过不等于收益验证通过。
