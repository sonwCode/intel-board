# 调研结论与优化路线

本文记录 2026-09-28 对公开文档和开源项目的调研结果，并把结论映射到当前代码。目标是让项目从“可以录入和展示条目”逐步变成“可持续运行、可追溯的情报管线”。

## 参考资料

- [FastAPI Lifespan Events](https://fastapi.tiangolo.com/advanced/events/)：推荐用 `lifespan` 管理应用启动和关闭资源；当前项目已经使用这个入口初始化数据库。
- [PostgreSQL Full Text Search](https://www.postgresql.org/docs/current/textsearch.html) 和 [Preferred Index Types](https://www.postgresql.org/docs/current/textsearch-indexes.html)：经常检索的文本应建立索引，GIN 是全文检索的首选索引类型。
- [pgvector](https://github.com/pgvector/pgvector)：在关键词检索不够用时，可以在 PostgreSQL 中加入向量检索；HNSW 查询速度和召回率通常更好，但建索引更慢、占用更多内存。
- [OWASP SSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)：抓取外部 URL 时应优先使用可信来源白名单，并校验解析结果，避免访问本机、内网和云元数据地址。
- [Miniflux](https://github.com/miniflux/v2)：成熟的 RSS 阅读器选择 PostgreSQL、支持 Atom/RSS/JSON Feed、分类、收藏和 API，并把抓取与阅读体验分开。
- [FreshRSS](https://github.com/FreshRSS/FreshRSS)：提供 API、CLI、WebSub 和受控网页抓取，说明来源接入、定时更新和用户查询应当是独立能力。
- [my-focal-ai](https://github.com/YanCheng-go/my-focal-ai)：采用“多来源采集 → LLM 相关性评分 → 本地 SQLite/FastAPI 看板”的轻量路线。
- [morning-intelligence-dashboard](https://github.com/locnguyenphu107/morning-intelligence-dashboard)：把 `feeds`、`pipeline`、`ai_client`、`history`、`cost_estimator` 和通知拆开，并记录已发送文章以去重。
- [industry-intelligence-dashboard](https://github.com/stephenzzezz/industry-intelligence-dashboard)：用配置文件切换行业，把 RSS、GDELT、GitHub、arXiv 等来源经过规则过滤和 LLM 整理后写入 SQLite。

## 当前项目的优点

- `app/main.py` 已经有稳定的 FastAPI `lifespan`、SQLite 建表和示例数据初始化。
- `items`、`reports`、`report_items` 已经把原子条目和日报快照分开，报告可以保留关联顺序。
- 条目保留 `source_url`、`summary` 和 `content`，具备最基本的证据回溯能力。
- 看板可以筛选、手动录入，API 测试和 GitHub Actions 已经建立起来。

## 主要缺口

1. **没有原始采集层。** 当前条目可以手工写入，但没有保存抓取响应、发布时间、HTTP 状态、ETag、正文哈希和抓取时间。
2. **没有可靠去重。** 前端用标题和时间生成 `slug`，同一文章换一个来源地址或重复运行仍可能产生多条记录。
3. **来源配置和任务执行没有独立出来。** 还没有 feeds/sources 表，也没有重试、退避、单次运行记录和失败状态。
4. **审核状态和风险等级混在一起。** 兼容接口把高风险条目映射成 `pending`，这会让“风险高”和“尚未审核”无法独立表达。
5. **检索仍是 SQLite `LIKE`。** 数据量增大后，标题、摘要和正文的组合查询会变慢，也没有相关性排序。
6. **模型处理没有可追溯元数据。** 未来接 LLM 时，需要保存模型、提示词版本、结构化输出、成本和失败原因。
7. **外部抓取的安全边界尚未定义。** 如果直接开放任意 URL 抓取，会引入 SSRF、超大响应和恶意重定向风险。

## 建议的实施顺序

### P0：采集和去重基础

增加 `sources`、`documents` 和 `ingestion_runs` 三类记录：

```text
sources          来源名称、URL、类型、启用状态、ETag、Last-Modified
documents        原始标题、正文、规范化 URL、发布时间、内容哈希、抓取时间
ingestion_runs   开始/结束时间、成功数、跳过数、失败数、错误摘要
```

RSS/Atom 采集器先做规范化 URL、标题哈希和正文哈希去重，再把原始文档保存下来。只有通过清洗和校验的内容才创建 `items`。抓取器应该有超时、响应大小上限、重定向限制和可信域名白名单。

任务执行建议先用独立 CLI 或系统定时任务，避免把长时间网络请求和模型调用塞进 FastAPI 请求进程。以后需要并发和重试时，再引入 worker/队列。

### P1：审核和结构化提取

把条目字段拆成：

```text
risk_level    INFO / LOW / MEDIUM / HIGH
review_status pending / approved / rejected
confidence    0..1
evidence      原文片段及其在 document 中的位置
```

模型只负责按固定 JSON Schema 提取字段和生成摘要；原文、来源和证据由系统保存。每次提取记录 `provider`、`model`、`prompt_version`、token 用量和错误信息，方便复查成本与结果。

### P1：搜索

SQLite 阶段可以先加 FTS5，保持本地零依赖。迁移 PostgreSQL 后，为规范化文本增加 `tsvector` 列和 GIN 索引；只有关键词搜索无法满足“相似事件”需求时，再加入 pgvector 和 HNSW。这样不会一开始就为向量基础设施增加运维成本。

### P2：部署和协作

补充 PostgreSQL 的 Docker Compose 配置、环境变量配置、结构化日志、运行状态页和备份说明。对外部署前再加登录、CORS 白名单、来源管理权限和审计日志；本地单用户模式可以继续保持轻量。

## 下一步建议

下一轮最适合实现 P0 的最小切片：

1. 增加 RSS/Atom 解析器和 `sources` 配置接口。
2. 保存规范化 URL、正文哈希和抓取运行记录。
3. 增加“同步一个来源”的按钮和命令行入口。
4. 用测试覆盖重复文章、条件请求和失败重试。

这一步完成后，LLM 提取和 PostgreSQL 全文搜索都有稳定的输入边界，后续功能不会继续堆在单个 `items` 表上。
