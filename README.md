# ETF 投资分析工具

一个面向个人使用的、证据可追溯的 ETF 投资决策辅助工具。当前第一版只关注半导体主题和基金 `007300`。

本工具只生成分析和建议，不登录支付宝，不保存支付宝凭据，不提供申购、赎回、下单或任何账户控制能力。所有投资操作均由用户独立判断和完成。

详细实施方案见 [plan.md](plan.md)，冻结的 MVP 参数见 [docs/mvp-baseline.md](docs/mvp-baseline.md)。

## 当前进度

- [x] 步骤 0：冻结第一版业务参数
- [x] 步骤 1：工程骨架、安全配置、日志、健康检查和测试基线
- [x] 步骤 2：版本化领域模型、模块接口、状态机和 JSON Schema
- [x] 步骤 3：数据库、迁移、保留策略、审计和可选备份
- [x] 步骤 4：投资档案、持仓 CRUD、费用字段和不可变分析快照
- [x] 步骤 5：版本化 Topic、Entity、影响关系和基金 Exposure 配置
- [x] 步骤 6：统一抓取客户端、来源策略、限长、超时和失败隔离
- [x] 步骤 7：信息源注册、适配器白名单、健康状态和版本化游标
- [x] 步骤 8：GDELT 全球新闻发现、增量游标、持久化限额和失败记录
- [x] 步骤 9：SEC 公司披露元数据、表单过滤、官方域名全文边界和限速
- [x] 步骤 10：三小时调度、数据库租约、休眠补抓和下游任务队列
- [x] 步骤 11：安全纯文本清洗、语言检测、精确去重和事件聚类
- [x] 步骤 12：基于主题、实体和产业链配置的可解释相关性初筛
- [x] 步骤 13：DeepSeek/规则替代的受限结构化证据抽取

应用可以在 Windows 本机每三小时运行 GDELT 和已配置的 SEC 来源，并在休眠后补抓遗漏窗口；新文档
会自动清洗、去重、筛除明显无关内容，并将相关文档转换为结构化证据；尚未实现证据评分、最终建议或投资报告页面。SEC 真实请求前仍需按官方规则配置邮箱。

领域契约说明见 [docs/domain-contracts.md](docs/domain-contracts.md)。
信息源配置说明见 [docs/source-registry.md](docs/source-registry.md)。
GDELT 采集说明见 [docs/gdelt.md](docs/gdelt.md)。
SEC 披露采集说明见 [docs/sec.md](docs/sec.md)。
调度与休眠恢复说明见 [docs/scheduler.md](docs/scheduler.md)。
清洗、去重与事件聚类说明见 [docs/normalization.md](docs/normalization.md)。
相关性规则与人工标注说明见 [docs/relevance.md](docs/relevance.md)。
DeepSeek 隔离、预算和证据 Schema 说明见 [docs/ai-evidence.md](docs/ai-evidence.md)。

## 本地开发

项目要求 Python 3.11 或更高版本。当前开发环境使用 Python 3.13.2。

```powershell
.\scripts\bootstrap.ps1
Copy-Item .env.example .env
.\scripts\run-dev.ps1
```

打开：

- API 健康检查：http://127.0.0.1:8000/api/v1/health
- API 文档：http://127.0.0.1:8000/docs

## 检查

本地检查包含静态检查、格式检查、密钥扫描、自动化测试和覆盖率门槛：

```powershell
.\scripts\check.ps1
```

需要联网检查依赖漏洞时：

```powershell
.\scripts\check.ps1 -Audit
```

## 本地数据存储

本机 `.env` 已配置为把运行数据放在 `E:\data`，不会把数据库、抓取原文或备份写入 C 盘。先执行数据库迁移：

```powershell
.\.venv\Scripts\python.exe scripts\storage_admin.py migrate
```

数据库使用连续的 Alembic 版本，可以从旧版升级、回退后再升级。SQLite 只是第一版的本地实现；业务代码通过 SQLAlchemy 和仓储层访问数据，后续可以迁移到 PostgreSQL，原文目录也可以替换成云对象存储。

加密备份是可选功能，不是本地 Demo 的运行前提。确实需要备份时，才在本机 `.env` 中设置
不少于 16 个字符的 `INVEST_BACKUP_PASSPHRASE`，然后运行：

```powershell
.\.venv\Scripts\python.exe scripts\storage_admin.py backup
```

备份使用认证加密，默认位于 `E:\data\backups`。恢复会先校验口令、密文完整性和 SQLite 完整性，并且默认拒绝覆盖现有数据库。详细说明见 [docs/storage.md](docs/storage.md)。

## 模拟持仓

首次写入已确认的 `007300` 示例档案：

```powershell
.\.venv\Scripts\python.exe scripts\seed_demo_portfolio.py
```

脚本不会覆盖已经存在的档案。启动本地服务后，可在 API 文档的 `portfolio` 分组中录入、更新、删除模拟持仓，并生成分析快照。所有持仓接口只接受本机来源；服务一旦绑定公网地址，该组接口会直接禁用。详细说明见 [docs/portfolio.md](docs/portfolio.md)。

## 半导体主题配置

在示例持仓初始化后，发布半导体主题配置：

```powershell
.\.venv\Scripts\python.exe scripts\seed_demo_taxonomy.py
```

配置包含中英文别名、存储/晶圆/设备/设计/代工子主题、四类终端、铜和硅、代表性公司、
上下游关系及 `007300` 暴露映射。配置变更通过新版本发布，旧版本不可修改并可回滚。
详细说明见 [docs/taxonomy.md](docs/taxonomy.md)。

## 抓取基础设施

后续信息源统一使用受 `URLPolicy` 约束的异步 HTTP 客户端。它会检查域名和公网地址，逐次
验证重定向，并限制超时、Content-Type 与最大响应大小；失败只影响对应来源。可选代理只在
本机配置，不写入适配器。详细说明见 [docs/collection.md](docs/collection.md)。

## DeepSeek 密钥

不要把密钥粘贴到对话、代码或 Git 中。将 `.env.example` 复制为 `.env`，再只在本机填写：

```text
DEEPSEEK_API_KEY=你的本机密钥
```

`.env` 已被 Git 忽略。应用日志会尝试遮盖常见密钥字段，但这不能代替正确的密钥管理。

项目脚本会在当前进程中清除 `SSLKEYLOGFILE`，避免TLS会话密钥被写入外部文件，并启用Python UTF-8模式以兼容中文项目路径。脚本不会修改系统级环境变量。

## 安全默认值

- 默认只监听 `127.0.0.1`；
- 非本机绑定必须显式设置 `INVEST_ALLOW_PUBLIC_BIND=true`；
- 生产环境禁止开启调试模式；
- 健康检查不返回密钥、数据库地址或持仓；
- 基础安全响应头默认启用；
- 系统不存在交易路由或交易适配器。
