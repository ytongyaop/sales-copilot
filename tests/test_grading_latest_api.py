"""`GET /leads/{id}/grading/latest` 的行为测试：临时库（monkeypatch 环境变量），不落真实库。

三条判据：
1. 从没分级过的客户：200 + `current=unknown` + `record=null` + 四维分解照给（不 404、不报错）；
2. **只读**：连查两次，留痕条数不变（看详情不该产生留痕，只有 `propose` 才写一行）；
3. 人工改判后：`current` 取人工值，`result` 仍按规则现算（两者不同正是改判的留痕意义）。
"""

from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.entrypoints.api.app import app

# 软件外包模板的卡面声明了金额 / 约定 / 决策者 / 决策流程 / 我方匹配点，四维都算得出来。
LEAD = {
    "name": "华远建设有限公司",
    "contact": "13800001111",
    "source": "referral",
    "template": "software_outsourcing",
}

# 约定时间用「今天 + 3 天」：接口用真实时钟，写死日期会让紧迫度分档随日期漂移。
APPOINTMENT_AT = (date.today() + timedelta(days=3)).isoformat()

FULL_FACTS = {
    "amount": "60万",
    "appointment_at": APPOINTMENT_AT,
    "decision_maker": "王总",
    "approval_flow": "总部审批后签约",
    "our_fit": "方案正好对上他们的需求",
}


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "grading_latest.sqlite3"))
    yield TestClient(app)


def _lead_id(client: TestClient) -> str:
    response = client.post("/leads", json=LEAD)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _fill_facts(client: TestClient, lead_id: str) -> None:
    for field, value in FULL_FACTS.items():
        response = client.patch(f"/leads/{lead_id}/facts/{field}", json={"value": value})
        assert response.status_code == 200, (field, response.text)


def test_latest_snapshot_of_an_ungraded_lead(client: TestClient) -> None:
    """还没分级：200，`current=unknown`、`record=null`，四维分解照给（页面显示「未分级」）。"""
    lead_id = _lead_id(client)
    _fill_facts(client, lead_id)

    response = client.get(f"/leads/{lead_id}/grading/latest")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"current", "record", "result"}
    assert body["current"] == "unknown"
    assert body["record"] is None
    assert body["result"]["value"] == "A"
    assert body["result"]["summary"] == "四维齐全，加权 1.00 → A"
    assert "–" not in body["result"]["summary"], "短理由不带分数区间"
    assert [dim["key"] for dim in body["result"]["dims"]] == ["amount", "urgency", "clarity", "fit"]
    assert all(dim["known"] for dim in body["result"]["dims"])
    # 只读：查一次不该多出一行留痕。
    assert client.get(f"/leads/{lead_id}/grading/history").json() == []


def test_latest_is_read_only_and_keeps_the_human_decision(client: TestClient) -> None:
    """提议 + 改判 C 后：`current` 是人工值 C，`result` 仍按规则算 A；连查两次留痕还是两行。"""
    lead_id = _lead_id(client)
    _fill_facts(client, lead_id)
    client.post(f"/leads/{lead_id}/grading/propose")
    decided = client.post(
        f"/leads/{lead_id}/grading/decide",
        json={"value": "C", "reason": "预算砍半，先降级观察"},
    )
    assert decided.status_code == 201, decided.text

    first = client.get(f"/leads/{lead_id}/grading/latest")
    second = client.get(f"/leads/{lead_id}/grading/latest")

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["current"] == "C"
    assert body["record"]["human_value"] == "C"
    assert body["record"]["ai_value"] == "A"
    assert body["record"]["reason"] == "预算砍半，先降级观察"
    assert body["result"]["value"] == "A"
    assert second.status_code == 200
    assert len(client.get(f"/leads/{lead_id}/grading/history").json()) == 2


def test_latest_unknown_lead_is_404(client: TestClient) -> None:
    response = client.get("/leads/not-a-lead/grading/latest")

    assert response.status_code == 404
    assert response.json()["issues"][0]["location"] == "lead_id"
