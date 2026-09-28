from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import main


def test_legacy_database_gets_review_source_and_fts_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE items (
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
        CREATE TABLE sources (
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
        INSERT INTO items
            (slug, title, category, source, summary, content, risk_level,
             published_at, created_at, updated_at)
        VALUES ('legacy-high', 'Legacy high risk', 'alert', 'legacy',
                'summary', 'evidence', 'HIGH',
                '2026-09-28T00:00:00Z', '2026-09-28T00:00:00Z', '2026-09-28T00:00:00Z');
        """
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(main, "DB_PATH", database)
    main.init_db()

    migrated = sqlite3.connect(database)
    item_columns = {row[1] for row in migrated.execute("PRAGMA table_info(items)")}
    source_columns = {row[1] for row in migrated.execute("PRAGMA table_info(sources)")}
    status = migrated.execute("SELECT review_status FROM items WHERE slug = 'legacy-high'").fetchone()[0]
    fts_count = migrated.execute("SELECT COUNT(*) FROM items_fts").fetchone()[0]
    migrated.close()

    assert {"review_status", "confidence", "evidence_json"} <= item_columns
    assert {"timeout_seconds", "max_attempts", "backoff_seconds"} <= source_columns
    assert status == "pending"
    assert fts_count == 1

