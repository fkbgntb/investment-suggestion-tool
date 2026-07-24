# 自动报告闭环

定时调度现在会消费两类本地任务：

- `process-new-documents`：只有新产生的一级或专业证据才立即生成报告；只有聚合新闻时留到日报。
- `daily-summary`：有新增有效证据时生成当日报告；只有聚合新闻时也只允许生成观察性质报告。

每个任务都会保存明确结果：

- `GENERATED`
- `SKIPPED_NO_NEW_EVIDENCE`
- `SKIPPED_ONLY_AGGREGATOR`
- `SKIPPED_DATA_EXPIRED`
- `SKIPPED_POSITION_INCOMPLETE`
- `FAILED`

任务页面会展示结果和原因。报告幂等指纹包含本地持仓内容、有效证据及动态时效分、决策规则版本和
报告日期；重复任务不会生成第二份相同报告。

分析时会按证据影响周期重新计算时效：

- 短期证据半衰期 3 天；
- 中期证据半衰期 14 天；
- 长期证据半衰期 60 天；
- 未知周期半衰期 7 天。

超过四个半衰期的证据不再进入新报告。历史报告不受影响，仍保留当时使用的证据快照。

只处理本地积压报告任务、且不访问任何外部信息源：

```powershell
.\.venv\Scripts\python.exe scripts\run_report_tasks_once.py
```

该命令可能在确实需要生成报告且本机已配置 DeepSeek 时产生一次综合分析调用，但不会抓取新闻或
操作任何持仓。
