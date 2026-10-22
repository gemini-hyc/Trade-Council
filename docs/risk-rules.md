# Trade-Council 风控规则初版

本文件是 Phase 1 的规则表初版。机器可读的 `configs/risk.yaml` 在 Phase 6 落地。

| ID | 规则 | 参数 | 失败动作 |
|---|---|---|---|
| `risk.position.max_pct` | 单票仓位不得超过上限 | `max_position_pct` | block |
| `risk.price.order` | BUY 必须满足止损 < 入场 < 目标 | 无 | block |
| `risk.price.stop_distance` | 止损距离不得超过配置上限 | `max_stop_atr_multiple` | degrade |
| `risk.rr.min` | 风险收益比不得低于门槛 | `min_reward_risk` | block |
| `risk.tradability.suspended` | 停牌时禁止可执行 BUY/SELL | 无 | degrade |
| `risk.tradability.limit` | 涨跌停或不可成交价格禁止可执行指令 | `price_field` | degrade |
| `risk.trade.t_plus_one` | A 股当日买入不可当日卖出 | `market_rule` | block |
| `risk.lot.board` | 股数必须符合板块申报单位 | `market_rule` | block |
| `risk.hold.semantics` | HOLD 必须区分空仓和已有持仓 | `position_state` | block |
| `risk.capital.required` | 无资金分母时禁止输出金额仓位 | `capital` | degrade |

约定：

- 每条规则命中后写入 `decision.json`、`run.json` 和 `audit.json`。
- `block` 表示阻止最终交易建议。
- `degrade` 表示允许保留分析，但关闭对应能力位。
- 规则参数在 Phase 6 移入 `configs/risk.yaml`。

## Phase 7 接线口径

- `--capital` 提供资金分母；没有资金时不输出确定股数和金额。
- `--position-state flat|holding` 提供持仓语义；holding 必须同时提供
  `--position-shares` 与 `--position-opened-date`。
- `--board main|star|bj` 指定申报单位；未指定时按股票代码推断。
- 股数按板块 `lot_size` 与 `step` 校验，不统一写死 100 股。
- BUY 金额按入场区间上沿计算，不得超过 `capital × max_position_pct`。
- SELL 不得超过当前持仓股数；建仓日为基准日时由 T+1 规则阻止。
- `decision.json` 记录 `position_shares`、`position_amount` 和
  `position_opened_date`；命中规则 ID 进入 `run.json` 与 `audit.json`。
