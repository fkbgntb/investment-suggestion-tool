# DeepSeek 安全证据抽取

AI 阶段只接收已清洗、已判定相关且非精确重复的文档。输入限制为文档 ID、受限长度的标题/摘要/正文、
来源类型、发布时间及允许的 Topic/Entity ID；不会发送 API 密钥、完整持仓、数据库、日志或支付宝信息。

实现使用 DeepSeek 官方 `POST /chat/completions` 接口和 JSON Output。默认模型为
`deepseek-v4-flash`，关闭 thinking，未提供任何 tools，并显式设置 `tool_choice=none`。模型输出还要通过
本地 Pydantic Schema 和以下可信边界校验：

- 文档 ID 必须与输入完全一致；
- Topic/Entity 只能来自规则初筛给出的白名单；
- 来源是否为一手资料由本地 Source 配置决定，模型无权修改；
- 每条证据必须包含不超过 500 字、确实存在于输入中的原文摘录；
- 买入、卖出、加仓、减仓等动作不得出现在模型生成的结论中；
- 非法输出只修复重试一次，仍失败就记录为 `NEEDS_REVIEW`。

输入正文默认最多 12,000 字符，输出最多 1,200 token；默认每天最多 20 次调用、总计 100,000 token。
每次运行只保存模型、Prompt 版本、输入哈希、token 数、耗时、尝试次数和结构化证据，不保存发送给模型
的全文或密钥。可疑提示注入短语会作为标记送入隔离上下文，但文档内容永远不会变成系统指令。

没有密钥时自动使用低置信度的纯规则替代路径，不进行网络调用。手动处理待办文档：

```powershell
.\.venv\Scripts\python.exe scripts\run_evidence_once.py
```

单次连接及 Schema 验证（会产生一次很小的真实 API 调用）：

```powershell
.\.venv\Scripts\python.exe scripts\check_deepseek_connection.py
```

接口参数依据 [DeepSeek Chat Completion 官方文档](https://api-docs.deepseek.com/api/create-chat-completion)
和 [Token 用量说明](https://api-docs.deepseek.com/quick_start/token_usage)。模型名和价格可能变化，因此模型
通过本机配置注入，而不是散落在业务逻辑中。
