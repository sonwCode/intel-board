"""Command line helpers for managing feed sources outside the web process."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any

from app.collectors.rss import FeedError, validate_feed_url
from app.ingestion import sync_source
from app.main import get_db, init_db, seed_demo_data, source_from_row, utc_now


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def source_add(args: argparse.Namespace) -> int:
    try:
        canonical_url = validate_feed_url(args.url, resolve_dns=False)
    except FeedError as exc:
        print(f"无法添加来源：{exc}", file=sys.stderr)
        return 2
    name = args.name.strip()
    if not name:
        print("无法添加来源：名称不能为空", file=sys.stderr)
        return 2
    now = utc_now()
    try:
        with get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO sources (name, kind, url, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, args.kind, canonical_url, int(not args.disabled), now, now),
            )
            row = db.execute("SELECT * FROM sources WHERE id = ?", (cursor.lastrowid,)).fetchone()
    except sqlite3.IntegrityError as exc:
        print(f"无法添加来源：{exc}", file=sys.stderr)
        return 2
    _print(source_from_row(row))
    return 0


def source_list(_: argparse.Namespace) -> int:
    with get_db() as db:
        rows = db.execute("SELECT * FROM sources ORDER BY name COLLATE NOCASE, id").fetchall()
    _print([source_from_row(row) for row in rows])
    return 0


def source_sync(args: argparse.Namespace) -> int:
    with get_db() as db:
        if args.all:
            rows = db.execute("SELECT * FROM sources WHERE enabled = 1 ORDER BY id").fetchall()
        else:
            if args.source_id is None:
                print("source-sync 需要 --source-id 或 --all", file=sys.stderr)
                return 2
            rows = db.execute("SELECT * FROM sources WHERE id = ?", (args.source_id,)).fetchall()
        if not rows:
            print("没有找到匹配的来源", file=sys.stderr)
            return 1
        results = [sync_source(db, row) for row in rows]
    _print(results[0] if not args.all else results)
    return 1 if any(result["status"] == "failed" for result in results) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Intel Board 来源采集命令")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("source-add", help="添加 RSS/Atom 来源")
    add.add_argument("--name", required=True, help="来源名称")
    add.add_argument("--url", required=True, help="RSS/Atom 地址")
    add.add_argument("--kind", choices=("rss", "atom", "auto"), default="rss")
    add.add_argument("--disabled", action="store_true", help="创建后暂不启用")
    add.set_defaults(handler=source_add)

    listing = subparsers.add_parser("source-list", help="列出来源")
    listing.set_defaults(handler=source_list)

    sync = subparsers.add_parser("source-sync", help="同步一个或全部启用来源")
    sync.add_argument("--source-id", type=int)
    sync.add_argument("--all", action="store_true", help="同步所有启用来源")
    sync.set_defaults(handler=source_sync)
    return parser


def main() -> int:
    init_db()
    seed_demo_data()
    args = build_parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
