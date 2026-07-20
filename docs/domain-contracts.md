# 阶段二领域契约

本文件说明 `app/domain` 中已经冻结的第一版跨模块语言。完整机器可读定义见
[`schemas/domain-contracts-v1.json`](../schemas/domain-contracts-v1.json)。

## 1. 设计边界

- 所有模型包含固定的 `schema_version=1.0`；
- 模型不可变并拒绝未知字段；
- 未知枚举值直接校验失败；
- 金额使用 `Decimal` 和显式币种，时间必须包含时区；
- 外部网页内容存放在 `RawDocument.external`，系统状态存放在
  `RawDocument.control`；
- `SourceAdapter.fetch` 只能返回 `SourceFetchResult`，不能设置文档状态；
- AI 接口只能返回结构化证据或分析，不能返回数据库命令；
- `DecisionResult` 和 `Report` 固定为 `advisory_only=true`；
- 全部公共接口均不存在交易、下单、申购或赎回方法。

## 2. 核心对象

| 分类 | 对象 |
| --- | --- |
| 投资与行情 | `InvestmentProfile`、`Asset`、`Position`、`MarketSnapshot` |
| 主题配置 | `Topic`、`Entity`、`InfluenceRelation`、`Exposure`、`TaxonomyConfiguration`、`Source` |
| 文档 | `RawDocument`、`EventCluster` |
| 证据 | `Evidence`、`EvidenceScore` |
| 分析建议 | `DecisionContext`、`DecisionResult`、`AnalysisResult`、`Report` |
| 任务审计 | `JobRun`、`StateTransitionRecord` |

## 3. 文档状态机

正常路径只能逐步前进：

```text
DISCOVERED → FETCHED → NORMALIZED → DEDUPLICATED → CLASSIFIED
→ EXTRACTED → SCORED → ANALYZED → PUBLISHED
```

处理中可以进入 `RETRYABLE_FAILED`、`PERMANENT_FAILED` 或 `QUARANTINED`。
`RETRYABLE_FAILED` 只能重新回到 `DISCOVERED`，依靠幂等键安全重跑；永久失败、隔离和已发布状态均为终态。

每次转换生成稳定的 `StateTransitionRecord`：合法转换为 `APPLIED`，重复转换为
`NOOP`，非法跳转为 `REJECTED`。严格调用方可以抛出异常，但异常中仍保留拒绝记录，供第三阶段写入审计表。

## 4. 可替换接口

- `SourceAdapter`
- `MarketDataProvider`
- `AIProvider`
- `DecisionPolicy`
- `ReportRenderer`
- `NotificationProvider`
- `TaskDispatcher`
- `StorageProvider`

这些接口只依赖 Pydantic 领域契约，不依赖 FastAPI、SQLite 或具体 AI 厂商。后续增加黄金主题、信息源或 DeepSeek 以外的模型时，不需要修改现有对象。

## 5. Schema 更新规则

修改领域模型后运行：

```powershell
.\.venv\Scripts\python.exe scripts\export_schemas.py
```

日常检查会执行 `--check`，模型与已提交 Schema 不一致时直接失败。破坏兼容性的修改必须提升 Schema 主版本，不能悄悄覆盖 `1.0`。
