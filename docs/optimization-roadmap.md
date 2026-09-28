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

## 本轮已经落地的 P0 切片

- `app/collectors/rss.py` 支持 RSS 2.x 和 Atom，统一提取标题、链接、摘要、正文和发布时间。
- `sources`、`documents`、`ingestion_runs` 三张表已经加入 SQLite；旧数据库启动时会执行兼容迁移。
- 同步层按规范化链接和指纹去重，把原始文档与 `items` 关联起来，并保存每次运行的新增、跳过和失败状态。
- 来源 API 和 `app.cli` 已可注册、查看、同步来源；页面也提供了添加来源和手动同步入口。
- 抓取请求带有超时、响应大小限制、ETag / Last-Modified 条件头、来源白名单、DNS 公网地址检查和安全重定向检查。
- RSS/Atom 解析、条件请求、私网地址拦截、重复入库、304 和失败审计已经加入自动化测试。
- 条目已经拆出 `review_status`、`confidence` 和 `evidence_json`，看板可以直接审核待处理条目；同步新增条目默认保持待审核。
- SQLite 阶段已经启用可选 FTS5 镜像，`/api/items?q=...` 会优先使用全文索引，不支持 FTS5 时自动回退到 `LIKE`。
- 来源可以设置请求超时、最大尝试次数和指数退避；仅对网络/服务端瞬时错误重试，解析和安全校验错误不会重复请求。

## 仍待处理的缺口

1. **正文保存仍是清洗后的文档。** 如果需要法律或审计级回溯，还要增加原始响应存储、HTTP 状态、响应头和内容类型字段。
2. **同步目前由 CLI 或请求触发。** 还没有并发 worker 和调度器；定时任务可以先调用 `python -m app.cli source-sync --all`。
3. **全文检索还没有相关性排序。** FTS5 已解决本地关键词检索的基础性能问题，后续可以增加 BM25 排序和高亮。
4. **模型处理没有可追溯元数据。** 未来接 LLM 时，需要保存模型、提示词版本、结构化输出、成本和失败原因。

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

本轮已经完成 P1 的本地切片，下一轮建议继续做：

1. 为 FTS5 增加 BM25 相关性排序和关键词高亮；迁移 PostgreSQL 后使用 `tsvector + GIN`，并保留当前 API 的查询契约。
2. 增加同步调度器、并发上限和跨来源的运行汇总。
3. 接入模型提取时保存 provider、model、prompt_version、token 用量和失败原因。
4. 对原始响应保存 HTTP 状态、响应头和内容类型，满足更严格的审计回溯需求。

这样 LLM 提取和 PostgreSQL 全文搜索都有稳定的输入边界，后续功能不会继续堆在单个 `items` 表上。
