"""线索接口的行为测试：全部走临时数据库（monkeypatch 环境变量），不落真实库。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.entrypoints.api.app import app

VALID = {"name": "张伟", "contact": "13800001111", "source": "referral", "owner": "李娜"}


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "api.sqlite3"))
    yield TestClient(app)


def test_post_lead_returns_201_with_all_fields(client: TestClient) -> None:
    response = client.post("/leads", json=VALID)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "id",
        "name",
        "contact",
        "source",
        "stage",
        "owner",
        "created_at",
        "template",
        "fields",
        "warnings",
    }
    assert body["name"] == VALID["name"]
    assert body["stage"] == "intake"
    assert body["owner"] == VALID["owner"]
    # 新线索还没有任何已采集字段（前端据此算「还缺什么」）
    assert body["fields"] == {}
    # 联系方式齐全：没有提醒（不是缺字段，是空数组）
    assert body["warnings"] == []


def test_post_lead_without_contact_returns_201_with_warning(client: TestClient) -> None:
    """联系方式可以为空（0.1.3）：照样 201，只是回一条「无法触达、请补录」的提醒。"""
    response = client.post("/leads", json={"name": "张总", "contact": "", "source": "referral"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "张总"
    assert body["contact"] == ""
    assert [warning["location"] for warning in body["warnings"]] == ["contact"]
    assert "没有联系方式" in body["warnings"][0]["message"]
    # 提醒不阻碍落库：这条客户真的建出来了。
    assert [item["name"] for item in client.get("/leads").json()] == ["张总"]


def test_post_lead_missing_name_returns_422_with_issues(client: TestClient) -> None:
    response = client.post(
        "/leads",
        json={"contact": VALID["contact"], "source": VALID["source"]},
    )

    assert response.status_code == 422
    body = response.json()
    assert "issues" in body
    assert body["issues"][0]["location"] == "name"
    assert "名称" in body["issues"][0]["message"]


def test_post_lead_bad_source_returns_422(client: TestClient) -> None:
    response = client.post("/leads", json={**VALID, "source": "billboard"})

    assert response.status_code == 422
    body = response.json()
    assert body["issues"][0]["location"] == "source"


def test_post_lead_unknown_template_returns_422(client: TestClient) -> None:
    """模板校验在 service 里做：不在已加载清单就 422，不落库。"""
    response = client.post("/leads", json={**VALID, "template": "foo"})

    assert response.status_code == 422
    issues = response.json()["issues"]
    assert [issue["location"] for issue in issues] == ["template"]
    assert "未知模板" in issues[0]["message"]
    assert "foo" in issues[0]["message"]
    assert client.get("/leads").json() == []


def test_post_lead_blank_template_falls_back_to_default(client: TestClient) -> None:
    """空白模板回落到 `DEFAULT_TEMPLATE`（T010 起是主演示模板 tob_enterprise）。"""
    response = client.post("/leads", json={**VALID, "template": "  "})

    assert response.status_code == 201, response.text
    assert response.json()["template"] == "tob_enterprise"


def test_get_leads_returns_created_items(client: TestClient) -> None:
    assert client.get("/leads").json() == []
    client.post("/leads", json=VALID)

    response = client.get("/leads")

    assert response.status_code == 200
    assert len(response.json()) == 1
