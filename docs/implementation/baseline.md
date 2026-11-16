# 实施基线记录（T00）

## 1. 本次实施开始时的实测状态

| 项目 | 实测值 | 核对方式 |
|---|---|---|
| 本地 HEAD | `bdb38ab8e71669528d0e71020c9691e9624d2dea` | `git rev-parse HEAD` |
| 分支 | `main`，跟踪 `origin/main` | `git branch -vv` |
| 工作区 | 仅未跟踪 `.workbuddy/`（保留，不提交） | `git status --short` |
| 远端（本地引用） | `origin/main` 与本地同点，0 ahead / 0 behind | `git rev-list --left-right --count` |
| 测试 | `86 passed`（7.79s） | `.venv/bin/python -m pytest -q --basetemp=.pytest-tmp` |
| Python | 3.12.14（项目 `.venv`） | `.venv/bin/python -V` |

## 2. 未完成的核对与原因

`git fetch origin` 失败：

```
fatal: unable to access 'https://github.com/gemini-hyc/Trade-Council.git/':
Failed to connect to 127.0.0.1 port 58731 after 0 ms: Couldn't connect to server
```

本机配置的 HTTP 代理（`127.0.0.1:58731`）当时不可用，因此**本次没有真正查询远端最新提交**。
上面表中的“远端”只是本地 `origin/main` 引用，不能据此断言远端未前进。

影响范围与处理：

- 不影响本地实施、测试与验收。
- 影响最终同步。恢复网络后必须先 `git fetch`，确认远端未新增提交再推送；禁止强推。
- 该阻塞在 `acceptance.md` 中记为未完成项，不写成“已同步”。

## 3. 兼容与版本策略

- 新增结构化产物（`research_questions.json`、`claims.json`、`revisions.jsonl`、
  `research_decision.json`、`financial_quarters.csv`、`earnings_bridge.json`、
  `financial_adjustments.json`）各自带 `schema_version`，与包版本独立演进。
- 新工作流不使用“猜测旧字段含义”的适配。旧版 `decision.json`、`run.json`、`report.html`
  仍可读取与查看，但不参与新的 `validate`/`finalize` 通过判定；需要重新分析时新建 run。
- 角色与决策 Schema 的 `extra="ignore"` 只保留在历史兼容模型上；新增研究结构默认拒绝未知关键字段。
- 既有测试若因语义修正需要更新，必须写明理由并在提交信息中说明，保留原先要防止的错误场景。

## 4. 分阶段实施记录

见同目录 `notes.md`，逐阶段记录实际改动、提交 ID、测试命令与结果。
