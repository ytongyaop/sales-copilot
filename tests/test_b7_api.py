"""B7 五个接口的行为测试：pool / claim / release / reminders / reclaim-scan。

全部离线：临时库 + 默认假模型；需要「创建很久以前」的线索时直接写仓储（接口不提供改创建时间的入口）。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.adapters.db.sqlite import SqliteLeadRepository
from salescopilot.domain.models import INITIAL_STAGE, Lead
from salescopilot.entrypoints.api.app import app

LEAD = {"name": "张伟", "contact": "13800001111", "source": "referral"}


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "b7.sqlite3"))
    yield TestClient(app)


def _create_lead(client: TestClient, **overrides) -> dict:
    response = client.post("/leads", json={**LEAD, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def _insert_lead(tmp_path: Path, *, lead_id: str, owner: str | None, created_at: str) -> None:
    """直接落一条「很旧」的线索：首响超时这类场景在接口里造不出来。"""
    SqliteLeadRepository(tmp_path / "b7.sqlite3").add(
        Lead(
            id=lead_id,
            name=f"线索-{lead_id}",
            contact="13800001111",
            source="inbound",
            stage=INITIAL_STAGE,
            owner=owner,
            created_at=created_at,
        )
    )


def test_pool_lists_only_unassigned_leads(client: TestClient) -> None:
    unassigned = _create_lead(client, name="未分配线索")
    _create_lead(client, name="已分配线索", owner="王五")

    pool = client.get("/leads/pool").json()

    assert [lead["id"] for lead in pool] == [unassigned["id"]]
    assert pool[0]["owner"] is None


def test_claim_then_duplicate_claim_returns_409(client: TestClient) -> None:
    lead = _create_lead(client, name="待认领线索")

    claimed = client.post(f"/leads/{lead['id']}/claim", json={"owner": "王五"})
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["owner"] == "王五"

    conflict = client.post(f"/leads/{lead['id']}/claim", json={"owner": "李四"})
    assert conflict.status_code == 409
    assert conflict.json()["issues"][0]["message"] == "该线索已归属「王五」"

    blank = client.post(f"/leads/{lead['id']}/claim", json={"owner": "  "})
    assert blank.status_code == 422
    assert blank.json()["issues"][0]["message"] == "归属人不能为空"

    missing = client.post("/leads/missing/claim", json={"owner": "王五"})
    assert missing.status_code == 404
    assert missing.json()["issues"][0]["message"] == "线索不存在"


def test_release_returns_lead_to_pool_and_releases_twice_is_409(client: TestClient) -> None:
    lead = _create_lead(client, name="待释放线索", owner="王五")

    released = client.post(f"/leads/{lead['id']}/release")
    assert released.status_code == 200, released.text
    assert released.json()["owner"] is None
    assert [item["id"] for item in client.get("/leads/pool").json()] == [lead["id"]]

    again = client.post(f"/leads/{lead['id']}/release")
    assert again.status_code == 409
    assert again.json()["issues"][0]["message"] == "该线索尚未归属"

    missing = client.post("/leads/missing/release")
    assert missing.status_code == 404


def test_reminders_list_and_per_lead(client: TestClient, tmp_path: Path) -> None:
    _insert_lead(tmp_path, lead_id="old-lead", owner=None, created_at="2020-01-01T00:00:00+00:00")
    fresh = _create_lead(client, name="新线索")

    all_reminders = client.get("/reminders").json()

    assert [item["lead_id"] for item in all_reminders] == ["old-lead"]
    reminder = all_reminders[0]
    assert reminder["lead_name"] == "线索-old-lead"
    assert reminder["kind"] == "first_response"
    assert reminder["level"] == "red"
    # 阈值取自「这条线索的模板」；`_insert_lead` 用实体默认模板（T010 起是
    # tob_enterprise，首响 120 分钟），所以文案是 120 而不是教培模板的 30。
    assert reminder["message"] == "超过 120 分钟未首联"
    assert isinstance(reminder["since_minutes"], int)

    # 新线索刚建、什么都没发生 → 单线索提醒是空的
    assert client.get(f"/leads/{fresh['id']}/reminders").json() == []

    # 线索不存在 → 404 + 中文原因（不再用 200 + 空列表掩盖「查错了 id」）
    missing = client.get("/leads/missing/reminders")
    assert missing.status_code == 404
    assert missing.json() == {
        "issues": [{"location": "lead_id", "message": "线索不存在"}],
    }


def test_reclaim_scan_reclaims_overdue_lead_only_once(client: TestClient, tmp_path: Path) -> None:
    _insert_lead(
        tmp_path, lead_id="stale-owner", owner="王五", created_at="2020-01-01T00:00:00+00:00"
    )
    _insert_lead(
        tmp_path, lead_id="fresh-owner", owner="李四", created_at="2021-01-01T00:00:00+00:00"
    )

    first = client.post("/reclaim/scan")
    assert first.status_code == 200
    reclaimed = first.json()["reclaimed"]
    assert {(item["lead_id"], item["from_owner"], item["reason"]) for item in reclaimed} == {
        ("stale-owner", "王五", "auto_reclaim"),
        ("fresh-owner", "李四", "auto_reclaim"),
    }

    # 再扫一遍：都已经在公海了，没有新的回收，也不会重复写事件
    second = client.post("/reclaim/scan").json()
    assert second == {"reclaimed": []}

    owners = {item["id"]: item["owner"] for item in client.get("/leads").json()}
    assert owners == {"stale-owner": None, "fresh-owner": None}
