"""POST /leads/import 的行为测试：200 + 返回结构、重复导入幂等、缺字段只跳过该行。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.entrypoints.api.app import app

CSV = "姓名,联系方式,来源,归属\n张三,13800001111,转介绍,王五\n李四,13800002222,活动,\n王五,13800003333,other,\n"


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "import_api.sqlite3"))
    yield TestClient(app)


def test_import_returns_counts_and_reasons(client: TestClient) -> None:
    response = client.post(
        "/leads/import",
        json={"csv_text": CSV, "default_owner": None},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"imported": 3, "skipped": []}
    assert len(client.get("/leads").json()) == 3


def test_import_twice_is_idempotent(client: TestClient) -> None:
    client.post("/leads/import", json={"csv_text": CSV})

    again = client.post("/leads/import", json={"csv_text": CSV})

    assert again.status_code == 200, again.text
    body = again.json()
    assert body["imported"] == 0
    assert body["skipped"] == [
        {"row": 2, "reason": "联系方式已存在"},
        {"row": 3, "reason": "联系方式已存在"},
        {"row": 4, "reason": "联系方式已存在"},
    ]


def test_bad_row_is_skipped_without_failing_the_batch(client: TestClient) -> None:
    text = "姓名,联系方式,来源\n张三,13800001111,inbound\n李四,,inbound\n"

    response = client.post("/leads/import", json={"csv_text": text})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "imported": 1,
        "skipped": [{"row": 3, "reason": "缺少联系方式"}],
    }
    assert len(client.get("/leads").json()) == 1


def test_default_owner_from_request_body(client: TestClient) -> None:
    text = "姓名,联系方式,来源\n张三,13800001111,inbound\n"

    client.post("/leads/import", json={"csv_text": text, "default_owner": "赵六"})

    assert [lead["owner"] for lead in client.get("/leads").json()] == ["赵六"]
