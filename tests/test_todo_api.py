"""`GET /todo` 接口测试：临时库 + 假模型，全程离线。

三条判据：
1. 200 结构：每项含 `lead_id / lead_name / grade / priority / due_kind / reason /
   action / value_offered / missing`，且 `value_offered` 非空；
2. 空态：一条线索都没有时返回 `[]`；
3. 跟进接口带回 `value_provided`（写进去什么就读出什么）。
"""

from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.entrypoints.api.app import app

LEAD = {
    "name": "华远建设有限公司",
    "contact": "13800001111",
    "source": "referral",
    "template": "building_materials",
}
TODO_KEYS = {
    "lead_id",
    "lead_name",
    "grade",
    "priority",
    "due_kind",
    "reason",
    "action",
    "value_offered",
    "missing",
}


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "todo_api.sqlite3"))
    yield TestClient(app)


def _lead_id(client: TestClient) -> str:
    response = client.post("/leads", json=LEAD)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_todo_is_empty_without_leads(client: TestClient) -> None:
    """没有线索：200 + 空数组，不是 404、也不是一条占位。"""
    response = client.get("/todo")

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_todo_returns_ranked_item_with_value(client: TestClient) -> None:
    """有客户、已定档、有一次过期约定 → 一条待办，带理由 / 动作 / 价值点。"""
    lead_id = _lead_id(client)
    decided = client.post(
        f"/leads/{lead_id}/grading/decide", json={"value": "A", "reason": "预算与决策人都已落实"}
    )
    assert decided.status_code == 201, decided.text

    overdue = (date.today() - timedelta(days=2)).isoformat()
    recorded = client.post(
        f"/leads/{lead_id}/followups",
        json={
            "conclusion": "客户要先看同类项目清单",
            "objection": "price",
            "next_followup_at": overdue,
            "value_provided": "同类客户案例",
        },
    )
    assert recorded.status_code == 201, recorded.text
    assert recorded.json()["value_provided"] == "同类客户案例"

    response = client.get("/todo")

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    item = body[0]
    assert set(item) == TODO_KEYS
    assert item["lead_id"] == lead_id
    assert item["lead_name"] == LEAD["name"]
    assert item["grade"] == "A"
    assert item["due_kind"] == "overdue"
    assert item["value_offered"].strip()
    assert item["action"].strip()
    assert item["reason"].startswith("A 类")
    assert isinstance(item["missing"], list)
    assert isinstance(item["priority"], int)


def test_followup_list_exposes_value_provided(client: TestClient) -> None:
    """`GET .../followups` 带 `value_provided`：填过的原样返回，没填的是空串。"""
    lead_id = _lead_id(client)
    base = {"conclusion": "客户下周再谈", "objection": "price", "next_followup_at": "2026-11-20"}
    assert (
        client.post(
            f"/leads/{lead_id}/followups", json={**base, "value_provided": " 报价区间说明 "}
        ).status_code
        == 201
    )
    assert client.post(f"/leads/{lead_id}/followups", json=base).status_code == 201

    listed = client.get(f"/leads/{lead_id}/followups").json()

    assert [item["value_provided"] for item in listed] == ["报价区间说明", ""]
