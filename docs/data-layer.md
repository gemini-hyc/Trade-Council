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
2. 调用配置的搜索 endpoint。
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
web_supplement_enabled: true
web_supplement_endpoint: http://127.0.0.1:PORT/search
web_supplement_api_key_env: TRADE_COUNCIL_WEB_SEARCH_KEY
```

没有配置搜索 provider 时，系统会明确写入 `unavailable`，不会伪造搜索结果。

## 6. 数据库与任务数据的边界

- 任务级补充：自动生成，只属于当前 run。
- 数据库回填：独立操作，必须经过来源校验、对账和验证。
- 外部搜索结果不能静默覆盖本地数据。
