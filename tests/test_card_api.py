"""`GET /leads/{id}/card` 接口测试：临时库 + 假模型，全程离线。

三条判据：
1. 200 结构：覆盖度、四要素分区、缺项清单（每项 question 非空）；
2. 线索不存在 404，问题位置是 `lead_id`；
3. 模板声明的字段能被 `PATCH /leads/{id}/facts/{field}` 写入，写完后覆盖度上升。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.entrypoints.api.app import app

# 建材模板：basic 3 + needs 3 + decision 5 + followup 3 = 14 个声明字段。
LEAD = {
    "name": "华远建设有限公司",
    "contact": "13800001111",
    "source": "referral",
    "template": "building_materials",
}
DECLARED_TOTAL = 14


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "card_api.sqlite3"))
    yield TestClient(app)


def _lead_id(client: TestClient) -> str:
    response = client.post("/leads", json=LEAD)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_card_returns_sections_and_missing_questions(client: TestClient) -> None:
    """新客户没有事实：覆盖度 0，缺项覆盖模板声明的全部字段，每项带问题句。"""
    lead_id = _lead_id(client)

    response = client.get(f"/leads/{lead_id}/card")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["coverage"] == 0.0
    assert [section["key"] for section in body["sections"]] == [
        "basic",
        "needs",
        "decision",
        "followup",
    ]
    assert [section["label"] for section in body["sections"]] == [
        "基础信息",
        "需求与痛点",
        "决策流程与预算",
        "跟进记录",
    ]
    assert [section["total"] for section in body["sections"]] == [3, 3, 5, 3]
    assert all(section["filled"] == 0 for section in body["sections"])
    assert len(body["missing"]) == DECLARED_TOTAL
    assert all(item["question"].strip() for item in body["missing"])
    assert body["missing"][0] == {
        "section": "basic",
        "section_label": "基础信息",
        "field": "company",
        "label": "公司全称",
        "question": "对方公司全称是什么？",
    }
    # 每个字段都带问题句，前端可以直接照着问
    assert all(
        field["question"].strip() for section in body["sections"] for field in section["fields"]
    )


def test_card_404_when_lead_missing(client: TestClient) -> None:
    """线索不存在：404，问题位置是 `lead_id`。"""
    response = client.get("/leads/does-not-exist/card")

    assert response.status_code == 404
    issues = response.json()["issues"]
    assert [issue["location"] for issue in issues] == ["lead_id"]


def test_patched_template_fields_raise_coverage(client: TestClient) -> None:
    """模板声明的字段能通过 PATCH 写入（含旧白名单之外的 company / approval_flow）。"""
    lead_id = _lead_id(client)

    first = client.patch(f"/leads/{lead_id}/facts/company", json={"value": "华远建设有限公司"})
    second = client.patch(f"/leads/{lead_id}/facts/approval_flow", json={"value": "先议价再报批"})

    assert first.status_code == 200, first.text
    assert first.json()["source"] == "manual"
    assert first.json()["locked"] is True
    assert second.status_code == 200, second.text

    body = client.get(f"/leads/{lead_id}/card").json()
    assert body["coverage"] == round(2 / DECLARED_TOTAL, 4)
    assert "company" not in [item["field"] for item in body["missing"]]
    assert "approval_flow" not in [item["field"] for item in body["missing"]]
    company = body["sections"][0]["fields"][0]
    assert company["value"] == "华远建设有限公司"
    assert company["source"] == "manual"
    assert company["locked"] is True
