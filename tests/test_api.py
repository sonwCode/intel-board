from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run each test against a fresh SQLite database."""
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "test-intel.db")
    with TestClient(main.app) as test_client:
        yield test_client


def test_health_and_seed_data(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["items"] == 5
    assert payload["reports"] == 1


def test_claim_compatibility_shape_marks_high_risk_pending(client: TestClient) -> None:
    response = client.get("/api/claims")

    assert response.status_code == 200
    claims = response.json()
    assert len(claims) == 5
    high_risk = [claim for claim in claims if claim["risk_level"] == "high"]
    assert high_risk and high_risk[0]["status"] == "pending"


def test_today_report_returns_markdown(client: TestClient) -> None:
    response = client.post("/api/reports/today")

    assert response.status_code == 200
    payload = response.json()
    assert payload["item_count"] == 5
    assert "今日情报报告" in payload["body_markdown"]


def test_create_and_search_item(client: TestClient) -> None:
    item = {
        "slug": "test-item",
        "title": "测试条目",
        "category": "intel",
        "source": "pytest",
        "source_url": "https://example.com/test",
        "summary": "用于验证入库流程",
        "content": "这段文字是可追溯证据。",
        "risk_level": "LOW",
        "tags": ["test"],
    }

    created = client.post("/api/items", json=item)
    assert created.status_code == 201
    assert created.json()["slug"] == "test-item"

    search = client.get("/api/items", params={"q": "测试条目"})
    assert search.status_code == 200
    assert search.json()["total"] == 1

    duplicate = client.post("/api/items", json=item)
    assert duplicate.status_code == 409


def test_review_fields_are_independent_from_risk_and_search_uses_fts(client: TestClient) -> None:
    item = {
        "slug": "reviewable-item",
        "title": "全文检索目标条目",
        "source": "pytest",
        "summary": "可以通过全文检索找到",
        "content": "原始证据片段",
        "risk_level": "HIGH",
        "review_status": "pending",
        "confidence": 0.42,
        "evidence": ["原始证据片段"],
    }
    created = client.post("/api/items", json=item)
    assert created.status_code == 201
    payload = created.json()
    assert payload["review_status"] == "pending"
    assert payload["confidence"] == 0.42
    assert payload["evidence"] == ["原始证据片段"]

    search = client.get("/api/items", params={"q": "全文检索目标"})
    assert search.status_code == 200
    assert search.json()["total"] == 1

    reviewed = client.patch(
        f"/api/items/{payload['id']}/review",
        json={"review_status": "approved", "confidence": 0.9},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["review_status"] == "approved"
    assert reviewed.json()["risk_level"] == "HIGH"

    stats = client.get("/api/stats").json()
    assert stats["by_review_status"]["approved"] >= 1
