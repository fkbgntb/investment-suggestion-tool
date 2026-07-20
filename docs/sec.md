# SEC EDGAR 公司披露

正式信息源使用美国 SEC 的
[EDGAR Submissions API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)。
该接口无需 API Key，返回按公司 CIK 组织的实时申报元数据。第一版配置了半导体 Topic 中已经存在的
NVIDIA（CIK `0001045810`）和 TSMC（CIK `0001046179`），CIK 可在 SEC 官方公司页面核对。

## 合规访问

SEC 的[开发者 FAQ](https://www.sec.gov/about/webmaster-frequently-asked-questions)要求自动工具声明
User-Agent，并公布当前最高访问率为每秒 10 次。本工具将自身限制为每个来源最多每秒 4 次，且只有
在本机 `.env` 配置真实联系邮箱后才允许发起请求：

```text
INVEST_SEC_CONTACT_EMAIL=你的联系邮箱
```

邮箱不会进入 Git、数据库、抓取摘要或日志，只用于发给 SEC 的 User-Agent。未配置时一次性脚本会
在联网前停止，因此不会用虚假身份访问。

## 表单、链接和可信边界

公司配置引用通用 Taxonomy 中的 `Entity`，不写死在适配器逻辑中。NVIDIA 关注 `10-K`、`10-Q`、
`8-K`，TSMC 关注 `20-F`、`6-K`；其他表单默认过滤。保存 accession number、CIK、Entity ID、
表单类型、SEC 接受/申报时间、申报索引和主文档链接。同一 accession 只保存一次。

元数据仅允许从 `data.sec.gov` 获取。正文方法只接受 `www.sec.gov/Archives` 链接，统一 HTTP 客户端
仍会执行域名、公网地址、重定向、Content-Type、5 MB 限长和超时检查；不会遍历或下载附件列表。
即便是官方申报正文，对 AI 而言仍是不可信外部文本，后续必须经过清洗和提示注入隔离。

SEC 来源标记为 `REGULATOR / PRIMARY`，表示“文件确由官方披露系统提供”，不表示申报内容一定利好，
更不表示它对 `007300` 有直接影响。每条元数据都会保留 `direct_etf_impact_unverified=true`，后续由
Exposure、相关性规则和证据评分判断。

## 本地运行

注册来源不需要邮箱，也不会联网：

```powershell
.\.venv\Scripts\python.exe scripts\seed_sec_source.py
```

配置邮箱后才执行一次三小时窗口：

```powershell
.\.venv\Scripts\python.exe scripts\run_sec_once.py
```

SEC 失败只更新自己的 CrawlRun 和来源健康状态，不会阻塞 GDELT。自动调度和跨来源任务隔离将在
步骤 10 统一编排。
