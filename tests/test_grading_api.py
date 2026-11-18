"""分级接口的行为测试：临时库（monkeypatch 环境变量），不落真实库、不联网。

覆盖：提议 200（含上下界 / 四维 / 理由 / 依据）、定档 201、历史 200 且倒序、
线索不存在 404、取值非法与缺理由 422。
"""

from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.entrypoints.api.app import app

# 软件外包模板的卡面声明了决策者 / 决策流程 / 我方匹配点，四条事实都写得进去。
LEAD = {
    "name": "华远建设有限公司",
    "contact": "13800001111",
    "source": "referral",
    "template": "software_outsourcing",
}

# 约定时间要用「今天 + 3 天」：接口用的是真实时钟，写死日期会让紧迫度分档随日期漂移。
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
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "grading_api.sqlite3"))
    yield TestClient(app)


def _lead_id(client: TestClient) -> str:
    response = client.post("/leads", json=LEAD)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _fill_facts(client: TestClient, lead_id: str, facts: dict[str, str] | None = None) -> None:
    for field, value in (FULL_FACTS if facts is None else facts).items():
        response = client.patch(f"/leads/{lead_id}/facts/{field}", json={"value": value})
        assert response.status_code == 200, (field, response.text)


# ------------------------------------------------------------------ 提议


def test_propose_returns_grade_with_bounds_and_dims(client: TestClient) -> None:
    """四维齐全且都满分：200，返回 A、上下界相等、四维明细、理由与依据。"""
    lead_id = _lead_id(client)
    _fill_facts(client, lead_id)

    response = client.post(f"/leads/{lead_id}/grading/propose")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"value", "low", "high", "dims", "reason", "basis"}
    assert body["value"] == "A"
    assert body["low"] == 1.0
    assert body["high"] == 1.0
    assert [dim["key"] for dim in body["dims"]] == ["amount", "urgency", "clarity", "fit"]
    assert all(dim["known"] for dim in body["dims"])
    assert all(dim["weight"] > 0 for dim in body["dims"])
    assert "四维齐全" in body["reason"]
    assert body["basis"], "A 档必须带回原文依据"


def test_propose_needs_info_when_a_dim_is_missing(client: TestClient) -> None:
    """缺成交金额：跨档 → needs_info（不硬给 A/B/C），理由点名缺哪一维。"""
    lead_id = _lead_id(client)
    _fill_facts(
        client,
        lead_id,
        {
            "appointment_at": APPOINTMENT_AT,
            "decision_maker": "王总",
            "approval_flow": "总部审批后签约",
            "our_fit": "方案正好对上他们的需求",
        },
    )

    response = client.post(f"/leads/{lead_id}/grading/propose")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["value"] == "needs_info"
    assert body["low"] < body["high"]
    assert "缺「成交金额」" in body["reason"]


def test_propose_auto_mode_is_accepted(client: TestClient) -> None:
    """`?mode=auto`：接口按次覆盖模板默认模式，不报错（留痕里会多一行采纳）。"""
    lead_id = _lead_id(client)
    _fill_facts(client, lead_id)

    response = client.post(f"/leads/{lead_id}/grading/propose", params={"mode": "auto"})

    assert response.status_code == 200, response.text
    history = client.get(f"/leads/{lead_id}/grading/history").json()
    assert [row["mode"] for row in history] == ["auto", "propose"]


def test_propose_bad_mode_is_422(client: TestClient) -> None:
    lead_id = _lead_id(client)

    response = client.post(f"/leads/{lead_id}/grading/propose", params={"mode": "silent"})

    assert response.status_code == 422
    assert response.json()["issues"][0]["location"] == "mode"


def test_propose_unknown_lead_is_404(client: TestClient) -> None:
    response = client.post("/leads/not-a-lead/grading/propose")

    assert response.status_code == 404
    assert response.json()["issues"][0]["location"] == "lead_id"


# ------------------------------------------------------------------ 定档


def test_decide_returns_201_with_the_new_row(client: TestClient) -> None:
    """改档成功：201 返回新行（AI 原值 A、人工值 C、理由原样）。"""
    lead_id = _lead_id(client)
    _fill_facts(client, lead_id)
    client.post(f"/leads/{lead_id}/grading/propose")

    response = client.post(
        f"/leads/{lead_id}/grading/decide",
        json={"value": "C", "reason": "预算砍半，先降级", "actor": "李娜"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {
        "id",
        "lead_id",
        "mode",
        "at",
        "ai_value",
        "human_value",
        "reason",
        "actor",
    }
    assert body["ai_value"] == "A"
    assert body["human_value"] == "C"
    assert body["reason"] == "预算砍半，先降级"
    assert body["actor"] == "李娜"
    assert body["at"]


def test_decide_changing_value_without_reason_is_422(client: TestClient) -> None:
    lead_id = _lead_id(client)
    _fill_facts(client, lead_id)
    client.post(f"/leads/{lead_id}/grading/propose")

    response = client.post(f"/leads/{lead_id}/grading/decide", json={"value": "C"})

    assert response.status_code == 422
    assert response.json()["issues"][0]["location"] == "reason"


def test_decide_invalid_value_is_422(client: TestClient) -> None:
    lead_id = _lead_id(client)

    response = client.post(
        f"/leads/{lead_id}/grading/decide", json={"value": "D", "reason": "随便定"}
    )

    assert response.status_code == 422
    assert response.json()["issues"][0]["location"] == "value"


def test_decide_unknown_lead_is_404(client: TestClient) -> None:
    response = client.post(
        "/leads/not-a-lead/grading/decide", json={"value": "A", "reason": "先定一档"}
    )

    assert response.status_code == 404
    assert response.json()["issues"][0]["location"] == "lead_id"


# ------------------------------------------------------------------ 历史


def test_history_is_newest_first(client: TestClient) -> None:
    """提议 + 定档 → 两行，最新一行在最前；没有历史返回 `[]`。"""
    lead_id = _lead_id(client)
    _fill_facts(client, lead_id)
    assert client.get(f"/leads/{lead_id}/grading/history").json() == []

    client.post(f"/leads/{lead_id}/grading/propose")
    client.post(
        f"/leads/{lead_id}/grading/decide",
        json={"value": "B", "reason": "先降一档观察"},
    )

    response = client.get(f"/leads/{lead_id}/grading/history")

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 2
    assert rows[0]["human_value"] == "B"
    assert rows[0]["ai_value"] == "A"
    assert rows[1]["human_value"] is None
    assert rows[1]["ai_value"] == "A"


def test_history_unknown_lead_is_404(client: TestClient) -> None:
    response = client.get("/leads/not-a-lead/grading/history")

    assert response.status_code == 404
    assert response.json()["issues"][0]["location"] == "lead_id"
