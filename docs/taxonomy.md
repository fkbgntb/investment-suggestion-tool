# 主题、实体与基金暴露配置

第五阶段把半导体分析范围从业务代码移到了纯 JSON 配置中。当前示例配置位于
[`config_data/taxonomy/semiconductor-1.0.0.json`](../config_data/taxonomy/semiconductor-1.0.0.json)。

## 配置内容

一份 `TaxonomyConfiguration` 是完整且不可变的版本快照，包含：

- `Topic`：根主题、子主题和终端市场；
- `Entity`：公司、商品、指数、产品等可识别对象；
- `InfluenceRelation`：主题或实体之间的上下游、成本和需求影响关系；
- `Exposure`：基金与主题、指数或产品之间的关系。

半导体 1.0.0 配置包括存储、晶圆、设备、设计、代工，以及手机、PC、服务器、汽车
四类终端；商品包括铜和硅。全球代表性公司用于发现行业信号，不等于 `007300` 的
实际持仓。没有官方成分和权重数据时，暴露权重保持 `null/UNKNOWN`，不得虚构数值。

## 发布与回滚

先确保 `007300` 示例资产已经存在，然后执行：

```powershell
.\.venv\Scripts\python.exe scripts\seed_demo_portfolio.py
.\.venv\Scripts\python.exe scripts\seed_demo_taxonomy.py
```

初始化脚本会校验 JSON，再通过本地服务发布和激活版本。相同内容可以重复执行；已经
存在的版本不会被覆盖。

本地 API 提供：

```text
POST /api/v1/taxonomy/configurations
GET  /api/v1/taxonomy/configurations
GET  /api/v1/taxonomy/configurations/active
GET  /api/v1/taxonomy/configurations/{config_version}
POST /api/v1/taxonomy/configurations/{config_version}/activate
```

修改关键词、别名或启停状态时，不直接更新旧记录，而是复制完整配置、修改
`config_version` 和 `configuration_id`，并把 `based_on_version` 设为当前活动版本后发布。
切换历史版本只更新活动指针，不修改历史快照。

## 安全边界

- 配置模型拒绝未知字段，只存储数据，不解释或执行表达式；
- 发布和激活 API 只允许本机访问，公开绑定时整组接口关闭；
- 只有固定的 `local_user` 可信角色可以通过服务发布配置；
- 新闻和其他外部文档没有配置写入接口；
- 每次发布或激活都写入不含持仓和密钥的审计记录；
- 已发布配置禁止更新和直接删除；
- 新版本必须基于当前活动版本，避免并发覆盖；
- 暴露引用的基金资产必须已经存在于同一 workspace。

## 扩展其他主题

增加黄金、人工智能、中证 500 或沪深 300 时，可以新增 JSON 配置版本，无需修改核心
领域模型、存储结构或 API。一个配置可以包含多个根 Topic；事件和证据契约本身支持
多个 `topic_ids`，一个基金也可以包含多条 Exposure。
