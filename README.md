# ETF 投资分析工具

一个面向个人使用的、证据可追溯的 ETF 投资决策辅助工具。当前第一版只关注半导体主题和基金 `007300`。

本工具只生成分析和建议，不登录支付宝，不保存支付宝凭据，不提供申购、赎回、下单或任何账户控制能力。所有投资操作均由用户独立判断和完成。

详细实施方案见 [plan.md](plan.md)，冻结的 MVP 参数见 [docs/mvp-baseline.md](docs/mvp-baseline.md)。

## 当前进度

- [x] 步骤 0：冻结第一版业务参数
- [x] 步骤 1：工程骨架、安全配置、日志、健康检查和测试基线
- [x] 步骤 2：版本化领域模型、模块接口、状态机和 JSON Schema
- [x] 步骤 3：数据库、迁移、保留策略、审计和加密备份

步骤 3 完成后，应用已经具备可迁移的本地数据库和安全备份能力，但仍不包含抓取器、AI 调用或投资报告页面。下一阶段会接入第一个信息源适配器。

领域契约说明见 [docs/domain-contracts.md](docs/domain-contracts.md)。

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

数据库使用两个连续的 Alembic 版本，可以从旧版升级、回退后再升级。SQLite 只是第一版的本地实现；业务代码通过 SQLAlchemy 和仓储层访问数据，后续可以迁移到 PostgreSQL，原文目录也可以替换成云对象存储。

需要备份时，只在本机 `.env` 中设置不少于 16 个字符的 `INVEST_BACKUP_PASSPHRASE`，然后运行：

```powershell
.\.venv\Scripts\python.exe scripts\storage_admin.py backup
```

备份使用认证加密，默认位于 `E:\data\backups`。恢复会先校验口令、密文完整性和 SQLite 完整性，并且默认拒绝覆盖现有数据库。详细说明见 [docs/storage.md](docs/storage.md)。

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
