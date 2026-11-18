"""T004 三个接口的行为测试：临时库 + 假模型，全部离线。

覆盖 200 与 422 两条路径：
- `POST /sorting/{id}/confirm`：同意（`applied` / `skipped` + 事实落库）、改归属、拒绝；
- `POST /sorting/{id}/claim`：挂已有客户、按名字新建；
- `GET /sorting/{id}/review`：灰区建议、没有候选时的提示；
- 单条查询带 `dropped`；分拣单不存在 404。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.entrypoints.api.app import app

LEAD_A = {"name": "华远建设有限公司", "contact": "13800001111", "source": "referral"}
LEAD_B = {"name": "恒信商贸有限公司", "contact": "13900002222", "source": "referral"}

# 四个块各一条要素（公司 / 需求 / 决策人 / 时间）。
CONTENT = "华远建设有限公司的老板说预算三十万，下周再聊"
# 没有任何候选：进待认领池。
NEW_CONTENT = "完全不相干的一通闲聊，预算还在谈，下周再说"
# 只差一个字级差别：落进复核区，有候选但不敢自动归属。
FUZZY_CONTENT = "华远建设工程有限公司那边说预算三十万，下周一下午再聊"
# 原文里带联系方式：新建客户时取得到。
CREATE_CONTENT = "远大科技有限公司，预算三十万 电话13800009999，下周再聊"


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "sorting_t004.sqlite3"))
    yield TestClient(app)


def _lead_id(client: TestClient, payload: dict) -> str:
    response = client.post("/leads", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _sort(client: TestClient, content: str, lead_id: str | None = None) -> dict:
    body: dict = {"content": content, "source": "paste"}
    if lead_id is not None:
        body["lead_id"] = lead_id
    response = client.post("/sorting", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_accept_returns_applied_and_writes_facts(client: TestClient) -> None:
    """同意：200 返回 applied / skipped，事实接口能看到四块归块。"""
    lead_id = _lead_id(client, LEAD_A)
    batch_id = _sort(client, CONTENT, lead_id)["batch_id"]

    response = client.post(f"/sorting/{batch_id}/confirm", json={"action": "accept"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == 4
    assert body["skipped"] == []
    assert body["batch"]["status"] == "accepted"
    assert body["batch"]["decided_action"] == "accepted"

    facts = client.get(f"/leads/{lead_id}/facts")
    assert facts.status_code == 200
    listed = facts.json()
    assert {fact["field"]: fact["card_section"] for fact in listed} == {
        "company": "basic",
        "need": "needs",
        "decision_maker": "decision",
        "appointment_at": "followup",
    }
    assert all(fact["source"] == "llm" and fact["locked"] is False for fact in listed)


def test_accept_on_pending_claim_is_422(client: TestClient) -> None:
    """待认领池里的单不能直接同意：422 且指出状态不对（要先认领再确认）。"""
    _lead_id(client, LEAD_A)
    batch_id = _sort(client, NEW_CONTENT)["batch_id"]

    response = client.post(f"/sorting/{batch_id}/confirm", json={"action": "accept"})

    assert response.status_code == 422
    assert response.json()["issues"][0]["location"] == "status"
    assert "pending_review" in response.json()["issues"][0]["message"]


def test_reject_needs_reason_then_closes_the_batch(client: TestClient) -> None:
    """拒绝：缺理由 422；有理由则状态变 rejected 且事实层为空。"""
    lead_id = _lead_id(client, LEAD_A)
    batch_id = _sort(client, CONTENT, lead_id)["batch_id"]

    missing = client.post(f"/sorting/{batch_id}/confirm", json={"action": "reject"})
    assert missing.status_code == 422
    assert missing.json()["issues"][0]["location"] == "reason"
    assert client.get(f"/leads/{lead_id}/facts").json() == []

    done = client.post(
        f"/sorting/{batch_id}/confirm", json={"action": "reject", "reason": "客户跟丢了"}
    )
    assert done.status_code == 200, done.text
    assert done.json()["applied"] == 0
    assert done.json()["batch"]["status"] == "rejected"
    assert done.json()["batch"]["decided_action"] == "rejected"
    assert client.get(f"/leads/{lead_id}/facts").json() == []


def test_fix_moves_batch_and_facts_to_another_lead(client: TestClient) -> None:
    """改归属：事实落到新客户，分拣单改挂过去。"""
    lead_a = _lead_id(client, LEAD_A)
    lead_b = _lead_id(client, LEAD_B)
    batch_id = _sort(client, CONTENT, lead_a)["batch_id"]

    response = client.post(
        f"/sorting/{batch_id}/confirm", json={"action": "fix", "lead_id": lead_b}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == 4
    assert body["batch"]["lead_id"] == lead_b
    assert body["batch"]["decided_action"] == "fixed_and_accepted"
    assert client.get(f"/leads/{lead_a}/facts").json() == []
    assert len(client.get(f"/leads/{lead_b}/facts").json()) == 4


def test_confirm_bad_action_and_unknown_lead(client: TestClient) -> None:
    """动作不认识 / 改到不存在的客户：422；分拣单不存在 404。"""
    lead_id = _lead_id(client, LEAD_A)
    batch_id = _sort(client, CONTENT, lead_id)["batch_id"]

    bad_action = client.post(f"/sorting/{batch_id}/confirm", json={"action": "同意"})
    assert bad_action.status_code == 422
    assert bad_action.json()["issues"][0]["location"] == "action"

    bad_lead = client.post(
        f"/sorting/{batch_id}/confirm", json={"action": "fix", "lead_id": "not-a-lead"}
    )
    assert bad_lead.status_code == 422
    assert bad_lead.json()["issues"][0]["location"] == "lead_id"

    missing = client.post("/sorting/not-there/confirm", json={"action": "accept"})
    assert missing.status_code == 404
    assert missing.json()["issues"][0]["location"] == "batch_id"


def test_claim_by_lead_id_then_confirm(client: TestClient) -> None:
    """认领：200 返回 lead_id 与 warnings；认领后能接着同意。"""
    lead_id = _lead_id(client, LEAD_A)
    batch_id = _sort(client, NEW_CONTENT)["batch_id"]

    response = client.post(f"/sorting/{batch_id}/claim", json={"lead_id": lead_id})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lead_id"] == lead_id
    assert body["warnings"] == []
    assert body["batch"]["status"] == "pending_review"
    assert body["batch"]["lead_id"] == lead_id

    accepted = client.post(f"/sorting/{batch_id}/confirm", json={"action": "accept"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["applied"] >= 1


def test_claim_with_create_name_creates_lead(client: TestClient) -> None:
    """按名字新建：客户出现在列表里，联系方式取自要素。"""
    _lead_id(client, LEAD_A)
    batch_id = _sort(client, CREATE_CONTENT)["batch_id"]

    response = client.post(f"/sorting/{batch_id}/claim", json={"create_name": "远大科技有限公司"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["warnings"] == []

    created = [lead for lead in client.get("/leads").json() if lead["id"] == body["lead_id"]]
    assert len(created) == 1
    assert created[0]["name"] == "远大科技有限公司"
    assert created[0]["contact"] == "13800009999"


def test_claim_422_paths(client: TestClient) -> None:
    """两个参数都给 / 都没给 / 状态不对：422。"""
    lead_id = _lead_id(client, LEAD_A)
    batch_id = _sort(client, NEW_CONTENT)["batch_id"]

    both = client.post(
        f"/sorting/{batch_id}/claim",
        json={"lead_id": lead_id, "create_name": "另一个名字"},
    )
    assert both.status_code == 422
    assert both.json()["issues"][0]["location"] == "lead_id"

    neither = client.post(f"/sorting/{batch_id}/claim", json={})
    assert neither.status_code == 422

    assigned = _sort(client, NEW_CONTENT, lead_id)["batch_id"]
    wrong_state = client.post(f"/sorting/{assigned}/claim", json={"lead_id": lead_id})
    assert wrong_state.status_code == 422
    assert wrong_state.json()["issues"][0]["location"] == "status"

    missing = client.post("/sorting/not-there/claim", json={"lead_id": lead_id})
    assert missing.status_code == 404


def test_review_suggests_candidate_and_422_paths(client: TestClient) -> None:
    """灰区复核：200 给建议；没有候选 / 状态不对 422；不落库。"""
    lead_id = _lead_id(client, LEAD_A)
    fuzzy_batch = _sort(client, FUZZY_CONTENT)["batch_id"]

    response = client.get(f"/sorting/{fuzzy_batch}/review")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["suggested_lead_id"] == lead_id
    assert body["reason"]
    # 只建议、不落库：分拣单的 lead_id 仍是空。
    assert client.get(f"/sorting/batches/{fuzzy_batch}").json()["lead_id"] is None

    empty_batch = _sort(client, NEW_CONTENT)["batch_id"]
    empty = client.get(f"/sorting/{empty_batch}/review")
    assert empty.status_code == 422
    assert empty.json()["issues"][0]["location"] == "candidates"
    assert "直接新建" in empty.json()["issues"][0]["message"]

    assigned = _sort(client, CONTENT, lead_id)["batch_id"]
    wrong_state = client.get(f"/sorting/{assigned}/review")
    assert wrong_state.status_code == 422
    assert wrong_state.json()["issues"][0]["location"] == "status"

    missing = client.get("/sorting/not-there/review")
    assert missing.status_code == 404


def test_single_batch_returns_dropped(client: TestClient) -> None:
    """单条查询带 `dropped`（投料时没有丢弃就是空数组）。"""
    lead_id = _lead_id(client, LEAD_A)
    batch_id = _sort(client, CONTENT, lead_id)["batch_id"]

    body = client.get(f"/sorting/batches/{batch_id}").json()

    assert body["dropped"] == []
