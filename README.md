# ETF 投资分析工具

一个面向个人使用的、证据可追溯的 ETF 投资决策辅助工具。当前第一版只关注半导体主题和基金 `007300`。

本工具只生成分析和建议，不登录支付宝，不保存支付宝凭据，不提供申购、赎回、下单或任何账户控制能力。所有投资操作均由用户独立判断和完成。

详细实施方案见 [plan.md](plan.md)，冻结的 MVP 参数见 [docs/mvp-baseline.md](docs/mvp-baseline.md)。

## 当前进度

- [x] 步骤 0：冻结第一版业务参数
- [x] 步骤 1：工程骨架、安全配置、日志、健康检查和测试基线
- [ ] 步骤 2：领域模型、接口和状态机

步骤 1 完成后，应用只提供基础健康检查，不包含数据库、抓取器或 AI 调用。

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
