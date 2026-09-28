"""Database ingestion for RSS and Atom sources.

The collector only knows how to fetch and parse a feed.  This module owns the
durable part of the pipeline: source state, raw documents, deduplication,
normalized dashboard items, and an audit row for every synchronization run.
Keeping those concerns separate makes the same code usable from the API,
the command line, and a scheduled task.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from app.collectors.rss import FeedEntry, fetch_feed


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _value(source: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        value = source[key]
    except (KeyError, IndexError):
        getter = getattr(source, "get", None)
        value = getter(key, default) if getter else default
    return default if value is None else value


def _slug_for_entry(title: str, fingerprint: str) -> str:
    """Create a readable, deterministic slug while retaining uniqueness."""
    base = re.sub(r"[^\w\u4e00-\u9fff]+", "-", (title or "feed-item").strip().lower()).strip("-")
    base = base[:80].strip("-") or "feed-item"
    return f"{base}-{fingerprint[:12]}"[:120]


def _run_result(db: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = db.execute("SELECT * FROM ingestion_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"ingestion run {run_id} disappeared")
    return dict(row)


def _finish_run(
    db: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    finished_at: str,
    fetched_count: int,
    inserted_count: int,
    skipped_count: int,
    error: Optional[str] = None,
) -> dict[str, Any]:
    db.execute(
        """
        UPDATE ingestion_runs
        SET status = ?, finished_at = ?, fetched_count = ?, inserted_count = ?,
            skipped_count = ?, error = ?
        WHERE id = ?
        """,
        (status, finished_at, fetched_count, inserted_count, skipped_count, error, run_id),
    )
    return _run_result(db, run_id)


def _existing_document(db: sqlite3.Connection, entry: FeedEntry) -> Optional[sqlite3.Row]:
    if entry.link:
        return db.execute(
            """
            SELECT * FROM documents
            WHERE fingerprint = ? OR canonical_url = ?
            ORDER BY id
            LIMIT 1
            """,
            (entry.fingerprint, entry.link),
        ).fetchone()
    return db.execute(
        "SELECT * FROM documents WHERE fingerprint = ? ORDER BY id LIMIT 1",
        (entry.fingerprint,),
    ).fetchone()


def _existing_item(db: sqlite3.Connection, fingerprint: str) -> Optional[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM items WHERE dedupe_key = ? ORDER BY id LIMIT 1",
        (fingerprint,),
    ).fetchone()


def _insert_entry(
    db: sqlite3.Connection,
    source: Mapping[str, Any],
    entry: FeedEntry,
    fetched_at: str,
) -> bool:
    """Insert one document and its dashboard item.

    A savepoint keeps a partially inserted document from surviving if an item
    hits a uniqueness constraint.  Returning ``False`` means the entry was a
    duplicate and should be counted as skipped by the caller.
    """
    if _existing_document(db, entry) is not None or _existing_item(db, entry.fingerprint) is not None:
        return False

    savepoint = f"feed_entry_{abs(hash((entry.fingerprint, fetched_at)))}"
    # SQLite savepoint names are identifiers; the hash is an integer and thus
    # safe to interpolate here.
    db.execute(f"SAVEPOINT {savepoint}")
    try:
        document_cursor = db.execute(
            """
            INSERT INTO documents
                (source_id, external_id, canonical_url, title, body, published_at,
                 fingerprint, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(source["id"]),
                entry.external_id,
                entry.link,
                entry.title,
                entry.content,
                entry.published_at,
                entry.fingerprint,
                fetched_at,
            ),
        )
        document_id = document_cursor.lastrowid
        source_name = str(_value(source, "name", "RSS 来源"))[:120]
        source_url = entry.link or str(_value(source, "url", ""))
        slug = _slug_for_entry(entry.title, entry.fingerprint)
        db.execute(
            """
            INSERT INTO items
                (slug, title, category, source, source_url, summary, content,
                 risk_level, tags, status, published_at, expires_at, created_at,
                 updated_at, source_id, document_id, dedupe_key, fetched_at)
            VALUES (?, ?, 'intel', ?, ?, ?, ?, 'INFO', ?, 'published', ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                slug,
                entry.title,
                source_name,
                source_url,
                entry.summary,
                entry.content,
                json.dumps(["rss"], ensure_ascii=False),
                entry.published_at or fetched_at,
                fetched_at,
                fetched_at,
                int(source["id"]),
                document_id,
                entry.fingerprint,
                fetched_at,
            ),
        )
        db.execute(f"RELEASE SAVEPOINT {savepoint}")
        return True
    except sqlite3.IntegrityError:
        db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        db.execute(f"RELEASE SAVEPOINT {savepoint}")
        return False


def sync_source(db: sqlite3.Connection, source: Mapping[str, Any]) -> dict[str, Any]:
    """Synchronize one source and return the persisted ingestion run.

    Fetch and parse errors are recorded in ``ingestion_runs`` and on the
    source row instead of escaping.  Callers can therefore show a useful
    status in the UI and scheduled jobs can continue with other sources.
    """
    source_id = int(source["id"])
    started_at = utc_now()
    run_cursor = db.execute(
        """
        INSERT INTO ingestion_runs
            (source_id, status, started_at, fetched_count, inserted_count, skipped_count)
        VALUES (?, 'running', ?, 0, 0, 0)
        """,
        (source_id, started_at),
    )
    run_id = int(run_cursor.lastrowid)
    fetched_count = inserted_count = skipped_count = 0

    if not bool(_value(source, "enabled", 1)):
        now = utc_now()
        return _finish_run(
            db,
            run_id,
            status="skipped",
            finished_at=now,
            fetched_count=0,
            inserted_count=0,
            skipped_count=0,
            error="source is disabled",
        )

    try:
        result = fetch_feed(
            str(source["url"]),
            etag=_value(source, "etag"),
            last_modified=_value(source, "last_modified"),
        )
        checked_at = utc_now()
        if result.not_modified:
            db.execute(
                """
                UPDATE sources
                SET last_checked_at = ?, last_success_at = ?, last_error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (checked_at, checked_at, checked_at, source_id),
            )
            return _finish_run(
                db,
                run_id,
                status="not_modified",
                finished_at=checked_at,
                fetched_count=0,
                inserted_count=0,
                skipped_count=0,
            )

        fetched_count = len(result.entries)
        for entry in result.entries:
            if _insert_entry(db, source, entry, checked_at):
                inserted_count += 1
            else:
                skipped_count += 1

        db.execute(
            """
            UPDATE sources
            SET etag = ?, last_modified = ?, last_checked_at = ?,
                last_success_at = ?, last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (
                result.etag if result.etag is not None else _value(source, "etag"),
                result.last_modified if result.last_modified is not None else _value(source, "last_modified"),
                checked_at,
                checked_at,
                checked_at,
                source_id,
            ),
        )
        return _finish_run(
            db,
            run_id,
            status="success",
            finished_at=checked_at,
            fetched_count=fetched_count,
            inserted_count=inserted_count,
            skipped_count=skipped_count,
        )
    except Exception as exc:
        finished_at = utc_now()
        message = str(exc)[:1000] or exc.__class__.__name__
        db.execute(
            """
            UPDATE sources
            SET last_checked_at = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (finished_at, message, finished_at, source_id),
        )
        return _finish_run(
            db,
            run_id,
            status="failed",
            finished_at=finished_at,
            fetched_count=fetched_count,
            inserted_count=inserted_count,
            skipped_count=skipped_count,
            error=message,
        )
