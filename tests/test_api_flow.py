"""推进与跟进接口的行为测试：临时库，覆盖 200/201/404/422 与模板清单。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.adapters.db.sqlite import SqliteStageEventRepository
from salescopilot.entrypoints.api.app import app

LEAD = {"name": "张伟", "contact": "13800001111", "source": "referral"}
FOLLOWUP = {"conclusion": "客户下周再谈", "objection": "price", "next_followup_at": "2026-11-20"}


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "api.sqlite3"))
    yield TestClient(app)


def _create_lead(client: TestClient, **overrides) -> dict:
    response = client.post("/leads", json={**LEAD, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def test_get_templates_returns_three_with_flow_lengths(client: TestClient) -> None:
    body = client.get("/templates").json()

    assert {item["key"] for item in body} == {
        "edu_training",
        "software_outsourcing",
        "building_materials",
    }
    lengths = {item["key"]: len(item["flow"]) for item in body}
    assert lengths == {"edu_training": 6, "software_outsourcing": 6, "building_materials": 5}


def test_get_templates_carries_labels(client: TestClient) -> None:
    """labels 里行业叫法来自模板 label，通用叫法来自 domain/labels.py。"""
    body = {item["key"]: item for item in client.get("/templates").json()}
    edu_labels = body["edu_training"]["labels"]

    assert edu_labels["appointment"] == "试听"
    assert edu_labels["first_contact"] == "首次联系"
    assert set(edu_labels) == set(body["edu_training"]["flow"])

    # 建材模板没有 appointment 块，其余块同样要有名字
    building_labels = body["building_materials"]["labels"]
    assert building_labels["intake"] == "线索入库"
    assert set(building_labels) == set(body["building_materials"]["flow"])


def test_create_lead_defaults_to_edu_template(client: TestClient) -> None:
    body = _create_lead(client)

    assert body["stage"] == "intake"
    assert body["template"] == "edu_training"


def test_advance_success_returns_lead_and_stage_event(client: TestClient) -> None:
    lead = _create_lead(client)

    response = client.post(f"/leads/{lead['id']}/advance", json={"target_stage": "first_contact"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lead"]["stage"] == "first_contact"
    assert body["stage_event"]["from_stage"] == "intake"
    assert body["stage_event"]["to_stage"] == "first_contact"
    assert body["stage_event"]["lead_id"] == lead["id"]

    # 列表接口也应看到推进后的阶段
    listed = client.get("/leads").json()[0]
    assert listed["stage"] == "first_contact"
    assert listed["template"] == "edu_training"


def test_advance_event_matches_persisted_history(client: TestClient, tmp_path: Path) -> None:
    """返回体里的 stage_event 就是刚写进库的那条，不是事后猜的。"""
    lead = _create_lead(client)

    response = client.post(f"/leads/{lead['id']}/advance", json={"target_stage": "first_contact"})
    assert response.status_code == 200, response.text

    returned = response.json()["stage_event"]
    persisted = SqliteStageEventRepository(tmp_path / "api.sqlite3").list_for(lead["id"])
    assert persisted[-1].id == returned["id"]
    assert len(persisted) == 1


def test_advance_skip_stage_returns_422_with_location(client: TestClient) -> None:
    lead = _create_lead(client)

    response = client.post(f"/leads/{lead['id']}/advance", json={"target_stage": "proposal"})

    assert response.status_code == 422
    issues = response.json()["issues"]
    assert issues[0]["location"] == "target_stage"
    assert "只能推进到「首次联系」" in issues[0]["message"]
    assert "first_contact" not in issues[0]["message"]
    assert client.get("/leads").json()[0]["stage"] == "intake"


def test_advance_unknown_lead_returns_404(client: TestClient) -> None:
    response = client.post("/leads/missing/advance", json={"target_stage": "first_contact"})

    assert response.status_code == 404
    assert response.json()["issues"][0]["message"] == "线索不存在"


def test_followup_create_and_list(client: TestClient) -> None:
    lead = _create_lead(client)

    response = client.post(f"/leads/{lead['id']}/followups", json=FOLLOWUP)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["lead_id"] == lead["id"]
    assert body["conclusion"] == FOLLOWUP["conclusion"]
    assert body["objection"] == "price"
    assert body["next_followup_at"] == "2026-11-20"

    listed = client.get(f"/leads/{lead['id']}/followups").json()
    assert len(listed) == 1
    assert listed[0]["id"] == body["id"]


def test_followup_missing_field_returns_422(client: TestClient) -> None:
    lead = _create_lead(client)

    response = client.post(
        f"/leads/{lead['id']}/followups",
        json={"conclusion": "客户下周再谈", "objection": "price"},
    )

    assert response.status_code == 422
    issues = response.json()["issues"]
    assert issues[0]["location"] == "next_followup_at"
    assert "下次跟进时间" in issues[0]["message"]


def test_followup_invalid_values_return_422(client: TestClient) -> None:
    lead = _create_lead(client)

    response = client.post(
        f"/leads/{lead['id']}/followups",
        json={**FOLLOWUP, "objection": "budget", "next_followup_at": "2026/11/20"},
    )

    assert response.status_code == 422
    locations = [issue["location"] for issue in response.json()["issues"]]
    assert locations == ["objection", "next_followup_at"]
    assert client.get(f"/leads/{lead['id']}/followups").json() == []


def test_followup_unknown_lead_returns_404(client: TestClient) -> None:
    response = client.post("/leads/missing/followups", json=FOLLOWUP)

    assert response.status_code == 404
    assert response.json()["issues"][0]["message"] == "线索不存在"
