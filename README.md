# Intel Board

这是一个本地优先的情报看板模板。它把来源、原始文档、摘要、证据、风险和审核状态保存成结构化条目，再通过 FastAPI 提供全文检索和日报接口。页面可以直接筛选已有情报、手动录入新条目，也可以配置 RSS / Atom 来源并执行同步。

服务启动时会自动建表并插入一组演示数据。旧版 SQLite 文件会在启动时自动补齐采集相关字段，不需要删除数据库。采集器会保存规范化来源 URL、正文、发布时间和指纹；重复链接或重复指纹只会生成一条条目，每次同步都会留下运行记录。

## 启动

```powershell
cd "D:\桌面\咸鱼\intel-board-template"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
.\run.ps1
```

浏览器访问 <http://127.0.0.1:8770/>；接口文档在 <http://127.0.0.1:8770/docs>。已安装依赖时可直接运行：

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8770 --reload
```

默认 SQLite 文件为 `data/intel.db`，可用 `INTEL_DB_PATH` 指定路径。删掉该文件后重启会重新插入演示数据。

来源抓取支持两个可选环境变量：

- `INTEL_SOURCE_ALLOWLIST`：逗号分隔的可信域名，例如 `example.com,feeds.example.org`。设置后只有这些域名及其子域名可以注册和抓取。
- `INTEL_DB_PATH`：覆盖默认 SQLite 路径，适合测试或多套本地环境。

开发环境可以运行 API 测试：

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

每次推送到 `main` 或创建 Pull Request 时，GitHub Actions 会自动运行同一组测试。

## 接口

- `GET /api/claims`：前端直接使用的数组，支持 `q`、`category`、`status`、`risk`。
- `GET /api/claims/{id}`：前端 claim 形状的详情。
- `POST /api/reports/today`：按最新日报条目生成 Markdown 报告，返回 `body_markdown`。
- `GET /api/items`：标准分页列表，支持 `q`、`category`、`risk_level`、`source`、`tag`、`status`、`review_status`、`limit`、`offset`。SQLite 支持 FTS5 时，`q` 会优先走全文索引；不支持时自动回退到 `LIKE`。
- `GET /api/items/{id}`：完整条目详情。
- `POST /api/items`：写入条目，`slug` 唯一；新条目默认进入 `pending` 审核状态。
- `PATCH /api/items/{id}/review`：更新审核状态、置信度和证据片段。
- `GET /api/reports`、`GET /api/reports/{id}`：日报和关联条目。
- `GET /api/reports/latest/items`：最新报告及条目。
- `GET /api/stats`：分类与风险统计。

来源与采集接口：

- `GET /api/sources`：分页列出来源，支持 `enabled` 筛选。
- `POST /api/sources`：注册 RSS / Atom 地址，可设置 `timeout_seconds`、`max_attempts` 和 `backoff_seconds`。注册阶段会检查协议、凭据和字面量私网地址；真正同步时还会解析 DNS 并检查重定向目标。
- `GET /api/sources/{id}`：查看来源状态、ETag、Last-Modified、最近成功时间和错误。
- `POST /api/sources/{id}/sync`：同步一个来源，返回 `success`、`not_modified`、`failed` 或 `skipped` 的运行结果。
- `GET /api/ingestion/runs`：查看同步审计记录，支持 `source_id`、`status`、`limit`、`offset`。

长时间或定时同步可以放在独立命令行进程中：

```powershell
python -m app.cli source-add --name "官方博客" --url "https://example.com/feed.xml"
python -m app.cli source-list
python -m app.cli source-sync --source-id 1
python -m app.cli source-sync --all
```

同步请求带有 `ETag` / `Last-Modified` 条件头，并限制超时时间和响应大小。遇到超时、网络错误、429 或 5xx 时会按来源配置做有限次数的指数退避重试；解析错误和 URL 校验错误不会重复请求。抓取器会拒绝非 HTTP(S) 地址、带凭据的 URL、解析到非公网地址的主机，以及重定向到不安全目标的请求。对外部署前仍应配置域名白名单和鉴权。

看板中的“录入一条情报”表单会调用 `POST /api/items`。标题、来源、摘要和证据是必填项；来源 URL、标签和风险等级会随条目一起保存。

## 数据模型

`sources` 保存来源配置、条件请求状态和重试参数，`documents` 保存每次采集进入系统的原始标题、正文、规范化链接、发布时间和指纹，`ingestion_runs` 保存每次同步的开始/结束时间、抓取数、新增数、跳过数和错误。`items` 保存清洗后的原子事实，并通过 `source_id`、`document_id`、`dedupe_key` 和 `fetched_at` 关联采集记录；`review_status`、`confidence` 和 `evidence_json` 分别记录审核、置信度和证据片段；`items_fts` 在可用时提供本地全文检索。`reports` 保存按日期生成的报告快照，`report_items` 保存报告和条目的关联顺序。

当前 SQLite 阶段使用索引和指纹去重；数据量增大后，可以按 `docs/optimization-roadmap.md` 迁移到 PostgreSQL 全文检索，只有关键词检索无法满足需求时再引入向量索引。
