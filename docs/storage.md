# 阶段三：本地存储与迁移

## 当前结果

第一版使用 SQLite，运行数据根目录为 `E:\data`：

```text
E:\data
├── investment_tool.db
├── raw-documents\
└── backups\
```

仓库中的 `data/` 只保留占位文件，不保存实际数据库。`.env`、数据库、抓取原文和备份文件均被 Git 忽略。

## 数据结构

数据库包含工作区、投资档案、资产、持仓、主题、实体、暴露关系、信息源、抓取任务、原始文档、事件簇、证据、评分、分析、建议、报告、审计事件和幂等记录。

关键约束如下：

- 所有敏感记录都属于一个 `workspace_id`；
- 子记录使用“工作区 + 父记录 ID”复合外键，数据库会拒绝跨工作区引用；
- 删除工作区时，相关持仓、文档、报告和审计数据级联删除；
- 原始文档按工作区和内容哈希去重；
- 抓取任务和分析任务具有幂等键，避免重复运行；
- 报告保存输入快照、流水线版本、规则版本和提示词版本；
- 时间统一按 UTC 保存，SQLite 读取时恢复时区信息。

## 初始化与升级

```powershell
.\.venv\Scripts\python.exe scripts\storage_admin.py migrate
```

迁移脚本支持空数据库初始化、旧版本升级和回滚测试。日常只应运行升级命令；回滚入口保留给自动化测试和受控故障恢复，不通过普通管理命令暴露。

## 原文保留策略

默认保留抓取原文 90 天。执行以下命令后，超过工作区保留期的 `raw_body` 会被清空，但文档 ID、来源、标题、URL、哈希、抓取时间和清理时间仍保留，便于去重和审计：

```powershell
.\.venv\Scripts\python.exe scripts\storage_admin.py purge
```

审计详情会拒绝密码、令牌、API 密钥、认证头、完整持仓和原文等敏感字段，并限制为 20 KB。

## 加密备份与恢复

该功能为可选工具，不配置备份口令不会影响本地服务、抓取、分析或报告，也不作为个人
Demo 的完成条件。只有确实需要保存独立备份时才启用。

备份口令只放在本机 `.env`：

```text
INVEST_BACKUP_PASSPHRASE=至少16个字符且不要提交到Git
```

创建备份：

```powershell
.\.venv\Scripts\python.exe scripts\storage_admin.py backup
```

从 `E:\data\backups` 恢复指定文件：

```powershell
.\.venv\Scripts\python.exe scripts\storage_admin.py restore investment-tool-时间戳.istbackup
```

目标数据库已存在时，恢复会停止。确认确实要替换后才可显式增加 `--overwrite`。备份通过 Scrypt 派生密钥，使用 AES-GCM 认证加密；恢复前验证密文，写入前验证 SQLite 完整性，并使用临时文件原子替换目标。

不要把备份口令写进命令行参数、日志、GitHub 或聊天。丢失口令后无法解密备份。

加密备份不等于主数据库加密。`investment_tool.db` 在程序运行时仍是普通 SQLite 文件；如果电脑有多个用户、E 盘是移动硬盘或存在丢失风险，应为 E 盘启用 BitLocker（或等效的整盘加密），并在 Windows 文件属性中限制 `E:\data` 只允许当前账户访问。程序在 Windows 上沿用目录现有 ACL，不会擅自修改系统权限。

## 后续迁移到云端

当前数据模型没有绑定 SQLite 专有业务查询，并且会在测试中编译 PostgreSQL DDL。后续迁移可以：

1. 安装项目的 `postgres` 可选依赖；
2. 将 `INVEST_DATABASE_URL` 改为受保护的 PostgreSQL 连接；
3. 使用同一套 Alembic 迁移建表；
4. 把抓取原文目录实现替换为对象存储提供者；
5. 为云数据库启用私网、防火墙、最小权限账户、TLS 和托管备份。

云迁移不会改变领域模型和建议规则。云端数据库不得直接暴露到公网，凭据必须迁移到密钥管理服务。
