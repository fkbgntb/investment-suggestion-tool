# 规则相关性初筛

相关性初筛在 AI 调用之前运行，使用当前已激活的 Topic、Entity、产业链关系和 ETF Exposure 配置。
外部文档只能作为待匹配的纯文本，不能修改规则、阈值或配置。

每次判断保存命中的词、目标 Topic/Entity、各项分值、总分、规则版本与分类理由。结果分为：

- `RELEVANT`：达到自动证据抽取阈值；
- `REVIEW`：有弱信号，但先不调用 AI；
- `IRRELEVANT`：归档结果，不进入 AI 流程。

规则对英文 `memory` 要求同时出现芯片、半导体、DRAM、NAND、HBM 等上下文，并排除
`human memory`、`memory care` 等常见无关语境。分类器通过 `RelevanceClassifier` 接口隔离，
后续可替换为统计或向量模型。阈值和规则版本会随判断一起持久化。

手动运行初筛：

```powershell
.\.venv\Scripts\python.exe scripts\run_relevance_once.py
```

人工标注入口是仅允许本机访问的
`POST /api/v1/relevance/documents/{document_id}/labels`，接受 `RELEVANT`、`IRRELEVANT` 或
`REVIEW`，并记录审计事件。这些标签用于后续调整规则，不会直接修改受信任配置或触发投资动作。
