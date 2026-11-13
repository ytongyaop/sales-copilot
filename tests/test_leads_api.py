"""线索接口的行为测试：全部走临时数据库（monkeypatch 环境变量），不落真实库。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.entrypoints.api.app import app

VALID = {"name": "张伟", "contact": "13800001111", "source": "referral", "owner": "李娜"}


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "api.sqlite3"))
    yield TestClient(app)


def test_post_lead_returns_201_with_all_fields(client: TestClient) -> None:
    response = client.post("/leads", json=VALID)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "name", "contact", "source", "stage", "owner", "created_at"}
    assert body["name"] == VALID["name"]
    assert body["stage"] == "intake"
    assert body["owner"] == VALID["owner"]


def test_post_lead_missing_name_returns_422_with_issues(client: TestClient) -> None:
    response = client.post(
        "/leads",
        json={"contact": VALID["contact"], "source": VALID["source"]},
    )

    assert response.status_code == 422
    body = response.json()
    assert "issues" in body
    assert body["issues"][0]["location"] == "name"
    assert "名称" in body["issues"][0]["message"]


def test_post_lead_bad_source_returns_422(client: TestClient) -> None:
    response = client.post("/leads", json={**VALID, "source": "billboard"})

    assert response.status_code == 422
    body = response.json()
    assert body["issues"][0]["location"] == "source"


def test_get_leads_returns_created_items(client: TestClient) -> None:
    assert client.get("/leads").json() == []
    client.post("/leads", json=VALID)

    response = client.get("/leads")

    assert response.status_code == 200
    assert len(response.json()) == 1
