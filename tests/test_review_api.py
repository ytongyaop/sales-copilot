"""`GET /review` 与 `POST /review/answers` 接口测试：临时库 + 假模型，全程离线。

四条判据：
1. 空库 200：窗口两端有值、数字全 0、三个问题照给、`answered=false`；
2. 有数据时结构完整，`new_leads` / `no_value_followups` 与造的数据对得上；
3. 作答 3 条 → 201 `{"saved": 3}`，再查 `answered=true`；
4. 问题编号非法 / 回答过短 / 空列表 / 超过 3 条 → 422，且一条都不写。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.entrypoints.api.app import app

LEAD = {"name": "华远建设有限公司", "contact": "13800001111", "source": "referral"}
REVIEW_KEYS = {
    "window_start",
    "window_end",
    "new_leads",
    "advanced",
    "closed",
    "stuck",
    "needs_info_leads",
    "missing_top",
    "grading_changes",
    "no_value_followups",
    "questions",
    "answered",
    "answers",
}


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "review_api.sqlite3"))
    yield TestClient(app)


def _lead_id(client: TestClient) -> str:
    response = client.post("/leads", json=LEAD)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_review_is_empty_before_any_data(client: TestClient) -> None:
    """一条线索都没有：200、数字全 0、问题三条、还没答过。"""
    response = client.get("/review")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == REVIEW_KEYS
    assert body["new_leads"] == 0
    assert body["advanced"] == 0
    assert body["closed"] == 0
    assert body["needs_info_leads"] == 0
    assert body["stuck"] == []
    assert body["missing_top"] == []
    assert body["grading_changes"] == []
    assert body["no_value_followups"] == []
    assert [item["key"] for item in body["questions"]] == ["b_to_a", "c_kept", "wrong_grade"]
    assert body["answered"] is False
    assert body["answers"] == []
    assert body["window_start"] and body["window_end"]


def test_review_counts_new_lead_and_empty_followup(client: TestClient) -> None:
    """新建的客户算本周新增；不带 `value_provided` 的跟进进「无价值跟进」。"""
    lead_id = _lead_id(client)
    recorded = client.post(
        f"/leads/{lead_id}/followups",
        json={
            "conclusion": "只是问候了一下",
            "objection": "time",
            "next_followup_at": "2026-11-20",
        },
    )
    assert recorded.status_code == 201, recorded.text
    assert recorded.json()["value_provided"] == ""

    body = client.get("/review").json()

    assert body["new_leads"] == 1
    assert [item["lead_id"] for item in body["no_value_followups"]] == [lead_id]
    assert body["no_value_followups"][0]["lead_name"] == LEAD["name"]
    # 没有事实 → 四个分区都缺，按客户数并列，取前三块
    assert [item["section"] for item in body["missing_top"]] == ["basic", "needs", "decision"]
    assert body["needs_info_leads"] == 0


def test_answer_three_questions_then_answered_is_true(client: TestClient) -> None:
    """答完三条：201 saved=3，再查复盘 `answered=true`。"""
    payload = {
        "answers": [
            {"key": "b_to_a", "answer": "城建集团：预算批下来了"},
            {"key": "c_kept", "answer": "一直跟着，对方在等立项"},
            {"key": "wrong_grade", "answer": "老客户被判成 C，其实复购意向强"},
        ],
        "actor": "李娜",
    }

    saved = client.post("/review/answers", json=payload)

    assert saved.status_code == 201, saved.text
    assert saved.json() == {"saved": 3}
    body = client.get("/review").json()
    assert body["answered"] is True
    # 已作答的样本原样还给前端：编号由问题中文反查回来，回答与时间都在。
    assert [item["key"] for item in body["answers"]] == ["b_to_a", "c_kept", "wrong_grade"]
    assert [item["answer"] for item in body["answers"]] == [
        item["answer"] for item in payload["answers"]
    ]
    assert [item["question"] for item in body["answers"]] == [
        item["text"] for item in body["questions"]
    ]


@pytest.mark.parametrize(
    "answers",
    [
        [{"key": "unknown", "answer": "编号不认识"}],
        [{"key": "b_to_a", "answer": "好"}],
        [],
        [
            {"key": "b_to_a", "answer": "理由一"},
            {"key": "c_kept", "answer": "理由二"},
            {"key": "wrong_grade", "answer": "理由三"},
            {"key": "b_to_a", "answer": "理由四"},
        ],
    ],
)
def test_answer_rejects_invalid_payloads(client: TestClient, answers: list[dict]) -> None:
    """编号非法 / 回答过短 / 空列表 / 超过三条：422 且不写库（`answered` 仍为 false）。"""
    response = client.post("/review/answers", json={"answers": answers})

    assert response.status_code == 422, response.text
    assert response.json()["issues"]
    assert client.get("/review").json()["answered"] is False
