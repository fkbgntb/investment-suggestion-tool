# 信息来源升级与报告闭环

第一版只分析 `007300`，来源按角色分工，不以“抓取数量”衡量质量：

- `OFFICIAL_DISCLOSURE`：中证指数、上交所/国联安披露、Micron IR。
- `MARKET_DATA`：H30184 事实表及后续可验证的净值/行情快照。
- `NEWS_DISCOVERY`：Alpha Vantage，只用于发现和情绪，不直接升级为一级证据。

Alpha Vantage 每个三小时窗口只轮询一个 ticker：`MU → TSM → ASML → NVDA`。多个 ticker
不会塞入同一个请求，因为该接口的多 ticker 语义不是 OR。目标价和分析师选股文章分别标记为
`PRICE_TARGET`、`ANALYST_OPINION`，证据上限低于普通二级报道；转载独立性按原始发布者和原始
域名计算。

## 当前官方来源

| 来源 | 周期 | 用途 |
|---|---:|---|
| 中证 H30184 事实表 | 24 小时 | 样本数及可无歧义解析的市场字段 |
| 中证 H30184 编制方案 | 168 小时 | 权重上限、调样规则和版本变化 |
| 上交所 512480 产品资料 | 24 小时 | 跟踪标的、产品结构和费率披露 |
| 上交所 512480 拆分结果 | 168 小时 | 识别 1:2 拆分，避免误判暴跌 |
| 007300 产品资料基线 | 168 小时 | 验证产品类型与目标 ETF 关系 |
| Micron IR 新闻页 | 3 小时 | 存储、HBM、产能、库存和指引信号 |

`007300` 产品资料基线来自监管指定披露平台，但不是实时净值接口；在找到稳定、合法、官方的
最新净值接口前，系统不会用第三方销售平台数据冒充官方数据。持仓页面中的当前金额仍由用户
手工更新。

H30184 事实表的 PDF 文本层可能打乱表格列顺序。当前只自动保存能无歧义确认的样本数和数据日期；
市盈率、市净率、波动率等字段在无法可靠对应表头时保持为空，不使用正则猜测。可运行以下命令从
已经保存的事实表生成或补齐市场快照，不需要再次联网：

```powershell
.\.venv\Scripts\python.exe scripts\process_market_snapshots.py
```

## 安全边界

- 仅访问配置与代码共同批准的 HTTPS 域名。
- PDF 最大 5 MB、80 页、15 万字符；拒绝加密、JavaScript、Launch 与嵌入附件。
- HTML 只保存受限文本，后续仍经过脚本、隐藏文本和提示注入清洗。
- 官方原文保存 `OriginProvenance`，聚合发现记录不能自行升级为一级来源。
- 市场指标由确定性代码计算；份额拆分通过复权因子处理，AI 不估算净值、波动或回撤。
- 官方资料优先使用确定性事实抽取；来源等级完全由本地注册表决定，不允许 AI 回传或修改。

## 本地配置与验证

注册来源：

```powershell
.\.venv\Scripts\python.exe scripts\configure_official_sources.py
```

只验证官方来源、不消耗 Alpha Vantage 次数：

```powershell
.\.venv\Scripts\python.exe scripts\run_official_sources_once.py
```

也可以只测试一个来源：

```powershell
.\.venv\Scripts\python.exe scripts\run_official_sources_once.py --source-id micron-ir-news
```

只处理已经保存的文档、且不消耗新闻 API 配额：

```powershell
.\.venv\Scripts\python.exe scripts\process_pending_once.py
```

手动检索完成后，页面会同时显示新增文档、评分证据、是否生成报告以及未生成报告的原因。只有
新增一级/专业证据才会在当前周期立即触发报告；只有聚合新闻时，最多生成观察性质的当日摘要。
