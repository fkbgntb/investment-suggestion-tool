# 三小时调度与休眠恢复

调度核心不依赖 APScheduler、Celery 或 Windows API。它使用 UTC 三小时窗口和数据库状态表保存
`last_completed_at`、`next_due_at`、每日摘要/清理日期及带过期时间的任务租约。以后迁移到服务器、
PostgreSQL 或 Celery 时，采集用例不需要重写。

## 并发和恢复

每次运行先原子获取 `crawl-sources` 数据库租约，租约有效期间第二个实例返回 `LOCKED`，对应
`max_instances=1`。进程异常退出后租约最多 60 分钟自动过期。Windows 休眠或关机期间没有运行时，
下次启动按三小时窗口补抓，单次最多回补 8 个窗口（24 小时），避免突然产生无限请求。

每个来源在独立事务中运行；Alpha Vantage、GDELT 和 SEC 任一来源失败只增加自己的失败计数，
不会阻塞其他来源。当前个人 Demo 默认启用 Alpha Vantage 与 SEC，GDELT 作为可选回退保留。
CrawlRun 保存计划时间、完成时间、状态、数量和脱敏错误代码。超过 8 小时没有成功记录的启用来源
通过 `/api/v1/sources/{source_id}/status` 标记 `is_stale=true`。

## 下游任务和每日工作

只有窗口产生新文档时，才向数据库 `scheduled_tasks` 表写入 `process-new-documents`。目前不会直接
调用 AI；清洗、证据抽取和 AI 阶段完成后再消费这张表。没有新文档时不会产生处理或 AI 任务。
`daily-summary` 每个 UTC 日期最多排队一次，过期正文清理也每天最多执行一次。任务使用稳定摘要去重，
重启或重复触发不会重复入队。

## Windows 安装和手动触发

安装当前用户的 Windows 计划任务：

```powershell
.\scripts\install-windows-scheduler.ps1
```

任务每三小时启动一次，启用 `StartWhenAvailable`，并忽略重叠实例。它只调用仓库中固定的
`run_scheduler_once.py`，接口不能接收任意命令或函数名。

安全手动检查一次到期窗口：

```powershell
.\.venv\Scripts\python.exe scripts\run_scheduler_once.py
```

需要立即检查补抓状态时可使用固定的 `--force` 开关；它仍然运行同一个白名单任务，不能改变 URL、
适配器或系统命令：

```powershell
.\.venv\Scripts\python.exe scripts\run_scheduler_once.py --force
```

任务输出只含状态和数量，不打印完整异常、网页内容、请求头、邮箱或持仓。
