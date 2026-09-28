from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ingestion, main
from app.collectors.rss import FeedEntry, FeedError, FeedFetchResult, fingerprint_for_entry


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "ingestion.db")
    with TestClient(main.app) as test_client:
        yield test_client


def _entry(number: int) -> FeedEntry:
    link = f"https://example.com/posts/{number}"
    title = f"Feed item {number}"
    content = f"Evidence {number}"
    published_at = "2026-09-28T00:00:00Z"
    return FeedEntry(
        external_id=f"id-{number}",
        title=title,
        link=link,
        summary=f"Summary {number}",
        content=content,
        published_at=published_at,
        fingerprint=fingerprint_for_entry(link, title, published_at, content),
    )


def test_source_sync_inserts_once_and_skips_duplicates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = client.post(
        "/api/sources",
        json={"name": "Example feed", "url": "https://93.184.216.34/feed.xml", "kind": "rss"},
    )
    assert created.status_code == 201
    source_id = created.json()["id"]
    result = FeedFetchResult([_entry(1), _entry(2)], '"v1"', "now", False, 128)
    monkeypatch.setattr(ingestion, "fetch_feed", lambda *args, **kwargs: result)

    first = client.post(f"/api/sources/{source_id}/sync")
    assert first.status_code == 200
    assert first.json()["run"]["status"] == "success"
    assert first.json()["run"]["inserted_count"] == 2

    second = client.post(f"/api/sources/{source_id}/sync")
    assert second.json()["run"]["status"] == "success"
    assert second.json()["run"]["inserted_count"] == 0
    assert second.json()["run"]["skipped_count"] == 2

    items = client.get("/api/items", params={"source": "Example feed"}).json()
    assert items["total"] == 2
    documents = client.get("/api/ingestion/runs", params={"source_id": source_id}).json()
    assert documents["total"] == 2


def test_not_modified_creates_audit_run_without_new_items(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = client.post(
        "/api/sources",
        json={"name": "Conditional feed", "url": "https://93.184.216.34/conditional.xml"},
    )
    source_id = created.json()["id"]
    monkeypatch.setattr(
        ingestion,
        "fetch_feed",
        lambda *args, **kwargs: FeedFetchResult([], '"v1"', "now", True, 0),
    )

    response = client.post(f"/api/sources/{source_id}/sync")

    assert response.status_code == 200
    assert response.json()["run"]["status"] == "not_modified"
    assert response.json()["run"]["inserted_count"] == 0
    source = client.get(f"/api/sources/{source_id}").json()
    assert source["last_error"] is None
    assert source["last_success_at"] is not None


def test_failed_sync_is_recorded_and_source_keeps_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = client.post(
        "/api/sources",
        json={"name": "Broken feed", "url": "https://93.184.216.34/broken.xml"},
    )
    source_id = created.json()["id"]

    def fail(*args, **kwargs):
        raise FeedError("fixture unavailable")

    monkeypatch.setattr(ingestion, "fetch_feed", fail)
    response = client.post(f"/api/sources/{source_id}/sync")

    assert response.status_code == 200
    assert response.json()["run"]["status"] == "failed"
    assert response.json()["run"]["error"] == "fixture unavailable"
    source = client.get(f"/api/sources/{source_id}").json()
    assert source["last_error"] == "fixture unavailable"
    runs = client.get("/api/ingestion/runs", params={"status": "failed"}).json()
    assert runs["total"] == 1


def test_source_registration_rejects_private_target(client: TestClient) -> None:
    response = client.post(
        "/api/sources",
        json={"name": "Local feed", "url": "http://127.0.0.1:8000/feed.xml"},
    )
    assert response.status_code == 422
