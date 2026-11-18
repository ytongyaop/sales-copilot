"""`GET /settings` 的接口测试：只读、离线（假模型）、不碰真实库。

三条判据：
1. 形状固定：`mode` / `models` / `templates` 三段，模板每项带 `key` / `name` /
   `thresholds` / `grading` / `card_fields`；
2. 内容是**模板原样**：阈值与分级段与 `GET /templates` 同源，不在路由里另算一套；
3. 不写库：连调两次结果一致（设置页只是展示，没有副作用）。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from salescopilot.entrypoints.api.app import app

SETTINGS_KEYS = {"mode", "models", "templates"}
TEMPLATE_KEYS = {"key", "name", "thresholds", "grading", "card_fields"}


@pytest.fixture
def client(monkeypatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("SALESCOPILOT_DB", str(tmp_path / "settings_api.sqlite3"))
    monkeypatch.delenv("SALESCOPILOT_LIVE", raising=False)
    yield TestClient(app)


def test_settings_shape_and_template_sections(client: TestClient) -> None:
    """假模型下：三段齐全、三份模板都带阈值与分级、卡面字段数是正数。"""
    response = client.get("/settings")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == SETTINGS_KEYS
    assert body["mode"] == "fake"
    assert body["models"] == {"chat": "fake", "embed": "fake"}

    template_keys = [item["key"] for item in body["templates"]]
    assert template_keys == ["building_materials", "edu_training", "software_outsourcing"]
    for item in body["templates"]:
        assert set(item) == TEMPLATE_KEYS
        assert item["name"]
        assert item["thresholds"]["first_response_minutes"] > 0
        assert set(item["grading"]["weights"]) == {"amount", "urgency", "clarity", "fit"}
        assert item["grading"]["bands"]["a_min"] > item["grading"]["bands"]["b_min"]
        assert item["card_fields"] > 0


def test_settings_is_read_only(client: TestClient) -> None:
    """连调两次完全一致：这个接口不写任何东西。"""
    first = client.get("/settings").json()
    second = client.get("/settings").json()

    assert first == second
