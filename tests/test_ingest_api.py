"""采集/提炼/事实接口的行为测试：临时库 + 假模型，全部离线。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.entrypoints.api.app import app

LEAD = {"name": "张伟", "contact": "13800001111", "source": "referral"}
CONTENT = "客户说太贵了，还说下周再商量"


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "api.sqlite3"))
    yield TestClient(app)


def _lead_id(client: TestClient) -> str:
    response = client.post("/leads", json=LEAD)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _raw_id(client: TestClient, lead_id: str, content: str = CONTENT) -> str:
    response = client.post(f"/leads/{lead_id}/raws", json={"content": content, "source": "inbound"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_post_raw_returns_201_then_200_when_duplicated(client: TestClient) -> None:
    lead_id = _lead_id(client)

    first = client.post(f"/leads/{lead_id}/raws", json={"content": CONTENT, "source": "inbound"})
    second = client.post(
        f"/leads/{lead_id}/raws", json={"content": f" {CONTENT} ", "source": "referral"}
    )

    assert first.status_code == 201
    assert first.json()["duplicated"] is False
    assert second.status_code == 200
    assert second.json()["duplicated"] is True
    assert second.json()["id"] == first.json()["id"]


def test_post_raw_unknown_lead_404_and_short_content_422(client: TestClient) -> None:
    missing = client.post("/leads/missing/raws", json={"content": CONTENT, "source": "inbound"})
    assert missing.status_code == 404
    assert missing.json()["issues"][0]["location"] == "lead_id"

    lead_id = _lead_id(client)
    short = client.post(f"/leads/{lead_id}/raws", json={"content": "太短", "source": "inbound"})
    assert short.status_code == 422
    assert short.json()["issues"][0]["location"] == "content"


def test_extract_returns_accepted_skipped_and_run(client: TestClient) -> None:
    lead_id = _lead_id(client)
    raw_id = _raw_id(client, lead_id)

    response = client.post(f"/raws/{raw_id}/extract")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"accepted", "skipped", "run", "field_updates"}
    assert [fact["field"] for fact in body["accepted"]] == [
        "objection",
        "need",
        "appointment_at",
    ]
    for fact in body["accepted"]:
        assert fact["source_span"] in CONTENT
        assert fact["source"] == "llm"
        assert fact["locked"] is False
    assert body["skipped"] == []
    assert body["run"]["model"] == "fake"
    assert body["run"]["candidates"] == 3
    assert body["run"]["accepted"] == 3


def test_extract_unknown_raw_404(client: TestClient) -> None:
    response = client.post("/raws/missing/extract")

    assert response.status_code == 404
    assert response.json()["issues"][0]["location"] == "raw_id"


def test_extract_twice_returns_empty_accepted_with_skipped(client: TestClient) -> None:
    """第二次提炼：accepted 为空，skipped 里能看到每条候选被跳过及其位置。"""
    lead_id = _lead_id(client)
    raw_id = _raw_id(client, lead_id)
    client.post(f"/raws/{raw_id}/extract")

    response = client.post(f"/raws/{raw_id}/extract")

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == []
    assert [item["location"] for item in body["skipped"]] == [
        "candidates[0].objection",
        "candidates[1].need",
        "candidates[2].appointment_at",
    ]
    assert [item["message"] for item in body["skipped"]] == ["重复值"] * 3
    assert set(body["skipped"][0]) == {"location", "message"}


def test_get_facts_ascending_and_patch_locks(client: TestClient) -> None:
    lead_id = _lead_id(client)
    raw_id = _raw_id(client, lead_id)
    client.post(f"/raws/{raw_id}/extract")

    listed = client.get(f"/leads/{lead_id}/facts").json()
    assert [fact["field"] for fact in listed] == ["objection", "need", "appointment_at"]
    assert [fact["at"] for fact in listed] == sorted(fact["at"] for fact in listed)

    patched = client.patch(f"/leads/{lead_id}/facts/objection", json={"value": "effect"})

    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["value"] == "effect"
    assert body["source"] == "manual"
    assert body["locked"] is True

    after = client.get(f"/leads/{lead_id}/facts").json()
    objection = next(fact for fact in after if fact["field"] == "objection")
    assert objection["value"] == "effect"


def test_patch_rejects_bad_field(client: TestClient) -> None:
    lead_id = _lead_id(client)
    raw_id = _raw_id(client, lead_id)
    client.post(f"/raws/{raw_id}/extract")

    bad_field = client.patch(f"/leads/{lead_id}/facts/hobby", json={"value": "钓鱼"})
    assert bad_field.status_code == 422
    assert bad_field.json()["issues"][0]["location"] == "field"


def test_patch_never_extracted_field_creates_locked_fact(client: TestClient) -> None:
    """从未提炼过的字段也能补录：PATCH 走 upsert，来源人工、片段占位、已锁定。"""
    lead_id = _lead_id(client)

    created = client.patch(f"/leads/{lead_id}/facts/amount", json={"value": "2000 元"})

    assert created.status_code == 200, created.text
    body = created.json()
    assert body["field"] == "amount"
    assert body["value"] == "2000 元"
    assert body["source"] == "manual"
    assert body["source_span"] == "(人工录入)"
    assert body["confidence"] == 1.0
    assert body["locked"] is True

    after = client.get(f"/leads/{lead_id}/facts").json()
    assert [fact["field"] for fact in after] == ["amount"]
    assert after[0]["locked"] is True


def test_delete_lock_unlocks_but_keeps_manual_source(client: TestClient) -> None:
    lead_id = _lead_id(client)
    raw_id = _raw_id(client, lead_id)
    client.post(f"/raws/{raw_id}/extract")
    client.patch(f"/leads/{lead_id}/facts/objection", json={"value": "effect"})

    response = client.delete(f"/leads/{lead_id}/facts/objection/lock")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["locked"] is False
    assert body["source"] == "manual"
    assert body["value"] == "effect"


def test_unlock_unknown_fact_404(client: TestClient) -> None:
    lead_id = _lead_id(client)

    response = client.delete(f"/leads/{lead_id}/facts/need/lock")

    assert response.status_code == 404
    assert response.json()["issues"][0]["location"] == "fact"


def test_get_facts_unknown_lead_404(client: TestClient) -> None:
    response = client.get("/leads/missing/facts")

    assert response.status_code == 404
