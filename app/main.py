"""FastAPI backend for the local intelligence dashboard template.

The service intentionally keeps the data model small: items are the atomic
observations/deals and reports are generated snapshots which reference items.
SQLite is used by default so the project can be run without external services.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Literal, Mapping, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.collectors.rss import FeedError, validate_feed_url
from app.ingestion import sync_source


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_DIR / "data" / "intel.db"
DB_PATH = Path(os.getenv("INTEL_DB_PATH", str(DEFAULT_DB_PATH))).expanduser()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'intel',
                source TEXT NOT NULL,
                source_url TEXT,
                summary TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                risk_level TEXT NOT NULL DEFAULT 'INFO',
                tags TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'published',
                published_at TEXT NOT NULL,
                expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_items_published_at ON items(published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_items_category ON items(category);
            CREATE INDEX IF NOT EXISTS idx_items_risk_level ON items(risk_level);
            CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                intro TEXT NOT NULL DEFAULT '',
                content_markdown TEXT NOT NULL DEFAULT '',
                report_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reports_report_date ON reports(report_date DESC);

            CREATE TABLE IF NOT EXISTS report_items (
                report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                position INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (report_id, item_id)
            );

            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'rss',
                url TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                etag TEXT,
                last_modified TEXT,
                last_checked_at TEXT,
                last_success_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sources_enabled ON sources(enabled);
            CREATE INDEX IF NOT EXISTS idx_sources_last_checked ON sources(last_checked_at);

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                external_id TEXT NOT NULL,
                canonical_url TEXT,
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                published_at TEXT,
                fingerprint TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id);
            CREATE INDEX IF NOT EXISTS idx_documents_external_id ON documents(external_id);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_fingerprint ON documents(fingerprint);
            CREATE INDEX IF NOT EXISTS idx_documents_canonical_url ON documents(canonical_url);

            CREATE TABLE IF NOT EXISTS ingestion_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                fetched_count INTEGER NOT NULL DEFAULT 0,
                inserted_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ingestion_runs_source ON ingestion_runs(source_id, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ingestion_runs_status ON ingestion_runs(status);
            """
        )
        # The template shipped before source ingestion was added. Keep old
        # databases usable by adding nullable linkage columns in place.
        existing_columns = {
            row[1] for row in db.execute("PRAGMA table_info(items)").fetchall()
        }
        migrations = {
            "source_id": "ALTER TABLE items ADD COLUMN source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL",
            "document_id": "ALTER TABLE items ADD COLUMN document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL",
            "dedupe_key": "ALTER TABLE items ADD COLUMN dedupe_key TEXT",
            "fetched_at": "ALTER TABLE items ADD COLUMN fetched_at TEXT",
        }
        for column, statement in migrations.items():
            if column not in existing_columns:
                db.execute(statement)
        db.execute("CREATE INDEX IF NOT EXISTS idx_items_source_id ON items(source_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_items_document_id ON items(document_id)")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_items_dedupe_key "
            "ON items(dedupe_key) WHERE dedupe_key IS NOT NULL"
        )


def seed_demo_data() -> None:
    """Insert a small deterministic dataset once, making the template usable immediately."""
    now = utc_now()
    demo_items = [
        {
            "slug": "discovery-free-api",
            "title": "Discovery 多模型 API 免费额度",
            "category": "deal",
            "source": "社区线索",
            "source_url": "https://example.com/discovery",
            "summary": "提供多款模型的试用 API 额度，适合做原型验证。",
            "content": "官方页面显示新用户可申请试用额度；使用前请核对条款、限流和有效期。",
            "risk_level": "LOW",
            "tags": ["API", "免费额度", "大模型"],
            "published_at": "2026-09-28T08:30:00Z",
            "expires_at": "2026-10-15T00:00:00Z",
        },
        {
            "slug": "justwork-credit",
            "title": "JustDoWork 新用户补偿额度",
            "category": "deal",
            "source": "官方公告",
            "source_url": "https://example.com/justwork",
            "summary": "部分新注册用户可以获得一次性试用额度。",
            "content": "额度、地区和资格以官方后台展示为准，不要提交支付密码或验证码。",
            "risk_level": "MEDIUM",
            "tags": ["补偿", "试用", "注册"],
            "published_at": "2026-09-28T07:15:00Z",
            "expires_at": None,
        },
        {
            "slug": "provider-status-degradation",
            "title": "第三方模型服务出现间歇性延迟",
            "category": "alert",
            "source": "状态页监测",
            "source_url": "https://status.example.com",
            "summary": "多个用户报告高峰时段请求延迟，建议给调用方增加重试和超时。",
            "content": "当前证据来自公开状态页和多条社区反馈，待服务商进一步确认。",
            "risk_level": "HIGH",
            "tags": ["可用性", "延迟", "监控"],
            "published_at": "2026-09-28T06:05:00Z",
            "expires_at": None,
        },
        {
            "slug": "cline-json-parse-fix",
            "title": "Cline 修复非标准响应解析问题",
            "category": "tech",
            "source": "GitHub Release",
            "source_url": "https://github.com/cline/cline",
            "summary": "新版本修复了边车服务收到非标准响应时的 JSON 解析崩溃。",
            "content": "升级前先在测试环境验证扩展版本与本地配置兼容性。",
            "risk_level": "INFO",
            "tags": ["开发者工具", "修复", "GitHub"],
            "published_at": "2026-09-27T23:45:00Z",
            "expires_at": None,
        },
        {
            "slug": "gemini-developer-credit",
            "title": "开发者平台试用金规则更新",
            "category": "deal",
            "source": "官方文档",
            "source_url": "https://example.com/developer-credit",
            "summary": "开发者试用金的地区、绑定方式和有效期发生变化。",
            "content": "建议在申请前阅读计费说明，避免把试用额度误当作长期免费额度。",
            "risk_level": "MEDIUM",
            "tags": ["开发者", "额度", "计费"],
            "published_at": "2026-09-27T18:20:00Z",
            "expires_at": None,
        },
    ]
    with get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        if count:
            return
        for item in demo_items:
            db.execute(
                """
                INSERT INTO items
                    (slug, title, category, source, source_url, summary, content,
                     risk_level, tags, status, published_at, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'published', ?, ?, ?, ?)
                """,
                (
                    item["slug"], item["title"], item["category"], item["source"], item["source_url"],
                    item["summary"], item["content"], item["risk_level"], json.dumps(item["tags"], ensure_ascii=False),
                    item["published_at"], item["expires_at"], now, now,
                ),
            )
        report_content = (
            "## 今日情报摘要\n\n"
            "本报告由示例数据生成，用于演示从数据库到看板的完整链路。\n\n"
            "- 先按风险等级处理告警，再查看额度和技术动态。\n"
            "- 所有链接都应在实际使用前重新核验。\n"
        )
        cursor = db.execute(
            """
            INSERT INTO reports (slug, title, intro, content_markdown, report_date, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "daily-2026-09-28",
                "今日模型与社区情报",
                "把公开来源的观察结果整理成可检索、可追溯的日报。",
                report_content,
                "2026-09-28",
                now,
                now,
            ),
        )
        report_id = cursor.lastrowid
        item_ids = [row[0] for row in db.execute("SELECT id FROM items ORDER BY published_at DESC").fetchall()]
        db.executemany(
            "INSERT INTO report_items (report_id, item_id, position) VALUES (?, ?, ?)",
            [(report_id, item_id, position) for position, item_id in enumerate(item_ids)],
        )


class ItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=240)
    category: str = Field(default="intel", max_length=40)
    source: str = Field(min_length=1, max_length=120)
    source_url: Optional[str] = None
    summary: str = ""
    content: str = ""
    risk_level: str = Field(default="INFO", max_length=20)
    tags: list[str] = Field(default_factory=list)
    published_at: Optional[str] = None
    expires_at: Optional[str] = None


class ItemOut(ItemCreate):
    id: int
    status: str
    created_at: str
    updated_at: str
    source_id: Optional[int] = None
    document_id: Optional[int] = None
    dedupe_key: Optional[str] = None
    fetched_at: Optional[str] = None


class ReportOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    slug: str
    title: str
    intro: str
    content_markdown: str
    report_date: str
    created_at: str
    updated_at: str
    item_ids: list[int] = Field(default_factory=list)


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    kind: Literal["rss", "atom", "auto"] = "rss"
    url: str = Field(min_length=1, max_length=2000)
    enabled: bool = True


class SourceOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    kind: str
    url: str
    enabled: bool
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    last_checked_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_error: Optional[str] = None
    created_at: str
    updated_at: str


class IngestionRunOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    source_id: int
    status: str
    started_at: str
    finished_at: Optional[str] = None
    fetched_count: int
    inserted_count: int
    skipped_count: int
    error: Optional[str] = None


def item_from_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    try:
        result["tags"] = json.loads(result.get("tags") or "[]")
    except json.JSONDecodeError:
        result["tags"] = []
    return result


def report_from_row(row: sqlite3.Row, item_ids: Optional[list[int]] = None) -> dict[str, Any]:
    result = dict(row)
    result["item_ids"] = item_ids or []
    return result


def source_from_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["enabled"] = bool(result.get("enabled", 0))
    return result


def ingestion_run_from_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    return dict(row)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    seed_demo_data()
    yield


app = FastAPI(
    title="Info Intel Dashboard API",
    version="0.2.0",
    description="本地情报/优惠信息看板的最小可运行后端模板。",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# The sibling frontend is optional. Mounting it here means the project can be
# started as one process, while the API remains usable on its own.
FRONTEND_DIR = PROJECT_DIR / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="frontend not found")
    return FileResponse(index_file)


@app.get("/health")
def health() -> dict[str, Any]:
    with get_db() as db:
        item_count = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        report_count = db.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    return {"ok": True, "service": "info-intel-dashboard", "items": item_count, "reports": report_count}


@app.get("/api/sources")
def list_sources(
    enabled: Optional[bool] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if enabled is not None:
        clauses.append("enabled = ?")
        params.append(int(enabled))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_db() as db:
        total = db.execute(f"SELECT COUNT(*) FROM sources {where}", params).fetchone()[0]
        rows = db.execute(
            f"SELECT * FROM sources {where} ORDER BY name COLLATE NOCASE, id LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "sources": [source_from_row(row) for row in rows],
    }


@app.post("/api/sources", response_model=SourceOut, status_code=201)
def create_source(payload: SourceCreate) -> dict[str, Any]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="source name cannot be blank")
    try:
        canonical_url = validate_feed_url(payload.url, resolve_dns=False)
    except FeedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    now = utc_now()
    try:
        with get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO sources (name, kind, url, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, payload.kind, canonical_url, int(payload.enabled), now, now),
            )
            row = db.execute("SELECT * FROM sources WHERE id = ?", (cursor.lastrowid,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="source URL already exists") from exc
    return source_from_row(row)


@app.get("/api/sources/{source_id}", response_model=SourceOut)
def get_source(source_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    return source_from_row(row)


@app.post("/api/sources/{source_id}/sync")
def sync_source_endpoint(source_id: int) -> dict[str, Any]:
    with get_db() as db:
        source = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        run = sync_source(db, source)
        refreshed_source = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return {
        "source": source_from_row(refreshed_source),
        "run": run,
    }


@app.get("/api/ingestion/runs")
def list_ingestion_runs(
    source_id: Optional[int] = Query(default=None, ge=1),
    status: Optional[str] = Query(default=None, max_length=30),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if source_id is not None:
        clauses.append("source_id = ?")
        params.append(source_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_db() as db:
        total = db.execute(f"SELECT COUNT(*) FROM ingestion_runs {where}", params).fetchone()[0]
        rows = db.execute(
            f"SELECT * FROM ingestion_runs {where} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "runs": [ingestion_run_from_row(row) for row in rows],
    }


@app.get("/api/items")
def list_items(
    q: Optional[str] = Query(default=None, description="标题、摘要、正文或来源关键词"),
    category: Optional[str] = Query(default=None),
    risk: Optional[str] = Query(default=None, alias="risk_level"),
    source: Optional[str] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    status: str = Query(default="published"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    clauses = ["status = ?"]
    params: list[Any] = [status]
    if q:
        clauses.append("(title LIKE ? OR summary LIKE ? OR content LIKE ? OR source LIKE ?)")
        needle = f"%{q}%"
        params.extend([needle, needle, needle, needle])
    if category:
        clauses.append("category = ?")
        params.append(category)
    if risk:
        clauses.append("risk_level = ?")
        params.append(risk.upper())
    if source:
        clauses.append("source LIKE ?")
        params.append(f"%{source}%")
    if tag:
        clauses.append("tags LIKE ?")
        params.append(f'%"{tag}%"%')
    where = " AND ".join(clauses)
    with get_db() as db:
        total = db.execute(f"SELECT COUNT(*) FROM items WHERE {where}", params).fetchone()[0]
        rows = db.execute(
            f"SELECT * FROM items WHERE {where} ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {"total": total, "limit": limit, "offset": offset, "items": [item_from_row(row) for row in rows]}


@app.get("/api/items/{item_id}", response_model=ItemOut)
def get_item(item_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="item not found")
    return item_from_row(row)


@app.post("/api/items", response_model=ItemOut, status_code=201)
def create_item(payload: ItemCreate) -> dict[str, Any]:
    now = utc_now()
    published_at = payload.published_at or now
    try:
        with get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO items
                    (slug, title, category, source, source_url, summary, content,
                     risk_level, tags, status, published_at, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'published', ?, ?, ?, ?)
                """,
                (
                    payload.slug, payload.title, payload.category, payload.source, payload.source_url,
                    payload.summary, payload.content, payload.risk_level.upper(),
                    json.dumps(payload.tags, ensure_ascii=False), published_at, payload.expires_at, now, now,
                ),
            )
            item_id = cursor.lastrowid
            row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="slug already exists") from exc
    return item_from_row(row)


@app.get("/api/reports")
def list_reports(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        rows = db.execute(
            "SELECT * FROM reports ORDER BY report_date DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    reports = []
    with get_db() as db:
        for row in rows:
            ids = [r[0] for r in db.execute("SELECT item_id FROM report_items WHERE report_id = ? ORDER BY position", (row["id"],)).fetchall()]
            reports.append(report_from_row(row, ids))
    return {"total": total, "limit": limit, "offset": offset, "reports": reports}


# Declare the static `today` routes before the parameterized `/{report_id}`
# route. Otherwise a GET request for `/api/reports/today` is parsed as an
# integer report id and FastAPI returns 422 before reaching the static route.
@app.post("/api/reports/today")
def create_today_report() -> dict[str, Any]:
    """Generate a deterministic Markdown snapshot for the bundled frontend."""
    return build_today_report()


@app.get("/api/reports/today")
def get_today_report() -> dict[str, Any]:
    return build_today_report()


@app.get("/api/reports/{report_id}", response_model=ReportOut)
def get_report(report_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="report not found")
        ids = [r[0] for r in db.execute("SELECT item_id FROM report_items WHERE report_id = ? ORDER BY position", (report_id,)).fetchall()]
    return report_from_row(row, ids)


@app.get("/api/reports/latest/items")
def latest_report_items(limit: int = Query(default=50, ge=1, le=100)) -> dict[str, Any]:
    """Convenience endpoint for a dashboard's latest report view."""
    with get_db() as db:
        report = db.execute("SELECT * FROM reports ORDER BY report_date DESC, id DESC LIMIT 1").fetchone()
        if report is None:
            return {"report": None, "items": []}
        rows = db.execute(
            """
            SELECT i.* FROM items i
            JOIN report_items ri ON ri.item_id = i.id
            WHERE ri.report_id = ?
            ORDER BY ri.position
            LIMIT ?
            """,
            (report["id"], limit),
        ).fetchall()
    return {"report": report_from_row(report), "items": [item_from_row(row) for row in rows]}


def claim_from_item(item: dict[str, Any]) -> dict[str, Any]:
    """Adapt the storage model to the small claim shape used by the bundled UI."""
    risk = str(item.get("risk_level", "INFO")).lower()
    confidence = {"low": 0.92, "medium": 0.78, "high": 0.58, "info": 0.88}.get(risk, 0.70)
    return {
        "id": item["id"],
        "title": item["title"],
        "category": item["category"],
        "benefit": item.get("summary", ""),
        "conditions": item.get("tags", []),
        "risk_level": risk,
        "status": "pending" if risk in {"high", "critical"} else "approved",
        "confidence": confidence,
        "source_url": item.get("source_url"),
        "evidence": item.get("content", ""),
        "published_at": item.get("published_at", ""),
        "source": item.get("source", ""),
    }


@app.get("/api/claims")
def list_claims(
    q: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    risk: Optional[str] = Query(default=None),
) -> list[dict[str, Any]]:
    """Compatibility endpoint for the bundled dashboard frontend.

    The canonical storage/list API is ``/api/items``; this endpoint returns a
    UI-friendly list so a static frontend can be used without a data adapter.
    """
    result = list_items(
        q=q,
        category=category,
        risk=risk,
        source=None,
        tag=None,
        status="published",
        limit=100,
        offset=0,
    )
    claims = [claim_from_item(item) for item in result["items"]]
    if status:
        claims = [claim for claim in claims if claim["status"] == status]
    return claims


@app.get("/api/claims/{claim_id}")
def get_claim(claim_id: int) -> dict[str, Any]:
    return claim_from_item(get_item(claim_id))


def build_today_report() -> dict[str, Any]:
    latest = latest_report_items(limit=100)
    report = latest.get("report")
    items = latest.get("items", [])
    report_date = report["report_date"] if report else datetime.now(timezone.utc).date().isoformat()
    lines = [
        "### 今日情报报告",
        "",
        f"生成日期：{report_date}",
        "",
    ]
    if report and report.get("intro"):
        lines.extend([report["intro"], ""])
    for position, item in enumerate(items, 1):
        lines.extend(
            [
                f"#### {position}. {item['title']}",
                f"- 分类：{item.get('category', '')}",
                f"- 内容：{item.get('summary', '')}",
                f"- 证据：{item.get('content', '')}",
                f"- 来源：{item.get('source_url') or item.get('source', '')}",
                "",
            ]
        )
    return {"report_date": report_date, "body_markdown": "\n".join(lines), "item_count": len(items)}


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    with get_db() as db:
        by_category = {row["category"]: row["count"] for row in db.execute("SELECT category, COUNT(*) AS count FROM items GROUP BY category")}
        by_risk = {row["risk_level"]: row["count"] for row in db.execute("SELECT risk_level, COUNT(*) AS count FROM items GROUP BY risk_level")}
    return {"by_category": by_category, "by_risk_level": by_risk}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=int(os.getenv("PORT", "8770")), reload=False)
