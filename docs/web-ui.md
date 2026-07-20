# 本机 Web 页面与 API

第 18 阶段将现有模块连成个人可用的本机工具。运行 `scripts/run-dev.ps1` 后打开
`http://127.0.0.1:8000/`，可查看概览、手动更新持仓、证据、历史报告、数据源、
任务和脱敏设置状态。页面只调用 `/api/v1` application service，未来可直接换成 React/Vue。

“运行分析”会依次：

1. 为选定持仓生成不可变快照；
2. 从数据库读取最新证据和评分；
3. 用组合参考总额在本地计算仓位比例；
4. 运行确定性决策、DeepSeek/规则降级综合和 HTML 报告；
5. 跳转到不可变报告。

组合参考总额不写入 AI 请求，只保留计算后的相对仓位。当前本机 `.env` 已根据用户
确认的数据设置 `INVEST_PORTFOLIO_REFERENCE_VALUE=3000`；该文件被 Git 忽略，不会提交到 GitHub。

修改和手动运行端点同时校验本机访问、同源 Origin、SameSite/HttpOnly CSRF Cookie、
CSRF Header 和内存频率限制。默认无 CORS 放行。一旦配置为非回环地址，个人页面和
私有 API 都直接返回 403；当前版本不因“有个页面”就允许公网访问。

主要 API：

```text
GET/POST /api/v1/positions
GET      /api/v1/evidence
POST     /api/v1/jobs/crawl
POST     /api/v1/analysis/run
GET      /api/v1/reports
GET      /api/v1/reports/latest
GET      /api/v1/reports/{id}
GET      /api/v1/sources/health
GET      /api/v1/health
```

命令行也可直接运行一次已保存持仓的分析：

```powershell
.\.venv\Scripts\python.exe scripts\run_analysis_once.py
```
