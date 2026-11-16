"""B9.5 接口行为：PATCH 线索字段、提炼回报 field_updates、GET /leads 带 fields。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.entrypoints.api.app import app

LEAD = {"name": "李校长", "contact": "13800001111", "source": "referral"}
CONTENT = "李校长想提高数学成绩，预算 1 万，下周一见面，他还在考虑"


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "api.sqlite3"))
    yield TestClient(app)


def _lead_id(client: TestClient) -> str:
    response = client.post("/leads", json=LEAD)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_patch_fields_returns_updated_lead_and_accumulates(client: TestClient) -> None:
    lead_id = _lead_id(client)

    first = client.patch(
        f"/leads/{lead_id}/fields",
        json={"fields": {"decision_maker": "李校长", "need": "想提高数学成绩"}},
    )

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["fields"] == {"decision_maker": "李校长", "need": "想提高数学成绩"}
    assert body["stage"] == "intake"

    second = client.patch(f"/leads/{lead_id}/fields", json={"fields": {"amount": "1 万"}})
    assert second.status_code == 200, second.text
    # 只覆盖传入的键：前面的字段仍在
    assert second.json()["fields"] == {
        "decision_maker": "李校长",
        "need": "想提高数学成绩",
        "amount": "1 万",
    }


def test_get_leads_carries_fields(client: TestClient) -> None:
    assert client.get("/leads").json() == []
    lead_id = _lead_id(client)

    listed = client.get("/leads").json()

    assert len(listed) == 1
    assert "fields" in listed[0]
    assert listed[0]["fields"] == {}

    client.patch(f"/leads/{lead_id}/fields", json={"fields": {"need": "想提高数学成绩"}})
    assert client.get("/leads").json()[0]["fields"] == {"need": "想提高数学成绩"}


def test_patch_fields_rejects_bad_patch_and_writes_nothing(client: TestClient) -> None:
    lead_id = _lead_id(client)

    bad_key = client.patch(f"/leads/{lead_id}/fields", json={"fields": {"hobby": "钓鱼"}})
    assert bad_key.status_code == 422, bad_key.text
    assert bad_key.json()["issues"][0]["location"] == "fields.hobby"

    blank = client.patch(f"/leads/{lead_id}/fields", json={"fields": {"need": "   "}})
    assert blank.status_code == 422, blank.text
    assert blank.json()["issues"][0]["location"] == "fields.need"

    bad_date = client.patch(
        f"/leads/{lead_id}/fields", json={"fields": {"appointment_at": "下周一"}}
    )
    assert bad_date.status_code == 422, bad_date.text
    assert bad_date.json()["issues"][0]["location"] == "fields.appointment_at"
    assert "YYYY-MM-DD" in bad_date.json()["issues"][0]["message"]

    assert client.get("/leads").json()[0]["fields"] == {}


def test_patch_fields_unknown_lead_returns_404(client: TestClient) -> None:
    response = client.patch("/leads/missing/fields", json={"fields": {"need": "想提高成绩"}})

    assert response.status_code == 404
    assert response.json()["issues"][0]["location"] == "lead_id"
    assert response.json()["issues"][0]["message"] == "线索不存在"


def test_extract_reports_field_updates(client: TestClient) -> None:
    lead_id = _lead_id(client)
    raw = client.post(f"/leads/{lead_id}/raws", json={"content": CONTENT, "source": "inbound"})
    assert raw.status_code == 201, raw.text
    raw_id = raw.json()["id"]

    first = client.post(f"/raws/{raw_id}/extract")

    assert first.status_code == 200, first.text
    body = first.json()
    assert "field_updates" in body
    # 约定时间是「下周一」→ 不是 ISO 日期，不自动补；其余三项补上
    assert body["field_updates"] == {
        "need": "待确认决策",
        "decision_maker": "校长",
        "amount": "1 万",
    }
    assert client.get("/leads").json()[0]["fields"] == body["field_updates"]

    second = client.post(f"/raws/{raw_id}/extract")
    assert second.status_code == 200, second.text
    # 再提炼一次：字段已齐（或已填），没有新补的键
    assert second.json()["field_updates"] == {}


def test_advance_passes_after_extract(client: TestClient) -> None:
    lead_id = _lead_id(client)
    raw = client.post(f"/leads/{lead_id}/raws", json={"content": CONTENT, "source": "inbound"})
    assert raw.status_code == 201, raw.text
    client.post(f"/raws/{raw.json()['id']}/extract")

    first = client.post(f"/leads/{lead_id}/advance", json={"target_stage": "first_contact"})
    assert first.status_code == 200, first.text

    qualified = client.post(f"/leads/{lead_id}/advance", json={"target_stage": "qualify"})
    assert qualified.status_code == 200, qualified.text
    assert qualified.json()["lead"]["stage"] == "qualify"
