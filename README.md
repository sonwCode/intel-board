# Intel Board

这是一个本地优先的情报看板模板。它把来源、摘要、证据和风险标记保存成结构化条目，再通过 FastAPI 提供检索和日报接口。页面可以直接筛选已有情报，也可以手动录入新条目。

服务启动时会自动建表并插入一组演示数据。生产接入时，可以把采集器接到 `POST /api/items`，保留原始证据后再接入模型提取和人工审核流程。

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
- `GET /api/items`：标准分页列表，支持 `q`、`category`、`risk_level`、`source`、`tag`、`status`、`limit`、`offset`。
- `GET /api/items/{id}`：完整条目详情。
- `POST /api/items`：写入条目，`slug` 唯一。
- `GET /api/reports`、`GET /api/reports/{id}`：日报和关联条目。
- `GET /api/reports/latest/items`：最新报告及条目。
- `GET /api/stats`：分类与风险统计。

看板中的“录入一条情报”表单会调用 `POST /api/items`。标题、来源、摘要和证据是必填项；来源 URL、标签和风险等级会随条目一起保存。

## 数据模型

`items` 保存清洗后的原子事实，`reports` 保存按日期生成的报告快照，`report_items` 保存报告和条目的关联顺序。接入真实采集器时，先把页面正文清洗成 `summary` / `content`，再调用 `POST /api/items`；之后可以按日期聚合条目并生成不可变报告。数据库为 SQLite，后续可把同样字段迁移到 PostgreSQL。
