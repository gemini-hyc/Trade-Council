# Trade-Council 数据层

## 1. 数据流

```text
本地 parquet
  → 标准化数据集
  → 事件/事实视图
  → DataPack + Evidence
  → 角色分析
```

## 2. 当前新增数据

- `announcements/cninfo/`：巨潮公告，按日分片，带 `event_tag` 和 `doc_role`。
- `news/em_fast/`：东财全市场快讯，按日分片，带 A 股关联代码。
- `reports/forecast/`：按公告日的业绩预告。
- `reports/express/`：季度业绩快报。
- `industry/sw_official/`：申万官方行业点位和估值。
- `futures/`：期货行情和股指基差。

## 3. DataPack 事件产物

运行 `trade-council data` 后会生成：

- `events.csv`
- `events.jsonl`
- `event_timeline.json`
- `announcements_cninfo.csv`
- `news_em_fast.csv`

公告只把 `doc_role=issuer` 的事件放入时间线；中介机构文档保留在原始公告数据中。

## 4. 数据质量门禁

当前至少检查：

- 行情关键列是否为整列为空。
- `daily_basic` 最新交易日 PE/PB/PS 是否整列为空。
- 事件 as-of 截断。
- 公告事件是否有真实 Evidence。
- `news/em_fast` 中 `a_codes` 是否可解析。

`manifest.json` 和 `DATA_DIGEST.md` 会记录数据状态。

## 5. 本次分析任务的外部补充

当 DataPack 诊断出 `forecast_single=WARN`、`daily_basic_quality=ERROR`
或公告/新闻缺失时，`analyze --allow-web` 会：

1. 生成 `data_needs.json`。
2. 显式启用的搜索 provider 查询外部来源。
3. 过滤晚于 as-of 的结果。
4. 把外部事实写入本次 Evidence。
5. 写入 `supplemental_data.json` 和 `supplemental_evidence.json`。

搜索服务必须返回：

```json
{
  "results": [
    {
      "title": "...",
      "url": "https://...",
      "published_at": "2026-10-16",
      "available_at": "2026-10-16 20:00:00",
      "summary": "...",
      "fields": {"net_profit_min": 1.0},
      "confidence": "high"
    }
  ]
}
```

配置：

```yaml
web_supplement_provider: cninfo
web_supplement_endpoint: http://127.0.0.1:PORT/search
web_supplement_api_key_env: TRADE_COUNCIL_WEB_SEARCH_KEY
```

`web_supplement_provider=cninfo` 时直接查询巨潮官方公告接口，不需要额外 endpoint；
通用搜索服务使用 `web_supplement_provider=http_json` 并配置 endpoint。
`--allow-web` 是唯一的网络搜索显式授权开关，不需要再修改配置开启。

没有配置搜索 provider 时，系统会明确写入 `unavailable`，不会伪造搜索结果。

结果状态要区分三种情况：

- `not_found`：查询已完整结束，目标窗口内确实没有匹配项。
- `partial`：已找到官方来源，但仍有关键字段无法从公告标题或结构化字段中提取。
- `error`：网络、分页或 orgId 查询失败，不能据此推断“没有发生”。

每个需求会在 `missing_fields` 中列出仍未获得的字段。例如只找到业绩情况公告标题、
但尚未解析 PDF 中的净利润区间时，会保留公告 Evidence 和官方链接，同时将
`net_profit_min`、`net_profit_max` 等字段标为缺口。

## 6. 数据库与任务数据的边界

- 任务级补充：自动生成，只属于当前 run。
- 数据库回填：独立操作，必须经过来源校验、对账和验证。
- 外部搜索结果不能静默覆盖本地数据。
