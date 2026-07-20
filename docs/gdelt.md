# GDELT 全球新闻发现

第一条真实信息源使用 [GDELT DOC 2.0 官方 API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)。
它是全球新闻聚合与发现来源，不是事实本身，也不会因为收录了一篇文章而提升文章可信度。
注册表将它标记为 `AGGREGATOR / SECONDARY`，后续需要由公司公告、监管披露等独立来源交叉验证。

## 抓取内容和边界

查询词由当前启用的 Topic 名称、别名和关键词生成，不包含个人持仓金额、支付宝信息或 AI 密钥。
请求使用 UTC 的 `STARTDATETIME`、`ENDDATETIME`、`DateAsc` 排序和最多 250 条的 API 上限；本项目默认
单次 50 条、每日 500 条。每日限额会结合数据库中当天已保存的文档数量计算，因此重启应用不会
清空限额。

适配器只保存 GDELT 返回的标题、文章链接、站点、语言、`seendate`、可选摘要和必要元数据。
`seendate` 被明确标记为 GDELT 观察时间，不冒充出版商精确发布时间。外部链接统一视为不可信：
过滤非 HTTP(S)、凭据 URL、本机/私网 IP 和异常端口，去除 fragment，而且不会自动下载文章全文。
页面阶段还必须对标题进行 HTML 转义，并对外链使用安全的 `rel` 属性。

## 增量、去重和失败

游标保存为最新已处理的 UTC 观察时间，并通过版本号防止并发覆盖。文档按规范化 URL 摘要去重；
同一响应重复运行不会产生重复文档。每次运行会保存时间窗口、Topic 数量、是否使用游标、查询摘要、
发现数量、新增数量和重复数量，不保存请求头或完整查询 URL。

断网、超时、429、返回格式异常和每日限额都会生成可识别的脱敏失败代码，并更新来源健康状态，
不会伪造空白新闻。GDELT 偶尔会响应较慢或限流，遇到 `TIMEOUT`/`HTTP_STATUS` 时应等待下一个周期，
而不是高频重试。

## 本机手动运行

先确保示例持仓与 Topic 已初始化，然后执行：

```powershell
.\.venv\Scripts\python.exe scripts\seed_gdelt_source.py
.\.venv\Scripts\python.exe scripts\run_gdelt_once.py
```

如果所在网络不能直接连接，可只在本机 `.env` 设置：

```text
INVEST_COLLECTOR_PROXY_URL=http://127.0.0.1:7897
```

该代理只提供网络路径，不绕过域名白名单、DNS/公网地址校验、超时、响应限长和每日数量上限。
本步骤提供手动运行；每三小时自动执行、漏跑恢复和跨进程锁在步骤 10 实现。
