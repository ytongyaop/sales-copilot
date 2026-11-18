"""`GET /leads/shelf` 的行为测试：临时库（monkeypatch 环境变量），不落真实库、不联网。

三条判据：
1. 没有客户：200 + `[]`（不是 404、也不是一张占位卡）；
2. 卡片字段齐全：名称 / 归属人 / 档位 / 覆盖度 / 缺项数 / 下次约定，覆盖度的分子分母能对上；
3. 覆盖度与档位都**只来自后端已有口径**：补两条事实后百分比跟着变，改判后档位跟着变。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.entrypoints.api.app import app

# 软件外包模板的卡面一共声明 10 个字段（3 + 2 + 3 + 2），分母用这个数。
LEAD = {
    "name": "华远建设有限公司",
    "contact": "13800001111",
    "source": "referral",
    "owner": "李娜",
    "template": "software_outsourcing",
}
CARD_FIELD_TOTAL = 10
SHELF_KEYS = {
    "lead_id",
    "name",
    "owner",
    "grade",
    "coverage",
    "filled",
    "total",
    "missing_count",
    "next_appointment",
    "reminder_level",
}
# 风险色点只用这四个取值（空串 = 没有提醒）：判定在 `domain/reminders.py`，这里只验形状。
REMINDER_LEVELS = {"", "red", "yellow", "gray"}


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "shelf_api.sqlite3"))
    yield TestClient(app)


def _lead_id(client: TestClient) -> str:
    response = client.post("/leads", json=LEAD)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_shelf_is_empty_without_leads(client: TestClient) -> None:
    response = client.get("/leads/shelf")

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_shelf_card_reflects_card_coverage_and_grade(client: TestClient) -> None:
    """空客户：0 覆盖度、10 个缺项、没判过档；补事实与跟进后数字与档位跟着变。"""
    lead_id = _lead_id(client)

    response = client.get("/leads/shelf")

    assert response.status_code == 200, response.text
    cards = response.json()
    assert len(cards) == 1
    card = cards[0]
    assert set(card) == SHELF_KEYS
    assert card["lead_id"] == lead_id
    assert card["name"] == LEAD["name"]
    assert card["owner"] == "李娜"
    assert card["grade"] == "unknown"
    assert card["coverage"] == 0.0
    assert card["filled"] == 0
    assert card["total"] == CARD_FIELD_TOTAL
    assert card["missing_count"] == CARD_FIELD_TOTAL
    assert card["next_appointment"] == ""
    # 刚建的客户还在首响时限内、也没有跟进 → 没有提醒，色点为空（取值必须在这四个之内）。
    assert card["reminder_level"] in REMINDER_LEVELS
    assert card["reminder_level"] == ""

    # 补两条卡面声明过的事实：覆盖度变成 2/10，缺项数同步减 2。
    for field, value in {"decision_maker": "王总", "need": "要一套订单系统"}.items():
        patched = client.patch(f"/leads/{lead_id}/facts/{field}", json={"value": value})
        assert patched.status_code == 200, (field, patched.text)

    recorded = client.post(
        f"/leads/{lead_id}/followups",
        json={
            "conclusion": "客户要先看样例",
            "objection": "price",
            "next_followup_at": "2026-12-01",
        },
    )
    assert recorded.status_code == 201, recorded.text

    card = client.get("/leads/shelf").json()[0]
    assert card["filled"] == 2
    assert card["missing_count"] == CARD_FIELD_TOTAL - 2
    assert card["coverage"] == 0.2
    assert card["next_appointment"] == "2026-12-01"

    # 人工定档后，货架上的档位跟着人工值走。
    decided = client.post(
        f"/leads/{lead_id}/grading/decide", json={"value": "B", "reason": "决策人已确认"}
    )
    assert decided.status_code == 201, decided.text
    assert client.get("/leads/shelf").json()[0]["grade"] == "B"
