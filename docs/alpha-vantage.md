# Alpha Vantage 新闻发现

个人 Demo 的默认全球财经新闻发现源使用 Alpha Vantage `NEWS_SENTIMENT` API。它只作为二级发现层，
不替代 SEC、基金公司或上市公司正式披露，也不会直接产生买卖操作。

## 本机配置

真实密钥只写入被 Git 忽略的 `.env`：

```text
INVEST_ALPHA_VANTAGE_API_KEY=你的本机密钥
```

密钥由配置层按 SecretStr 读取，不写入数据库、抓取摘要、日志或审计记录。API 请求必须通过统一安全
HTTP 客户端访问固定域名 `www.alphavantage.co`；新闻文章链接只作为不可信元数据保存，默认不抓正文。

## 查询与额度

每个三小时窗口只调用一次 `NEWS_SENTIMENT`，使用 `technology` 主题，最多返回 50 条元数据，然后由
本地半导体 taxonomy 做相关性筛选。查询保存 SHA-256 摘要而不是包含密钥的完整 URL。

免费账户公开额度为每日 25 次。工具默认最多使用 20 次，给手动验证和其他用途保留 5 次；达到本地
限额会记录 `DAILY_LIMIT_REACHED`，服务返回的限流信息会记录 `RATE_LIMITED`，均不会高频重试。

## 启用和手动验证

先注册 Alpha Vantage 并禁用不稳定的 GDELT 自动抓取：

```powershell
.\.venv\Scripts\python.exe scripts\configure_alpha_vantage_source.py
```

随后可安全运行一次三小时窗口：

```powershell
.\.venv\Scripts\python.exe scripts\run_alpha_vantage_once.py
```

脚本只输出抓取状态和数量，不显示密钥。完成后也可以在本机 `/sources` 页面点击一次“立即检索最近
3 小时”。
